"""Tool Result Storage — inspired by Claude Code's toolResultStorage.ts.

Handles oversized tool results by:
  1. Persisting full output to disk (run_dir/tool_results/)
  2. Replacing context with a truncated preview + metadata
  3. Providing empty-result placeholders to avoid model confusion
  4. Tracking token usage per turn for context window management
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULT_CHARS = 16000
BASH_MAX_RESULT_CHARS = 24000
LARGE_RESULT_PREVIEW_LINES = 30
CONTEXT_BUDGET_CHARS = 100_000


class ToolResultStore:
    """Manages tool result persistence and context compaction.

    Large results are written to disk and replaced with a summary
    that preserves the first/last N lines — ensuring the LLM sees
    both initial output and final errors/results.
    """

    def __init__(
        self,
        run_dir: Path,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    ) -> None:
        self._run_dir = run_dir
        self._store_dir = run_dir / "tool_results"
        self._max_chars = max_result_chars
        self._results: dict[str, dict[str, Any]] = {}
        self._total_chars = 0

    def process_result(
        self,
        tool_name: str,
        tool_call_id: str,
        result_text: str,
        is_error: bool = False,
    ) -> str:
        """Process a tool result: persist if oversized, return text for context.

        Returns the (possibly truncated) result string to include in the
        conversation context sent to the LLM.
        """
        if not result_text or not result_text.strip():
            placeholder = f"({tool_name} completed with no output)"
            self._track(tool_call_id, tool_name, placeholder, persisted=False)
            return placeholder

        threshold = BASH_MAX_RESULT_CHARS if tool_name == "bash" else self._max_chars

        if len(result_text) <= threshold:
            self._track(tool_call_id, tool_name, result_text, persisted=False)
            return result_text

        persisted_path = self._persist(tool_call_id, tool_name, result_text)

        preview = _build_preview(result_text, tool_name, threshold)
        wrapped = (
            f"<persisted-output path=\"{persisted_path}\" "
            f"total_chars=\"{len(result_text)}\">\n"
            f"{preview}\n"
            f"</persisted-output>"
        )

        self._track(tool_call_id, tool_name, wrapped, persisted=True,
                     full_size=len(result_text), path=str(persisted_path))
        return wrapped

    def compact_messages(
        self,
        messages: list[dict[str, Any]],
        budget_chars: int = CONTEXT_BUDGET_CHARS,
        keep_recent: int = 6,
    ) -> list[dict[str, Any]]:
        """Compact conversation messages to fit within a token budget.

        Inspired by Claude Code's context management: keep system + recent
        messages intact, summarize older turns.
        """
        if not messages:
            return messages

        total = sum(_msg_size(m) for m in messages)
        if total <= budget_chars:
            return messages

        protected = messages[-keep_recent:]
        older = messages[:-keep_recent]

        compacted: list[dict[str, Any]] = []
        for msg in older:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 2000:
                compacted.append({
                    **msg,
                    "content": _compact_content(content),
                })
            else:
                compacted.append(msg)

        result = compacted + protected

        final_total = sum(_msg_size(m) for m in result)
        if final_total > budget_chars:
            drop_count = 0
            while final_total > budget_chars and drop_count < len(compacted):
                final_total -= _msg_size(compacted[drop_count])
                drop_count += 1
            summary_msg = {
                "role": "user",
                "content": f"[{drop_count} earlier messages omitted to fit context window]",
            }
            result = [summary_msg] + compacted[drop_count:] + protected

        return result

    def get_persisted(self, tool_call_id: str) -> str | None:
        """Retrieve the full persisted result for a tool call."""
        info = self._results.get(tool_call_id)
        if not info or not info.get("persisted"):
            return None
        path = Path(info["path"])
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def stats(self) -> dict[str, Any]:
        persisted = sum(1 for v in self._results.values() if v.get("persisted"))
        return {
            "total_results": len(self._results),
            "persisted_count": persisted,
            "total_context_chars": self._total_chars,
        }

    def _persist(self, tool_call_id: str, tool_name: str, text: str) -> Path:
        self._store_dir.mkdir(parents=True, exist_ok=True)
        hash_suffix = hashlib.md5(tool_call_id.encode()).hexdigest()[:8]
        filename = f"{tool_name}_{hash_suffix}.txt"
        path = self._store_dir / filename
        path.write_text(text, encoding="utf-8")
        logger.debug("Persisted tool result: %s (%d chars)", path, len(text))
        return path

    def _track(
        self,
        tool_call_id: str,
        tool_name: str,
        context_text: str,
        persisted: bool,
        full_size: int = 0,
        path: str = "",
    ) -> None:
        self._results[tool_call_id] = {
            "tool": tool_name,
            "persisted": persisted,
            "context_size": len(context_text),
            "full_size": full_size or len(context_text),
            "path": path,
            "timestamp": time.time(),
        }
        self._total_chars += len(context_text)


def _build_preview(
    text: str, tool_name: str, max_chars: int,
) -> str:
    """Build a head+tail preview of a large result."""
    lines = text.splitlines()

    if tool_name == "bash":
        head_ratio = 0.3
    else:
        head_ratio = 0.5

    head_budget = int(max_chars * head_ratio)
    tail_budget = max_chars - head_budget - 200

    head_lines: list[str] = []
    head_chars = 0
    for line in lines:
        if head_chars + len(line) > head_budget:
            break
        head_lines.append(line)
        head_chars += len(line) + 1

    tail_lines: list[str] = []
    tail_chars = 0
    for line in reversed(lines):
        if tail_chars + len(line) > tail_budget:
            break
        tail_lines.insert(0, line)
        tail_chars += len(line) + 1

    omitted = len(lines) - len(head_lines) - len(tail_lines)
    if omitted <= 0:
        return text[:max_chars]

    return (
        "\n".join(head_lines) +
        f"\n\n... [{omitted} lines omitted, {len(text)} total chars] ...\n\n" +
        "\n".join(tail_lines)
    )


def _msg_size(msg: dict[str, Any]) -> int:
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(str(b)) for b in content)
    return 0


def _compact_content(content: str, max_chars: int = 800) -> str:
    """Compact a long message content to a summary."""
    lines = content.splitlines()
    if len(lines) <= 5:
        return content[:max_chars] + ("..." if len(content) > max_chars else "")

    head = "\n".join(lines[:3])
    tail = "\n".join(lines[-2:])
    return (
        f"{head}\n"
        f"... [{len(lines) - 5} lines omitted] ...\n"
        f"{tail}"
    )[:max_chars]
