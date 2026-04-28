"""Tests for TaskGraph node stage ranges, launch_agent_for_task integration, and node_action."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _load_agent_bridge_module():
    module_path = Path(__file__).resolve().parents[2] / "services" / "agent_bridge.py"
    spec = importlib.util.spec_from_file_location("agent_bridge_node_tests", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def agent_bridge():
    return _load_agent_bridge_module()


def test_intersect_stage_bounds(agent_bridge):
    assert agent_bridge._intersect_stage_bounds(1, 7, 3, 5) == (3, 5)
    assert agent_bridge._intersect_stage_bounds(1, 7, 1, 8) == (1, 7)
    assert agent_bridge._intersect_stage_bounds(8, 8, 1, 7) is None


def test_effective_launch_range_uses_node_when_not_discussion(agent_bridge):
    state = SimpleNamespace(discussion_mode=False)
    agent = SimpleNamespace(layer="coding", _is_idea_factory_s7_only=False, _is_discussion_s8=False)
    task = agent_bridge.Task(
        id="n1",
        project_id="p1",
        run_dir="/r",
        config_path="/c",
        source_layer="experiment",
        target_layer="coding",
        stage_from=11,
        stage_to=12,
    )
    lo, hi = agent_bridge._effective_stage_range_for_launch(
        state, agent, task, {"mode": "lab"}, is_discussion_s8=False,
    )
    assert (lo, hi) == (11, 12)


def test_effective_launch_range_discussion_idea_phase1_intersects_node(agent_bridge):
    state = SimpleNamespace(discussion_mode=True)
    agent = SimpleNamespace(layer="idea", _is_idea_factory_s7_only=False, _is_discussion_s8=False)
    task = agent_bridge.Task(
        id="n1",
        project_id="p1",
        run_dir="/r",
        config_path="/c",
        source_layer="init",
        target_layer="idea",
        stage_from=3,
        stage_to=8,
    )
    lo, hi = agent_bridge._effective_stage_range_for_launch(
        state, agent, task, {"mode": "lab"}, is_discussion_s8=False,
    )
    assert (lo, hi) == (3, 7)


def test_effective_launch_range_discussion_reproduce_uses_full_node(agent_bridge):
    state = SimpleNamespace(discussion_mode=True)
    agent = SimpleNamespace(layer="idea", _is_idea_factory_s7_only=False, _is_discussion_s8=False)
    task = agent_bridge.Task(
        id="n1",
        project_id="p1",
        run_dir="/r",
        config_path="/c",
        source_layer="init",
        target_layer="idea",
        stage_from=1,
        stage_to=8,
    )
    lo, hi = agent_bridge._effective_stage_range_for_launch(
        state, agent, task, {"mode": "reproduce"}, is_discussion_s8=False,
    )
    assert (lo, hi) == (1, 8)


def test_effective_launch_range_s8_intersects_narrow_node(agent_bridge):
    state = SimpleNamespace(discussion_mode=True)
    agent = SimpleNamespace(layer="idea", _is_idea_factory_s7_only=False, _is_discussion_s8=True)
    task = agent_bridge.Task(
        id="n1",
        project_id="p1",
        run_dir="/r",
        config_path="/c",
        source_layer="init",
        target_layer="idea",
        stage_from=8,
        stage_to=8,
    )
    lo, hi = agent_bridge._effective_stage_range_for_launch(
        state, agent, task, {"mode": "lab"}, is_discussion_s8=True,
    )
    assert (lo, hi) == (8, 8)


def test_effective_launch_range_s8_keeps_node_window_when_node_excludes_s8(agent_bridge):
    state = SimpleNamespace(discussion_mode=True)
    agent = SimpleNamespace(layer="idea", _is_idea_factory_s7_only=False, _is_discussion_s8=True)
    task = agent_bridge.Task(
        id="n1",
        project_id="p1",
        run_dir="/r",
        config_path="/c",
        source_layer="init",
        target_layer="idea",
        stage_from=1,
        stage_to=7,
    )
    lo, hi = agent_bridge._effective_stage_range_for_launch(
        state, agent, task, {"mode": "lab"}, is_discussion_s8=True,
    )
    assert (lo, hi) == (1, 7)


def test_discussion_required_only_when_node_spans_s7_to_s8(agent_bridge):
    legacy = SimpleNamespace()
    assert agent_bridge._agent_requires_discussion_before_s8(legacy)

    s1_s7 = SimpleNamespace(_task_stage_from=1, _task_stage_to=7)
    assert not agent_bridge._agent_requires_discussion_before_s8(s1_s7)

    s8_only = SimpleNamespace(_task_stage_from=8, _task_stage_to=8)
    assert not agent_bridge._agent_requires_discussion_before_s8(s8_only)

    s1_s8 = SimpleNamespace(_task_stage_from=1, _task_stage_to=8)
    assert agent_bridge._agent_requires_discussion_before_s8(s1_s8)


def test_node_action_skip_retry_pause_rollback(agent_bridge, tmp_path: Path):
    ab = agent_bridge
    TaskGraph, TaskNode = ab.TaskGraph, ab.TaskNode
    state = ab.BridgeState(
        runs_base_dir=str(tmp_path / "runs"),
        python_path="python",
        agent_package_dir=str(tmp_path),
        discussion_mode=False,
    )
    state.projects_dir().mkdir(parents=True, exist_ok=True)
    proj_dir = state.projects_dir() / "proj-x"
    proj_dir.mkdir(parents=True, exist_ok=True)
    run_dir = proj_dir / "run-main"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "checkpoint.json").write_text(
        json.dumps({"last_completed_stage": 10}),
        encoding="utf-8",
    )

    g = TaskGraph("proj-x")
    n = TaskNode(
        id="node-a",
        layer="idea",
        title="t",
        description="d",
        stage_from=3,
        stage_to=5,
        dependencies=[],
        run_dir=str(run_dir),
        config_path=str(tmp_path / "cfg.yaml"),
        status="ready",
    )
    g.add_node(n)
    state.task_graphs.graphs["proj-x"] = g

    out = asyncio.run(ab.handle_command(
        state,
        {"command": "node_action", "projectId": "proj-x", "nodeId": "node-a", "action": "skip"},
    ))
    assert any(m.get("type") == "task_graph_update" for m in out)
    assert g.nodes["node-a"].status == "skipped"

    g.nodes["node-a"].status = "failed"
    out2 = asyncio.run(ab.handle_command(
        state,
        {"command": "node_action", "projectId": "proj-x", "taskId": "node-a", "action": "retry"},
    ))
    assert g.nodes["node-a"].status in ("pending", "ready")

    g.nodes["node-a"].status = "ready"
    out3 = asyncio.run(ab.handle_command(
        state,
        {"command": "node_action", "projectId": "proj-x", "nodeId": "node-a", "action": "pause"},
    ))
    assert g.nodes["node-a"].status == "paused"

    out4 = asyncio.run(ab.handle_command(
        state,
        {"command": "node_action", "projectId": "proj-x", "nodeId": "node-a", "action": "resume"},
    ))
    assert g.nodes["node-a"].status in ("pending", "ready")

    g.nodes["node-a"].status = "done"
    g.nodes["node-a"].run_dir = str(run_dir)
    asyncio.run(ab.handle_command(
        state,
        {"command": "node_action", "projectId": "proj-x", "nodeId": "node-a", "action": "rollback"},
    ))
    assert g.nodes["node-a"].status in ("pending", "ready")
    cp = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert cp.get("last_completed_stage") == 2
    assert "bridge_rollback" in cp


def test_running_skip_stops_assigned_agent(agent_bridge, tmp_path: Path):
    ab = agent_bridge
    TaskGraph, TaskNode = ab.TaskGraph, ab.TaskNode
    state = ab.BridgeState(runs_base_dir=str(tmp_path / "runs"), python_path="py", agent_package_dir=str(tmp_path))
    state.projects_dir().mkdir(parents=True, exist_ok=True)
    (state.projects_dir() / "p1").mkdir(parents=True, exist_ok=True)
    g = TaskGraph("p1")
    g.add_node(TaskNode("x", "idea", "", "", 1, 8, [], status="ready", run_dir="", config_path=""))
    g.mark_running("x", "ag1")
    state.task_graphs.graphs["p1"] = g
    proc = MagicMock()
    proc.poll.return_value = None
    state.agents["ag1"] = ab.LobsterAgent(
        id="ag1", name="A", layer="idea", run_id="", run_dir="", config_path="",
        project_id="p1", assigned_task_id="x", process=proc,
    )

    asyncio.run(ab.handle_command(state, {"command": "node_action", "projectId": "p1", "nodeId": "x", "action": "skip"}))

    assert proc.terminate.called
    assert g.nodes["x"].status == "skipped"


def test_rollback_stops_running_dependents(agent_bridge, tmp_path: Path):
    ab = agent_bridge
    TaskGraph, TaskNode = ab.TaskGraph, ab.TaskNode
    state = ab.BridgeState(runs_base_dir=str(tmp_path / "runs"), python_path="py", agent_package_dir=str(tmp_path))
    state.projects_dir().mkdir(parents=True, exist_ok=True)
    proj_dir = state.projects_dir() / "p1"
    proj_dir.mkdir(parents=True, exist_ok=True)
    run_dir = proj_dir / "run-main"
    run_dir.mkdir()
    (run_dir / "checkpoint.json").write_text(json.dumps({"last_completed_stage": 8}), encoding="utf-8")
    g = TaskGraph("p1")
    g.nodes["up"] = TaskNode("up", "idea", "", "", 1, 8, [], status="done", run_dir=str(run_dir), config_path="")
    g.nodes["down"] = TaskNode("down", "experiment", "", "", 9, 9, ["up"], status="running", run_dir=str(run_dir), config_path="")
    state.task_graphs.graphs["p1"] = g
    proc = MagicMock()
    proc.poll.return_value = None
    state.agents["ag-down"] = ab.LobsterAgent(
        id="ag-down", name="D", layer="experiment", run_id="", run_dir=str(run_dir), config_path="",
        project_id="p1", assigned_task_id="down", process=proc,
    )

    asyncio.run(ab.handle_command(state, {"command": "node_action", "projectId": "p1", "nodeId": "up", "action": "rollback"}))

    assert proc.terminate.called
    assert g.nodes["up"].status in ("pending", "ready")
    assert g.nodes["down"].status == "pending"


def test_skip_task_backward_compatible(agent_bridge, tmp_path: Path):
    ab = agent_bridge
    TaskGraph, TaskNode = ab.TaskGraph, ab.TaskNode
    state = ab.BridgeState(runs_base_dir=str(tmp_path / "runs"), python_path="py", agent_package_dir=str(tmp_path))
    state.projects_dir().mkdir(parents=True, exist_ok=True)
    (state.projects_dir() / "p1").mkdir(parents=True, exist_ok=True)
    g = TaskGraph("p1")
    g.add_node(TaskNode("x", "idea", "", "", 1, 8, [], status="ready", run_dir="", config_path=""))
    state.task_graphs.graphs["p1"] = g
    out = asyncio.run(ab.handle_command(state, {"command": "skip_task", "projectId": "p1", "taskId": "x"}))
    assert g.nodes["x"].status == "skipped"
    assert any(m.get("type") == "task_graph_update" for m in out)


def test_launch_agent_passes_rc_stages(monkeypatch, agent_bridge, tmp_path: Path):
    ab = agent_bridge
    cfg = tmp_path / "c.yaml"
    cfg.write_text("x: 1\n", encoding="utf-8")
    rd = tmp_path / "run"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "project_meta.json").write_text(json.dumps({"mode": "lab"}), encoding="utf-8")

    state = ab.BridgeState(
        runs_base_dir=str(tmp_path / "runs"),
        python_path="python",
        agent_package_dir=str(tmp_path),
        discussion_mode=False,
    )
    agent = ab.LobsterAgent(
        id="ag1",
        name="A",
        layer="coding",
        run_id="",
        run_dir="",
        config_path="",
    )
    task = ab.Task(
        id="t1",
        project_id="p1",
        run_dir=str(rd),
        config_path=str(cfg),
        source_layer="experiment",
        target_layer="coding",
        topic="hi",
        stage_from=11,
        stage_to=12,
    )
    captured: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        captured.append(cmd)
        m = MagicMock()
        m.pid = 4242
        m.poll = MagicMock(return_value=None)
        return m

    monkeypatch.setattr(ab.subprocess, "Popen", fake_popen)
    msgs = ab.launch_agent_for_task(state, agent, task)
    assert captured, "Popen should be invoked"
    cmd = captured[0]
    idx_from = cmd.index("--from-stage")
    idx_to = cmd.index("--to-stage")
    assert cmd[idx_from + 1] == ab.STAGE_NAMES[11]
    assert cmd[idx_to + 1] == ab.STAGE_NAMES[12]
    assert any("S11→S12" in str(m) for m in msgs)


def test_metaprompt_node_id_rejects_path_traversal(agent_bridge):
    assert agent_bridge._safe_metaprompt_node_id("node-1")
    assert agent_bridge._safe_metaprompt_node_id("node.alpha_1")
    assert agent_bridge._safe_metaprompt_node_id("../secret") is None
    assert agent_bridge._safe_metaprompt_node_id("..") is None
    assert agent_bridge._safe_metaprompt_node_id("node/sub") is None


def test_agent_subprocess_env_includes_metaprompt_context(agent_bridge, tmp_path: Path):
    state = agent_bridge.BridgeState(runs_base_dir=str(tmp_path / "runs"), python_path="py", agent_package_dir=str(tmp_path))
    agent = agent_bridge.LobsterAgent(
        id="ag1", name="A", layer="idea", run_id="", run_dir=str(tmp_path / "projects" / "p1" / "run-main"),
        config_path="", project_id="p1", assigned_task_id="node-1",
    )

    env = agent_bridge._agent_subprocess_env(state, agent)

    assert env["SCHOLARCLAW_PROJECT_ID"] == "p1"
    assert env["SCHOLARCLAW_NODE_ID"] == "node-1"
    assert env["SCHOLARCLAW_METAPROMPT_PROJECT_DIR"].endswith("p1")
