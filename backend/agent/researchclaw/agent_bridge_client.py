"""Lightweight client for pushing pipeline status to agent_bridge WebSocket.

Used by CLI runs to broadcast live progress to the frontend.
If agent_bridge is not running, all calls silently no-op.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


def _has_websocket() -> bool:
    try:
        import websocket  # noqa: F401
        return True
    except Exception:
        return False


class AgentBridgeClient:
    """Push pipeline stage/activity updates to a local agent_bridge WebSocket."""

    def __init__(
        self,
        run_id: str,
        topic: str,
        url: str = "ws://localhost:8906/ws/agents",
    ):
        self.run_id = run_id
        self.topic = topic
        self.url = url
        self._ws: Any | None = None
        self._connected = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """Try to connect to agent_bridge. Return False if not available."""
        if not _has_websocket():
            return False

        # Quick TCP check — avoid hanging if bridge not running
        try:
            sock = socket.create_connection(("localhost", 8906), timeout=2)
            sock.close()
        except (socket.timeout, OSError, ConnectionRefusedError):
            logger.debug("agent_bridge not listening on port 8906")
            return False

        try:
            import websocket as ws_lib

            self._ws = ws_lib.create_connection(self.url, timeout=5)
            self._connected = True
            self._ws.send(
                json.dumps(
                    {
                        "command": "register_run",
                        "run_id": self.run_id,
                        "topic": self.topic,
                    }
                )
            )
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()
            logger.info(
                "Connected to agent_bridge for live frontend updates (run=%s)",
                self.run_id,
            )
            return True
        except Exception as exc:
            logger.debug("Failed to connect to agent_bridge: %s", exc)
            self._connected = False
            return False

    def push_stage(
        self,
        stage_num: int,
        stage_name: str,
        status: str,
        duration_sec: float = 0.0,
    ) -> None:
        """Push stage completion status."""
        if not self._connected or self._ws is None:
            return
        try:
            with self._lock:
                self._ws.send(
                    json.dumps(
                        {
                            "command": "run_status",
                            "run_id": self.run_id,
                            "stage": stage_num,
                            "stage_name": stage_name,
                            "status": status,
                            "duration_sec": duration_sec,
                        }
                    )
                )
        except Exception:
            self._connected = False

    def push_activity(
        self,
        activity_type: str,
        summary: str,
        detail: str = "",
        layer: str = "",
    ) -> None:
        """Push thinking / tool_call / tool_result activity."""
        if not self._connected or self._ws is None:
            return
        try:
            with self._lock:
                self._ws.send(
                    json.dumps(
                        {
                            "command": "run_activity",
                            "run_id": self.run_id,
                            "type": activity_type,
                            "summary": summary,
                            "detail": detail,
                            "layer": layer,
                            "timestamp": time.time(),
                        }
                    )
                )
        except Exception:
            self._connected = False

    def _heartbeat(self) -> None:
        while self._connected and self._ws is not None:
            try:
                with self._lock:
                    self._ws.send(json.dumps({"command": "ping"}))
                time.sleep(15)
            except Exception:
                self._connected = False
                break

    def close(self) -> None:
        with self._lock:
            self._connected = False
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
