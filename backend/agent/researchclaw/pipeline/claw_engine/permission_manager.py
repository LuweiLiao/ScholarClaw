"""Permission Manager — inspired by Claude Code's useCanUseTool.tsx.

Provides a rich permission system with:
  - Per-tool-type default rules (allow/deny/ask)
  - Pattern-based allow/deny rules with persistence
  - WebSocket-based approval flow (not file-based)
  - Interactive confirmation queue with "allow once" / "always allow" / "deny"
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PermissionLevel(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ApprovalMode(str, Enum):
    AUTO = "auto"
    CONFIRM_WRITES = "confirm_writes"
    CONFIRM_ALL = "confirm_all"


class ApprovalDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALWAYS_ALLOW = "always_allow"
    DENY = "deny"
    ABORT = "abort"


DEFAULT_TOOL_PERMISSIONS: dict[str, PermissionLevel] = {
    "read_file": PermissionLevel.ALLOW,
    "glob_search": PermissionLevel.ALLOW,
    "grep_search": PermissionLevel.ALLOW,
    "bib_search": PermissionLevel.ALLOW,
    "data_analysis": PermissionLevel.ALLOW,
    "web_search": PermissionLevel.ALLOW,
    "write_file": PermissionLevel.ASK,
    "edit_file": PermissionLevel.ASK,
    "bash": PermissionLevel.ASK,
    "latex_compile": PermissionLevel.ALLOW,
}

DANGEROUS_BASH_PATTERNS = frozenset({
    "rm -rf /", "rm -rf /*", "mkfs.", "dd if=/dev/zero",
    ":(){ :", "> /dev/sda", "chmod -R 777 /",
    "curl | sh", "wget | sh", "shutdown", "reboot",
    "kill -9 1", "pkill -9",
})


class ApprovalRequest:
    """A pending approval request waiting for user response."""

    def __init__(
        self,
        request_id: str,
        tool_name: str,
        args: dict[str, Any],
        agent_id: str,
    ) -> None:
        self.request_id = request_id
        self.tool_name = tool_name
        self.args = args
        self.agent_id = agent_id
        self.timestamp = time.time()
        self._event = threading.Event()
        self._decision: ApprovalDecision | None = None

    def resolve(self, decision: ApprovalDecision) -> None:
        self._decision = decision
        self._event.set()

    def wait(self, timeout: float = 300.0) -> ApprovalDecision:
        """Wait for user decision. Returns DENY on timeout."""
        self._event.wait(timeout=timeout)
        return self._decision or ApprovalDecision.DENY

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "toolName": self.tool_name,
            "args": self.args,
            "agentId": self.agent_id,
            "timestamp": int(self.timestamp * 1000),
        }


class PermissionManager:
    """Manages tool permissions with persistent storage and interactive approval.

    Mirrors Claude Code's permission architecture:
    - Default rules per tool type
    - User-granted persistent permissions (always_allow patterns)
    - Interactive approval queue via WebSocket
    - Approval mode (auto / confirm_writes / confirm_all)
    """

    def __init__(
        self,
        workspace: Path,
        approval_mode: ApprovalMode = ApprovalMode.AUTO,
        permissions_file: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._mode = approval_mode
        self._permissions_file = permissions_file or workspace / ".scholarlab" / "permissions.json"
        self._lock = threading.Lock()

        self._always_allow: dict[str, set[str]] = {}
        self._always_deny: dict[str, set[str]] = {}
        self._pending: dict[str, ApprovalRequest] = {}

        self._on_request: Callable[[ApprovalRequest], None] | None = None

        self._load_persistent()

    def set_mode(self, mode: ApprovalMode) -> None:
        self._mode = mode

    def set_request_handler(self, handler: Callable[[ApprovalRequest], None]) -> None:
        """Set callback for when an approval request is created.

        The handler should send the request to the frontend via WebSocket.
        """
        self._on_request = handler

    def check_permission(
        self,
        tool_name: str,
        args: dict[str, Any],
        agent_id: str = "",
    ) -> PermissionLevel:
        """Check if a tool call should be allowed, denied, or needs approval.

        Returns the permission level. In AUTO mode, most tools are allowed.
        In CONFIRM_WRITES mode, write tools require approval.
        In CONFIRM_ALL mode, all tools require approval.
        """
        if tool_name == "bash":
            cmd = args.get("command", "").lower()
            for pattern in DANGEROUS_BASH_PATTERNS:
                if pattern in cmd:
                    return PermissionLevel.DENY

        if self._check_always_allow(tool_name, args):
            return PermissionLevel.ALLOW

        if self._check_always_deny(tool_name, args):
            return PermissionLevel.DENY

        if self._mode == ApprovalMode.AUTO:
            default = DEFAULT_TOOL_PERMISSIONS.get(tool_name, PermissionLevel.ALLOW)
            if default == PermissionLevel.ASK:
                return PermissionLevel.ALLOW
            return default

        if self._mode == ApprovalMode.CONFIRM_ALL:
            return PermissionLevel.ASK

        if self._mode == ApprovalMode.CONFIRM_WRITES:
            default = DEFAULT_TOOL_PERMISSIONS.get(tool_name, PermissionLevel.ALLOW)
            if default == PermissionLevel.ALLOW:
                return PermissionLevel.ALLOW
            return PermissionLevel.ASK

        return DEFAULT_TOOL_PERMISSIONS.get(tool_name, PermissionLevel.ALLOW)

    def request_approval(
        self,
        tool_name: str,
        args: dict[str, Any],
        agent_id: str = "",
        timeout: float = 300.0,
    ) -> ApprovalDecision:
        """Create an approval request and wait for user response.

        This blocks the calling thread until the user responds or timeout.
        """
        request_id = str(uuid.uuid4())[:8]
        request = ApprovalRequest(
            request_id=request_id,
            tool_name=tool_name,
            args=args,
            agent_id=agent_id,
        )

        with self._lock:
            self._pending[request_id] = request

        if self._on_request:
            try:
                self._on_request(request)
            except Exception:
                logger.exception("Error in approval request handler")

        decision = request.wait(timeout=timeout)

        with self._lock:
            self._pending.pop(request_id, None)

        if decision == ApprovalDecision.ALWAYS_ALLOW:
            self._add_always_allow(tool_name, args)

        return decision

    def resolve_request(self, request_id: str, decision: ApprovalDecision) -> bool:
        """Resolve a pending approval request with the user's decision.

        Called by the WebSocket handler when the frontend responds.
        """
        with self._lock:
            request = self._pending.get(request_id)
        if not request:
            return False
        request.resolve(decision)
        return True

    def get_pending_requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._pending.values()]

    def grant_always_allow(self, tool_name: str, pattern: str = "*") -> None:
        self._always_allow.setdefault(tool_name, set()).add(pattern)
        self._save_persistent()

    def grant_always_deny(self, tool_name: str, pattern: str = "*") -> None:
        self._always_deny.setdefault(tool_name, set()).add(pattern)
        self._save_persistent()

    def get_rules(self) -> dict[str, Any]:
        return {
            "mode": self._mode.value,
            "always_allow": {k: list(v) for k, v in self._always_allow.items()},
            "always_deny": {k: list(v) for k, v in self._always_deny.items()},
            "defaults": {k: v.value for k, v in DEFAULT_TOOL_PERMISSIONS.items()},
        }

    def _check_always_allow(self, tool_name: str, args: dict[str, Any]) -> bool:
        patterns = self._always_allow.get(tool_name, set())
        if "*" in patterns:
            return True
        path = args.get("path", "")
        if path:
            for p in patterns:
                if self._path_matches(path, p):
                    return True
        return False

    def _check_always_deny(self, tool_name: str, args: dict[str, Any]) -> bool:
        patterns = self._always_deny.get(tool_name, set())
        if "*" in patterns:
            return True
        return False

    def _add_always_allow(self, tool_name: str, args: dict[str, Any]) -> None:
        path = args.get("path")
        pattern = path if path else "*"
        self._always_allow.setdefault(tool_name, set()).add(pattern)
        self._save_persistent()

    @staticmethod
    def _path_matches(path: str, pattern: str) -> bool:
        import fnmatch
        return fnmatch.fnmatch(path, pattern)

    def _load_persistent(self) -> None:
        if not self._permissions_file.exists():
            return
        try:
            data = json.loads(self._permissions_file.read_text(encoding="utf-8"))
            for tool, patterns in data.get("always_allow", {}).items():
                self._always_allow[tool] = set(patterns)
            for tool, patterns in data.get("always_deny", {}).items():
                self._always_deny[tool] = set(patterns)
            mode_str = data.get("mode")
            if mode_str:
                try:
                    self._mode = ApprovalMode(mode_str)
                except ValueError:
                    pass
        except Exception:
            logger.debug("Could not load persistent permissions")

    def _save_persistent(self) -> None:
        try:
            self._permissions_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "mode": self._mode.value,
                "always_allow": {k: sorted(v) for k, v in self._always_allow.items()},
                "always_deny": {k: sorted(v) for k, v in self._always_deny.items()},
            }
            self._permissions_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Could not save persistent permissions")
