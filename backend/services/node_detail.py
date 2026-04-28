"""Node detail helpers for TaskGraph-backed UI panels.

The WebSocket bridge owns filesystem access and project lookup, while this
module exposes small pure helpers that are easy to test and reuse.
"""

from __future__ import annotations

from typing import Any


def build_node_stage_detail(node: Any) -> dict[str, Any]:
    """Return the stage-range summary expected by the node detail UI."""
    if isinstance(node, dict):
        stage_from = int(node.get("stage_from", node.get("stageFrom", 1)) or 1)
        stage_to = int(node.get("stage_to", node.get("stageTo", stage_from)) or stage_from)
    else:
        stage_from = int(getattr(node, "stage_from", 1) or 1)
        stage_to = int(getattr(node, "stage_to", stage_from) or stage_from)

    if stage_from > stage_to:
        stage_from, stage_to = stage_to, stage_from

    stage_labels = [f"S{stage}" for stage in range(stage_from, stage_to + 1)]
    return {
        "stage_from": stage_from,
        "stage_to": stage_to,
        "stage_count": len(stage_labels),
        "stage_labels": stage_labels,
    }


summarize_node_stages = build_node_stage_detail
