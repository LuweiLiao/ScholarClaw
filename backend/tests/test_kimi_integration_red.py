"""RED contract tests for Kimi enhanced wiring (expected to fail until sibling subtasks land).

See docs/kimi_enhanced_acceptance_checklist.md for merge order and manual UI checks.

Markers:
  - ``not yet implemented`` assertions document the expected public surface area.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_SERVICES = _BACKEND / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from kimi_contract_helpers import expected_node_detail_stage_fields  # noqa: E402


def test_red_bridge_task_carries_per_node_stage_range():
    """Executor/agent_bridge must receive explicit bounds for TaskGraph nodes.

    **Expected:** ``Task`` gains ``stage_from`` / ``stage_to`` (or equivalent) populated from ``TaskNode``
    so ``launch_agent_for_task`` can build ``--from-stage`` / ``--to-stage`` from the node, not only ``LAYER_RANGE``.
    """
    spec = importlib.util.find_spec("agent_bridge")
    if spec is None or not spec.origin:
        pytest.fail("agent_bridge must be importable from backend/services (sys.path)")
    bridge = importlib.import_module("agent_bridge")
    Task = getattr(bridge, "Task")
    fields = {f.name for f in dataclasses.fields(Task)}
    missing = {"stage_from", "stage_to"} - fields
    assert not missing, (
        "RED: extend Task with stage_from/stage_to (ints) for graph-driven runs; "
        f"missing={missing!r}"
    )


def test_red_node_detail_exports_stage_range_summary():
    """Node detail panel should expose a structured summary for ``stage_from``–``stage_to``.

    **Expected API (illustrative):** a callable such as
    ``build_node_stage_detail(node_dict | TaskNode, graph: TaskGraph) -> dict`` returning at least
    keys from ``expected_node_detail_stage_fields()``.
    """
    detail_py = _SERVICES / "node_detail.py"
    if not detail_py.is_file():
        pytest.fail(
            "RED: add backend/services/node_detail.py (or agreed module) that aggregates "
            f"stage range for the detail view; expected keys include {sorted(expected_node_detail_stage_fields())}",
        )
    spec = importlib.util.spec_from_file_location("node_detail", detail_py)
    if spec is None or spec.loader is None:
        pytest.fail("could not load node_detail module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "build_node_stage_detail", None) or getattr(mod, "summarize_node_stages", None)
    assert callable(fn), (
        "RED: node_detail.py should expose build_node_stage_detail or summarize_node_stages callable"
    )
