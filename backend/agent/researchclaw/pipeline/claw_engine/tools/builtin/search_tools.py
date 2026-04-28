"""Search tools: glob and grep."""

from __future__ import annotations

import fnmatch
import re
import time
from pathlib import Path
from typing import Any

from researchclaw.pipeline.claw_engine.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
)


class GlobSearchTool(Tool):
    name = "glob_search"
    description = (
        "Find files by glob pattern in the workspace or data directories. "
        "Returns matching file paths sorted by modification time (newest first). "
        "Capped at 200 results."
    )
    is_read_only = True
    is_concurrency_safe = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py', '*.yaml')."},
                "path": {
                    "type": "string",
                    "description": "Base directory to search in (default: workspace root).",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        import os
        pattern = args["pattern"]
        base_str = args.get("path")
        if base_str:
            base = Path(base_str).resolve() if os.path.isabs(base_str) else (context.workspace / base_str).resolve()
        else:
            base = context.workspace
        return_relative = base == context.workspace

        if not base.is_dir():
            return ToolResult(data=f"Not a directory: {base}")

        is_recursive = "**" in pattern
        matches: list[tuple[float, Path]] = []
        cap = 200
        deadline = time.monotonic() + 8.0
        timed_out = False
        scanned = 0

        for p in base.glob(pattern):
            scanned += 1
            if time.monotonic() > deadline or scanned > 50000:
                timed_out = True
                break
            if p.is_symlink() and is_recursive:
                continue
            if p.is_file() and not any(
                part.startswith(".") or part == "__pycache__" for part in p.parts
            ):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0
                matches.append((mtime, p))
                if len(matches) >= cap * 5:
                    break

        matches.sort(key=lambda x: -x[0])
        truncated = len(matches) > cap
        matches = matches[:cap]

        lines: list[str] = []
        for _, p in matches:
            if return_relative:
                try:
                    lines.append(str(p.relative_to(base)))
                    continue
                except ValueError:
                    pass
            lines.append(str(p))

        header = f"Found {len(lines)} file(s)"
        if truncated:
            header += f" (showing first {cap})"
        if timed_out:
            header += " [TIMEOUT: directory too large]"
        data = header + "\n" + "\n".join(lines) if lines else "No files matched."
        return ToolResult(data=data)

    def summarize_input(self, args: dict[str, Any]) -> str:
        return args.get("pattern", "?")


class GrepSearchTool(Tool):
    name = "grep_search"
    description = (
        "Search file contents with a regex pattern. Returns matching lines with "
        "file paths and line numbers."
    )
    is_read_only = True
    is_concurrency_safe = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for."},
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: workspace root).",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. '*.py').",
                },
                "context": {
                    "type": "integer", "minimum": 0, "default": 2,
                    "description": "Number of context lines before and after each match.",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        import os
        pattern_str = args["pattern"]
        base_str = args.get("path")
        file_glob = args.get("glob")
        context_lines = args.get("context", 2)

        if base_str:
            base = Path(base_str).resolve() if os.path.isabs(base_str) else (context.workspace / base_str).resolve()
        else:
            base = context.workspace

        try:
            regex = re.compile(pattern_str, re.IGNORECASE)
        except re.error as e:
            return ToolResult(data=f"Invalid regex: {e}", is_error=True)

        if base.is_file():
            files = [base]
        else:
            deadline = time.monotonic() + 8.0
            collected: list[Path] = []
            for f in base.rglob("*"):
                if time.monotonic() > deadline or len(collected) > 5000:
                    break
                if f.is_symlink():
                    continue
                if f.is_file() and not any(
                    p.startswith(".") or p == "__pycache__"
                    for p in f.relative_to(base).parts
                ):
                    collected.append(f)
            files = sorted(collected)
            if file_glob:
                files = [f for f in files if fnmatch.fnmatch(f.name, file_glob)]

        results: list[str] = []
        match_count = 0
        max_matches = 200

        for fpath in files:
            if match_count >= max_matches:
                break
            try:
                if fpath.stat().st_size > 2 * 1024 * 1024:
                    continue
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            flines = text.splitlines()
            file_matches: list[str] = []
            for i, line in enumerate(flines):
                if regex.search(line):
                    match_count += 1
                    start = max(0, i - context_lines)
                    end = min(len(flines), i + context_lines + 1)
                    for j in range(start, end):
                        prefix = ":" if j == i else "-"
                        file_matches.append(f"  {j+1}{prefix} {flines[j]}")
                    if end < len(flines):
                        file_matches.append("  ---")
            if file_matches:
                try:
                    rel = fpath.relative_to(base)
                except ValueError:
                    rel = fpath
                results.append(f"{rel}:\n" + "\n".join(file_matches))

        if not results:
            return ToolResult(data=f"No matches for /{pattern_str}/")

        header = f"{match_count} match(es) in {len(results)} file(s)"
        if match_count >= max_matches:
            header += " (truncated)"
        data = header + "\n\n" + "\n\n".join(results)
        if len(data) > 16000:
            data = data[:16000] + f"\n... [truncated, {len(data)} total chars]"
        return ToolResult(data=data)

    def summarize_input(self, args: dict[str, Any]) -> str:
        return args.get("pattern", "?")
