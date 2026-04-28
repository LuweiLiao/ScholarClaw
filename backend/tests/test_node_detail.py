"""WebSocket get_node_detail: TaskGraph-backed stage range file aggregation and errors."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def test_build_node_detail_aggregates_stage_range_files(tmp_path):
    from services.agent_bridge import BridgeState, _build_node_detail_payload
    from task_graph import TaskGraph, TaskGraphRegistry, TaskNode

    base = tmp_path / "runs"
    proj = base / "projects" / "p1"
    proj.mkdir(parents=True)
    (proj / "stage-01").mkdir()
    (proj / "stage-01" / "goal.md").write_text("x", encoding="utf-8")
    (proj / "stage-02").mkdir()
    (proj / "stage-02" / "problem_tree.md").write_text("y", encoding="utf-8")

    graph = TaskGraph("p1")
    graph.add_node(
        TaskNode(
            id="n1",
            layer="idea",
            title="test",
            description="d",
            stage_from=1,
            stage_to=2,
            dependencies=[],
            status="done",
        )
    )
    reg = TaskGraphRegistry()
    reg.graphs["p1"] = graph

    state = BridgeState(runs_base_dir=str(base), task_graphs=reg)
    pl = _build_node_detail_payload(state, "p1", "n1")
    assert pl["ok"] is True
    assert len(pl["outputFiles"]) == 2
    assert len(pl["stages"]) == 2
    by_stage = {row["stage"]: row["name"] for row in pl["outputFiles"]}
    assert by_stage[1] == "goal.md"
    assert by_stage[2] == "problem_tree.md"
    # S2 declares goal.md as input; it exists from S1 on disk
    s2_hints = [h for h in pl["inputFiles"] if h["forStage"] == 2]
    assert any(h["path"] == "goal.md" and h["present"] for h in s2_hints)


def test_build_node_detail_no_task_graph(tmp_path):
    from services.agent_bridge import BridgeState, _build_node_detail_payload
    from task_graph import TaskGraphRegistry

    base = tmp_path / "runs"
    (base / "projects" / "p1").mkdir(parents=True)
    state = BridgeState(runs_base_dir=str(base), task_graphs=TaskGraphRegistry())
    pl = _build_node_detail_payload(state, "p1", "n1")
    assert pl["ok"] is False
    assert pl["error"] == "no_task_graph"


def test_build_node_detail_node_not_found(tmp_path):
    from services.agent_bridge import BridgeState, _build_node_detail_payload
    from task_graph import TaskGraph, TaskGraphRegistry

    base = tmp_path / "runs"
    (base / "projects" / "p1").mkdir(parents=True)
    graph = TaskGraph("p1")
    reg = TaskGraphRegistry()
    reg.graphs["p1"] = graph
    state = BridgeState(runs_base_dir=str(base), task_graphs=reg)
    pl = _build_node_detail_payload(state, "p1", "missing")
    assert pl["ok"] is False
    assert pl["error"] == "node_not_found"


def test_handle_command_get_node_detail_async(tmp_path):
    from services.agent_bridge import BridgeState, handle_command
    from task_graph import TaskGraph, TaskGraphRegistry, TaskNode

    base = tmp_path / "runs"
    proj = base / "projects" / "p1"
    proj.mkdir(parents=True)
    (proj / "stage-01").mkdir()
    (proj / "stage-01" / "goal.md").write_text("ok", encoding="utf-8")

    graph = TaskGraph("p1")
    graph.add_node(
        TaskNode(
            id="n1",
            layer="idea",
            title="t",
            description="d",
            stage_from=1,
            stage_to=1,
            dependencies=[],
            status="running",
        )
    )
    reg = TaskGraphRegistry()
    reg.graphs["p1"] = graph
    state = BridgeState(runs_base_dir=str(base), task_graphs=reg)

    async def run():
        return await handle_command(
            state,
            {"command": "get_node_detail", "projectId": "p1", "nodeId": "n1"},
        )

    msgs = asyncio.run(run())
    assert len(msgs) == 1
    assert msgs[0]["type"] == "node_detail"
    assert msgs[0]["payload"]["ok"] is True
    assert msgs[0]["payload"]["node"]["id"] == "n1"


def test_handle_command_get_node_detail_bad_request():
    from services.agent_bridge import BridgeState, handle_command
    from task_graph import TaskGraphRegistry

    state = BridgeState(runs_base_dir="/tmp/x", task_graphs=TaskGraphRegistry())

    async def run():
        return await handle_command(
            state,
            {"command": "get_node_detail", "projectId": "", "nodeId": "x"},
        )

    msgs = asyncio.run(run())
    assert msgs[0]["payload"]["ok"] is False
    assert msgs[0]["payload"]["error"] == "bad_request"
