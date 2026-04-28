"""
TaskGraph — DAG-based task scheduling for ScholarLab v2.0.

Replaces the fixed 22-stage linear pipeline with a dynamic dependency graph.
Each TaskNode maps to a researchclaw stage range via --from-stage/--to-stage.

Node status (compatible with legacy task_graph.json; `done` kept for frontend):
pending | ready | running | paused | done | failed | skipped | rolled_back | blocked
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

LAYERS = ["idea", "experiment", "coding", "execution", "writing"]

ALLOWED_NODE_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "ready",
        "running",
        "paused",
        "done",
        "failed",
        "skipped",
        "rolled_back",
        "blocked",
    }
)

# Load-time only: external / legacy labels ??canonical
_STATUS_ALIASES: dict[str, str] = {
    "completed": "done",
}


def _normalize_status(raw: str | None) -> str:
    if not raw:
        return "pending"
    s = str(raw).strip().lower()
    if s in _STATUS_ALIASES:
        return _STATUS_ALIASES[s]
    if s in ALLOWED_NODE_STATUSES:
        return s
    return "pending"


@dataclass
class TaskNode:
    id: str
    layer: str
    title: str
    description: str
    stage_from: int
    stage_to: int
    dependencies: list[str] = field(default_factory=list)
    assigned_agent: str | None = None
    status: str = "pending"
    config_overrides: dict = field(default_factory=dict)
    run_dir: str = ""
    config_path: str = ""

    def can_run(self) -> bool:
        return self.status in ("ready", "paused")

    def can_pause(self) -> bool:
        return self.status in ("pending", "ready", "running")

    def can_retry(self) -> bool:
        return self.status in ("failed", "rolled_back")

    def can_skip(self) -> bool:
        return self.status in ("pending", "ready", "blocked", "paused")

    def can_rollback(self) -> bool:
        return self.status in ("done", "running")

    def mark_paused(self) -> bool:
        if not self.can_pause():
            return False
        self.status = "paused"
        return True

    def mark_blocked(self) -> bool:
        if self.status not in ("pending", "ready"):
            return False
        self.status = "blocked"
        return True

    def mark_rolled_back(self) -> bool:
        if not self.can_rollback():
            return False
        self.status = "rolled_back"
        self.assigned_agent = None
        self.run_dir = ""
        return True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "layer": self.layer,
            "title": self.title,
            "description": self.description,
            "stage_from": self.stage_from,
            "stage_to": self.stage_to,
            "dependencies": self.dependencies,
            "assigned_agent": self.assigned_agent,
            "status": self.status,
            "run_dir": self.run_dir.replace("\\", "/") if self.run_dir else "",
            "config_path": self.config_path.replace("\\", "/") if self.config_path else "",
        }


class TaskGraph:
    """A directed acyclic graph of tasks for a single project."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.nodes: dict[str, TaskNode] = {}

    def add_node(self, node: TaskNode) -> None:
        self.nodes[node.id] = node
        self._update_readiness()

    def get_ready_tasks(self, layer: str | None = None) -> list[TaskNode]:
        """Return tasks whose dependencies are all 'done' and status is 'ready'."""
        result = []
        for node in self.nodes.values():
            if node.status != "ready":
                continue
            if layer and node.layer != layer:
                continue
            result.append(node)
        return result

    def get_running_tasks(self, layer: str | None = None) -> list[TaskNode]:
        result = []
        for node in self.nodes.values():
            if node.status != "running":
                continue
            if layer and node.layer != layer:
                continue
            result.append(node)
        return result

    def mark_running(self, node_id: str, agent_id: str | None = None) -> None:
        node = self.nodes.get(node_id)
        if node:
            node.status = "running"
            node.assigned_agent = agent_id
            self._update_readiness()

    def mark_done(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node:
            node.status = "done"
            node.assigned_agent = None
            self._update_readiness()

    def mark_failed(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node:
            node.status = "failed"
            node.assigned_agent = None
            self._invalidate_dependents(node_id)

    def mark_skipped(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node:
            node.status = "skipped"
            node.assigned_agent = None
            self._update_readiness()

    def mark_paused(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        if not node or not node.mark_paused():
            return False
        self._update_readiness()
        return True

    def mark_blocked(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        if not node or not node.mark_blocked():
            return False
        self._update_readiness()
        return True

    def mark_rolled_back(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        if not node or not node.mark_rolled_back():
            return False
        self._invalidate_dependents(node_id)
        self._update_readiness()
        return True

    def resume_node(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        if not node or node.status != "paused":
            return False
        node.status = "pending"
        node.assigned_agent = None
        self._update_readiness()
        return True

    def rollback_node(self, node_id: str) -> TaskNode | None:
        """Set node back to pending without deleting research artifacts."""
        node = self.nodes.get(node_id)
        if not node:
            return None
        node.status = "pending"
        node.assigned_agent = None
        self._invalidate_dependents(node_id)
        self._update_readiness()
        return node

    def reset_node(self, node_id: str) -> None:
        """Reset a node to pending for retry. Re-evaluates readiness."""
        node = self.nodes.get(node_id)
        if node:
            node.status = "pending"
            node.assigned_agent = None
            node.run_dir = ""
            self._invalidate_dependents(node_id)
            self._update_readiness()

    def is_complete(self) -> bool:
        return all(n.status in ("done", "failed", "skipped") for n in self.nodes.values())

    def get_layer_tasks(self, layer: str) -> list[TaskNode]:
        return [n for n in self.nodes.values() if n.layer == layer]

    def dependent_ids(self, node_id: str) -> set[str]:
        """Return all transitive dependents of a node."""
        result: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            for child in self.nodes.values():
                if child.id in result or current not in child.dependencies:
                    continue
                result.add(child.id)
                stack.append(child.id)
        return result

    def _update_readiness(self) -> None:
        """Promote 'pending' tasks to 'ready' if all dependencies are done or skipped."""
        for node in self.nodes.values():
            if node.status != "pending":
                continue
            if not node.dependencies:
                node.status = "ready"
                continue
            all_resolved = all(
                self.nodes.get(dep_id) and self.nodes[dep_id].status in ("done", "skipped")
                for dep_id in node.dependencies
            )
            if all_resolved:
                node.status = "ready"

    def _invalidate_dependents(self, node_id: str) -> None:
        """Mark descendants pending when an upstream node is no longer resolved."""
        for child_id in self.dependent_ids(node_id):
            child = self.nodes[child_id]
            child.status = "pending"
            child.assigned_agent = None

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
        }

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> TaskGraph | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

        graph = cls(data.get("project_id", ""))
        for nid, nd in data.get("nodes", {}).items():
            raw_status = nd.get("status", "pending")
            graph.nodes[nid] = TaskNode(
                id=nd["id"],
                layer=nd.get("layer", "idea"),
                title=nd.get("title", ""),
                description=nd.get("description", ""),
                stage_from=nd.get("stage_from", 1),
                stage_to=nd.get("stage_to", 22),
                dependencies=nd.get("dependencies", []),
                assigned_agent=nd.get("assigned_agent"),
                status=_normalize_status(raw_status),
                run_dir=str(Path(nd["run_dir"])) if nd.get("run_dir") else "",
                config_path=str(Path(nd["config_path"])) if nd.get("config_path") else "",
            )
        return graph


class TaskGraphRegistry:
    """Manages TaskGraphs across projects. Coexists with the old TaskQueue system."""

    def __init__(self) -> None:
        self.graphs: dict[str, TaskGraph] = {}

    def get(self, project_id: str) -> TaskGraph | None:
        return self.graphs.get(project_id)

    def has_graph(self, project_id: str) -> bool:
        return project_id in self.graphs

    def create_from_plan(
        self,
        project_id: str,
        plan_dict: dict,
        run_dir: str = "",
        config_path: str = "",
    ) -> TaskGraph:
        """Create a TaskGraph from a ProjectPlan dict (as stored in project_plan.json)."""
        graph = TaskGraph(project_id)

        for ts in plan_dict.get("task_specs", []):
            node = TaskNode(
                id=ts.get("id", f"task-{uuid.uuid4().hex[:8]}"),
                layer=ts.get("layer", "idea"),
                title=ts.get("title", ""),
                description=ts.get("description", ""),
                stage_from=ts.get("stage_from", 1),
                stage_to=ts.get("stage_to", 22),
                dependencies=ts.get("dependencies", []),
                run_dir=run_dir,
                config_path=config_path,
            )
            graph.add_node(node)

        self.graphs[project_id] = graph
        return graph

    def load_from_disk(self, project_id: str, project_dir: Path) -> TaskGraph | None:
        """Try to load a TaskGraph from project_dir/task_graph.json."""
        path = project_dir / "task_graph.json"
        graph = TaskGraph.load(path)
        if graph:
            graph.project_id = project_id
            self.graphs[project_id] = graph
        return graph

    def save_to_disk(self, project_id: str, project_dir: Path) -> None:
        graph = self.graphs.get(project_id)
        if graph:
            path = project_dir / "task_graph.json"
            graph.save(path)

    def remove(self, project_id: str) -> None:
        self.graphs.pop(project_id, None)
