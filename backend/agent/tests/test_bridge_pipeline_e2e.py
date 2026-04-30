"""End-to-end pipeline test for Phase 3 WebSocket-driven architecture.

Verifies the full message flow from agent file writes → watchdog detection →
WebSocket broadcast, including adaptive poll-loop intervals and deduplication.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.anyio(backend="asyncio")

@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


def _load_agent_bridge():
    module_path = Path(__file__).resolve().parents[2] / "services" / "agent_bridge.py"
    spec = importlib.util.spec_from_file_location("agent_bridge_pipeline_e2e", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_bridge = _load_agent_bridge()
_AgentFileHandler = _bridge._AgentFileHandler
LobsterAgent = _bridge.LobsterAgent
BridgeState = _bridge.BridgeState
poll_agent = _bridge.poll_agent
broadcast = _bridge.broadcast
msg_agent_update = _bridge.msg_agent_update
msg_stage_update = _bridge.msg_stage_update
msg_activity = _bridge.msg_activity


def _make_agent(tmp_path: Path, **overrides: Any) -> LobsterAgent:
    defaults = dict(
        id="test-agent-1",
        name="TestAgent",
        layer="idea",
        run_id="run-001",
        run_dir=str(tmp_path),
        config_path="/tmp/test_config.yaml",
        project_id="proj-test-001",
    )
    defaults.update(overrides)
    return LobsterAgent(**defaults)


def _attach_running_process(agent: LobsterAgent):
    """Attach a mock running process so poll_agent enters the file-read block."""
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None
    agent.process = mock_proc


def _write_session(tmp_path: Path, stage_num: int, status: str = "running") -> Path:
    stage_dir = tmp_path / f"stage-{stage_num:02d}"
    stage_dir.mkdir(exist_ok=True)
    session_file = stage_dir / f"stage_{stage_num}_session.json"
    session_data = {
        "stage_name": f"stage_{stage_num}",
        "status": status,
        "elapsed_sec": 12.3,
        "llm_calls": 5,
        "sandbox_runs": 2,
        "phase_log": [],
        "artifacts": [],
        "errors": [],
        "metadata": {},
    }
    session_file.write_text(json.dumps(session_data), encoding="utf-8")
    return session_file


class FakeWebSocket:
    """Records messages sent via broadcast()."""

    def __init__(self):
        self.messages: list[dict] = []
        self.closed = False

    async def send(self, data: str):
        if self.closed:
            raise Exception("ConnectionClosed")
        self.messages.append(json.loads(data))


class TestPipelineMessageFlow:
    """Full pipeline: file writes → watchdog → poll_agent → broadcast."""

    async def test_pipeline_watchdog_messages_broadcast(self, tmp_path: Path):
        """Watchdog-generated messages are picked up by poll_agent and broadcast."""
        agent = _make_agent(tmp_path)
        _attach_running_process(agent)
        handler = _AgentFileHandler(agent)
        state = BridgeState()
        state.agents[agent.id] = agent

        ws = FakeWebSocket()
        state.clients.add(ws)

        # Simulate heartbeat write
        hb = {"last_stage": 5, "status": "working", "run_id": "r1"}
        (tmp_path / "heartbeat.json").write_text(json.dumps(hb), encoding="utf-8")
        handler.on_modified(
            type("E", (), {"src_path": str(tmp_path / "heartbeat.json"), "is_directory": False})()
        )

        # poll_agent drains watchdog messages
        msgs = poll_agent(agent, state)
        await broadcast(state, msgs)

        types = [m["type"] for m in ws.messages]
        assert "agent_update" in types
        assert "stage_update" in types

    async def test_pipeline_session_dedup_across_watchdog_and_poll(self, tmp_path: Path):
        """Watchdog processes a session file; poll_agent should skip it via checksum."""
        agent = _make_agent(tmp_path)
        _attach_running_process(agent)
        handler = _AgentFileHandler(agent)
        state = BridgeState()
        state.agents[agent.id] = agent

        ws = FakeWebSocket()
        state.clients.add(ws)

        # Write session file
        session_file = _write_session(tmp_path, 3, "running")
        handler.on_modified(
            type("E", (), {"src_path": str(session_file), "is_directory": False})()
        )

        # First poll_agent call: watchdog messages + session update
        msgs = poll_agent(agent, state)
        await broadcast(state, msgs)

        session_updates = [m for m in ws.messages if m["type"] == "stage_session_update"]
        assert len(session_updates) == 1

        # Second poll_agent call: should NOT emit duplicate session update
        ws.messages.clear()
        msgs = poll_agent(agent, state)
        await broadcast(state, msgs)

        session_updates = [m for m in ws.messages if m["type"] == "stage_session_update"]
        assert len(session_updates) == 0

    async def test_pipeline_activity_jsonl_tailing(self, tmp_path: Path):
        """Activity.jsonl lines are tailed and emitted as agent_activity messages."""
        agent = _make_agent(tmp_path)
        _attach_running_process(agent)
        handler = _AgentFileHandler(agent)
        state = BridgeState()
        state.agents[agent.id] = agent

        ws = FakeWebSocket()
        state.clients.add(ws)

        activity_file = tmp_path / "activity.jsonl"
        activity_file.write_text(
            json.dumps({"type": "tool_call", "detail": "arxiv search"}) + "\n" +
            json.dumps({"type": "tool_result", "detail": "found 5 papers"}) + "\n",
            encoding="utf-8",
        )
        handler.on_modified(
            type("E", (), {"src_path": str(activity_file), "is_directory": False})()
        )

        msgs = poll_agent(agent, state)
        await broadcast(state, msgs)

        activity_msgs = [m for m in ws.messages if m["type"] == "agent_activity"]
        details = [m["payload"]["summary"] for m in activity_msgs]
        assert "arxiv search" in details
        assert "found 5 papers" in details


class TestPollLoopAdaptiveInterval:
    """Verify adaptive interval logic by reading poll_loop source."""

    def test_adaptive_interval_source_logic(self):
        """The poll_loop source contains adaptive interval backoff logic."""
        import inspect
        src = inspect.getsource(_bridge.poll_loop)
        assert "_adaptive_interval = interval" in src
        assert "_idle_ticks += 1" in src
        assert "_idle_ticks >= 3" in src
        assert "interval * 2" in src
        assert "300.0" in src
        assert "watchdog_active" in src
        assert "_adaptive_interval = interval" in src  # reset path


class TestPipelineCheckpointAdvancement:
    """Checkpoint files advance stage progress correctly through the pipeline."""

    async def test_checkpoint_advances_stage_and_emits_updates(self, tmp_path: Path):
        """Writing checkpoint.json advances current_stage and emits stage_update."""
        agent = _make_agent(tmp_path, current_stage=2)
        _attach_running_process(agent)
        handler = _AgentFileHandler(agent)
        state = BridgeState()
        state.agents[agent.id] = agent
        ws = FakeWebSocket()
        state.clients.add(ws)

        cp = {"last_completed_stage": 2, "run_id": "r1"}
        (tmp_path / "checkpoint.json").write_text(json.dumps(cp), encoding="utf-8")
        handler.on_modified(
            type("E", (), {"src_path": str(tmp_path / "checkpoint.json"), "is_directory": False})()
        )

        msgs = poll_agent(agent, state)
        await broadcast(state, msgs)

        assert agent.current_stage == 3
        assert agent.stage_progress.get(3) == "running"

        stage_updates = [m for m in ws.messages if m["type"] == "stage_update"]
        assert any(u["payload"]["stage"] == 3 and u["payload"]["status"] == "running" for u in stage_updates)
