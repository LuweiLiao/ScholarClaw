"""Unified Tool Framework — inspired by Claude Code's Tool.ts architecture.

Provides a strongly-typed `Tool` base class with:
  - JSON Schema input validation
  - Permission checking (allow / deny / ask)
  - Separate formatting for LLM context vs. human display
  - Concurrency & read-only flags for orchestration

Plus a `ToolRegistry` that assembles built-in + external tools.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class ToolResult:
    """Result of a tool execution."""
    data: str
    is_error: bool = False
    files_modified: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.data or not self.data.strip()

    def for_llm(self, tool_name: str, max_chars: int = 16000) -> str:
        if self.is_empty:
            return f"({tool_name} completed with no output)"
        if len(self.data) <= max_chars:
            return self.data
        head_budget = max_chars * 3 // 10
        tail_budget = max_chars - head_budget - 200
        return (
            f"{self.data[:head_budget]}\n\n"
            f"... [{len(self.data)} total chars, middle truncated] ...\n\n"
            f"{self.data[-tail_budget:]}"
        )

    def for_display(self) -> dict[str, Any]:
        return {
            "data": self.data[:500] if self.data else "",
            "is_error": self.is_error,
            "files_modified": self.files_modified,
        }


class Tool(ABC):
    """Base class for all tools — mirrors Claude Code's Tool interface."""

    name: str = ""
    description: str = ""
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """Return JSON Schema for this tool's input."""

    @abstractmethod
    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool. Must be implemented by subclasses."""

    def check_permissions(
        self, args: dict[str, Any], context: ToolContext,
    ) -> PermissionDecision:
        """Check if this tool call should be allowed. Override for custom logic."""
        if self.is_read_only:
            return PermissionDecision.ALLOW
        return PermissionDecision.ALLOW

    def validate_input(self, args: dict[str, Any]) -> str | None:
        """Validate input args. Returns error message or None if valid."""
        schema = self.input_schema()
        required = set(schema.get("required", []))
        props = schema.get("properties", {})
        for r in required:
            if r not in args:
                return f"Missing required parameter: {r}"
        for k, v in args.items():
            if k in props:
                expected_type = props[k].get("type")
                if expected_type == "string" and not isinstance(v, str):
                    return f"Parameter '{k}' must be a string"
                if expected_type == "integer" and not isinstance(v, int):
                    return f"Parameter '{k}' must be an integer"
                if expected_type == "boolean" and not isinstance(v, bool):
                    return f"Parameter '{k}' must be a boolean"
        return None

    def to_api_spec(self) -> dict[str, Any]:
        """Convert to OpenAI function-calling API format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema(),
            },
        }

    def to_tool_spec(self) -> dict[str, Any]:
        """Convert to legacy TOOL_SPECS format for backward compatibility."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema(),
        }

    def summarize_input(self, args: dict[str, Any]) -> str:
        """Short human-readable summary of the call arguments."""
        return json.dumps(args, ensure_ascii=False)[:80]


@dataclass
class ToolContext:
    """Execution context passed to every tool call."""
    workspace: Path
    allowed_read_dirs: list[Path] = field(default_factory=list)
    python_path: str = ""
    bash_timeout: int = 60
    run_dir: Path | None = None
    project_dir: Path | None = None


class ToolRegistry:
    """Assembles and manages tools (built-in + external).

    Analogous to Claude Code's getTools() + assembleToolPool().
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def register_many(self, tools: list[Tool]) -> None:
        for t in tools:
            self.register(t)

    def disable(self, name: str) -> None:
        self._disabled.add(name)

    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    def get(self, name: str) -> Tool | None:
        if name in self._disabled:
            return None
        return self._tools.get(name)

    def find(self, name: str) -> Tool | None:
        """Find tool by name or alias."""
        tool = self.get(name)
        if tool:
            return tool
        _ALIASES: dict[str, str] = {
            "file_read": "read_file",
            "file_write": "write_file",
            "file_edit": "edit_file",
            "search": "grep_search",
            "glob": "glob_search",
            "shell": "bash",
            "execute": "bash",
            "latex_compile": "latex_compile",
            "bib_search": "bib_search",
        }
        canonical = _ALIASES.get(name)
        return self.get(canonical) if canonical else None

    def all_tools(self) -> list[Tool]:
        return [t for n, t in self._tools.items() if n not in self._disabled]

    def api_specs(self) -> list[dict[str, Any]]:
        return [t.to_api_spec() for t in self.all_tools()]

    def tool_specs(self) -> list[dict[str, Any]]:
        """Legacy TOOL_SPECS format for backward compatibility."""
        return [t.to_tool_spec() for t in self.all_tools()]

    def partition_for_concurrency(
        self, tool_calls: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Partition tool calls into concurrent-safe and serial groups.

        Inspired by Claude Code's partitionToolCalls() in toolOrchestration.ts.
        """
        concurrent: list[dict[str, Any]] = []
        serial: list[dict[str, Any]] = []
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            tool = self.find(name)
            if tool and tool.is_read_only and tool.is_concurrency_safe:
                concurrent.append(tc)
            else:
                serial.append(tc)
        return concurrent, serial

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute a tool by name with full validation and permission checks."""
        tool = self.find(name)
        if not tool:
            return ToolResult(data=f"Unknown tool: {name}", is_error=True)

        validation_error = tool.validate_input(args)
        if validation_error:
            return ToolResult(data=validation_error, is_error=True)

        permission = tool.check_permissions(args, context)
        if permission == PermissionDecision.DENY:
            return ToolResult(
                data=f"Permission denied for tool: {name}",
                is_error=True,
            )

        try:
            return tool.call(args, context)
        except PermissionError as e:
            return ToolResult(data=f"Permission denied: {e}", is_error=True)
        except FileNotFoundError as e:
            return ToolResult(data=f"File not found: {e}", is_error=True)
        except Exception as e:
            return ToolResult(
                data=f"Tool error ({type(e).__name__}): {e}", is_error=True,
            )


def create_default_registry() -> ToolRegistry:
    """Create a registry with all built-in research tools."""
    from researchclaw.pipeline.claw_engine.tools.builtin import ALL_BUILTIN_TOOLS

    registry = ToolRegistry()
    registry.register_many(ALL_BUILTIN_TOOLS)
    return registry
