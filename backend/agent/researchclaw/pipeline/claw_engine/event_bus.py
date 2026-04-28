"""Event Bus for real-time streaming — inspired by Claude Code's signal.ts.

Provides decoupled event propagation between the agent turn loop and
the WebSocket bridge. Events are pushed to a thread-safe queue and can
be consumed by multiple subscribers.

Event types mirror the frontend ActivityType + conversation deltas:
  thinking_delta, text_delta, tool_use_start, tool_use_end,
  tool_result, stage_change, llm_call, llm_response,
  permission_request, permission_response, error, conversation_turn
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    THINKING_DELTA = "thinking_delta"
    TEXT_DELTA = "text_delta"
    TOOL_USE_START = "tool_use_start"
    TOOL_USE_END = "tool_use_end"
    TOOL_RESULT = "tool_result"
    STAGE_CHANGE = "stage_change"
    LLM_CALL = "llm_call"
    LLM_RESPONSE = "llm_response"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_RESPONSE = "permission_response"
    ERROR = "error"
    CONVERSATION_TURN = "conversation_turn"
    FILE_WRITE = "file_write"
    FILE_READ = "file_read"


@dataclass
class AgentEvent:
    """A single event from the agent execution pipeline."""
    type: EventType
    agent_id: str
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "agent_id": self.agent_id,
            "timestamp": int(self.timestamp * 1000),
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


Subscriber = Callable[[AgentEvent], None]


class EventBus:
    """Thread-safe event bus with pub/sub + queue-based consumption.

    The turn loop (running in a subprocess/thread) pushes events.
    The bridge (asyncio) polls the queue and broadcasts via WebSocket.
    """

    def __init__(self, max_queue_size: int = 10000) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._queue: queue.Queue[AgentEvent] = queue.Queue(maxsize=max_queue_size)
        self._lock = threading.Lock()
        self._running = True

    def emit(self, event: AgentEvent) -> None:
        """Emit an event: push to queue and notify subscribers."""
        if not self._running:
            return

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("EventBus queue full, dropping event: %s", event.type)

        with self._lock:
            subs = self._subscribers.get("*", []) + \
                   self._subscribers.get(event.type.value, [])
        for sub in subs:
            try:
                sub(event)
            except Exception:
                logger.exception("Subscriber error for event %s", event.type)

    def subscribe(self, event_type: str, callback: Subscriber) -> None:
        """Subscribe to events. Use '*' for all events."""
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def unsubscribe(self, event_type: str, callback: Subscriber) -> None:
        with self._lock:
            subs = self._subscribers.get(event_type, [])
            if callback in subs:
                subs.remove(callback)

    def poll(self, timeout: float = 0.1) -> AgentEvent | None:
        """Poll the queue for the next event. Non-blocking if timeout=0."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, max_events: int = 100) -> list[AgentEvent]:
        """Drain up to max_events from the queue."""
        events: list[AgentEvent] = []
        for _ in range(max_events):
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    def close(self) -> None:
        self._running = False
        with self._lock:
            self._subscribers.clear()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()


class EventEmitter:
    """Convenience wrapper for emitting events from within a turn loop.

    Bound to a specific agent_id so callers don't repeat it.
    Also writes events to activity.jsonl for backward compatibility.
    """

    def __init__(
        self,
        bus: EventBus,
        agent_id: str,
        run_dir: Path | None = None,
    ) -> None:
        self._bus = bus
        self._agent_id = agent_id
        self._run_dir = run_dir

    def _emit(self, event_type: EventType, **data: Any) -> None:
        event = AgentEvent(type=event_type, agent_id=self._agent_id, data=data)
        self._bus.emit(event)
        if self._run_dir:
            self._write_activity(event)

    def _write_activity(self, event: AgentEvent) -> None:
        """Write to activity.jsonl for backward compatibility with file-based polling."""
        _type_map = {
            EventType.THINKING_DELTA: "thinking",
            EventType.TEXT_DELTA: "thinking",
            EventType.TOOL_USE_START: "tool_call",
            EventType.TOOL_USE_END: "tool_call",
            EventType.TOOL_RESULT: "tool_result",
            EventType.STAGE_CHANGE: "stage_transition",
            EventType.LLM_CALL: "llm_call",
            EventType.LLM_RESPONSE: "llm_response",
            EventType.ERROR: "error",
            EventType.FILE_WRITE: "file_write",
            EventType.FILE_READ: "file_read",
        }
        activity_type = _type_map.get(event.type, event.type.value)
        from researchclaw.pipeline.activity_writer import write_event
        write_event(
            self._run_dir,
            activity_type,
            event.data.get("summary", ""),
            event.data.get("detail", ""),
            tool_name=event.data.get("tool_name", ""),
            tokens=event.data.get("tokens", 0),
            elapsed_ms=event.data.get("elapsed_ms", 0),
        )

    def thinking(self, text: str, is_delta: bool = False) -> None:
        preview = text[:200].replace('\n', ' ')
        self._emit(
            EventType.THINKING_DELTA if is_delta else EventType.TEXT_DELTA,
            summary=f"💭 {preview}{'...' if len(text) > 200 else ''}",
            detail=text[:2000] if len(text) > 200 else "",
            text=text,
        )

    def llm_call(self, model: str, n_messages: int, turn: int = 0) -> None:
        self._emit(
            EventType.LLM_CALL,
            summary=f"Turn {turn}: 调用 {model}...",
            model=model,
            n_messages=n_messages,
            turn=turn,
        )

    def llm_response(
        self, model: str, tokens: int = 0, text_len: int = 0,
        elapsed_ms: int = 0, n_tool_calls: int = 0,
    ) -> None:
        self._emit(
            EventType.LLM_RESPONSE,
            summary=f"🤖 回复: {tokens} tokens, {n_tool_calls} 工具调用 ({elapsed_ms}ms)",
            model=model,
            tokens=tokens,
            text_len=text_len,
            elapsed_ms=elapsed_ms,
            n_tool_calls=n_tool_calls,
        )

    def tool_start(self, tool_name: str, summary: str, args: dict[str, Any] | None = None) -> None:
        self._emit(
            EventType.TOOL_USE_START,
            summary=summary,
            tool_name=tool_name,
            args=args or {},
        )

    def tool_result(
        self, tool_name: str, summary: str, detail: str = "",
        is_error: bool = False, elapsed_ms: int = 0,
    ) -> None:
        etype = EventType.ERROR if is_error else EventType.TOOL_RESULT
        self._emit(
            etype,
            summary=summary,
            detail=detail,
            tool_name=tool_name,
            is_error=is_error,
            elapsed_ms=elapsed_ms,
        )

    def stage_change(self, stage: int, stage_name: str, status: str = "start") -> None:
        self._emit(
            EventType.STAGE_CHANGE,
            summary=f"{'⏳' if status == 'start' else '✅'} S{stage} {stage_name} {status}",
            stage=stage,
            stage_name=stage_name,
            status=status,
        )

    def file_write(self, path: str, size: int = 0) -> None:
        name = Path(path).name
        self._emit(
            EventType.FILE_WRITE,
            summary=f"写入 {name}" + (f" ({size/1024:.1f}KB)" if size > 0 else ""),
            path=path,
        )

    def file_read(self, path: str, size: int = 0) -> None:
        name = Path(path).name
        self._emit(
            EventType.FILE_READ,
            summary=f"读取 {name}" + (f" ({size/1024:.1f}KB)" if size > 0 else ""),
            path=path,
        )

    def error(self, summary: str, detail: str = "") -> None:
        self._emit(EventType.ERROR, summary=summary, detail=detail)

    def permission_request(
        self, tool_name: str, args: dict[str, Any], request_id: str,
    ) -> None:
        self._emit(
            EventType.PERMISSION_REQUEST,
            summary=f"🔐 需要确认: {tool_name}",
            tool_name=tool_name,
            args=args,
            request_id=request_id,
        )

    def conversation_turn(
        self, turn_number: int, messages_count: int,
        tool_calls_count: int, elapsed_ms: int,
    ) -> None:
        self._emit(
            EventType.CONVERSATION_TURN,
            summary=f"Turn {turn_number} 完成: {tool_calls_count} tools, {elapsed_ms}ms",
            turn_number=turn_number,
            messages_count=messages_count,
            tool_calls_count=tool_calls_count,
            elapsed_ms=elapsed_ms,
        )


_global_buses: dict[str, EventBus] = {}
_buses_lock = threading.Lock()


def get_event_bus(project_id: str) -> EventBus:
    """Get or create an EventBus for a project."""
    with _buses_lock:
        if project_id not in _global_buses:
            _global_buses[project_id] = EventBus()
        return _global_buses[project_id]


def remove_event_bus(project_id: str) -> None:
    with _buses_lock:
        bus = _global_buses.pop(project_id, None)
        if bus:
            bus.close()
