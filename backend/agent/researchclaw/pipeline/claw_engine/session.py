"""Generic session state tracker for claw-code agentic stages.

A stage-agnostic version of CodegenSession that any pipeline stage
can use to track progress, log entries, and persist debug artifacts.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Lazy import to avoid circular deps at module load time
_get_event_bus = None
_EventType = None
_AgentEvent = None


def _emit_stage_session(stage_dir: Path, payload: dict[str, Any]) -> None:
    """Try to infer project_id from stage_dir and emit a StageSessionUpdate event."""
    global _get_event_bus, _EventType, _AgentEvent
    if _get_event_bus is None:
        try:
            from researchclaw.pipeline.claw_engine.event_bus import (
                AgentEvent,
                EventType,
                get_event_bus,
            )
            _get_event_bus = get_event_bus
            _EventType = EventType
            _AgentEvent = AgentEvent
        except Exception:
            return
    if _get_event_bus is None or _EventType is None:
        return

    # Infer project_id from path: .../projects/<proj-id>/stage-NN/...
    parts = stage_dir.parts
    project_id: str | None = None
    for i, part in enumerate(parts):
        if part == "projects" and i + 1 < len(parts):
            project_id = parts[i + 1]
            break
    if not project_id:
        # Fallback: use parent directory name if it looks like a project dir
        for i, part in enumerate(parts):
            if part.startswith("proj-"):
                project_id = part
                break
    if not project_id:
        return

    try:
        bus = _get_event_bus(project_id)
        event = _AgentEvent(
            type=_EventType.STAGE_SESSION_UPDATE,
            agent_id="",
            data={"stage_dir": str(stage_dir), "session": payload},
        )
        bus.emit(event)
    except Exception:
        pass


@dataclass
class StageSession:
    """Mutable state accumulated across an agentic stage execution.

    Provides timestamped logging with auto-persist (JSON snapshot +
    streaming text log for live ``tail -f`` debugging).
    """

    stage_dir: Path
    stage_name: str = ""
    phase_log: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    llm_calls: int = 0
    sandbox_runs: int = 0
    current_phase: str = ""
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _start_time: float = field(default_factory=time.monotonic)
    _auto_persist: bool = True

    def log(self, phase: str, message: str) -> None:
        elapsed = time.monotonic() - self._start_time
        self.current_phase = phase
        entry = f"[{elapsed:7.1f}s] [{phase}] {message}"
        self.phase_log.append(entry)
        logger.info("[%s] %s", self.stage_name or "StageSession", entry)
        self._append_to_live_log(entry)
        if self._auto_persist:
            self._try_persist()

    def log_error(self, phase: str, message: str, exc: Exception | None = None) -> None:
        error_msg = message
        if exc is not None:
            error_msg = f"{message}: {type(exc).__name__}: {exc}"
        self.errors.append(error_msg)
        self.log(phase, f"ERROR: {error_msg}")

    def add_artifact(self, name: str) -> None:
        if name not in self.artifacts:
            self.artifacts.append(name)

    def elapsed_sec(self) -> float:
        return time.monotonic() - self._start_time

    def _infer_status(self) -> str:
        """Infer runtime status from current state."""
        if self.errors:
            return "failed"
        if self.current_phase in ("FINALIZE", "COMPLETE", "DONE"):
            return "completed"
        if self.current_phase:
            return "running"
        return "pending"

    def persist(self) -> Path:
        log_path = self.stage_dir / f"{self.stage_name or 'stage'}_session.json"
        payload: dict[str, Any] = {
            "stage_name": self.stage_name,
            "current_phase": self.current_phase,
            "status": self._infer_status(),
            "elapsed_sec": round(self.elapsed_sec(), 1),
            "llm_calls": self.llm_calls,
            "sandbox_runs": self.sandbox_runs,
            "phase_log": self.phase_log,
            "artifacts": self.artifacts,
            "errors": self.errors,
            "metadata": self.metadata,
        }
        log_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _emit_stage_session(self.stage_dir, payload)
        return log_path

    def _try_persist(self) -> None:
        try:
            self.persist()
        except Exception:
            pass

    def _append_to_live_log(self, entry: str) -> None:
        try:
            log_path = self.stage_dir / f"{self.stage_name or 'stage'}_live.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass
