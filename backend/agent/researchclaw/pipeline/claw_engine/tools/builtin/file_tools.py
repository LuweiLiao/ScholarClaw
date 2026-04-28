"""File operation tools: read, write, edit.

Ported from the monolithic ToolExecutor into individual Tool subclasses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from researchclaw.pipeline.claw_engine.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
    PermissionDecision,
)
from researchclaw.pipeline.claw_engine.tools.permissions import (
    SandboxPermissionPolicy,
    resolve_allowed_read_path,
    resolve_workspace_write_path,
)


def _resolve_read_path(
    raw: str, workspace: Path, allowed_read_dirs: list[Path],
) -> Path:
    return resolve_allowed_read_path(raw, workspace, allowed_read_dirs)


def _resolve_write_path(raw: str, workspace: Path) -> Path:
    return resolve_workspace_write_path(raw, workspace)


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file from the workspace or allowed data directories. "
        "Returns numbered lines. Use offset/limit for large files."
    )
    is_read_only = True
    is_concurrency_safe = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read."},
                "offset": {
                    "type": "integer", "minimum": 0,
                    "description": "Line offset to start reading from (0-based).",
                },
                "limit": {
                    "type": "integer", "minimum": 1,
                    "description": "Max number of lines to return.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def check_permissions(
        self, args: dict[str, Any], context: ToolContext,
    ) -> PermissionDecision:
        pol = SandboxPermissionPolicy(context.workspace, list(context.allowed_read_dirs or []))
        if pol.check(self.name, args):
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_read_path(
            args["path"], context.workspace, list(context.allowed_read_dirs or []),
        )
        offset = args.get("offset", 0)
        limit = args.get("limit")

        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)

        end = min(offset + limit, total) if limit else total
        selected = lines[offset:end]

        numbered = "\n".join(
            f"{offset + i + 1:6d} | {line}" for i, line in enumerate(selected)
        )
        header = f"File: {path.name} ({total} lines total, showing {offset+1}-{end})"
        data = f"{header}\n{numbered}"
        if len(data) > 16000:
            data = data[:16000] + f"\n... [truncated, {len(data)} total chars]"
        return ToolResult(data=data)

    def summarize_input(self, args: dict[str, Any]) -> str:
        return args.get("path", "?")


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write a text file in the workspace. Creates parent directories if needed. "
        "Use for creating new experiment files (main.py, helpers, configs)."
    )
    is_read_only = False
    is_concurrency_safe = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write."},
                "content": {"type": "string", "description": "Full content to write to the file."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def check_permissions(
        self, args: dict[str, Any], context: ToolContext,
    ) -> PermissionDecision:
        pol = SandboxPermissionPolicy(context.workspace, list(context.allowed_read_dirs or []))
        if pol.check(self.name, args):
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_write_path(args["path"], context.workspace)
        content = args.get("content", "")
        existed = path.exists()

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        kind = "updated" if existed else "created"
        line_count = len(content.splitlines())
        try:
            display = str(path.relative_to(context.workspace))
        except ValueError:
            display = str(path)

        _save_snapshot(path, context.workspace)
        return ToolResult(
            data=f"File {kind}: {display} ({line_count} lines)",
            files_modified=[display],
        )

    def summarize_input(self, args: dict[str, Any]) -> str:
        path = args.get("path", "?")
        size = len(args.get("content", ""))
        return f"{path} ({size} chars)"


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace text in an existing workspace file. Finds old_string and replaces "
        "with new_string. Use for targeted fixes instead of rewriting entire files."
    )
    is_read_only = False
    is_concurrency_safe = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit."},
                "old_string": {"type": "string", "description": "Exact text to find and replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean", "default": False,
                    "description": "Replace all occurrences (default: first only).",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        }

    def check_permissions(
        self, args: dict[str, Any], context: ToolContext,
    ) -> PermissionDecision:
        pol = SandboxPermissionPolicy(context.workspace, list(context.allowed_read_dirs or []))
        if pol.check(self.name, args):
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_write_path(args["path"], context.workspace)
        old_string = args["old_string"]
        new_string = args["new_string"]
        replace_all = args.get("replace_all", False)

        if not path.exists():
            raise FileNotFoundError(f"{path} does not exist")
        if old_string == new_string:
            return ToolResult(data="old_string and new_string are identical — no change needed")

        text = path.read_text(encoding="utf-8")
        if old_string not in text:
            snippet = old_string[:100].replace("\n", "\\n")
            return ToolResult(data=f"old_string not found in {path.name}: '{snippet}...'")

        if replace_all:
            count = text.count(old_string)
            new_text = text.replace(old_string, new_string)
        else:
            count = 1
            new_text = text.replace(old_string, new_string, 1)

        path.write_text(new_text, encoding="utf-8")
        try:
            display = str(path.relative_to(context.workspace))
        except ValueError:
            display = str(path)

        _save_snapshot(path, context.workspace)
        return ToolResult(
            data=f"Edited {display}: {count} replacement(s)",
            files_modified=[display],
        )

    def summarize_input(self, args: dict[str, Any]) -> str:
        path = args.get("path", "?")
        size = len(args.get("new_string", ""))
        return f"{path} ({size} chars)"


_snapshot_count = 0

def _save_snapshot(path: Path, workspace: Path) -> None:
    global _snapshot_count
    if not str(path).endswith(".py"):
        return
    try:
        if not path.exists():
            return
        _snapshot_count += 1
        snap_dir = workspace / ".snapshots"
        snap_dir.mkdir(exist_ok=True)
        snap_path = snap_dir / f"{path.stem}_v{_snapshot_count:03d}.py"
        snap_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
