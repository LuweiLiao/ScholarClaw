"""Bash/shell execution tool."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from researchclaw.pipeline.claw_engine.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
    PermissionDecision,
)
from researchclaw.pipeline.claw_engine.tools.permissions import SandboxPermissionPolicy

DANGEROUS_PATTERNS = (
    "rm -rf /", "rm -rf /*", "mkfs.", "dd if=/dev/zero",
    ":(){ :", "> /dev/sda", "chmod -R 777 /",
    "curl | sh", "wget | sh", "shutdown", "reboot",
    "kill -9 1", "pkill -9",
)


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command in the experiment workspace. "
        "Use for running Python scripts, installing packages, checking output, etc. "
        "Commands run with a timeout. Prefer short, targeted commands."
    )
    is_read_only = False
    is_concurrency_safe = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute."},
                "timeout": {
                    "type": "integer", "minimum": 1, "default": 60,
                    "description": "Timeout in seconds (default 60).",
                },
                "description": {
                    "type": "string",
                    "description": "Brief description of what this command does (5-10 words).",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    def check_permissions(
        self, args: dict[str, Any], context: ToolContext,
    ) -> PermissionDecision:
        pol = SandboxPermissionPolicy(
            context.workspace, list(context.allowed_read_dirs or []),
        )
        err = pol.check("bash", args)
        if err:
            return PermissionDecision.DENY
        command = args.get("command", "").lower().strip()
        for pattern in DANGEROUS_PATTERNS:
            if pattern in command:
                return PermissionDecision.DENY
        return PermissionDecision.ALLOW

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        command = args.get("command", "")
        if not command:
            return ToolResult(data="command is required", is_error=True)
        timeout = min(args.get("timeout", context.bash_timeout), context.bash_timeout)

        cmd_lower = command.lower()
        for pattern in DANGEROUS_PATTERNS:
            if pattern in cmd_lower:
                return ToolResult(
                    data=f"Dangerous command blocked: {command[:80]}",
                    is_error=True,
                )

        env = os.environ.copy()
        env["WORKSPACE"] = str(context.workspace)

        if context.python_path and os.path.isfile(context.python_path):
            python_bin_dir = os.path.dirname(os.path.realpath(context.python_path))
            sep = ";" if os.name == "nt" else ":"
            env["PATH"] = python_bin_dir + sep + env.get("PATH", "")
            env_prefix = os.path.dirname(python_bin_dir)
            env["CONDA_PREFIX"] = env_prefix
            env["VIRTUAL_ENV"] = env_prefix

        try:
            if os.name == "nt":
                shell_cmd = ["cmd.exe", "/c", command]
            else:
                shell_cmd = ["bash", "-c", command]
            result = subprocess.run(
                shell_cmd,
                cwd=str(context.workspace),
                env=env,
                capture_output=True,
                timeout=timeout,
            )
            stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            output_parts = []
            if stdout:
                output_parts.append(stdout)
            if stderr:
                output_parts.append(f"[stderr]\n{stderr}")
            if result.returncode != 0:
                output_parts.append(f"[exit_code: {result.returncode}]")
            output = "\n".join(output_parts) or "(no output)"
        except subprocess.TimeoutExpired:
            output = f"Command timed out after {timeout}s: {command[:100]}"

        data = self._truncate(output)
        return ToolResult(data=data)

    def summarize_input(self, args: dict[str, Any]) -> str:
        cmd = args.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")

    @staticmethod
    def _truncate(text: str, max_chars: int = 24000) -> str:
        if len(text) <= max_chars:
            return text
        head_budget = max_chars * 3 // 10
        tail_budget = max_chars - head_budget - 200
        return (
            f"{text[:head_budget]}\n\n"
            f"... [{len(text)} total chars, middle truncated] ...\n\n"
            f"{text[-tail_budget:]}"
        )
