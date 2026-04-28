"""TaskGraph / TaskNode state machine and persistence compatibility."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_BACKEND / "services") not in sys.path:
    sys.path.insert(0, str(_BACKEND / "services"))

from task_graph import TaskGraph, TaskNode


def _node(**kwargs):
    defaults = dict(
        id="a",
        layer="idea",
        title="t",
        description="d",
        stage_from=1,
        stage_to=4,
        dependencies=[],
    )
    defaults.update(kwargs)
    return TaskNode(**defaults)


def test_readiness_only_pending_to_ready_with_deps_done():
    g = TaskGraph("p1")
    dep = _node(id="dep", dependencies=[])
    child = _node(id="child", dependencies=["dep"])
    g.add_node(dep)
    g.add_node(child)
    assert dep.status == "ready"
    assert child.status == "pending"
    g.mark_done("dep")
    g._update_readiness()
    assert child.status == "ready"


def test_blocked_not_misclassified_as_ready():
    g = TaskGraph("p1")
    dep = _node(id="dep")
    blk = _node(id="blk", dependencies=["dep"])
    g.add_node(dep)
    g.add_node(blk)
    g.mark_done("dep")
    g._update_readiness()
    assert blk.status == "ready"
    assert g.mark_blocked("blk")
    assert blk.status == "blocked"
    g._update_readiness()
    assert blk.status == "blocked"


def test_paused_and_rolled_back_not_misclassified_as_ready():
    g = TaskGraph("p1")
    n = _node(id="n", dependencies=[])
    g.add_node(n)
    assert n.status == "ready"
    g.mark_running("n")
    assert g.mark_paused("n")
    assert n.status == "paused"
    g._update_readiness()
    assert n.status == "paused"
    g.mark_running("n")
    g.mark_done("n")
    assert g.mark_rolled_back("n")
    assert n.status == "rolled_back"
    g._update_readiness()
    assert n.status == "rolled_back"


def test_tasknode_can_methods():
    n = _node()
    n.status = "ready"
    assert n.can_run() and n.can_pause()
    n.status = "running"
    assert n.can_pause() and n.can_rollback() and not n.can_run()
    n.status = "paused"
    assert n.can_run() and n.can_skip()
    n.status = "failed"
    assert n.can_retry() and not n.can_rollback()
    n.status = "rolled_back"
    assert n.can_retry()
    n.status = "blocked"
    assert not n.can_run() and n.can_skip()
    n.status = "done"
    assert n.can_rollback() and not n.can_run()


def test_mark_helpers_return_false_when_invalid():
    g = TaskGraph("p1")
    n = _node(id="n")
    g.add_node(n)
    g.mark_running("n")
    assert not g.mark_blocked("n")
    assert g.mark_paused("n")
    assert not g.mark_paused("n")


def test_load_backward_compat_legacy_status_and_missing_status():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "task_graph.json"
        legacy = {
            "project_id": "proj",
            "nodes": {
                "x": {
                    "id": "x",
                    "layer": "idea",
                    "title": "",
                    "description": "",
                    "stage_from": 1,
                    "stage_to": 22,
                    "dependencies": [],
                    "status": "completed",
                    "run_dir": "",
                    "config_path": "",
                }
            },
        }
        p.write_text(json.dumps(legacy), encoding="utf-8")
        g = TaskGraph.load(p)
        assert g is not None
        assert g.nodes["x"].status == "done"


def test_load_unknown_status_defaults_to_pending():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "task_graph.json"
        p.write_text(
            json.dumps(
                {
                    "project_id": "p",
                    "nodes": {
                        "u": {
                            "id": "u",
                            "layer": "idea",
                            "title": "",
                            "description": "",
                            "stage_from": 1,
                            "stage_to": 22,
                            "dependencies": [],
                            "status": "not_a_real_status",
                            "run_dir": "",
                            "config_path": "",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        g = TaskGraph.load(p)
        assert g.nodes["u"].status == "pending"


def test_roundtrip_preserves_extended_statuses():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "tg.json"
        g = TaskGraph("pr")
        for sid, st in (
            ("b", "blocked"),
            ("pb", "paused"),
            ("rb", "rolled_back"),
        ):
            n = _node(id=sid, status=st)
            g.nodes[sid] = n
        g.save(p)
        g2 = TaskGraph.load(p)
        assert g2.nodes["b"].status == "blocked"
        assert g2.nodes["pb"].status == "paused"
        assert g2.nodes["rb"].status == "rolled_back"


def test_is_complete_ignores_non_terminal_extended_states():
    g = TaskGraph("p1")
    g.nodes["a"] = _node(id="a", status="done")
    assert g.is_complete()
    g.nodes["b"] = _node(id="b", status="rolled_back")
    assert not g.is_complete()


def test_reset_from_blocked_allows_readiness():
    g = TaskGraph("p1")
    dep = _node(id="dep")
    c = _node(id="c", dependencies=["dep"])
    g.add_node(dep)
    g.add_node(c)
    g.mark_done("dep")
    g._update_readiness()
    assert g.mark_blocked("c")
    g.reset_node("c")
    # reset_node() ends with _update_readiness(); satisfied deps ??ready
    assert c.status == "ready"


def test_reset_upstream_invalidates_ready_descendants():
    g = TaskGraph("p1")
    dep = _node(id="dep")
    child = _node(id="child", dependencies=["dep"])
    grandchild = _node(id="grandchild", dependencies=["child"])
    g.add_node(dep)
    g.add_node(child)
    g.add_node(grandchild)
    g.mark_done("dep")
    g.mark_done("child")
    assert grandchild.status == "ready"

    g.reset_node("dep")

    assert dep.status == "ready"
    assert child.status == "pending"
    assert grandchild.status == "pending"


def test_rollback_upstream_invalidates_done_descendants():
    g = TaskGraph("p1")
    dep = _node(id="dep")
    child = _node(id="child", dependencies=["dep"])
    g.add_node(dep)
    g.add_node(child)
    g.mark_done("dep")
    g.mark_done("child")

    rolled = g.rollback_node("dep")

    assert rolled is dep
    assert dep.status == "ready"
    assert child.status == "pending"


def test_dependent_ids_returns_transitive_descendants():
    g = TaskGraph("p1")
    g.nodes["a"] = _node(id="a")
    g.nodes["b"] = _node(id="b", dependencies=["a"])
    g.nodes["c"] = _node(id="c", dependencies=["b"])
    g.nodes["d"] = _node(id="d")

    assert g.dependent_ids("a") == {"b", "c"}
