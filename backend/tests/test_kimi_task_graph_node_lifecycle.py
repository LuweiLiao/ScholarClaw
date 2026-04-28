"""TaskGraph node lifecycle — validates DAG scheduling state machine (GREEN vs TaskGraph implementation).

Production owners: TaskGraph / planner subtasks may extend fields; these tests only fix current semantics.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND / "services") not in sys.path:
    sys.path.insert(0, str(_BACKEND / "services"))

from task_graph import TaskGraph, TaskNode  # noqa: E402


def _node(
    nid: str,
    *,
    layer: str = "idea",
    deps: list[str] | None = None,
    sf: int = 1,
    st: int = 8,
) -> TaskNode:
    return TaskNode(
        id=nid,
        layer=layer,
        title=nid,
        description="",
        stage_from=sf,
        stage_to=st,
        dependencies=deps or [],
    )


def test_pending_without_dependencies_becomes_ready():
    g = TaskGraph("p1")
    g.add_node(_node("a"))
    assert g.nodes["a"].status == "ready"


def test_pending_waits_on_dependencies():
    g = TaskGraph("p1")
    g.add_node(_node("a"))
    g.add_node(_node("b", deps=["a"]))
    assert g.nodes["b"].status == "pending"


def test_ready_promoted_when_dependency_done():
    g = TaskGraph("p1")
    g.add_node(_node("a"))
    g.add_node(_node("b", deps=["a"]))
    g.mark_done("a")
    assert g.nodes["b"].status == "ready"


def test_running_done_failed_skipped_reset_flow():
    g = TaskGraph("p1")
    g.add_node(_node("a"))
    assert g.nodes["a"].status == "ready"
    g.mark_running("a", "agent-1")
    assert g.nodes["a"].status == "running"
    assert g.nodes["a"].assigned_agent == "agent-1"

    g.mark_done("a")
    assert g.nodes["a"].status == "done"
    assert g.nodes["a"].assigned_agent is None

    g.add_node(_node("c"))
    g.mark_running("c", "x")
    g.mark_failed("c")
    assert g.nodes["c"].status == "failed"

    g.add_node(_node("d"))
    g.mark_running("d", "y")
    g.mark_skipped("d")
    assert g.nodes["d"].status == "skipped"

    g.add_node(_node("e"))
    g.mark_running("e", "z")
    g.mark_failed("e")
    g.reset_node("e")
    assert g.nodes["e"].status == "ready"


def test_skipped_satisfies_dependencies():
    g = TaskGraph("p1")
    g.add_node(_node("a"))
    g.add_node(_node("b", deps=["a"]))
    g.mark_skipped("a")
    assert g.nodes["b"].status == "ready"


def test_graph_complete_when_all_terminal():
    g = TaskGraph("p1")
    g.add_node(_node("a"))
    g.add_node(_node("b", deps=["a"]))
    assert not g.is_complete()
    g.mark_done("a")
    g.mark_done("b")
    assert g.is_complete()


def test_stage_range_round_trips_json(tmp_path: Path):
    g = TaskGraph("proj-x")
    g.add_node(_node("n1", sf=3, st=7))
    path = tmp_path / "tg.json"
    g.save(path)
    loaded = TaskGraph.load(path)
    assert loaded is not None
    assert loaded.nodes["n1"].stage_from == 3
    assert loaded.nodes["n1"].stage_to == 7
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["nodes"]["n1"]["stage_from"] == 3
    assert raw["nodes"]["n1"]["stage_to"] == 7
