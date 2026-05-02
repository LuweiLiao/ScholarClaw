"""Sandbox permission policy for tool execution.

Adapted from claw-code's ``runtime/src/permissions.rs`` PermissionPolicy
which checks tool_modes and optionally prompts the user. Our version is
simpler: all tools are auto-allowed within sandbox boundaries.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _is_under_or_equal(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or a path under ``root`` (after resolve)."""
    try:
        p = path.resolve()
        r = root.resolve()
    except (OSError, ValueError):
        return False
    if p == r:
        return True
    try:
        p.relative_to(r)
        return True
    except ValueError:
        return False


def resolve_workspace_write_path(raw: str, workspace: Path) -> Path:
    """Resolve a write path; only locations under ``workspace`` are allowed."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("path is required")
    p = Path(text)
    ws = workspace.resolve()
    if p.is_absolute():
        resolved = p.resolve()
    else:
        # Try cwd-relative first, then workspace-relative
        cwd_resolved = (Path.cwd() / text).resolve()
        if _is_under_or_equal(cwd_resolved, ws):
            resolved = cwd_resolved
        else:
            resolved = (ws / text).resolve()
    if not _is_under_or_equal(resolved, ws):
        raise PermissionError(f"Write outside workspace denied: {raw}")
    return resolved


def resolve_allowed_read_path(
    raw: str,
    workspace: Path,
    allowed_read_dirs: list[Path],
) -> Path:
    """Resolve a read path; must be under workspace or under ``allowed_read_dirs``."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("path is required")
    p = Path(text)
    ws = workspace.resolve()
    if p.is_absolute():
        resolved = p.resolve()
    else:
        # Try cwd-relative first (LLM may return paths relative to cwd),
        # then fall back to workspace-relative.
        cwd_resolved = (Path.cwd() / text).resolve()
        if _is_under_or_equal(cwd_resolved, ws):
            resolved = cwd_resolved
        else:
            resolved = (ws / text).resolve()
    if _is_under_or_equal(resolved, ws):
        if not resolved.exists():
            raise FileNotFoundError(f"{raw} not found")
        return resolved
    for d in allowed_read_dirs:
        if not d:
            continue
        try:
            dr = d.resolve()
        except (OSError, ValueError):
            continue
        if _is_under_or_equal(resolved, dr):
            if not resolved.exists():
                raise FileNotFoundError(f"{raw} not found")
            return resolved
    raise PermissionError(
        f"Read outside allowed directories: {raw}"
    )


DANGEROUS_BASH_PATTERNS = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs.",
    "dd if=/dev/zero",
    ":(){ :",
    "> /dev/sda",
    "chmod -R 777 /",
    "curl | sh",
    "wget | sh",
    "pip install --",
    "shutdown",
    "reboot",
    "kill -9 1",
    "pkill -9",
)

# Bash write-redirect patterns that could escape the workspace
_REDIRECT_WRITE_PATTERN = (
    "> /",       # redirect stdout to absolute path
    ">> /",      # append to absolute path
    "tee /",     # tee to absolute path
    "cp * /",    # copy to root
    "mv * /",    # move to root
)


class SandboxPermissionPolicy:
    """Permission policy that confines tools to workspace boundaries.

    Analogous to claw-code's ``PermissionPolicy`` which has Allow/Deny/Prompt
    modes per tool. Our simplified version:
    - write_file / edit_file: workspace only
    - bash: workspace cwd, timeout enforced, dangerous commands blocked
    - read_file / glob_search / grep_search: workspace + allowed_read_dirs
    """

    def __init__(
        self,
        workspace: Path,
        allowed_read_dirs: list[Path] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.allowed_read_dirs = [
            d.resolve() for d in (allowed_read_dirs or []) if d and Path(d).is_dir()
        ]

    def check(self, tool_name: str, input_data: dict[str, Any]) -> str | None:
        """Check permission. Returns None if allowed, error string if denied."""
        if tool_name == "bash":
            return self._check_bash(input_data)
        if tool_name in ("write_file", "edit_file"):
            return self._check_write(input_data)
        if tool_name == "read_file":
            return self._check_read(input_data)
        if tool_name == "glob_search":
            base = input_data.get("path")
            if not base:
                return None
            return self._check_read({"path": str(base)})
        if tool_name == "grep_search":
            base = input_data.get("path")
            if not base:
                return None
            return self._check_read({"path": str(base)})
        return None

    def _check_bash(self, inp: dict[str, Any]) -> str | None:
        command = inp.get("command", "")
        cmd_lower = command.lower().strip()

        # Block dangerous commands
        for pattern in DANGEROUS_BASH_PATTERNS:
            if pattern in cmd_lower:
                return f"Dangerous command blocked: {command[:80]}"

        # Block write-redirects to absolute paths outside workspace
        ws_str = str(self.workspace)
        for pattern in _REDIRECT_WRITE_PATTERN:
            idx = cmd_lower.find(pattern)
            if idx >= 0:
                # Extract the path after the redirect
                after = command[idx + len(pattern) - 1:].strip().split()[0] if len(command) > idx + len(pattern) else ""
                if after and not after.startswith(ws_str) and after.startswith("/"):
                    return (
                        f"Bash write to path outside workspace blocked: {after}\n"
                        f"All file modifications must be inside: {ws_str}"
                    )

        return None

    def _check_write(self, inp: dict[str, Any]) -> str | None:
        raw_path = inp.get("path", "")
        if not raw_path:
            return "path is required"
        p = Path(raw_path)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (self.workspace / raw_path).resolve()
        if not _is_under_or_equal(resolved, self.workspace):
            return f"Write outside workspace denied: {raw_path}"
        return None

    def _check_read(self, inp: dict[str, Any]) -> str | None:
        raw_path = inp.get("path", "")
        if not raw_path:
            return "path is required for read"
        p = Path(raw_path)
        try:
            resolved = p.resolve() if p.is_absolute() else (self.workspace / raw_path).resolve()
        except (OSError, ValueError) as e:
            return f"Invalid path: {raw_path} ({e})"

        if _is_under_or_equal(resolved, self.workspace):
            return None
        for allowed in self.allowed_read_dirs:
            if _is_under_or_equal(resolved, allowed):
                return None
        return (
            f"Read outside allowed directories: {raw_path}\n"
            f"Allowed: workspace + {[str(d) for d in self.allowed_read_dirs]}"
        )
