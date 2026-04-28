"""
Activity event writer for ScholarLab timeline.

Writes structured JSONL events to `activity.jsonl` in the run directory.
These events are read by `agent_bridge.py` and broadcast as `agent_activity`
WebSocket messages to the frontend timeline.

Event types match the frontend `ActivityType` union:
  thinking | tool_call | tool_result | file_read | file_write |
  llm_call | llm_response | stage_transition | error
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()


def _ts() -> int:
    return int(time.time() * 1000)


def write_event(
    run_dir: str | Path,
    event_type: str,
    summary: str,
    detail: str = "",
    *,
    stage: int | None = None,
    tool_name: str = "",
    tokens: int = 0,
    elapsed_ms: int = 0,
) -> None:
    """Append one activity event to activity.jsonl."""
    event = {
        "type": event_type,
        "summary": summary,
        "timestamp": _ts(),
    }
    if detail:
        event["detail"] = detail
    if stage is not None:
        event["stage"] = stage
    if tool_name:
        event["tool"] = tool_name
    if tokens:
        event["tokens"] = tokens
    if elapsed_ms:
        event["elapsed_ms"] = elapsed_ms

    line = json.dumps(event, ensure_ascii=False) + "\n"
    path = Path(run_dir) / "activity.jsonl"
    with _lock:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


class ActivityLogger:
    """Context-bound activity logger for a specific run directory."""

    def __init__(self, run_dir: str | Path, stage: int | None = None):
        self._run_dir = str(run_dir)
        self._stage = stage

    def set_stage(self, stage: int) -> None:
        self._stage = stage

    def thinking(self, summary: str, detail: str = "") -> None:
        write_event(self._run_dir, "thinking", summary, detail, stage=self._stage)

    def llm_call(self, model: str, n_messages: int, summary: str = "") -> None:
        write_event(
            self._run_dir, "llm_call",
            summary or f"调用 {model} ({n_messages} 条消息)",
            stage=self._stage,
        )

    def llm_response(
        self, model: str, tokens: int = 0, text_len: int = 0,
        elapsed_ms: int = 0, summary: str = "",
    ) -> None:
        write_event(
            self._run_dir, "llm_response",
            summary or f"{model} 回复 ({tokens} tokens, {text_len} chars)",
            stage=self._stage, tokens=tokens, elapsed_ms=elapsed_ms,
        )

    def tool_call(
        self, tool_name: str, summary: str, detail: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        write_event(
            self._run_dir, "tool_call", summary, detail,
            stage=self._stage, tool_name=tool_name, elapsed_ms=elapsed_ms,
        )

    def tool_result(
        self, tool_name: str, summary: str, detail: str = "",
        is_error: bool = False,
    ) -> None:
        _type = "error" if is_error else "tool_result"
        write_event(
            self._run_dir, _type, summary, detail,
            stage=self._stage, tool_name=tool_name,
        )

    def file_read(self, path: str, size: int = 0) -> None:
        _name = Path(path).name
        _s = f"{size / 1024:.1f}KB" if size > 0 else ""
        write_event(
            self._run_dir, "file_read",
            f"读取 {_name}" + (f" ({_s})" if _s else ""),
            stage=self._stage,
        )

    def file_write(self, path: str, size: int = 0) -> None:
        _name = Path(path).name
        _s = f"{size / 1024:.1f}KB" if size > 0 else ""
        write_event(
            self._run_dir, "file_write",
            f"写入 {_name}" + (f" ({_s})" if _s else ""),
            stage=self._stage,
        )

    def stage_start(self, stage: int, stage_name: str) -> None:
        self._stage = stage
        write_event(
            self._run_dir, "stage_transition",
            f"⏳ 开始 S{stage} {stage_name}",
            stage=stage,
        )

    def stage_done(self, stage: int, stage_name: str, elapsed_sec: float = 0, artifacts: str = "") -> None:
        write_event(
            self._run_dir, "stage_transition",
            f"✅ S{stage} {stage_name} 完成 ({elapsed_sec:.1f}s)",
            detail=f"产出: {artifacts}" if artifacts else "",
            stage=stage, elapsed_ms=int(elapsed_sec * 1000),
        )

    def stage_failed(self, stage: int, stage_name: str, error: str = "") -> None:
        write_event(
            self._run_dir, "error",
            f"❌ S{stage} {stage_name} 失败",
            detail=error,
            stage=stage,
        )

    def error(self, summary: str, detail: str = "") -> None:
        write_event(self._run_dir, "error", summary, detail, stage=self._stage)
