"""Shared tool definitions, executor, and permission policy.

The new Tool / ToolRegistry / ToolResult framework lives in `base.py`
and `builtin/`. Legacy ToolExecutor is kept for backward compatibility.
"""

from researchclaw.pipeline.claw_engine.tools.definitions import TOOL_SPECS, TOOL_NAMES, tool_spec
from researchclaw.pipeline.claw_engine.tools.executor import ToolExecutor
from researchclaw.pipeline.claw_engine.tools.permissions import SandboxPermissionPolicy
from researchclaw.pipeline.claw_engine.tools.base import (
    Tool,
    ToolResult,
    ToolContext,
    ToolRegistry,
    PermissionDecision,
    create_default_registry,
)

__all__ = [
    "TOOL_SPECS", "TOOL_NAMES", "tool_spec",
    "ToolExecutor", "SandboxPermissionPolicy",
    "Tool", "ToolResult", "ToolContext", "ToolRegistry",
    "PermissionDecision", "create_default_registry",
]
