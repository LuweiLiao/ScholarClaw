"""E2E test for Phase 3 watchdog-driven real-time push.

Verifies that file changes under an agent's run_dir are detected by the
watchdog handler and converted to WebSocket messages without polling.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_agent_bridge():
    module_path = Path(__file__).resolve().parents[2] / "services" / "agent_bridge.py"
    spec = importlib.util.spec_from_file_location("agent_bridge_e2e", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_bridge = _load_agent_bridge()
_AgentFileHandler = _bridge._AgentFileHandler
LobsterAgent = _bridge.LobsterAgent
msg_agent_update = _bridge.msg_agent_update
msg_stage_update = _bridge.msg_stage_update
msg_log = _bridge.msg_log
msg_activity = _bridge.msg_activity


def _make_agent() -> LobsterAgent:
    """Create a minimal LobsterAgent for testing."""
    return LobsterAgent(
        id="test-agent-1",
        name="TestAgent",
        layer="idea",
        run_id="run-001",
        run_dir="/tmp/test_run_dir",
        config_path="/tmp/test_config.yaml",
        project_id="proj-test-001",
    )


class TestWatchdogHeartbeat:
    def test_watchdog_heartbeat_triggers_stage_change(self, tmp_path: Path) -> None:
        agent = _make_agent()
        agent.run_dir = str(tmp_path)
        handler = _AgentFileHandler(agent)

        # Simulate agent writing heartbeat.json
        hb = {"last_stage": 3, "status": "working", "run_id": "r1"}
        (tmp_path / "heartbeat.json").write_text(json.dumps(hb), encoding="utf-8")

        handler.on_modified(
            type("E", (), {"src_path": str(tmp_path / "heartbeat.json"), "is_directory": False})()
        )

        assert agent.current_stage == 3
        assert agent.status == "working"
        assert any(m["type"] == "agent_update" for m in agent._watchdog_messages)
        assert any(m["type"] == "stage_update" for m in agent._watchdog_messages)
        assert agent._prev_heartbeat == hb

    def test_watchdog_heartbeat_ignores_duplicate(self, tmp_path: Path) -> None:
        agent = _make_agent()
        agent.run_dir = str(tmp_path)
        handler = _AgentFileHandler(agent)

        hb = {"last_stage": 3, "status": "working", "run_id": "r1"}
        (tmp_path / "heartbeat.json").write_text(json.dumps(hb), encoding="utf-8")

        handler.on_modified(
            type("E", (), {"src_path": str(tmp_path / "heartbeat.json"), "is_directory": False})()
        )
        prev_len = len(agent._watchdog_messages)

        # Second identical write
        handler.on_modified(
            type("E", (), {"src_path": str(tmp_path / "heartbeat.json"), "is_directory": False})()
        )

        assert len(agent._watchdog_messages) == prev_len  # no new messages


class TestWatchdogCheckpoint:
    def test_watchdog_checkpoint_completes_stage(self, tmp_path: Path) -> None:
        agent = _make_agent()
        agent.run_dir = str(tmp_path)
        agent.current_stage = 2
        handler = _AgentFileHandler(agent)

        cp = {"last_completed_stage": 2, "run_id": "r1"}
        (tmp_path / "checkpoint.json").write_text(json.dumps(cp), encoding="utf-8")

        handler.on_modified(
            type("E", (), {"src_path": str(tmp_path / "checkpoint.json"), "is_directory": False})()
        )

        assert agent.stage_progress[2] == "completed"
        # Activity message uses payload.summary for agent_activity
        assert any(
            "S2" in (m.get("payload", {}).get("summary", ""))
            for m in agent._watchdog_messages
            if m["type"] == "agent_activity"
        )
        assert agent._prev_checkpoint == cp

    def test_watchdog_checkpoint_advances_to_next_stage(self, tmp_path: Path) -> None:
        agent = _make_agent()
        agent.run_dir = str(tmp_path)
        agent.current_stage = 2
        handler = _AgentFileHandler(agent)

        cp = {"last_completed_stage": 2, "run_id": "r1"}
        (tmp_path / "checkpoint.json").write_text(json.dumps(cp), encoding="utf-8")

        handler.on_modified(
            type("E", (), {"src_path": str(tmp_path / "checkpoint.json"), "is_directory": False})()
        )

        assert agent.current_stage == 3
        assert agent.stage_progress[3] == "running"


class TestWatchdogActivityJsonl:
    def test_watchdog_tails_activity_jsonl(self, tmp_path: Path) -> None:
        agent = _make_agent()
        agent.run_dir = str(tmp_path)
        handler = _AgentFileHandler(agent)

        activity_file = tmp_path / "activity.jsonl"
        activity_file.write_text(
            json.dumps({"type": "tool_call", "detail": "searching arxiv"}) + "\n",
            encoding="utf-8",
        )

        handler.on_modified(
            type("E", (), {"src_path": str(activity_file), "is_directory": False})()
        )

        assert any(
            m.get("payload", {}).get("detail") == "searching arxiv"
            for m in agent._watchdog_messages
            if m["type"] == "agent_activity"
        )
        assert agent._activity_offset > 0

    def test_watchdog_skips_already_seen_activity_lines(self, tmp_path: Path) -> None:
        agent = _make_agent()
        agent.run_dir = str(tmp_path)
        handler = _AgentFileHandler(agent)

        activity_file = tmp_path / "activity.jsonl"
        activity_file.write_text(
            json.dumps({"type": "thinking", "detail": "step 1"}) + "\n",
            encoding="utf-8",
        )

        handler.on_modified(
            type("E", (), {"src_path": str(activity_file), "is_directory": False})()
        )
        prev_len = len(agent._watchdog_messages)

        # Re-modify with no new content
        handler.on_modified(
            type("E", (), {"src_path": str(activity_file), "is_directory": False})()
        )

        assert len(agent._watchdog_messages) == prev_len


class TestWatchdogSessionJson:
    def test_watchdog_session_update_checksum_tracking(self, tmp_path: Path) -> None:
        agent = _make_agent()
        agent.run_dir = str(tmp_path)
        handler = _AgentFileHandler(agent)

        stage_dir = tmp_path / "stage-05"
        stage_dir.mkdir()
        session_file = stage_dir / "iterative_refine_session.json"
        session_data = {
            "stage_name": "iterative_refine",
            "status": "running",
            "elapsed_sec": 12.3,
            "llm_calls": 5,
            "sandbox_runs": 2,
            "phase_log": [],
            "artifacts": ["experiment_final.py"],
            "errors": [],
            "metadata": {},
        }
        session_file.write_text(json.dumps(session_data), encoding="utf-8")

        handler.on_modified(
            type("E", (), {"src_path": str(session_file), "is_directory": False})()
        )

        assert any(m["type"] == "stage_session_update" for m in agent._watchdog_messages)
        # Checksum should be recorded so _read_session_updates skips it
        key = "stage-05/iterative_refine_session.json"
        assert key in agent._prev_session_checksums
