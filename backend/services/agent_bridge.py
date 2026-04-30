#!/usr/bin/env python3
"""
Agent Bridge v2 — project isolation, inter-layer task queues, idle-pull scheduling.

Architecture:
  runs_base/
  ├── projects/
  │   ├── proj-xxx/          # Each project has its own run_dir
  │   │   ├── stage-01/ ... stage-26/
  │   │   ├── checkpoint.json
  │   │   └── heartbeat.json
  │   └── proj-yyy/ ...
  └── queues/
      ├── idea_to_experiment.json
      ├── experiment_to_coding.json
      ├── coding_to_execution.json
      └── execution_feedback.json

Usage:
    python agent_bridge.py [--port 8766] [--agent-dir /path/to/agent]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import websockets
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

_this_dir = Path(__file__).resolve().parent
if str(_this_dir) not in sys.path:
    sys.path.insert(0, str(_this_dir))
from project_scanner import scan_project as _deep_scan_project
from project_planner import PlannerManager
from task_graph import TaskGraphRegistry, TaskGraph, TaskNode
from layer_coordinator import LayerCoordinator
from knowledge_manager import KnowledgeManager

if TYPE_CHECKING:
    from result_registry import ResultRegistry

logger = logging.getLogger(__name__)

# EventBus for real-time streaming from agent turn loops
_agent_dir = _this_dir.parent / "agent"
if str(_agent_dir) not in sys.path:
    sys.path.insert(0, str(_agent_dir))
try:
    from researchclaw.pipeline.claw_engine.event_bus import (
        EventBus,
        AgentEvent,
        get_event_bus,
    )
    _HAS_EVENT_BUS = True
except ImportError:
    _HAS_EVENT_BUS = False

# ── Constants ───────────────────────────────────────────────────────────────

STAGE_TO_LAYER: dict[int, str] = {
    1: "idea", 2: "idea", 3: "idea", 4: "idea",
    5: "idea", 6: "idea", 7: "idea", 8: "idea",
    9: "experiment",
    10: "coding", 11: "coding", 12: "coding", 13: "coding",
    14: "execution", 15: "execution", 16: "execution", 17: "execution", 18: "execution",
    19: "writing", 20: "writing", 21: "writing", 22: "writing",
    23: "writing", 24: "writing", 25: "writing", 26: "writing",
}

LAYER_STAGES: dict[str, list[int]] = {
    "idea": [1, 2, 3, 4, 5, 6, 7, 8],
    "experiment": [9],
    "coding": [10, 11, 12, 13],
    "execution": [14, 15, 16, 17, 18],
    "writing": [19, 20, 21, 22, 23, 24, 25, 26],
}

LAYER_RANGE: dict[str, tuple[int, int]] = {
    "idea": (1, 8),
    "experiment": (9, 9),
    "coding": (10, 13),
    "execution": (14, 18),
    "writing": (19, 26),
}

LAYER_RANGE_PHASE1: dict[str, tuple[int, int]] = {"idea": (1, 7)}
LAYER_RANGE_PHASE2: dict[str, tuple[int, int]] = {"idea": (8, 8)}


def _intersect_stage_bounds(lo1: int, hi1: int, lo2: int, hi2: int) -> tuple[int, int] | None:
    lo = max(lo1, lo2)
    hi = min(hi1, hi2)
    if lo > hi:
        return None
    return (lo, hi)


def _task_node_stage_window(task: "Task") -> tuple[int, int] | None:
    if task.stage_from is not None and task.stage_to is not None:
        return (int(task.stage_from), int(task.stage_to))
    return None


def _canonical_runtime_stage_range(
    state: "BridgeState",
    agent: "LobsterAgent",
    *,
    is_discussion_s8: bool,
    task_meta: dict | None,
) -> tuple[int, int]:
    if getattr(agent, "_is_idea_factory_s7_only", False):
        return (7, 7)
    if is_discussion_s8:
        return LAYER_RANGE_PHASE2["idea"]
    is_reproduce = bool(task_meta and task_meta.get("mode") == "reproduce")
    if state.discussion_mode and agent.layer == "idea" and not is_reproduce:
        return LAYER_RANGE_PHASE1["idea"]
    return LAYER_RANGE.get(agent.layer, (1, 15))


def _effective_stage_range_for_launch(
    state: "BridgeState",
    agent: "LobsterAgent",
    task: "Task | None",
    task_meta: dict | None,
    *,
    is_discussion_s8: bool,
    node_stage_override: tuple[int, int] | None = None,
) -> tuple[int, int]:
    b_lo, b_hi = _canonical_runtime_stage_range(
        state, agent, is_discussion_s8=is_discussion_s8, task_meta=task_meta,
    )
    if node_stage_override is not None:
        n_lo, n_hi = node_stage_override
    elif task is not None:
        tw = _task_node_stage_window(task)
        if not tw:
            return (b_lo, b_hi)
        n_lo, n_hi = tw
    else:
        return (b_lo, b_hi)
    hit = _intersect_stage_bounds(b_lo, b_hi, n_lo, n_hi)
    if hit is None:
        return (int(n_lo), int(n_hi))
    return hit


def _monitor_stage_range(
    state: "BridgeState",
    agent: "LobsterAgent",
    task_meta: dict | None,
) -> tuple[int, int]:
    is_discussion_s8 = bool(getattr(agent, "_is_discussion_s8", False))
    b_lo, b_hi = _canonical_runtime_stage_range(
        state, agent, is_discussion_s8=is_discussion_s8, task_meta=task_meta,
    )
    af = getattr(agent, "_task_stage_from", None)
    at = getattr(agent, "_task_stage_to", None)
    if af is not None and at is not None:
        hit = _intersect_stage_bounds(b_lo, b_hi, int(af), int(at))
        if hit is not None:
            return hit
        return (int(af), int(at))
    return (b_lo, b_hi)


def _agent_node_stage_window(agent: "LobsterAgent") -> tuple[int, int] | None:
    start = getattr(agent, "_task_stage_from", None)
    end = getattr(agent, "_task_stage_to", None)
    if start is None or end is None:
        return None
    return (int(start), int(end))


def _agent_requires_discussion_before_s8(agent: "LobsterAgent") -> bool:
    """Whether an idea task should pause after S7 for discussion before S8."""
    window = _agent_node_stage_window(agent)
    if window is None:
        return True
    start, end = window
    if start > end:
        start, end = end, start
    return start <= 7 and end >= 8


def _agent_subprocess_env(state: "BridgeState", agent: "LobsterAgent") -> dict[str, str]:
    """Environment shared by researchclaw subprocess launches."""
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "SCHOLARCLAW_PROJECT_ID": agent.project_id or "",
        "SCHOLARCLAW_TASK_ID": agent.assigned_task_id or "",
        "SCHOLARCLAW_NODE_ID": agent.assigned_task_id or "",
        "SCHOLARCLAW_RUN_DIR": agent.run_dir or "",
        "SCHOLARCLAW_AGENT_ID": agent.id or "",
    }
    if agent.run_dir:
        env["SCHOLARCLAW_METAPROMPT_PROJECT_DIR"] = str(Path(agent.run_dir).parent)
    workspace_dir = _get_workspace_dir(state, agent.project_id) if agent.project_id else ""
    if workspace_dir:
        env["SCHOLARCLAW_METAPROMPT_PROJECT_DIR"] = workspace_dir
    env.pop("CUDA_VISIBLE_DEVICES", None)
    return env


DISCUSSION_STAGE = 100

PASSTHROUGH_LAYERS: set[str] = set()

STAGE_NAMES: dict[int, str] = {
    1: "TOPIC_INIT", 2: "PROBLEM_DECOMPOSE", 3: "SEARCH_STRATEGY",
    4: "LITERATURE_COLLECT", 5: "LITERATURE_SCREEN", 6: "KNOWLEDGE_EXTRACT",
    7: "SYNTHESIS", 8: "HYPOTHESIS_GEN", 9: "EXPERIMENT_DESIGN",
    10: "CODEBASE_SEARCH", 11: "CODE_GENERATION", 12: "SANITY_CHECK",
    13: "RESOURCE_PLANNING", 14: "EXPERIMENT_RUN", 15: "ITERATIVE_REFINE",
    16: "RESULT_ANALYSIS", 17: "RESEARCH_DECISION", 18: "KNOWLEDGE_SUMMARY",
    19: "PAPER_OUTLINE", 20: "PAPER_DRAFT", 21: "PEER_REVIEW", 22: "PAPER_REVISION",
    23: "QUALITY_GATE", 24: "KNOWLEDGE_ARCHIVE", 25: "EXPORT_PUBLISH", 26: "CITATION_VERIFY",
}

STAGE_OUTPUTS: dict[int, list[str]] = {
    1: ["goal.md", "hardware_profile.json"], 2: ["problem_tree.md"],
    3: ["search_plan.yaml", "sources.json", "queries.json"], 4: ["candidates.jsonl"],
    5: ["shortlist.jsonl"], 6: ["cards/"], 7: ["synthesis.md"], 8: ["hypotheses.md"],
    9: ["exp_plan.yaml"], 10: ["codebase_candidates.json"],
    11: ["experiment/", "experiment_spec.md"], 12: ["sanity_report.json"],
    13: ["schedule.json"], 14: ["runs/"],
    15: ["refinement_log.json", "experiment_final/"],
    16: ["analysis.md", "experiment_summary.json", "charts/"], 17: ["decision.md"], 18: ["knowledge_entry.json"],
    19: ["outline.md"], 20: ["paper_draft.md"], 21: ["reviews.md"], 22: ["paper_revised.md", "latex_package.zip"],
    23: ["quality_report.json"], 24: ["archive.md", "bundle_index.json"],
    25: ["paper_final.md", "code/"], 26: ["verification_report.json", "references_verified.bib"],
}

STAGE_INPUTS: dict[int, list[str]] = {
    1: [], 2: ["goal.md"],
    3: ["problem_tree.md"], 4: ["search_plan.yaml"],
    5: ["candidates.jsonl"], 6: ["shortlist.jsonl"],
    7: ["cards/"], 8: ["synthesis.md"],
    9: ["hypotheses.md"],
    10: ["exp_plan.yaml"], 11: ["exp_plan.yaml", "codebase_candidates.json"],
    12: ["experiment/"], 13: ["exp_plan.yaml"],
    14: ["schedule.json", "experiment/"], 15: ["runs/"],
    16: ["runs/"], 17: ["analysis.md"],
    18: ["analysis.md", "decision.md", "exp_plan.yaml"],
    19: ["analysis.md", "decision.md"], 20: ["outline.md"],
    21: ["paper_draft.md"], 22: ["paper_draft.md", "reviews.md"],
    23: ["paper_revised.md"], 24: [],
    25: ["paper_revised.md"], 26: ["paper_final.md"],
}

# Curated artifacts to display in the frontend DataShelf (subset of STAGE_OUTPUTS)
DISPLAY_ARTIFACTS: set[str] = {
    # Idea 仓库 — only S8 hypotheses
    "hypotheses.md",
    # 知识库
    "knowledge_entry.json",
    # 论文仓库 — final paper + LaTeX package
    "paper_revised.md", "latex_package.zip",
    # 结果
    "analysis.md", "charts/", "decision.md",
    # 实验设计
    "exp_plan.yaml",
    # 代码
    "experiment/", "experiment_spec.md", "experiment_final/",
}

REPO_FOR_STAGE: dict[int, str] = {
    1: "knowledge", 2: "knowledge", 3: "knowledge", 4: "knowledge",
    5: "knowledge", 6: "knowledge", 7: "knowledge", 8: "knowledge",
    9: "exp_design",
    10: "codebase", 11: "codebase", 12: "codebase", 13: "codebase",
    14: "results", 15: "results", 16: "results", 17: "results", 18: "insights",
    19: "papers", 20: "papers", 21: "papers", 22: "papers",
    23: "papers", 24: "knowledge", 25: "papers", 26: "papers",
}

# Queue names between layers
QUEUE_NAMES: dict[str, tuple[str, str]] = {
    "idea_to_experiment":     ("idea",       "experiment"),
    "experiment_to_coding":   ("experiment", "coding"),
    "coding_to_execution":    ("coding",     "execution"),
    "execution_to_writing":   ("execution",  "writing"),
    "execution_feedback":     ("execution",  "idea"),
}

# Which queue a completing layer feeds into
LAYER_OUTPUT_QUEUE: dict[str, str] = {
    "idea":       "idea_to_experiment",
    "experiment": "experiment_to_coding",
    "coding":     "coding_to_execution",
    "execution":  "execution_to_writing",
    "writing":    "execution_feedback",
}

# Which queue a layer pulls tasks from
LAYER_INPUT_QUEUE: dict[str, str] = {
    "experiment": "idea_to_experiment",
    "coding":     "experiment_to_coding",
    "execution":  "coding_to_execution",
    "writing":    "execution_to_writing",
    "idea":       "execution_feedback",
}

# ── Human Feedback Persistence ───────────────────────────────────────────────

def _save_feedback(state: "BridgeState", content: str, target_layer: str, message_id: str) -> None:
    """Persist human feedback to disk so running agents can pick it up.

    Writes to:
    1. Global ``runs_base/feedback/feedback_log.jsonl`` — full audit trail
    2. Each matching project's ``run_dir/human_feedback.jsonl`` — consumed by
       the executor's ``_load_human_feedback()`` before each pipeline stage
    """
    feedback_dir = Path(state.runs_base_dir) / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": message_id,
        "content": content,
        "targetLayer": target_layer,
        "timestamp": _now_ms(),
    }
    log_path = feedback_dir / "feedback_log.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    latest_path = feedback_dir / "latest_feedback.json"
    _write_json(latest_path, entry)

    injected_to: list[str] = []
    for agent in state.agents.values():
        if not agent.run_dir or agent.status not in ("working", "idle"):
            continue
        if target_layer != "all" and agent.layer != target_layer:
            continue
        run_dir = Path(agent.run_dir)
        if not run_dir.exists():
            continue
        fb_path = run_dir / "human_feedback.jsonl"
        try:
            with open(fb_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            injected_to.append(f"{agent.name}({agent.project_id})")
        except OSError:
            pass

    if injected_to:
        print(f"[feedback] Injected to {len(injected_to)} project(s): {', '.join(injected_to)}")


# ── Utilities ───────────────────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:12]

def _now_ms() -> int:
    return int(time.time() * 1000)

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        bak = path.with_suffix(path.suffix + ".bak")
        if bak.exists():
            try:
                return json.loads(bak.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None
    except (FileNotFoundError, OSError):
        return None

def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    if path.exists():
        try:
            path.replace(path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass
    path.write_text(content, encoding="utf-8")


_intent_llm_client: "object | None" = None
_intent_llm_init_done: bool = False

_INTENT_SYSTEM_PROMPT = (
    "你是一个意图分类器。用户在一个 AI 研究 pipeline 的控制面板中输入了一条消息。"
    "判断这条消息的意图类别:\n"
    "- query: 查询当前运行状态/进度/阶段\n"
    "- steer: 要求调整方向/重点/优先级（如'先做实验','重点写Introduction'）\n"
    "- feedback: 提供具体指导/建议/修改指令\n"
    "只回复一个词: query / steer / feedback"
)


def _init_intent_llm(state: "BridgeState") -> None:
    """Lazily create a lightweight LLM client for intent classification."""
    global _intent_llm_client, _intent_llm_init_done
    if _intent_llm_init_done:
        return
    _intent_llm_init_done = True
    try:
        agent_dir = state.agent_package_dir
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        from researchclaw.llm.client import LLMClient, LLMConfig

        api_key = os.environ.get("RESEARCHCLAW_API_KEY", "")

        # Read api_key from project YAML configs if not in env
        if not api_key:
            import yaml as _yaml
            for proj_dir in state.projects_dir().iterdir():
                meta = _read_json(proj_dir / "project_meta.json")
                if not meta or not meta.get("config_path"):
                    continue
                cfg_path = Path(meta["config_path"])
                if not cfg_path.exists():
                    continue
                try:
                    with open(cfg_path, encoding="utf-8") as f:
                        raw = _yaml.safe_load(f) or {}
                    llm_sec = raw.get("llm", {})
                    api_key = llm_sec.get("api_key", "")
                    if api_key:
                        break
                except Exception:
                    continue

        if not api_key:
            print("[intent-llm] No API key found, using keyword fallback")
            return

        from researchclaw.llm import resolve_provider_base_url
        base_url = resolve_provider_base_url("openai-compatible", "")
        _intent_llm_client = LLMClient(LLMConfig(
            base_url=base_url,
            api_key=api_key,
            primary_model="claude-opus-4-1-20250805",
            fallback_models=["gpt-4o-mini"],
            max_retries=1,
            timeout_sec=10,
        ))
        print(f"[intent-llm] Initialized ({base_url})")
    except Exception as exc:
        print(f"[intent-llm] Init failed, will use keyword fallback: {exc}")


def _classify_chat_intent_keywords(text: str) -> str:
    """Fast keyword-based fallback for intent classification: query / steer / feedback."""
    t = text.lower()
    q, s, f = 0, 0, 0
    for kw in ("状态", "进度", "进展", "阶段", "跑到", "做到", "到哪", "到第几",
               "什么阶段", "什么状态", "查看", "查询", "怎么样了", "情况",
               "status", "progress", "stage", "how far"):
        if kw in t:
            q += 1
    for kw in ("重点", "优先", "先做", "集中", "关注", "方向", "转向", "改为",
               "focus", "prioritize", "concentrate", "switch to", "work on"):
        if kw in t:
            s += 1
    for kw in ("请", "应该", "建议", "不要", "换成", "改成", "使用",
               "注意", "确保", "调整", "修改", "尝试", "模型", "参数",
               "checkpoint", "路径", "下载"):
        if kw in t:
            f += 1
    if t.rstrip()[-1:] in ("?", "？"):
        q += 2
    if any(p in t for p in ("吗", "呢")):
        q += 1
    if len(t) > 80:
        f += 1
    best = max(q, s, f)
    if best == q and q > 0:
        return "query"
    if best == s and s > 0:
        return "steer"
    return "feedback"


async def _classify_chat_intent(text: str, state: "BridgeState") -> str:
    """Classify user chat intent: LLM first, keyword fallback on failure."""
    _init_intent_llm(state)
    if _intent_llm_client is not None:
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _intent_llm_client.chat(  # type: ignore[union-attr]
                    [{"role": "user", "content": text}],
                    system=_INTENT_SYSTEM_PROMPT,
                    max_tokens=10,
                    temperature=0,
                ),
            )
            answer = resp.content.strip().lower()
            if "query" in answer:
                return "query"
            if "steer" in answer:
                return "steer"
            if "feedback" in answer:
                return "feedback"
        except Exception as exc:
            print(f"[intent-llm] Call failed, falling back to keywords: {exc}")
    return _classify_chat_intent_keywords(text)


def _pause_project(state: "BridgeState", project_id: str) -> list[dict]:
    """Pause a running project: stop agents and remove queued tasks, but keep all files."""
    messages: list[dict] = []
    sys_agent = LobsterAgent(
        id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="",
    )

    if not project_id:
        return messages

    stopped = 0
    for agent in list(state.agents.values()):
        if agent.project_id == project_id:
            if agent.process is not None and agent.process.poll() is None:
                agent.process.terminate()
                try:
                    agent.process.wait(timeout=5)
                except Exception:
                    agent.process.kill()
                stopped += 1
            _reset_agent_idle(agent)
            messages.append(msg_agent_update(agent))

    removed = 0
    for q in state.queues.values():
        before = len(q.tasks)
        q.tasks = [t for t in q.tasks if t.project_id != project_id]
        removed += before - len(q.tasks)

    released = state.gpu_allocator.release(project_id)
    if released:
        messages.append(msg_log(sys_agent, f"GPU {released} 已释放 (项目暂停)", "info"))

    messages.append(msg_log(
        sys_agent,
        f"项目 [{project_id}] 已暂停 (停止 {stopped} 个 Agent, 移除 {removed} 个队列任务)",
        "warning",
    ))
    return messages


def _restart_project(state: "BridgeState", project_id: str) -> list[dict]:
    """Restart a project from scratch: stop agents, clear checkpoint, re-submit."""
    import shutil

    messages: list[dict] = []
    sys_agent = LobsterAgent(
        id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="",
    )

    if not project_id:
        return messages

    proj_dir = state.projects_dir() / project_id
    if not proj_dir.exists():
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 不存在", "error"))
        return messages

    messages.extend(_pause_project(state, project_id))

    meta = _read_json(proj_dir / "project_meta.json")
    config_path = meta.get("config_path", "") if meta else ""
    topic = meta.get("topic", "") if meta else ""
    mode = meta.get("mode", "lab") if meta else "lab"

    # Try to recover config_path from alternative locations
    if not config_path:
        config_path = _recover_config_path(state, project_id, proj_dir, meta)
        if config_path:
            _update_project_meta(proj_dir / "project_meta.json", {"config_path": config_path})
            meta = _read_json(proj_dir / "project_meta.json") or meta or {}

    if not config_path:
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 缺少配置文件路径, 正在重新生成…", "warning"))
        try:
            config_path = _generate_config_from_template(state, project_id, topic or project_id)
            _g_llm = state.global_llm_config
            if _g_llm and _g_llm.get("model") and Path(config_path).exists():
                try:
                    config_path = _create_model_config(
                        config_path, _g_llm["model"], str(proj_dir),
                        base_url=_g_llm.get("base_url", ""),
                        api_key=_g_llm.get("api_key", ""),
                    )
                except Exception:
                    pass
            _update_project_meta(proj_dir / "project_meta.json", {"config_path": config_path})
            messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已重新生成配置文件", "success"))
        except Exception as e:
            messages.append(msg_log(sys_agent, f"项目 [{project_id}] 配置文件重新生成失败: {e}", "error"))
            return messages

    # Lab mode with run-* sub-dirs: clear progress inside each angle but keep structure
    angle_dirs = sorted(proj_dir.glob("run-*"))
    if mode == "lab" and angle_dirs:
        for angle_dir in angle_dirs:
            if not angle_dir.is_dir():
                continue
            for item in angle_dir.iterdir():
                if item.name in ("project_meta.json",):
                    continue
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
        # Also clean any non-run files at project root (except meta/config)
        for item in proj_dir.iterdir():
            if item.name in ("project_meta.json", "_workspace_link.json") or item.name.startswith("run-"):
                continue
            if item.name.startswith("config_") and item.suffix == ".yaml":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已清除进度，正在重新启动…", "info"))
        messages.extend(resume_project(state, project_id))
    else:
        # For planner-mode projects with workspace, also clean .scholar/ stage dirs
        _ws_link = _read_json(proj_dir / "_workspace_link.json")
        if _ws_link and _ws_link.get("scholar_dir"):
            _scholar_p = Path(_ws_link["scholar_dir"])
            if _scholar_p.exists():
                for item in _scholar_p.iterdir():
                    if item.name in ("logs", "backups", "diffs", "knowledge_base"):
                        continue
                    if item.name.startswith("config_") and item.suffix == ".yaml":
                        continue
                    if item.name == "project_meta.json":
                        continue
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)

        for item in proj_dir.iterdir():
            if item.name in ("project_meta.json", "_workspace_link.json"):
                continue
            if item.name.startswith("config_") and item.suffix == ".yaml":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        # If the config file recorded in meta no longer exists, regenerate it
        if config_path and not Path(config_path).exists():
            _base_cfg = _generate_config_from_template(state, project_id, topic)
            _g_llm = state.global_llm_config
            if _g_llm and _g_llm.get("model") and Path(_base_cfg).exists():
                try:
                    _base_cfg = _create_model_config(
                        _base_cfg, _g_llm["model"], str(proj_dir),
                        base_url=_g_llm.get("base_url", ""),
                        api_key=_g_llm.get("api_key", ""),
                    )
                except Exception:
                    pass
            config_path = _base_cfg
            _update_project_meta(proj_dir / "project_meta.json", {"config_path": config_path})
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已清除进度，正在重新启动…", "info"))
        messages.extend(submit_new_project(state, project_id, config_path, topic, mode=mode))
    return messages


def _delete_project(state: "BridgeState", project_id: str) -> list[dict]:
    """Delete a project: stop any running processes, clean up state, remove files."""
    import shutil

    messages: list[dict] = []
    sys_agent = LobsterAgent(
        id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="",
    )

    if not project_id:
        messages.append(msg_feedback_ack(f"del-{_uid()}", "请指定要删除的项目 ID。"))
        return messages

    for agent in list(state.agents.values()):
        if agent.project_id == project_id:
            if agent.process is not None and agent.process.poll() is None:
                agent.process.terminate()
                try:
                    agent.process.wait(timeout=5)
                except Exception:
                    agent.process.kill()
            _reset_agent_idle(agent)
            messages.append(msg_agent_update(agent))

    released = state.gpu_allocator.release(project_id)
    if released:
        messages.append(msg_log(sys_agent, f"GPU {released} 已释放 (项目删除)", "info"))

    for q in state.queues.values():
        q.tasks = [t for t in q.tasks if t.project_id != project_id]

    state._fail_counts.pop(project_id, None)

    # Clean up discussion state
    for aid in list(state.discussion_waiting):
        a = state.discussion_waiting[aid]
        if a.project_id == "" or a.project_id == project_id:
            state.discussion_waiting.pop(aid, None)
    state.discussion_groups.pop(project_id, None)

    proj_dir = state.projects_dir() / project_id
    if proj_dir.exists() and proj_dir.is_dir():
        try:
            shutil.rmtree(proj_dir)
            messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已删除", "success"))
        except OSError as exc:
            messages.append(msg_log(sys_agent, f"删除项目目录失败: {exc}", "error"))
    else:
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 目录不存在", "warning"))

    return messages


def _build_status_summary(state: "BridgeState", target_layer: str = "all") -> str:
    """Build a human-readable status summary for all running/recent projects."""
    lines: list[str] = []
    projects_dir = state.projects_dir()

    active_projects: dict[str, LobsterAgent] = {}
    for agent in state.agents.values():
        if agent.project_id and agent.status in ("working", "idle"):
            active_projects[agent.project_id] = agent

    project_dirs = sorted(
        (d for d in projects_dir.iterdir() if d.is_dir() and not d.name.startswith("_")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    ) if projects_dir.is_dir() else []

    if not project_dirs:
        return "当前没有任何项目。"

    for proj_dir in project_dirs[:5]:
        pid = proj_dir.name
        if target_layer != "all" and pid not in active_projects:
            continue

        lines.append(f"📋 项目: {pid}")

        agent = active_projects.get(pid)
        if agent:
            layer_cn = {"idea": "调研", "experiment": "设计", "coding": "编码",
                        "execution": "执行", "writing": "写作"}.get(agent.layer, agent.layer)
            status_cn = {"working": "运行中", "idle": "空闲", "error": "错误",
                         "done": "完成"}.get(agent.status, agent.status)
            lines.append(f"  状态: {status_cn} | 层: {layer_cn}")
            if agent.current_stage:
                sname = STAGE_NAMES.get(agent.current_stage, "?")
                lines.append(f"  当前阶段: S{agent.current_stage} {sname}")
        else:
            lines.append("  状态: 未活跃")

        stage_statuses = []
        for s in range(1, 27):
            health = _read_json(proj_dir / f"stage-{s:02d}" / "stage_health.json")
            if health:
                st = health.get("status", "?")
                dur = health.get("duration_sec")
                err = health.get("error")
                icon = "✅" if st == "done" else "❌" if st == "failed" else "🔄"
                sname = STAGE_NAMES.get(s, "?")
                entry = f"  {icon} S{s} {sname}"
                if dur is not None:
                    if dur < 60:
                        entry += f" ({dur:.0f}s)"
                    else:
                        entry += f" ({dur / 60:.1f}min)"
                if err:
                    entry += f" — {err[:60]}"
                stage_statuses.append(entry)

        if stage_statuses:
            last_done = [s for s in stage_statuses if "✅" in s]
            failed = [s for s in stage_statuses if "❌" in s]
            lines.append(f"  已完成: {len(last_done)}/22 阶段" + (f", {len(failed)} 失败" if failed else ""))
            for s in stage_statuses:
                lines.append(s)
        else:
            lines.append("  暂无阶段数据")

        heartbeat = _read_json(proj_dir / "heartbeat.json")
        if heartbeat:
            ts = heartbeat.get("timestamp", "")
            lines.append(f"  最后心跳: {ts}")

        lines.append("")

    gpu_info = state.gpu_allocator.summary()
    lines.append(f"🖥️ GPU: {gpu_info['free']}/{gpu_info['total']} 空闲")
    if gpu_info["assignments"]:
        for proj, gpus in gpu_info["assignments"].items():
            lines.append(f"  {proj} → GPU {gpus}")

    return "\n".join(lines)


# ── Data structures ─────────────────────────────────────────────────────────

@dataclass
class Task:
    id: str
    project_id: str
    run_dir: str
    config_path: str
    source_layer: str
    target_layer: str
    topic: str = ""
    status: str = "pending"          # pending | assigned | completed | failed
    assigned_to: str | None = None
    created_at: int = 0
    assigned_at: int = 0
    completed_at: int = 0
    # TaskGraph node stage window; when set, launch_agent_for_task intersects with layer/discussion bounds
    stage_from: int | None = None
    stage_to: int | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def from_dict(d: dict) -> "Task":
        t = Task(
            id=d["id"], project_id=d["project_id"], run_dir=d["run_dir"],
            config_path=d.get("config_path", ""),
            source_layer=d["source_layer"], target_layer=d["target_layer"],
            topic=d.get("topic", ""),
        )
        t.status = d.get("status", "pending")
        t.assigned_to = d.get("assigned_to")
        t.created_at = d.get("created_at", 0)
        t.assigned_at = d.get("assigned_at", 0)
        t.completed_at = d.get("completed_at", 0)
        t.stage_from = d.get("stage_from")
        t.stage_to = d.get("stage_to")
        return t


@dataclass
class TaskQueue:
    """File-backed FIFO task queue."""
    name: str
    path: Path
    tasks: list[Task] = field(default_factory=list)

    def load(self):
        data = _read_json(self.path)
        if data and isinstance(data, list):
            self.tasks = [Task.from_dict(d) for d in data]

    def save(self):
        _write_json(self.path, [t.to_dict() for t in self.tasks])

    def push(self, task: Task):
        self.tasks.append(task)
        self.save()

    def peek_pending(self) -> Task | None:
        for t in self.tasks:
            if t.status == "pending":
                return t
        return None

    def assign(self, task_id: str, agent_id: str) -> Task | None:
        for t in self.tasks:
            if t.id == task_id and t.status == "pending":
                t.status = "assigned"
                t.assigned_to = agent_id
                t.assigned_at = _now_ms()
                self.save()
                return t
        return None

    def complete(self, task_id: str):
        for t in self.tasks:
            if t.id == task_id:
                t.status = "completed"
                t.completed_at = _now_ms()
                self.save()
                return

    def fail(self, task_id: str):
        for t in self.tasks:
            if t.id == task_id:
                t.status = "failed"
                self.save()
                return

    def pending_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == "pending")

    def summary(self) -> dict:
        return {
            "name": self.name,
            "total": len(self.tasks),
            "pending": sum(1 for t in self.tasks if t.status == "pending"),
            "assigned": sum(1 for t in self.tasks if t.status == "assigned"),
            "completed": sum(1 for t in self.tasks if t.status == "completed"),
        }


@dataclass
class DiscussionGroup:
    """Tracks a group of L1 agents discussing the same topic."""
    project_id: str
    topic: str
    config_path: str
    agent_ids: list[str] = field(default_factory=list)
    run_dirs: dict[str, str] = field(default_factory=dict)   # agent_id -> run_dir
    completed_s7: set[str] = field(default_factory=set)       # agent_ids done with S7
    completed_s8: set[str] = field(default_factory=set)       # agent_ids done with S8
    best_agent_id: str = ""
    status: str = "gathering"    # gathering | waiting | discussing | done
    discussion_process: subprocess.Popen | None = field(default=None, repr=False)
    discussion_output_dir: str = ""

    def all_ready(self) -> bool:
        return len(self.completed_s7) >= len(self.agent_ids) and len(self.agent_ids) >= 2

    def all_s8_done(self) -> bool:
        return len(self.completed_s8) >= len(self.agent_ids) and len(self.agent_ids) >= 2

    def synthesis_dirs(self) -> list[str]:
        dirs = []
        for aid in self.agent_ids:
            rd = self.run_dirs.get(aid, "")
            if rd:
                dirs.append(str(Path(rd) / "stage-07"))
        return dirs


@dataclass
class LobsterAgent:
    id: str
    name: str
    layer: str
    run_id: str
    run_dir: str
    config_path: str
    project_id: str = ""
    status: str = "idle"
    current_stage: int | None = None
    current_task: str = ""
    assigned_task_id: str | None = None
    stage_progress: dict[int, str] = field(default_factory=dict)
    role_tag: str = ""
    process: subprocess.Popen | None = field(default=None, repr=False)
    _prev_heartbeat: dict = field(default_factory=dict, repr=False)
    _prev_checkpoint: dict = field(default_factory=dict, repr=False)
    _known_artifacts: set[str] = field(default_factory=set, repr=False)
    _activity_offset: int = 0
    _log_activity_offset: int = 0
    _prev_session_checksums: dict[str, str] = field(default_factory=dict, repr=False)
    _watchdog_messages: list[dict] = field(default_factory=list, repr=False)
    _observer: "Observer | None" = field(default=None, repr=False)

    def to_frontend(self) -> dict:
        return {
            "id": self.id, "name": self.name, "layer": self.layer,
            "runId": self.run_id, "status": self.status,
            "currentStage": self.current_stage,
            "currentTask": self.current_task,
            "stageProgress": self.stage_progress,
            "projectId": self.project_id,
            "roleTag": self.role_tag,
        }


class GpuAllocator:
    """Manages GPU assignment across concurrent projects."""

    def __init__(self, total_gpus: int = 8, gpus_per_project: int = 2):
        self.total_gpus = total_gpus
        self.gpus_per_project = gpus_per_project
        self.assignments: dict[str, list[int]] = {}  # project_id -> [gpu_ids]
        self._occupied: set[int] = set()

    def available_count(self) -> int:
        return self.total_gpus - len(self._occupied)

    def can_allocate(self) -> bool:
        return self.available_count() >= self.gpus_per_project

    def allocate(self, project_id: str) -> list[int] | None:
        if project_id in self.assignments:
            return self.assignments[project_id]
        if not self.can_allocate():
            return None
        free = sorted(set(range(self.total_gpus)) - self._occupied)
        assigned = free[:self.gpus_per_project]
        self.assignments[project_id] = assigned
        self._occupied.update(assigned)
        return assigned

    def release(self, project_id: str) -> list[int]:
        gpus = self.assignments.pop(project_id, [])
        self._occupied -= set(gpus)
        return gpus

    def get(self, project_id: str) -> list[int] | None:
        return self.assignments.get(project_id)

    def summary(self) -> dict:
        return {
            "total": self.total_gpus,
            "per_project": self.gpus_per_project,
            "free": self.available_count(),
            "assignments": {k: v for k, v in self.assignments.items()},
        }


@dataclass
class BridgeState:
    agents: dict[str, LobsterAgent] = field(default_factory=dict)
    queues: dict[str, TaskQueue] = field(default_factory=dict)
    clients: set = field(default_factory=set)
    python_path: str = ""
    agent_package_dir: str = ""
    runs_base_dir: str = ""
    gpu_allocator: GpuAllocator = field(default_factory=GpuAllocator)
    result_registry: "ResultRegistry | None" = None
    auto_loop: bool = False
    # Discussion mode: L1 agents discuss after S7, before S8
    discussion_mode: bool = True
    discussion_groups: dict[str, DiscussionGroup] = field(default_factory=dict)
    discussion_rounds: int = 3
    discussion_models: list[str] = field(default_factory=lambda: ["glm-5-turbo", "glm-5-turbo"])
    # Cross-project discussion: agents waiting for a peer to discuss with
    discussion_waiting: dict[str, "LobsterAgent"] = field(default_factory=dict)
    # Idea factory: L1 idle → produce ideas via S7+S8
    idea_factory_topic: str = ""
    idea_factory_config: str = ""
    idea_factory_remaining: int = 0  # 0=disabled, -1=infinite, N=count
    idea_factory_produced: int = 0
    _fail_counts: dict[str, int] = field(default_factory=dict)  # project_id → consecutive fail count
    # Lab mode: track which sub-projects belong to the same batch
    lab_batches: dict[str, list[str]] = field(default_factory=dict)  # base_id → [sub_project_ids]
    # v2.0: Planner sessions, task graphs, and layer coordination
    planner: PlannerManager = field(default_factory=PlannerManager)
    task_graphs: TaskGraphRegistry = field(default_factory=TaskGraphRegistry)
    coordinator: LayerCoordinator = field(default_factory=LayerCoordinator)
    # Global LLM config (fallback when layer has no model configured)
    global_llm_config: dict = field(default_factory=dict)  # {base_url, api_key, model}
    # Knowledge base manager
    kb: KnowledgeManager = field(default_factory=KnowledgeManager)
    # P0: Approval mode (auto / confirm_writes / confirm_all)
    approval_mode: str = "auto"
    # 若非空，则 WebSocket 控制类命令与 HTTP /download/ 需携带相同 token
    control_token: str = ""
    # P0: Activity file read offsets per agent (agentId → byte offset)
    _activity_offsets: dict[str, int] = field(default_factory=dict)

    def projects_dir(self) -> Path:
        return Path(self.runs_base_dir) / "projects"

    def queues_dir(self) -> Path:
        return Path(self.runs_base_dir) / "queues"

    def archives_dir(self) -> Path:
        return Path(self.runs_base_dir) / "archives"


# ── Message builders ────────────────────────────────────────────────────────

def msg_agent_update(agent: LobsterAgent) -> dict:
    return {"type": "agent_update", "payload": agent.to_frontend()}

def msg_stage_update(agent_id: str, stage: int, status: str) -> dict:
    return {"type": "stage_update", "payload": {"agentId": agent_id, "stage": stage, "status": status}}

def msg_artifact(repo_id: str, filename: str, agent_name: str, size: str,
                  project_id: str = "", content: str = "", stage: int = 0) -> dict:
    payload: dict = {
        "id": _uid(), "repoId": repo_id, "projectId": project_id, "filename": filename,
        "producedBy": agent_name, "timestamp": _now_ms(), "size": size, "status": "fresh",
    }
    if content:
        payload["content"] = content
    if stage:
        payload["stage"] = stage
    return {"type": "artifact_produced", "payload": payload}


_NO_CONTENT_ARTIFACTS: set[str] = {"paper_revised.md", "paper_draft.md", "outline.md", "reviews.md"}

def _extract_artifact_summary(path: Path, filename: str, max_chars: int = 500) -> str:
    """Extract a human-readable summary from an artifact file."""
    if filename in _NO_CONTENT_ARTIFACTS:
        return ""
    try:
        if path.is_dir():
            children = list(path.iterdir())
            file_count = sum(1 for c in children if c.is_file())
            md_titles = []
            for c in sorted(children)[:8]:
                if c.suffix == ".md" and c.is_file():
                    first_line = c.read_text(encoding="utf-8", errors="ignore").strip().split("\n")[0]
                    title = first_line.lstrip("#").strip()
                    if title:
                        md_titles.append(title)
            if md_titles:
                return f"{file_count} files: " + "; ".join(md_titles[:6])
            return f"{file_count} files"

        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            return ""

        if filename == "hypotheses.md":
            lines = text.strip().split("\n")
            hyp_titles = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("##") and "hypothesis" in stripped.lower():
                    title = stripped.lstrip("#").strip().lstrip("—").lstrip("-").strip()
                    title = title.removeprefix("Final Hypothesis").strip()
                    title = title.removeprefix("Hypothesis").strip()
                    title = title.lstrip("0123456789").strip().lstrip("—").lstrip("-").strip()
                    title = title.replace("**", "").replace("*", "")
                    if title:
                        hyp_titles.append(f"• {title}")
            if hyp_titles:
                return "\n".join(hyp_titles)[:max_chars]

        if filename.endswith(".md"):
            lines = text.strip().split("\n")
            summary_parts = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    if summary_parts:
                        break
                    continue
                clean = stripped.lstrip("#").strip().rstrip("---").strip()
                if clean:
                    summary_parts.append(clean)
                if len(" ".join(summary_parts)) > max_chars:
                    break
            return " ".join(summary_parts)[:max_chars]

        if filename.endswith((".yaml", ".yml")):
            lines = text.strip().split("\n")
            top_keys = []
            for line in lines[:15]:
                if line and not line.startswith(" ") and not line.startswith("#") and ":" in line:
                    top_keys.append(line.split(":")[0].strip())
            return f"keys: {', '.join(top_keys[:8])}" if top_keys else ""

        if filename.endswith(".json"):
            data = json.loads(text)
            if isinstance(data, dict):
                # knowledge_entry.json: extract topic + hypothesis names
                if "topic" in data and "hypotheses" in data and isinstance(data["hypotheses"], list):
                    topic = str(data["topic"])[:120]
                    hyp_names = []
                    for h in data["hypotheses"][:5]:
                        if isinstance(h, dict):
                            name = h.get("name") or h.get("id", "")
                            status = h.get("status", "")
                            entry = str(name)[:60]
                            if status:
                                entry += f" ({status[:20]})"
                            hyp_names.append(entry)
                    summary = topic
                    if hyp_names:
                        summary += "\n" + "\n".join(f"• {n}" for n in hyp_names)
                    return summary[:max_chars]

                keys = list(data.keys())[:8]
                preview_parts = []
                for k in keys[:4]:
                    v = data[k]
                    if isinstance(v, str) and len(v) < 80:
                        preview_parts.append(f"{k}: {v}")
                    elif isinstance(v, (int, float, bool)):
                        preview_parts.append(f"{k}: {v}")
                    elif isinstance(v, list):
                        preview_parts.append(f"{k}: [{len(v)} items]")
                return "; ".join(preview_parts) if preview_parts else f"keys: {', '.join(keys)}"
            if isinstance(data, list):
                return f"{len(data)} entries"

        if filename.endswith(".jsonl"):
            line_count = text.count("\n")
            first_line = text.strip().split("\n")[0] if text.strip() else ""
            if first_line:
                try:
                    entry = json.loads(first_line)
                    title = entry.get("title") or entry.get("name") or entry.get("id", "")
                    if title:
                        return f"{line_count} entries — first: {str(title)[:80]}"
                except Exception:
                    pass
            return f"{line_count} entries"

    except Exception:
        pass
    return ""

def msg_log(agent: LobsterAgent, message: str, level: str = "info", stage: int | None = None) -> dict:
    return {"type": "log", "payload": {
        "id": _uid(), "agentId": agent.id, "agentName": agent.name,
        "layer": agent.layer, "stage": stage or agent.current_stage,
        "message": message, "level": level, "timestamp": _now_ms(),
    }}

def msg_activity(
    agent: LobsterAgent,
    activity_type: str,
    summary: str,
    detail: str = "",
    **extra: object,
) -> dict:
    payload: dict = {
        "id": _uid(), "agentId": agent.id, "agentName": agent.name,
        "projectId": agent.project_id or "", "layer": agent.layer,
        "activityType": activity_type, "summary": summary, "timestamp": _now_ms(),
    }
    if detail:
        payload["detail"] = detail
    if agent.assigned_task_id:
        payload["nodeId"] = agent.assigned_task_id
    if agent.current_stage:
        payload["stage"] = agent.current_stage
    for key, value in extra.items():
        if value is not None and value != "":
            payload[key] = value
    return {"type": "agent_activity", "payload": payload}


def _event_to_ws_message(evt: "AgentEvent", agent: LobsterAgent) -> dict:
    """Convert an EventBus AgentEvent to a WebSocket message."""
    # Special-case: stage_session_update carries its own structured payload
    if evt.type.value == "stage_session_update":
        session = evt.data.get("session", {})
        stage_dir = Path(evt.data.get("stage_dir", ""))
        stage_num = 0
        try:
            stage_num = int(stage_dir.name.replace("stage-", ""))
        except ValueError:
            pass
        return {
            "type": "stage_session_update",
            "payload": {
                "projectId": agent.project_id or "",
                "agentId": agent.id,
                "stage": stage_num,
                "stageName": session.get("stage_name", ""),
                "status": session.get("status", "pending"),
                "elapsedSec": session.get("elapsed_sec", 0),
                "llmCalls": session.get("llm_calls", 0),
                "sandboxRuns": session.get("sandbox_runs", 0),
                "phaseLog": session.get("phase_log", []),
                "artifacts": session.get("artifacts", []),
                "errors": session.get("errors", []),
                "metadata": session.get("metadata", {}),
            },
        }

    _type_map = {
        "thinking_delta": "thinking",
        "text_delta": "thinking",
        "tool_use_start": "tool_call",
        "tool_use_end": "tool_call",
        "tool_result": "tool_result",
        "stage_change": "stage_transition",
        "llm_call": "llm_call",
        "llm_response": "llm_response",
        "error": "error",
        "conversation_turn": "stage_transition",
        "file_write": "file_write",
        "file_read": "file_read",
        "permission_request": "tool_call",
    }
    activity_type = _type_map.get(evt.type.value, evt.type.value)
    detail = evt.data.get("detail", "") or evt.data.get("text", "") or evt.data.get("content", "")
    summary = evt.data.get("summary", "")
    if not summary and detail:
        summary = str(detail)[:240]
    payload: dict = {
        "id": _uid(),
        "agentId": evt.agent_id or agent.id,
        "agentName": agent.name,
        "projectId": agent.project_id or "",
        "layer": agent.layer,
        "activityType": activity_type,
        "summary": summary,
        "timestamp": evt.timestamp * 1000 if evt.timestamp < 1e12 else evt.timestamp,
    }
    if detail:
        payload["detail"] = detail
    for src_key, dst_key in (
        ("node_id", "nodeId"),
        ("nodeId", "nodeId"),
        ("stage", "stage"),
        ("model", "model"),
        ("prompt_hash", "promptHash"),
        ("promptHash", "promptHash"),
        ("duration_ms", "durationMs"),
        ("durationMs", "durationMs"),
        ("elapsed_ms", "elapsedMs"),
        ("tokens", "tokens"),
        ("tool_name", "toolName"),
        ("toolName", "toolName"),
        ("args", "args"),
    ):
        if src_key in evt.data and evt.data[src_key] not in (None, ""):
            payload[dst_key] = evt.data[src_key]
    if "stage" not in payload and agent.current_stage:
        payload["stage"] = agent.current_stage
    if "nodeId" not in payload and agent.assigned_task_id:
        payload["nodeId"] = agent.assigned_task_id
    return {"type": "agent_activity", "payload": payload}

def msg_approval_request(agent: LobsterAgent, request_id: str, action_type: str, description: str, detail: str = "") -> dict:
    payload: dict = {
        "requestId": request_id, "agentId": agent.id, "agentName": agent.name,
        "projectId": agent.project_id or "", "actionType": action_type,
        "description": description, "timestamp": _now_ms(),
    }
    if detail:
        payload["detail"] = detail
    return {"type": "approval_request", "payload": payload}

def msg_queue_update(queues: dict[str, TaskQueue]) -> dict:
    return {"type": "queue_update", "payload": {name: q.summary() for name, q in queues.items()}}

def msg_project_list(projects: list[dict]) -> dict:
    return {"type": "project_list", "payload": projects}


def msg_feedback_ack(message_id: str, content: str, target_layer: str = "all", plan_update: str = "") -> dict:
    return {"type": "chat_message", "payload": {
        "id": f"sys-{_uid()}", "role": "system", "content": content,
        "timestamp": _now_ms(), "targetLayer": target_layer,
    }}


# ── File monitoring ─────────────────────────────────────────────────────────

def _sync_completed_stages(
    agent: LobsterAgent, run_dir: Path, layer_range: tuple[int, int], done_up_to: int,
) -> list[dict]:
    """Mark all stages from layer_range[0] to done_up_to as completed, emit events for new ones."""
    messages: list[dict] = []
    for s in range(layer_range[0], min(done_up_to, layer_range[1]) + 1):
        if agent.stage_progress.get(s) == "completed":
            continue
        if s not in STAGE_TO_LAYER:
            continue
        agent.stage_progress[s] = "completed"
        messages.append(msg_stage_update(agent.id, s, "completed"))
        messages.append(msg_log(agent, f"{STAGE_NAMES.get(s, f'S{s}')} 完成", "success", s))
        stage_dir = run_dir / f"stage-{s:02d}"
        _stage_files: list[dict] = []
        if stage_dir.is_dir():
            for expected in STAGE_OUTPUTS.get(s, []):
                artifact_path = stage_dir / expected.rstrip("/")
                key = f"{s}:{expected}"
                if key not in agent._known_artifacts and artifact_path.exists():
                    agent._known_artifacts.add(key)
                    if expected not in DISPLAY_ARTIFACTS:
                        continue
                    size = "dir" if artifact_path.is_dir() else f"{artifact_path.stat().st_size / 1024:.1f} KB"
                    content = _extract_artifact_summary(artifact_path, expected)
                    messages.append(msg_artifact(
                        REPO_FOR_STAGE.get(s, "knowledge"), expected, agent.name, size, agent.project_id, content, stage=s,
                    ))
                    messages.append(msg_activity(
                        agent, "file_write",
                        f"📄 产出文件: stage-{s:02d}/{expected} ({size})",
                    ))
            for _f in sorted(stage_dir.rglob("*")):
                if _f.is_file():
                    try:
                        _stage_files.append({
                            "name": str(_f.relative_to(stage_dir)).replace("\\", "/"),
                            "size": _f.stat().st_size,
                        })
                    except OSError:
                        pass
        if _stage_files and agent.project_id:
            messages.append({
                "type": "stage_artifacts",
                "payload": {
                    "projectId": agent.project_id,
                    "stage": s,
                    "stageName": STAGE_NAMES.get(s, f"S{s}"),
                    "files": _stage_files,
                    "agentId": agent.id,
                },
            })
    return messages


def _read_activity_events(agent: LobsterAgent, run_dir: Path) -> list[dict]:
    """Read new lines from activity.jsonl and convert to agent_activity messages."""
    messages: list[dict] = []
    activity_file = run_dir / "activity.jsonl"
    if not activity_file.exists():
        return messages
    try:
        offset = getattr(agent, '_activity_offset', 0)
        size = activity_file.stat().st_size
        if size <= offset:
            return messages
        with open(activity_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    messages.append(msg_activity(
                        agent,
                        evt.get("type", "thinking"),
                        evt.get("summary", ""),
                        evt.get("detail", ""),
                    ))
                except json.JSONDecodeError:
                    pass
            agent._activity_offset = f.tell()
    except OSError:
        pass
    return messages


_LOG_STAGE_RUNNING = re.compile(r"Stage (\d+)/\d+ (\w+) — running")
_LOG_STAGE_DONE = re.compile(r"Stage (\d+)/\d+ (\w+) — done \(([^)]+)\)(?: → (.+))?")
_LOG_WEB_SEARCH = re.compile(r"\[web-search\]|\[search\]|Literature sites", re.IGNORECASE)
_LOG_IMP = re.compile(r"^IMP-\d+:")
_LOG_HARDWARE = re.compile(r"^Hardware advisory:")


def _read_log_activities(agent: LobsterAgent, run_dir: Path) -> list[dict]:
    """Parse the agent's stdout log file and convert structured lines to activity events."""
    messages: list[dict] = []
    log_pattern = run_dir / f"agent_{agent.id}.log"
    if not log_pattern.exists():
        return messages
    try:
        offset = getattr(agent, '_log_activity_offset', 0)
        size = log_pattern.stat().st_size
        if size <= offset:
            return messages
        with open(log_pattern, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                m = _LOG_STAGE_RUNNING.search(line)
                if m:
                    messages.append(msg_activity(
                        agent, "thinking",
                        f"⏳ 开始 S{m.group(1)} {m.group(2)}",
                    ))
                    continue
                m = _LOG_STAGE_DONE.search(line)
                if m:
                    outputs = m.group(4) or ""
                    messages.append(msg_activity(
                        agent, "stage_transition",
                        f"✅ S{m.group(1)} {m.group(2)} 完成 ({m.group(3)})",
                        detail=f"产出: {outputs}" if outputs else "",
                    ))
                    continue
                if _LOG_WEB_SEARCH.search(line):
                    messages.append(msg_activity(
                        agent, "tool_call",
                        f"🔍 {line[:100]}",
                    ))
                    continue
                if _LOG_IMP.match(line):
                    messages.append(msg_activity(
                        agent, "thinking",
                        f"💡 {line[:120]}",
                    ))
                    continue
                if _LOG_HARDWARE.match(line):
                    messages.append(msg_activity(
                        agent, "thinking",
                        f"🖥️ {line[:120]}",
                    ))
                    continue
            agent._log_activity_offset = f.tell()
    except OSError:
        pass
    return messages


def _check_approval_requests(agent: LobsterAgent, run_dir: Path) -> list[dict]:
    """Check if agent wrote a pending_approval.json and broadcast approval_request."""
    messages: list[dict] = []
    approval_file = run_dir / "pending_approval.json"
    if not approval_file.exists():
        return messages
    try:
        data = json.loads(approval_file.read_text(encoding="utf-8"))
        if data.get("_handled"):
            return messages
        req_id = data.get("request_id", _uid())
        messages.append(msg_approval_request(
            agent,
            req_id,
            data.get("action_type", "file_write"),
            data.get("description", "Action requires approval"),
            data.get("detail", ""),
        ))
        agent.status = "awaiting_approval"
        messages.append(msg_agent_update(agent))
        data["_handled"] = True
        approval_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass
    return messages


class _AgentFileHandler(FileSystemEventHandler):
    """Watchdog handler that pushes file changes into an agent's message queue."""

    def __init__(self, agent: LobsterAgent) -> None:
        self.agent = agent

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        self._process_event(event)

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        self._process_event(event)

    def _process_event(self, event) -> None:
        path = Path(event.src_path)
        if path.name.endswith("_session.json"):
            self._handle_session_update(path)
        elif path.name == "pending_approval.json":
            self._handle_approval_request(path)

    def _handle_session_update(self, path: Path) -> None:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            stage_dir = path.parent
            stage_num = 0
            try:
                stage_num = int(stage_dir.name.replace("stage-", ""))
            except ValueError:
                pass
            msg = msg_stage_session_update(self.agent, stage_num, data)
            self.agent._watchdog_messages.append(msg)
        except Exception:
            pass

    def _handle_approval_request(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("_handled"):
                return
            req_id = data.get("request_id", _uid())
            msg = msg_approval_request(
                self.agent,
                req_id,
                data.get("action_type", "file_write"),
                data.get("description", "Action requires approval"),
                data.get("detail", ""),
            )
            self.agent._watchdog_messages.append(msg)
            self.agent.status = "awaiting_approval"
            self.agent._watchdog_messages.append(msg_agent_update(self.agent))
        except Exception:
            pass


def msg_stage_session_update(agent: LobsterAgent, stage: int, session_data: dict) -> dict:
    """Construct a stage_session_update WebSocket message."""
    return {
        "type": "stage_session_update",
        "payload": {
            "projectId": agent.project_id,
            "agentId": agent.id,
            "stage": stage,
            "stageName": session_data.get("stage_name", ""),
            "status": session_data.get("status", "pending"),
            "elapsedSec": session_data.get("elapsed_sec", 0),
            "llmCalls": session_data.get("llm_calls", 0),
            "sandboxRuns": session_data.get("sandbox_runs", 0),
            "phaseLog": session_data.get("phase_log", []),
            "artifacts": session_data.get("artifacts", []),
            "errors": session_data.get("errors", []),
            "metadata": session_data.get("metadata", {}),
        },
    }


def _read_session_updates(agent: LobsterAgent, run_dir: Path) -> list[dict]:
    """Read *_session.json files under stage-* directories and emit stage_session_update messages when changed."""
    messages: list[dict] = []
    if not run_dir.exists():
        return messages
    for sd in sorted(run_dir.glob("stage-*")):
        if not sd.is_dir():
            continue
        session_files = list(sd.glob("*_session.json"))
        if not session_files:
            continue
        sf = session_files[0]
        try:
            raw = sf.read_text(encoding="utf-8")
            checksum = hashlib.md5(raw.encode()).hexdigest()
            key = str(sf.relative_to(run_dir))
            prev = agent._prev_session_checksums.get(key)
            if prev == checksum:
                continue
            agent._prev_session_checksums[key] = checksum
            data = json.loads(raw)
            stage_num = int(sd.name.replace("stage-", ""))
            messages.append(msg_stage_session_update(agent, stage_num, data))
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return messages


def _save_steering(project_id: str, layer: str, instruction: str, state: "BridgeState") -> int:
    """Write steering instruction to all matching agent run_dirs. Returns count of agents steered."""
    count = 0
    for agent in state.agents:
        if agent.project_id != project_id:
            continue
        if layer != "all" and agent.layer != layer:
            continue
        if not agent.run_dir:
            continue
        steering_path = Path(agent.run_dir) / "steering.json"
        try:
            steering_path.write_text(
                json.dumps({"instruction": instruction, "timestamp": _now_ms()}, ensure_ascii=False),
                encoding="utf-8",
            )
            count += 1
        except OSError:
            pass
    return count


def poll_agent(agent: LobsterAgent, state: "BridgeState | None" = None) -> list[dict]:
    messages: list[dict] = []
    run_dir = Path(agent.run_dir)
    if not run_dir.exists():
        return messages

    # Only read heartbeat/checkpoint if THIS agent's process is running,
    # to avoid cross-contamination when multiple agents share a run_dir.
    if agent.process is not None and agent.process.poll() is None:
        if state is not None:
            _task_meta_poll = _read_project_meta(str(run_dir))
            if not _task_meta_poll:
                _task_meta_poll = _read_project_meta(str(run_dir.parent))
            layer_range = _monitor_stage_range(state, agent, _task_meta_poll)
        else:
            _s7_only = getattr(agent, '_is_idea_factory_s7_only', False)
            layer_range = (7, 7) if _s7_only else LAYER_RANGE.get(agent.layer, (1, 15))

        hb = _read_json(run_dir / "heartbeat.json")
        if hb and hb != agent._prev_heartbeat:
            new_stage = hb.get("last_stage")
            old_stage = agent.current_stage
            if (
                new_stage and new_stage != old_stage
                and new_stage in STAGE_TO_LAYER
                and layer_range[0] <= new_stage <= layer_range[1]
            ):
                agent.current_stage = new_stage
                agent.current_task = f"Stage {new_stage}: {STAGE_NAMES.get(new_stage, '?')}"
                agent.status = "working"
                if new_stage not in agent.stage_progress or agent.stage_progress[new_stage] != "completed":
                    agent.stage_progress[new_stage] = "running"
                messages.append(msg_agent_update(agent))
                messages.append(msg_stage_update(agent.id, new_stage, "running"))
                messages.append(msg_log(agent, f"开始 {STAGE_NAMES.get(new_stage, f'S{new_stage}')}", "info", new_stage))
                # Synthesize activity event for the timeline
                messages.append(msg_activity(
                    agent, "stage_transition",
                    f"开始阶段 S{new_stage}: {STAGE_NAMES.get(new_stage, '?')}",
                ))
            # Emit a thinking activity from heartbeat data if present
            _hb_status = hb.get("status", "")
            if _hb_status and _hb_status not in ("idle",):
                messages.append(msg_activity(agent, "thinking", f"Agent 心跳: {_hb_status}"))
            agent._prev_heartbeat = hb

        cp = _read_json(run_dir / "checkpoint.json")
        if cp and cp != agent._prev_checkpoint:
            done_up_to = cp.get("last_completed_stage", 0)
            _prev_done = (agent._prev_checkpoint or {}).get("last_completed_stage", 0) if agent._prev_checkpoint else 0
            messages.extend(_sync_completed_stages(agent, run_dir, layer_range, done_up_to))
            agent._prev_checkpoint = cp

            # Emit activity events for newly completed stages
            for _cs in range(_prev_done + 1, done_up_to + 1):
                if layer_range[0] <= _cs <= layer_range[1]:
                    messages.append(msg_activity(
                        agent, "stage_transition",
                        f"✅ 阶段 S{_cs} ({STAGE_NAMES.get(_cs, '?')}) 已完成",
                    ))

            if agent.current_stage and done_up_to >= agent.current_stage and done_up_to < layer_range[1]:
                next_stage = done_up_to + 1
                if next_stage in STAGE_TO_LAYER and layer_range[0] <= next_stage <= layer_range[1]:
                    agent.current_stage = next_stage
                    agent.current_task = f"Stage {next_stage}: {STAGE_NAMES.get(next_stage, '?')}"
                    agent.stage_progress[next_stage] = "running"
                    messages.append(msg_agent_update(agent))
                    messages.append(msg_stage_update(agent.id, next_stage, "running"))
                    messages.append(msg_log(agent, f"开始 {STAGE_NAMES.get(next_stage, f'S{next_stage}')}", "info", next_stage))
                    messages.append(msg_activity(
                        agent, "stage_transition",
                        f"开始阶段 S{next_stage}: {STAGE_NAMES.get(next_stage, '?')}",
                    ))

        # P0: Read activity.jsonl for timeline events
        messages.extend(_read_activity_events(agent, run_dir))

        # P0: Parse agent log file for structured activity events
        messages.extend(_read_log_activities(agent, run_dir))

        # P0: Check for pending approval requests
        messages.extend(_check_approval_requests(agent, run_dir))

        # Phase 1: Read stage session.json for detailed per-stage progress
        messages.extend(_read_session_updates(agent, run_dir))

        # Phase 2: Drain watchdog real-time messages (file event driven)
        if agent._watchdog_messages:
            messages.extend(agent._watchdog_messages)
            agent._watchdog_messages.clear()

    if agent.process is not None:
        retcode = agent.process.poll()
        if retcode is not None:
            # Final read: catch any checkpoint/artifact updates written before exit
            if state is not None:
                _tm_final = _read_project_meta(str(run_dir))
                if not _tm_final:
                    _tm_final = _read_project_meta(str(run_dir.parent))
                layer_range = _monitor_stage_range(state, agent, _tm_final)
            else:
                _s7_only_final = getattr(agent, '_is_idea_factory_s7_only', False)
                layer_range = (7, 7) if _s7_only_final else LAYER_RANGE.get(agent.layer, (1, 15))
            cp = _read_json(run_dir / "checkpoint.json")
            if cp:
                done_up_to = cp.get("last_completed_stage", 0)
                messages.extend(_sync_completed_stages(agent, run_dir, layer_range, done_up_to))
                agent._prev_checkpoint = cp

            if retcode == 0:
                # Post-flight: validate outputs for each completed stage
                _layer_r = layer_range
                _last_done = (cp or {}).get("last_completed_stage", _layer_r[1])
                for _vs in range(_layer_r[0], min(_last_done, _layer_r[1]) + 1):
                    _missing_out = _validate_stage_outputs(str(run_dir), _vs)
                    if _missing_out:
                        _mo_str = ", ".join(_missing_out)
                        messages.append(msg_log(
                            agent,
                            f"⚠ S{_vs} 产出不完整: 缺少 {_mo_str}",
                            "warning", _vs,
                        ))
                agent.status = "done"
                agent.current_task = ""
                agent.current_stage = None
                messages.append(msg_agent_update(agent))
                messages.append(msg_log(agent, f"层任务完成 (project={agent.project_id})", "success"))
                messages.append(msg_activity(agent, "stage_transition", f"All stages completed for {agent.layer}"))
            else:
                agent.status = "error"
                agent.current_task = f"exit code={retcode}"
                messages.append(msg_agent_update(agent))
                messages.append(msg_log(agent, f"进程异常 (code={retcode})", "error"))
                messages.append(msg_activity(agent, "error", f"Process exited with code {retcode}"))
            agent.process = None
            # P0: Close TaskGraph node for this agent
            if agent.assigned_task_id and agent.project_id and state is not None:
                _graph = state.task_graphs.get(agent.project_id)
                if _graph:
                    if agent.status == "done":
                        _graph.mark_done(agent.assigned_task_id)
                    else:
                        _graph.mark_failed(agent.assigned_task_id)
                    _proj_dir = state.projects_dir() / agent.project_id
                    state.task_graphs.save_to_disk(agent.project_id, _proj_dir)
                    messages.append({"type": "task_graph_update", "payload": {
                        "projectId": agent.project_id, **_graph.to_dict(),
                    }})

    return messages


# ── Agent lifecycle ─────────────────────────────────────────────────────────

def create_agent(state: BridgeState, name: str, layer: str) -> LobsterAgent:
    agent = LobsterAgent(
        id=f"L-{_uid()}", name=name, layer=layer,
        run_id="", run_dir="", config_path="",
        stage_progress={s: "pending" for s in LAYER_STAGES.get(layer, [])},
    )
    state.agents[agent.id] = agent
    return agent


_POOL_NAMES = {"idea": "L1", "experiment": "L2", "coding": "L3", "execution": "L4", "writing": "L5"}


def resize_agent_pool(state: BridgeState, layer_counts: dict[str, int]) -> list[dict]:
    """Dynamically resize the agent pool per layer. Returns messages for the frontend."""
    messages: list[dict] = []
    for layer in _LAYER_ORDER:
        desired = max(1, min(layer_counts.get(layer, 0), 5))
        current = [a for a in state.agents.values() if a.layer == layer]
        current_count = len(current)

        if current_count < desired:
            for i in range(current_count, desired):
                tag = chr(ord('A') + i)
                name = f"{_POOL_NAMES.get(layer, 'L?')}·{tag}"
                agent = create_agent(state, name, layer)
                messages.append(msg_agent_update(agent))
                messages.append(msg_log(agent, f"动态添加 agent → {layer} 层 (共 {desired} 个)", "info"))
        elif current_count > desired:
            idle_agents = [a for a in current if a.status == "idle" and a.process is None]
            to_remove = idle_agents[:current_count - desired]
            for agent in to_remove:
                state.agents.pop(agent.id, None)
                messages.append({"type": "agent_removed", "payload": {"id": agent.id}})
                sys_agent = LobsterAgent(id="system", name="系统", layer=layer, run_id="", run_dir="", config_path="")
                messages.append(msg_log(sys_agent, f"缩减 {layer} 层 agent: {agent.name}", "info"))

        # Rename remaining agents for consistency (A, B, C...)
        remaining = sorted(
            [a for a in state.agents.values() if a.layer == layer],
            key=lambda a: a.name,
        )
        for idx, agent in enumerate(remaining):
            tag = chr(ord('A') + idx)
            new_name = f"{_POOL_NAMES.get(layer, 'L?')}·{tag}"
            if agent.name != new_name:
                agent.name = new_name
                messages.append(msg_agent_update(agent))

    return messages


def _assign_task_to_agent(agent: LobsterAgent, task: Task) -> None:
    """Common setup when assigning a task to an agent."""
    if not hasattr(agent, '_base_name'):
        agent._base_name = agent.name  # type: ignore[attr-defined]

    agent.project_id = task.project_id
    agent.run_dir = task.run_dir
    agent.run_id = task.project_id
    agent.config_path = task.config_path
    agent._base_config_path = task.config_path  # type: ignore[attr-defined]
    agent.assigned_task_id = task.id
    agent._topic = task.topic
    agent.status = "working"
    layer_stages = LAYER_STAGES.get(agent.layer, [])
    agent.stage_progress = {s: "pending" for s in layer_stages}
    agent.current_stage = layer_stages[0] if layer_stages else 0
    agent.current_task = f"准备执行 [{task.project_id}]"
    agent._prev_heartbeat = {}
    agent._prev_checkpoint = {}
    agent._known_artifacts = set()

    # Lab mode: extract role tag from topic pattern "[RoleName] topic"
    import re as _re
    _role_match = _re.match(r"^\[(.+?)\]\s", task.topic or "")
    agent.role_tag = _role_match.group(1) if _role_match else ""
    agent._task_stage_from = task.stage_from  # type: ignore[attr-defined]
    agent._task_stage_to = task.stage_to  # type: ignore[attr-defined]


def _passthrough_agent(agent: LobsterAgent) -> list[dict]:
    """For passthrough layers (e.g. coding): read existing artifacts, mark done immediately."""
    messages: list[dict] = []
    run_dir = Path(agent.run_dir)
    layer_range = LAYER_RANGE.get(agent.layer, (1, 15))

    for s in range(layer_range[0], layer_range[1] + 1):
        stage_dir = run_dir / f"stage-{s:02d}"
        if stage_dir.is_dir():
            agent.stage_progress[s] = "completed"
            messages.append(msg_stage_update(agent.id, s, "completed"))
            messages.append(msg_log(agent, f"{STAGE_NAMES.get(s, f'S{s}')} 结果已就绪 (由上层产出)", "success", s))
            for expected in STAGE_OUTPUTS.get(s, []):
                artifact_path = stage_dir / expected.rstrip("/")
                key = f"{s}:{expected}"
                if key not in agent._known_artifacts and artifact_path.exists():
                    agent._known_artifacts.add(key)
                    if expected not in DISPLAY_ARTIFACTS:
                        continue
                    size = "dir" if artifact_path.is_dir() else f"{artifact_path.stat().st_size / 1024:.1f} KB"
                    content = _extract_artifact_summary(artifact_path, expected)
                    messages.append(msg_artifact(
                        REPO_FOR_STAGE.get(s, "codebase"), expected, agent.name, size, agent.project_id, content, stage=s,
                    ))
        else:
            agent.stage_progress[s] = "failed"
            messages.append(msg_log(agent, f"S{s} 产物未找到 (stage-{s:02d}/ 不存在)", "warning", s))

    agent.status = "done"
    agent.current_task = ""
    agent.current_stage = None
    messages.append(msg_agent_update(agent))
    messages.append(msg_log(agent, f"验收完成 (project={agent.project_id})", "success"))
    return messages


def _validate_stage_inputs(run_dir: str, from_stage: int) -> list[str]:
    """Check that required input files exist before starting a stage.

    Returns a list of missing file descriptions (empty = all good).
    """
    required = STAGE_INPUTS.get(from_stage, [])
    if not required:
        return []
    missing: list[str] = []
    rd = Path(run_dir)
    for inp in required:
        is_dir = inp.endswith("/")
        found = False
        for s in range(from_stage - 1, 0, -1):
            stage_dir = rd / f"stage-{s:02d}"
            target = stage_dir / inp.rstrip("/")
            if is_dir:
                if target.is_dir() and any(target.iterdir()):
                    found = True
                    break
            else:
                if target.is_file() and target.stat().st_size > 0:
                    found = True
                    break
        if not found:
            missing.append(inp)
    return missing


def _validate_stage_outputs(run_dir: str, stage: int) -> list[str]:
    """Check that expected outputs exist after a stage completes.

    Returns a list of missing output descriptions.
    """
    expected = STAGE_OUTPUTS.get(stage, [])
    if not expected:
        return []
    missing: list[str] = []
    stage_dir = Path(run_dir) / f"stage-{stage:02d}"
    if not stage_dir.exists():
        return [f"stage-{stage:02d}/ directory"]
    for out in expected:
        is_dir = out.endswith("/")
        target = stage_dir / out.rstrip("/")
        if is_dir:
            if not target.is_dir() or not any(target.iterdir()):
                missing.append(out)
        else:
            if not target.is_file() or target.stat().st_size == 0:
                missing.append(out)
    return missing


def _diagnose_failure(agent: "LobsterAgent") -> tuple[str, str]:
    """Read the agent's log tail and classify the failure.

    Returns (category, detail) where category is one of:
      config_not_found, missing_input, api_error, timeout, import_error, unknown
    """
    log_path = Path(agent.run_dir) / f"agent_{agent.id}.log" if agent.run_dir else None
    log_tail = ""
    if log_path and log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-30:])
        except Exception:
            pass

    lower = log_tail.lower()
    if "config file not found" in lower or "config" in lower and "not found" in lower:
        return "config_not_found", log_tail.strip().splitlines()[-1] if log_tail.strip() else ""
    if "missing input" in lower or "missing required" in lower or "filenotfounderror" in lower:
        return "missing_input", log_tail.strip().splitlines()[-1] if log_tail.strip() else ""
    if "api" in lower and ("error" in lower or "timeout" in lower or "429" in lower or "rate" in lower):
        return "api_error", log_tail.strip().splitlines()[-1] if log_tail.strip() else ""
    if "timed out" in lower or "timeout" in lower:
        return "timeout", log_tail.strip().splitlines()[-1] if log_tail.strip() else ""
    if "modulenotfounderror" in lower or "importerror" in lower:
        return "import_error", log_tail.strip().splitlines()[-1] if log_tail.strip() else ""
    if "connection" in lower and ("refused" in lower or "error" in lower or "reset" in lower):
        return "api_error", log_tail.strip().splitlines()[-1] if log_tail.strip() else ""
    return "unknown", log_tail.strip().splitlines()[-1] if log_tail.strip() else ""


def _auto_recover(state: "BridgeState", agent: "LobsterAgent",
                  category: str, task: "Task | None") -> tuple[bool, str]:
    """Attempt automatic recovery based on failure category.

    Returns (recovered: bool, action_taken: str).
    """
    if not task:
        return False, ""

    if category == "config_not_found":
        if task.config_path and not Path(task.config_path).exists():
            try:
                _base = _generate_config_from_template(state, task.project_id, task.topic)
                _g = state.global_llm_config
                if _g and _g.get("model") and Path(_base).exists():
                    _base = _create_model_config(
                        _base, _g["model"], task.run_dir,
                        base_url=_g.get("base_url", ""),
                        api_key=_g.get("api_key", ""),
                    )
                task.config_path = _base
                proj_dir = state.projects_dir() / task.project_id
                if proj_dir.exists():
                    _update_project_meta(proj_dir / "project_meta.json", {"config_path": _base})
                return True, f"配置文件已重新生成: {Path(_base).name}"
            except Exception as e:
                return False, f"配置重建失败: {e}"

    if category == "api_error":
        _g = state.global_llm_config
        if _g and _g.get("model"):
            return True, f"API 错误，将延迟 10 秒后使用全局模型 ({_g['model']}) 重试"
        return True, "API 错误，将延迟 10 秒后重试"

    if category == "missing_input":
        return False, "前置产物缺失，需要人工检查"

    if category == "timeout":
        return True, "超时错误，将增加超时预算后重试"

    return False, ""


def launch_agent_for_task(state: BridgeState, agent: LobsterAgent, task: Task) -> list[dict]:
    """Assign a task to an agent. Passthrough layers skip process launch."""
    messages: list[dict] = []
    _assign_task_to_agent(agent, task)

    # Passthrough layers: just verify artifacts and mark done
    if agent.layer in PASSTHROUGH_LAYERS:
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"领取任务 [{task.project_id}] 验收 S10 代码产物", "info"))
        messages.extend(_passthrough_agent(agent))
        return messages

    # Normal layers: launch ScholarLab process
    # Discussion mode: L1 runs S1-S7 only (S8 runs after discussion)
    # Reproduce mode skips discussion → runs full S1-S8
    _task_meta = _read_project_meta(task.run_dir) if task.run_dir else None
    # Lab mode: run_dir is a sub-dir; layer_models lives in the parent project meta
    if not _task_meta and task.run_dir:
        _parent_dir = str(Path(task.run_dir).parent)
        _task_meta = _read_project_meta(_parent_dir)
    _is_reproduce = _task_meta.get("mode") == "reproduce" if _task_meta else False

    # Per-layer model override with connectivity test and fallback
    _layer_models = (_task_meta or {}).get("layer_models", {})
    _layer_cfg = _layer_models.get(agent.layer) if isinstance(_layer_models, dict) else None
    if _layer_cfg:
        _lm_model, _lm_url, _lm_key = _extract_layer_model_fields(_layer_cfg)
        if _lm_model or _lm_url or _lm_key:
            _reachable = _test_model_config_sync(_lm_url, _lm_key, _lm_model)
            if _reachable:
                try:
                    task.config_path = _create_model_config(
                        task.config_path, _lm_model, task.run_dir,
                        base_url=_lm_url, api_key=_lm_key,
                    )
                    _desc = _lm_model or _lm_url or "custom"
                    messages.append(msg_log(agent, f"层级模型覆盖: {agent.layer} → {_desc}", "info"))
                except Exception as _e:
                    messages.append(msg_log(agent, f"层级模型覆盖失败: {_e}，使用默认模型", "warning"))
            else:
                _desc = _lm_model or _lm_url or "custom"
                messages.append(msg_log(agent, f"层级模型不可用 ({_desc})，尝试从其他层回退...", "warning"))
                _fallback_applied = False
                for _fb_layer in _LAYER_ORDER:
                    if _fb_layer == agent.layer:
                        continue
                    _fb_cfg = _layer_models.get(_fb_layer)
                    if not _fb_cfg:
                        continue
                    _fb_model, _fb_url, _fb_key = _extract_layer_model_fields(_fb_cfg)
                    if not (_fb_model or _fb_url or _fb_key):
                        continue
                    if _test_model_config_sync(_fb_url, _fb_key, _fb_model):
                        try:
                            task.config_path = _create_model_config(
                                task.config_path, _fb_model, task.run_dir,
                                base_url=_fb_url, api_key=_fb_key,
                            )
                            _fb_desc = _fb_model or _fb_url or "custom"
                            messages.append(msg_log(agent, f"回退成功: 使用 {_fb_layer} 层模型 ({_fb_desc})", "info"))
                            _fallback_applied = True
                            break
                        except Exception:
                            continue
                if not _fallback_applied:
                    _g = state.global_llm_config
                    if _g and _g.get("model") and task.config_path and task.run_dir and Path(task.config_path).exists():
                        if _test_model_config_sync(_g.get("base_url", ""), _g.get("api_key", ""), _g["model"]):
                            try:
                                task.config_path = _create_model_config(
                                    task.config_path, _g["model"], task.run_dir,
                                    base_url=_g.get("base_url", ""), api_key=_g.get("api_key", ""),
                                )
                                messages.append(msg_log(agent, f"回退到全局模型: {_g['model']}", "info"))
                                _fallback_applied = True
                            except Exception:
                                pass
                    if not _fallback_applied:
                        messages.append(msg_log(agent, f"所有层级模型均不可用，使用默认模型", "warning"))
    else:
        _g = state.global_llm_config
        if _g and _g.get("model") and task.config_path and task.run_dir and Path(task.config_path).exists():
            _g_reachable = _test_model_config_sync(_g.get("base_url", ""), _g.get("api_key", ""), _g["model"])
            if _g_reachable:
                try:
                    task.config_path = _create_model_config(
                        task.config_path, _g["model"], task.run_dir,
                        base_url=_g.get("base_url", ""), api_key=_g.get("api_key", ""),
                    )
                    messages.append(msg_log(agent, f"使用全局模型: {_g['model']}", "info"))
                except Exception as _e:
                    messages.append(msg_log(agent, f"全局模型配置失败: {_e}，使用默认模型", "warning"))
    fs, ts = _effective_stage_range_for_launch(
        state, agent, task, _task_meta,
        is_discussion_s8=False,
    )

    # Checkpoint-aware resume: skip already-completed stages within this layer
    cp = _read_json(Path(task.run_dir) / "checkpoint.json")
    if cp:
        last_done = cp.get("last_completed_stage", 0)
        resume_stage = last_done + 1

        # Discussion mode: if S1-S7 already done, skip straight to discussion/S8
        if state.discussion_mode and agent.layer == "idea" and not _is_reproduce and last_done >= ts:
            for s in range(fs, ts + 1):
                agent.stage_progress[s] = "completed"
            messages.append(msg_log(
                agent,
                f"S1-S7 已完成 (checkpoint={last_done}), 跳过重跑 → 直接进入讨论/S8",
                "info",
            ))
            messages.extend(_skip_discussion_proceed_s8(state, agent))
            return messages

        if fs <= resume_stage <= ts:
            for s in range(fs, resume_stage):
                if s in STAGE_TO_LAYER:
                    agent.stage_progress[s] = "completed"
            fs = resume_stage
            agent.current_stage = fs
            agent.current_task = f"断点恢复 → {STAGE_NAMES.get(fs, f'S{fs}')}"
            messages.append(msg_agent_update(agent))
            messages.append(msg_log(
                agent,
                f"断点恢复: 跳过已完成阶段, 从 {STAGE_NAMES.get(fs, f'S{fs}')} 开始",
                "info",
            ))

    # Pre-flight: verify config file exists; auto-recover if missing
    if not task.config_path or not Path(task.config_path).exists():
        _old_cfg = task.config_path or "(empty)"
        try:
            _base_cfg = _generate_config_from_template(state, task.project_id, task.topic)
            _g_llm = state.global_llm_config
            if _g_llm and _g_llm.get("model") and Path(_base_cfg).exists():
                _base_cfg = _create_model_config(
                    _base_cfg, _g_llm["model"], task.run_dir,
                    base_url=_g_llm.get("base_url", ""), api_key=_g_llm.get("api_key", ""),
                )
            task.config_path = _base_cfg
            agent.config_path = _base_cfg
            messages.append(msg_log(agent, f"配置文件 {_old_cfg} 不存在，已自动重建", "warning"))
        except Exception as _cfg_err:
            agent.status = "error"
            agent.current_task = f"配置恢复失败: {_cfg_err}"
            messages.append(msg_agent_update(agent))
            messages.append(msg_log(agent, f"配置文件缺失且无法重建: {_cfg_err}", "error"))
            return messages

    # Pre-flight: validate required inputs for the starting stage
    _missing_inputs = _validate_stage_inputs(task.run_dir, fs)
    if _missing_inputs:
        _miss_str = ", ".join(_missing_inputs)
        messages.append(msg_log(
            agent,
            f"⚠ S{fs} 缺少前置产物: {_miss_str}，跳过不存在的可选输入继续运行",
            "warning",
        ))

    cmd = [
        state.python_path, "-m", "researchclaw", "run",
        "--config", task.config_path,
        "--output", task.run_dir,
        "--from-stage", STAGE_NAMES.get(fs, str(fs)),
        "--to-stage", STAGE_NAMES.get(ts, str(ts)),
        "--auto-approve",
        "--skip-preflight",
    ]
    if task.topic:
        cmd.extend(["--topic", task.topic])

    try:
        proc_env = _agent_subprocess_env(state, agent)

        log_path = Path(task.run_dir) / f"agent_{agent.id}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env=proc_env,
        )
        agent.process = proc
        agent.current_task = f"项目 {task.project_id} · PID={proc.pid}"
        # Start watchdog observer for real-time file push
        if task.run_dir:
            _rd = Path(task.run_dir)
            if _rd.exists():
                try:
                    handler = _AgentFileHandler(agent)
                    observer = Observer()
                    observer.schedule(handler, str(_rd), recursive=True)
                    observer.start()
                    agent._observer = observer
                except Exception:
                    pass
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"领取任务 [{task.project_id}] 启动 S{fs}→S{ts} (PID={proc.pid})", "info"))
        messages.append(msg_activity(
            agent, "tool_call",
            f"🚀 启动 Agent 进程 PID={proc.pid}，执行 S{fs}→S{ts}",
            detail=f"config: {Path(task.config_path).name}, run_dir: {task.run_dir}",
        ))
    except Exception as e:
        agent.status = "error"
        agent.current_task = f"启动失败: {e}"
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"启动失败: {e}", "error"))

    return messages


def stop_agent(agent: LobsterAgent) -> list[dict]:
    messages: list[dict] = []
    if agent.process and agent.process.poll() is None:
        agent.process.terminate()
        try:
            agent.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.process.kill()
    # Stop watchdog observer
    if agent._observer is not None:
        try:
            agent._observer.stop()
            agent._observer.join(timeout=2)
        except Exception:
            pass
        agent._observer = None
    agent.process = None
    agent.status = "idle"
    agent.current_task = ""
    agent.current_stage = None
    agent.assigned_task_id = None
    messages.append(msg_agent_update(agent))
    messages.append(msg_log(agent, "Agent 已停止", "warning"))
    return messages


# ── Task queue operations ───────────────────────────────────────────────────

def _save_project_meta(run_dir: str, project_id: str, config_path: str, topic: str,
                       mode: str = "lab", layer_models: dict[str, str] | None = None,
                       workspace_dir: str = "") -> None:
    """Persist project metadata so it can be recovered on restart."""
    meta_path = Path(run_dir) / "project_meta.json"
    existing = _read_json(meta_path) if meta_path.exists() else {}
    meta: dict = existing or {}
    meta["project_id"] = project_id
    meta["config_path"] = config_path
    meta["topic"] = topic
    meta["mode"] = mode
    if workspace_dir:
        meta["workspace_dir"] = workspace_dir
    if "created_at" not in meta:
        meta["created_at"] = _now_ms()
    if layer_models:
        meta["layer_models"] = layer_models
    _write_json(meta_path, meta)

    # Also persist a portable config inside the user's workspace folder so the
    # project can be recovered when the same folder is opened again.
    ws = workspace_dir or meta.get("workspace_dir", "")
    if ws:
        _save_workspace_config(ws, project_id, config_path, topic, mode, layer_models)


def _update_project_meta(meta_path: Path, updates: dict) -> None:
    """Merge *updates* into an existing project_meta.json."""
    meta = _read_json(meta_path) or {}
    meta.update(updates)
    _write_json(meta_path, meta)


def _read_project_meta(run_dir: str) -> dict | None:
    return _read_json(Path(run_dir) / "project_meta.json")


def _save_workspace_config(workspace_dir: str, project_id: str, config_path: str,
                           topic: str, mode: str = "lab",
                           layer_models: dict | None = None) -> None:
    """Write .scholarlab/project.json inside the user's workspace folder.

    This allows ScholarLab to detect and restore a previous project when the
    same folder is opened again, avoiding duplicate project IDs.
    """
    ws = Path(workspace_dir)
    if not ws.exists():
        return
    sl_dir = ws / ".scholarlab"
    sl_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = sl_dir / "project.json"
    existing = _read_json(cfg_path) or {}
    existing.update({
        "project_id": project_id,
        "config_path": config_path,
        "topic": topic,
        "mode": mode,
        "updated_at": _now_ms(),
    })
    if "created_at" not in existing:
        existing["created_at"] = _now_ms()
    if layer_models:
        existing["layer_models"] = layer_models
    _write_json(cfg_path, existing)


def _load_workspace_config(workspace_dir: str) -> dict | None:
    """Read .scholarlab/project.json from a workspace folder, if it exists."""
    cfg = Path(workspace_dir) / ".scholarlab" / "project.json"
    if cfg.exists():
        return _read_json(cfg)
    return None


def _recover_config_path(state: "BridgeState", project_id: str,
                         proj_dir: Path, meta: dict | None) -> str:
    """Try to recover a config_path from alternative sources.

    Search order:
      1. .scholar/project_meta.json (planner mode workspace)
      2. workspace .scholarlab/project.json
      3. project_configs/<project_id>.yaml on disk
      4. Any config_*.yaml inside proj_dir
    """
    # 1. Check .scholar/ via _workspace_link
    link = _read_json(proj_dir / "_workspace_link.json")
    if link and link.get("scholar_dir"):
        scholar_meta = _read_json(Path(link["scholar_dir"]) / "project_meta.json")
        if scholar_meta and scholar_meta.get("config_path"):
            cp = scholar_meta["config_path"]
            if Path(cp).exists():
                return cp

    # 2. Check workspace .scholarlab/project.json
    ws_dir = (meta or {}).get("workspace_dir", "")
    if not ws_dir and link:
        ws_dir = link.get("workspace_dir", "")
    if ws_dir:
        ws_cfg = _load_workspace_config(ws_dir)
        if ws_cfg and ws_cfg.get("config_path"):
            cp = ws_cfg["config_path"]
            if Path(cp).exists():
                return cp

    # 3. Check runs/project_configs/<pid>.yaml
    cfg_dir = Path(state.runs_base_dir) / "project_configs"
    candidate = cfg_dir / f"{project_id}.yaml"
    if candidate.exists():
        return str(candidate)

    # 4. Any config_*.yaml inside proj_dir
    for f in proj_dir.glob("config_*.yaml"):
        return str(f)

    return ""


def _find_existing_project_by_workspace(state: "BridgeState", workspace_dir: str) -> str | None:
    """Check if any existing project already uses this workspace folder.

    Returns the project_id if found, None otherwise. Checks both the workspace
    .scholarlab/project.json and the runs/projects/*/project_meta.json.
    """
    if not workspace_dir:
        return None
    ws_norm = Path(workspace_dir).resolve()

    # Fast path: check .scholarlab/project.json in the workspace
    ws_cfg = _load_workspace_config(workspace_dir)
    if ws_cfg and ws_cfg.get("project_id"):
        pid = ws_cfg["project_id"]
        proj_dir = state.projects_dir() / pid
        if proj_dir.exists():
            return pid

    # Slow path: scan all projects' meta for matching workspace_dir
    projects_dir = state.projects_dir()
    if not projects_dir.exists():
        return None
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        meta = _read_json(proj_dir / "project_meta.json")
        if meta and meta.get("workspace_dir"):
            try:
                if Path(meta["workspace_dir"]).resolve() == ws_norm:
                    return proj_dir.name
            except Exception:
                pass
        link = _read_json(proj_dir / "_workspace_link.json")
        if link and link.get("workspace_dir"):
            try:
                if Path(link["workspace_dir"]).resolve() == ws_norm:
                    return proj_dir.name
            except Exception:
                pass
    return None


def _update_project_layer_models(state: BridgeState, project_id: str,
                                  layer_models: dict) -> bool:
    """Hot-update layer_models in a project's meta without affecting running tasks.
    Next task launch will pick up the updated config automatically."""
    proj_dir = state.projects_dir() / project_id
    meta_path = proj_dir / "project_meta.json"
    meta = _read_json(meta_path)
    if meta is None:
        return False
    cleaned = {k: v for k, v in layer_models.items() if isinstance(v, dict) and any(v.values())} if layer_models else {}
    meta["layer_models"] = cleaned
    _write_json(meta_path, meta)
    return True


def _determine_resume_target(run_dir: str) -> tuple[str, int] | None:
    """Read checkpoint and return (target_layer, next_stage) or None if no checkpoint."""
    cp = _read_json(Path(run_dir) / "checkpoint.json")
    if not cp:
        return None
    last_done = cp.get("last_completed_stage", 0)
    if last_done <= 0:
        return None
    next_stage = last_done + 1
    if next_stage > 22:
        return None
    target_layer = STAGE_TO_LAYER.get(next_stage)
    if not target_layer:
        return None
    return (target_layer, next_stage)


def _queue_for_layer(target_layer: str) -> str:
    """Return the input queue name that feeds into target_layer."""
    if target_layer == "idea":
        return "init_to_idea"
    return LAYER_INPUT_QUEUE.get(target_layer, "init_to_idea")


def submit_new_project(state: BridgeState, project_id: str, config_path: str, topic: str = "", mode: str = "lab") -> list[dict]:
    """Submit a project — auto-detects checkpoint and resumes from where it left off.

    In cross-project discussion mode, each project gets ONE agent.
    After S7, agents from different projects discuss with each other.
    """
    messages: list[dict] = []
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    run_dir = str(state.projects_dir() / project_id)
    os.makedirs(run_dir, exist_ok=True)
    _save_project_meta(run_dir, project_id, config_path, topic, mode=mode)

    if state.discussion_mode:
        messages.append(msg_log(
            sys_agent,
            f"新项目 [{project_id}] 跨 project 讨论模式: 分配 1 个 agent, S7 后与其他 project agent 讨论",
            "info", DISCUSSION_STAGE,
        ))

    # Check for checkpoint to enable resume
    resume_info = _determine_resume_target(run_dir)
    if resume_info:
        target_layer, next_stage = resume_info
        queue_name = _queue_for_layer(target_layer)
        source_layer = {
            "idea": "init", "experiment": "idea", "coding": "experiment",
            "execution": "coding", "writing": "execution",
        }.get(target_layer, "init")

        task = Task(
            id=f"task-{_uid()}", project_id=project_id, run_dir=run_dir,
            config_path=config_path, topic=topic,
            source_layer=source_layer, target_layer=target_layer,
            created_at=_now_ms(),
        )
        state.queues[queue_name].push(task)
        stage_name = STAGE_NAMES.get(next_stage, f"S{next_stage}")
        messages.append(msg_log(
            sys_agent,
            f"项目 [{project_id}] 检测到断点 → 从 {stage_name} (Stage {next_stage}) 恢复",
            "success",
        ))
    else:
        task = Task(
            id=f"task-{_uid()}", project_id=project_id, run_dir=run_dir,
            config_path=config_path, topic=topic,
            source_layer="init", target_layer="idea",
            created_at=_now_ms(),
        )
        state.queues["init_to_idea"].push(task)
        messages.append(msg_log(sys_agent, f"新项目 [{project_id}] 已加入调研队列", "info"))

    messages.append(msg_queue_update(state.queues))
    return messages


def _check_s12_sanity_failure(state: "BridgeState", agent: "LobsterAgent") -> list[dict]:
    """If S12 sanity_report.json shows fail, pause the project and return notification messages."""
    messages: list[dict] = []
    _s12_dir = Path(agent.run_dir) / "stage-12"
    _sanity_path = _s12_dir / "sanity_report.json"
    if not _sanity_path.exists():
        return messages
    try:
        _sanity = json.loads(_sanity_path.read_text(encoding="utf-8"))
    except Exception:
        return messages
    if _sanity.get("status") != "fail":
        return messages

    _fix_log_path = _s12_dir / "fix_log.json"
    _exp_dir = None
    for _sd in sorted(Path(agent.run_dir).glob("stage-11*"), reverse=True):
        _ed = _sd / "experiment"
        if _ed.exists():
            _exp_dir = str(_ed)
            break

    _last_error = ""
    _iters = _sanity.get("iterations", [])
    if _iters:
        _last_iter = _iters[-1]
        _failed_checks = [c for c in _last_iter.get("checks", []) if not c.get("passed")]
        if _failed_checks:
            _fc = _failed_checks[-1]
            _last_error = (_fc.get("stderr_tail") or _fc.get("stderr") or "")[-800:]

    _detail = (
        f"⚠️ S12 SANITY_CHECK 循环修复失败，需要手动介入\n"
        f"项目: {agent.project_id}\n"
        f"修复轮次: {_sanity.get('total_iterations', '?')}/{_sanity.get('max_fix_iterations', '?')}\n"
        f"实验代码: {_exp_dir or 'N/A'}\n"
        f"修复日志: {_fix_log_path}\n"
        f"检查报告: {_sanity_path}"
    )
    if _last_error:
        _detail += f"\n最后报错:\n{_last_error}"

    # Persist intervention reason so the frontend can display it
    _meta_path = Path(agent.run_dir) / "project_meta.json"
    if _meta_path.exists():
        try:
            _meta = json.loads(_meta_path.read_text(encoding="utf-8"))
            _meta["intervention"] = _detail
            _meta_path.write_text(json.dumps(_meta, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    messages.append(msg_log(agent, _detail, "error", 12))
    messages.extend(_pause_project(state, agent.project_id))
    messages.append(msg_project_list(list_all_projects(state)))
    return messages


def on_agent_done(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """When an agent finishes, complete its task and create a follow-up task for the next layer."""
    messages: list[dict] = []

    state._fail_counts.pop(agent.project_id, None)  # reset fail counter on success

    # Complete assigned task
    if agent.assigned_task_id:
        for q in state.queues.values():
            q.complete(agent.assigned_task_id)

    # Discussion mode: L1 agent completed S7 → enter discussion
    # Reproduce mode skips discussion entirely
    _agent_proj_dir = state.projects_dir() / agent.project_id if agent.project_id else None
    _agent_meta = _read_project_meta(str(_agent_proj_dir)) if _agent_proj_dir and _agent_proj_dir.exists() else None
    _agent_is_reproduce = _agent_meta.get("mode") == "reproduce" if _agent_meta else False
    if (
        state.discussion_mode
        and agent.layer == "idea"
        and not _agent_is_reproduce
        and _agent_requires_discussion_before_s8(agent)
    ):
        state.discussion_waiting[agent.id] = agent
        agent.current_stage = DISCUSSION_STAGE
        agent.stage_progress[DISCUSSION_STAGE] = "running"
        agent.status = "waiting_discussion"
        agent.current_task = "S7 完成，等待讨论伙伴..."
        messages.append(msg_agent_update(agent))

        pid = agent.project_id
        expected_count = state.lab_batches.get(pid, 0)

        if expected_count >= 2:
            # Lab mode: same project_id, wait for all N agents to finish S7
            waiting_same_proj = [
                a for a in state.discussion_waiting.values()
                if a.project_id == pid
            ]
            if len(waiting_same_proj) >= expected_count:
                messages.append(msg_log(
                    agent,
                    f"项目 [{pid}] 全部 {len(waiting_same_proj)} 个方向 S7 完成 → 启动跨领域讨论",
                    "info", DISCUSSION_STAGE,
                ))
                group = DiscussionGroup(
                    project_id=pid,
                    topic=getattr(agent, '_topic', '') or agent.current_task,
                    config_path=getattr(agent, '_base_config_path', agent.config_path),
                    agent_ids=[a.id for a in waiting_same_proj],
                    run_dirs={a.id: a.run_dir for a in waiting_same_proj},
                )
                for a in waiting_same_proj:
                    state.discussion_waiting.pop(a.id, None)
                    group.completed_s7.add(a.id)
                state.discussion_groups[pid] = group
                messages.extend(_trigger_discussion(state, group))
            else:
                messages.append(msg_log(
                    agent,
                    f"S7 完成，等待同项目其他方向 ({len(waiting_same_proj)}/{expected_count})",
                    "info", DISCUSSION_STAGE,
                ))
            return messages

        # Non-Lab: cross-project pair discussion (original logic)
        # 1) Find another agent that also completed S7 (different project)
        peers = [a for a in state.discussion_waiting.values()
                 if a.id != agent.id and a.project_id != agent.project_id]

        if peers:
            peer = peers[0]
            messages.append(msg_log(agent, f"S7 完成，与 [{peer.project_id}] 的 agent 开始跨 project 讨论", "info", DISCUSSION_STAGE))
            messages.extend(_trigger_cross_project_discussion(state, agent, peer))
            return messages

        # 2) Find an idle agent (no project, can act as reviewer/critic)
        idle_agents = [
            a for a in state.agents.values()
            if a.layer == "idea" and a.status == "idle"
            and a.id != agent.id and not a.assigned_task_id
        ]
        if idle_agents:
            reviewer = idle_agents[0]
            reviewer.status = "discussing"
            reviewer.current_stage = DISCUSSION_STAGE
            reviewer.current_task = f"讨论评审: [{agent.project_id}]"
            reviewer.stage_progress[DISCUSSION_STAGE] = "running"
            messages.append(msg_agent_update(reviewer))
            messages.append(msg_log(agent, f"S7 完成，与空闲 agent [{reviewer.name}] 开始讨论评审", "info", DISCUSSION_STAGE))
            messages.extend(_trigger_cross_project_discussion(state, agent, reviewer))
            return messages

        # 3) No peer available at all — skip discussion
        messages.append(msg_log(agent, "S7 完成，无可用讨论伙伴，跳过讨论直接进入 S8", "info", DISCUSSION_STAGE))
        messages.extend(_skip_discussion_proceed_s8(state, agent))
        return messages

    # L3 (coding) → check S12 sanity_report: if failed, pause and notify user
    if agent.layer == "coding" and agent.project_id:
        _s12_msgs = _check_s12_sanity_failure(state, agent)
        if _s12_msgs:
            messages.extend(_s12_msgs)
            return messages

    # Create follow-up task in the next queue
    output_queue_name = LAYER_OUTPUT_QUEUE.get(agent.layer)
    if output_queue_name and output_queue_name in state.queues and agent.project_id:
        # L4→L5: only push if S17 PROCEED
        if agent.layer == "execution" and output_queue_name == "execution_to_writing":
            _decision_file = Path(agent.run_dir) / "stage-17" / "decision.md"
            _warning_file = Path(agent.run_dir) / "quality_warning.txt"
            _summary_file = Path(agent.run_dir) / "pipeline_summary.json"
            _is_proceed = False
            if _decision_file.exists():
                _dec_text = _decision_file.read_text(encoding="utf-8").upper()
                _is_proceed = "PROCEED" in _dec_text and "REFINE" not in _dec_text.split("PROCEED")[0][-50:]
            if not _is_proceed and _warning_file.exists():
                _warn_text = _warning_file.read_text(encoding="utf-8")
                if "max pivots" in _warn_text.lower():
                    _is_proceed = True
                    messages.append(msg_log(agent, "S17 决策为 REFINE 但已达最大迭代次数，强制进入论文写作", "warning"))
            if not _is_proceed and _summary_file.exists():
                try:
                    import json as _json
                    _summary = _json.loads(_summary_file.read_text(encoding="utf-8"))
                    if _summary.get("final_status") == "done" and _summary.get("stages_failed", 1) == 0:
                        _is_proceed = True
                        messages.append(msg_log(agent, "Pipeline 全部完成，进入论文写作", "info"))
                except Exception:
                    pass
            if not _is_proceed:
                messages.append(msg_log(agent, f"S17 决策非 PROCEED，跳过论文写作", "info"))
                output_queue_name = None

        if output_queue_name and output_queue_name in state.queues:
            _, target_layer = QUEUE_NAMES[output_queue_name]
            _base_cfg = getattr(agent, '_base_config_path', agent.config_path)
            follow_task = Task(
                id=f"task-{_uid()}",
                project_id=agent.project_id,
                run_dir=agent.run_dir,
                config_path=_base_cfg,
                topic=getattr(agent, '_topic', ''),
                source_layer=agent.layer,
                target_layer=target_layer,
                created_at=_now_ms(),
            )
            state.queues[output_queue_name].push(follow_task)
            messages.append(msg_log(
                agent,
                f"任务完成 → 项目 [{agent.project_id}] 已加入 {output_queue_name} 队列",
                "success",
            ))

    # Release GPU allocation for execution layer
    if agent.layer == "execution" and agent.project_id:
        released = state.gpu_allocator.release(agent.project_id)
        if released:
            messages.append(msg_log(agent, f"GPU {released} 已释放", "info"))

    # ── Sync outputs to workspace when writing layer finishes ──
    if agent.layer == "writing" and agent.project_id:
        try:
            proj_dir = state.projects_dir() / agent.project_id
            meta = _read_project_meta(str(proj_dir)) or {}
            ws_dir = meta.get("workspace_dir", "")
            if not ws_dir:
                # Also check sub-project parent
                parent = proj_dir.parent
                parent_meta = _read_project_meta(str(parent)) if parent != state.projects_dir() else None
                ws_dir = (parent_meta or {}).get("workspace_dir", "") if parent_meta else ""
            if ws_dir:
                copied = _sync_outputs_to_workspace(proj_dir, ws_dir)
                if copied:
                    messages.append(msg_log(
                        agent,
                        f"产出已同步到工作区: {ws_dir}/scholar_output/ ({', '.join(copied)})",
                        "success",
                    ))
        except Exception as _ws_err:
            messages.append(msg_log(agent, f"工作区同步警告: {_ws_err}", "warning"))

    # Reset agent for next task
    _reset_agent_idle(agent)
    messages.append(msg_agent_update(agent))
    messages.append(msg_queue_update(state.queues))

    return messages


def _reset_agent_idle(agent: LobsterAgent) -> None:
    """Reset agent to idle state, clearing all project-related fields."""
    agent.assigned_task_id = None
    agent.project_id = ""
    agent.status = "idle"
    agent.current_task = "等待任务..."
    agent.run_id = ""
    agent.run_dir = ""
    agent.config_path = ""
    agent.current_stage = 0
    agent.stage_progress = {}
    agent.role_tag = ""
    agent.process = None
    base = getattr(agent, '_base_name', None)
    if base:
        agent.name = base
    agent._task_stage_from = None  # type: ignore[attr-defined]
    agent._task_stage_to = None  # type: ignore[attr-defined]


def list_all_projects(state: BridgeState) -> list[dict]:
    """Scan runs/projects/ and return status info for all projects."""
    projects_dir = state.projects_dir()
    result: list[dict] = []
    if not projects_dir.exists():
        return result

    running_project_ids: set[str] = set()
    for a in state.agents.values():
        if a.project_id and a.process is not None and a.process.poll() is None:
            running_project_ids.add(a.project_id)

    queued_project_ids: set[str] = set()
    for q in state.queues.values():
        for t in q.tasks:
            if t.status in ("pending", "assigned") and t.project_id:
                queued_project_ids.add(t.project_id)

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        project_id = proj_dir.name
        meta = _read_json(proj_dir / "project_meta.json")
        project_mode = meta.get("mode", "lab") if meta else "lab"

        # Read checkpoint: top-level first, then aggregate from sub-runs (Lab mode)
        cp = _read_json(proj_dir / "checkpoint.json")
        if not cp:
            best_cp = None
            best_stage = 0
            for sub in proj_dir.iterdir():
                if sub.is_dir() and sub.name.startswith("run-"):
                    sub_cp = _read_json(sub / "checkpoint.json")
                    if sub_cp and sub_cp.get("last_completed_stage", 0) > best_stage:
                        best_stage = sub_cp.get("last_completed_stage", 0)
                        best_cp = sub_cp
            cp = best_cp

        last_stage = cp.get("last_completed_stage", 0) if cp else 0
        last_name = cp.get("last_completed_name", "") if cp else ""
        timestamp = cp.get("timestamp", "") if cp else ""

        first_stage = 1
        total_stages = 26
        completed_threshold = total_stages

        if last_stage >= completed_threshold:
            status = "completed"
        elif project_id in running_project_ids:
            status = "running"
        elif project_id in queued_project_ids:
            status = "queued"
        elif last_stage > 0:
            status = "interrupted"
        else:
            status = "new"

        topic = ""
        config_path = ""
        if meta:
            topic = meta.get("topic", "")
            config_path = meta.get("config_path", "")
        if not topic:
            goal_path = proj_dir / "stage-01" / "goal.md"
            if not goal_path.exists():
                for sub in proj_dir.iterdir():
                    if sub.is_dir() and sub.name.startswith("run-"):
                        goal_path = sub / "stage-01" / "goal.md"
                        if goal_path.exists():
                            break
            if goal_path.exists():
                try:
                    topic = goal_path.read_text(encoding="utf-8")[:300]
                except OSError:
                    pass

        intervention = meta.get("intervention", "") if meta else ""
        layer_models_raw = meta.get("layer_models", {}) if meta else {}
        project_name = meta.get("project_name", "") if meta else ""
        workspace_dir = ""
        if meta:
            workspace_dir = str(meta.get("workspace_dir", "") or "")
        if not workspace_dir:
            pointer = _read_json(proj_dir / "_workspace_link.json")
            workspace_dir = str((pointer or {}).get("workspace_dir", "") or "")

        result.append({
            "projectId": project_id,
            "projectName": project_name,
            "status": status,
            "lastCompletedStage": last_stage,
            "lastCompletedName": last_name,
            "firstStage": first_stage,
            "totalStages": total_stages,
            "timestamp": timestamp,
            "topic": topic,
            "configPath": config_path,
            "projectDir": str(proj_dir.resolve()),
            "workspaceDir": workspace_dir,
            "intervention": intervention,
            "layerModels": layer_models_raw if layer_models_raw else {},
        })

    return result


def _archive_manifest_path(state: BridgeState, archive_id: str) -> Path:
    return state.archives_dir() / archive_id / "manifest.json"


def list_project_archives(state: BridgeState) -> list[dict]:
    """Return saved project snapshots, newest first."""
    archives_dir = state.archives_dir()
    if not archives_dir.exists():
        return []
    items: list[dict] = []
    for archive_dir in archives_dir.iterdir():
        if not archive_dir.is_dir():
            continue
        manifest = _read_json(archive_dir / "manifest.json")
        if not isinstance(manifest, dict):
            continue
        manifest.setdefault("archiveId", archive_dir.name)
        items.append(manifest)
    return sorted(items, key=lambda x: x.get("createdAt", 0), reverse=True)


def _resolve_project_folder(state: BridgeState, project_id: str, target: str = "auto") -> Path:
    """Resolve the folder users should inspect for a project.

    target="auto" prefers the user's workspace if present, then falls back to
    ScholarLab's runs/projects/<project_id> directory.
    """
    project_id = project_id.strip()
    if not project_id:
        raise ValueError("project_id is required")
    project_dir = state.projects_dir() / project_id
    if not project_dir.is_dir():
        raise FileNotFoundError(f"Project not found: {project_id}")

    if target in ("auto", "workspace"):
        meta = _read_json(project_dir / "project_meta.json") or {}
        workspace = str(meta.get("workspace_dir", "") or "")
        if not workspace:
            pointer = _read_json(project_dir / "_workspace_link.json")
            workspace = str((pointer or {}).get("workspace_dir", "") or "")
        if workspace and Path(workspace).is_dir():
            return Path(workspace).resolve()
        if target == "workspace":
            raise FileNotFoundError(f"Workspace folder not found for project: {project_id}")

    return project_dir.resolve()


def _open_folder_in_file_manager(path: Path) -> None:
    """Open a local folder in the OS file manager."""
    path = path.resolve()
    if os.name == "nt":
        # `cmd /c start` delegates to the interactive shell and is more
        # reliable than os.startfile/explorer.exe when agent_bridge itself was
        # launched as a hidden background process by start.ps1.
        subprocess.Popen(["cmd.exe", "/c", "start", "", str(path)])
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def archive_project(state: BridgeState, project_id: str) -> dict:
    """Create a restorable snapshot of runs/projects/<project_id>."""
    project_id = project_id.strip()
    if not project_id:
        raise ValueError("project_id is required")
    project_dir = state.projects_dir() / project_id
    if not project_dir.is_dir():
        raise FileNotFoundError(f"Project not found: {project_id}")

    meta = _read_json(project_dir / "project_meta.json") or {}
    timestamp = _now_ms()
    archive_id = f"{project_id}__{timestamp}"
    archive_dir = state.archives_dir() / archive_id
    archive_project_dir = archive_dir / "project"
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project_dir, archive_project_dir)

    manifest = {
        "archiveId": archive_id,
        "projectId": project_id,
        "projectName": meta.get("project_name", ""),
        "topic": meta.get("topic", ""),
        "mode": meta.get("mode", "lab"),
        "createdAt": timestamp,
        "sourceDir": str(project_dir),
    }
    _write_json(archive_dir / "manifest.json", manifest)
    return manifest


def restore_project_archive(state: BridgeState, archive_id: str, overwrite: bool = False) -> dict:
    """Restore an archived project snapshot into runs/projects."""
    archive_id = archive_id.strip()
    manifest = _read_json(_archive_manifest_path(state, archive_id))
    if not isinstance(manifest, dict):
        raise FileNotFoundError(f"Archive not found: {archive_id}")

    project_id = str(manifest.get("projectId") or "").strip()
    if not project_id:
        raise ValueError("Archive manifest missing projectId")
    archived_project_dir = state.archives_dir() / archive_id / "project"
    if not archived_project_dir.is_dir():
        raise FileNotFoundError(f"Archived project payload missing: {archive_id}")

    target_dir = state.projects_dir() / project_id
    if target_dir.exists():
        if not overwrite:
            raise FileExistsError(f"Project already exists: {project_id}")
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(archived_project_dir, target_dir)
    _update_project_meta(target_dir / "project_meta.json", {"restored_from_archive": archive_id, "restored_at": _now_ms()})
    return {"archiveId": archive_id, "projectId": project_id}


def resume_project(state: BridgeState, project_id: str) -> list[dict]:
    """Resume a project from its last checkpoint."""
    messages: list[dict] = []
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    state._fail_counts.pop(project_id, None)  # reset fail counter on manual resume

    proj_dir = state.projects_dir() / project_id
    if not proj_dir.exists():
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 不存在", "error"))
        return messages

    for a in state.agents.values():
        if a.project_id == project_id and a.process is not None and a.process.poll() is None:
            messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已在运行中", "warning"))
            return messages

    meta = _read_json(proj_dir / "project_meta.json")
    config_path = meta.get("config_path", "") if meta else ""
    topic = meta.get("topic", "") if meta else ""
    mode = meta.get("mode", "lab") if meta else "lab"

    # Clear intervention flag on resume
    if meta and meta.get("intervention"):
        meta.pop("intervention", None)
        try:
            (proj_dir / "project_meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    if not config_path:
        config_path = _recover_config_path(state, project_id, proj_dir, meta)
        if config_path:
            _update_project_meta(proj_dir / "project_meta.json", {"config_path": config_path})

    if not config_path:
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 缺少配置文件路径, 正在重新生成…", "warning"))
        try:
            config_path = _generate_config_from_template(state, project_id, topic or project_id)
            _g_llm = state.global_llm_config
            if _g_llm and _g_llm.get("model") and Path(config_path).exists():
                try:
                    config_path = _create_model_config(
                        config_path, _g_llm["model"], str(proj_dir),
                        base_url=_g_llm.get("base_url", ""),
                        api_key=_g_llm.get("api_key", ""),
                    )
                except Exception:
                    pass
            _update_project_meta(proj_dir / "project_meta.json", {"config_path": config_path})
            messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已重新生成配置文件", "success"))
        except Exception as e:
            messages.append(msg_log(sys_agent, f"项目 [{project_id}] 配置文件恢复失败: {e}", "error"))
            return messages

    if not Path(config_path).exists():
        _base_cfg = _generate_config_from_template(state, project_id, topic)
        _g_llm = state.global_llm_config
        if _g_llm and _g_llm.get("model") and Path(_base_cfg).exists():
            try:
                _base_cfg = _create_model_config(
                    _base_cfg, _g_llm["model"], str(proj_dir),
                    base_url=_g_llm.get("base_url", ""),
                    api_key=_g_llm.get("api_key", ""),
                )
            except Exception:
                pass
        config_path = _base_cfg
        _update_project_meta(proj_dir / "project_meta.json", {"config_path": config_path})

    # Lab mode with run-* sub-directories: resume each angle separately
    angle_dirs = sorted(proj_dir.glob("run-*"))
    if mode == "lab" and angle_dirs:
        task_count = 0
        for angle_dir in angle_dirs:
            if not angle_dir.is_dir():
                continue
            slug = angle_dir.name.removeprefix("run-")
            angle_config_key = f"{project_id}--{slug}"
            angle_config = str(Path(state.runs_base_dir) / "project_configs" / f"{angle_config_key}.yaml")
            if not Path(angle_config).exists():
                angle_config = config_path

            run_dir = str(angle_dir)
            resume_info = _determine_resume_target(run_dir)
            if resume_info:
                target_layer, next_stage = resume_info
                queue_name = _queue_for_layer(target_layer)
                source_layer = {
                    "idea": "init", "experiment": "idea", "coding": "experiment",
                    "execution": "coding", "writing": "execution",
                }.get(target_layer, "init")
                stage_name = STAGE_NAMES.get(next_stage, f"S{next_stage}")
                messages.append(msg_log(
                    sys_agent,
                    f"  方向 [{slug}] 断点恢复 → {stage_name} (Stage {next_stage})",
                    "success",
                ))
            else:
                queue_name = "init_to_idea"
                source_layer = "init"
                target_layer = "idea"
                messages.append(msg_log(sys_agent, f"  方向 [{slug}] 从头开始", "info"))

            task = Task(
                id=f"task-{_uid()}",
                project_id=project_id,
                run_dir=run_dir,
                config_path=angle_config,
                topic=f"[{slug}] {topic}",
                source_layer=source_layer,
                target_layer=target_layer,
                created_at=_now_ms(),
            )
            state.queues[queue_name].push(task)
            task_count += 1

        if task_count >= 2:
            state.lab_batches[project_id] = task_count

        messages.append(msg_queue_update(state.queues))
        messages.append(msg_project_list(list_all_projects(state)))
        messages.append(msg_log(
            sys_agent,
            f"Lab 模式: 项目 [{project_id}] — {task_count} 个方向已恢复",
            "success",
        ))
        return messages

    messages.extend(submit_new_project(state, project_id, config_path, topic, mode=mode))
    return messages


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn arbitrary text into a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    text = text.strip('-')[:max_len].rstrip('-')
    if not text:
        text = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
    return text


def _generate_config_from_template(
    state: BridgeState, project_id: str, topic: str, role_prompt: str = "",
    reference_papers: list[str] | None = None,
    codebases_dir: str = "", datasets_dir: str = "", checkpoints_dir: str = "",
) -> str:
    """Generate a project-specific YAML config from the default template.

    If role_prompt is provided (Lab mode), it's prepended to the topic so the
    pipeline agent operates from that specialist perspective.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    template_path = repo_root / "examples" / "config_template.yaml"
    if not template_path.exists():
        template_path = Path(state.agent_package_dir).parent / "config_template.yaml"
    if not template_path.exists():
        template_path = Path(__file__).resolve().parent.parent / "config_template.yaml"
    if not template_path.exists():
        raise FileNotFoundError(f"Config template not found at {template_path}")

    full_topic = f"{role_prompt}\n\n研究主题: {topic}" if role_prompt else topic

    content = template_path.read_text(encoding="utf-8")
    content = content.replace("__PROJECT_ID__", project_id)
    content = content.replace("__TOPIC__", full_topic.replace('"', '\\"'))

    import re as _re

    def _quote_yaml_string(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    if reference_papers:
        yaml_list = "\n".join(f'    - "{_quote_yaml_string(p)}"' for p in reference_papers)
        new_block = f"  reference_papers:\n{yaml_list}"
    else:
        new_block = "  reference_papers: []"

    if "  reference_papers: __REFERENCE_PAPERS__" in content:
        content = content.replace("  reference_papers: __REFERENCE_PAPERS__", new_block)
    else:
        content = _re.sub(
            r"(?m)^  reference_papers:\n(?:    - .*(?:\n|$))*",
            new_block + "\n",
            content,
            count=1,
        )

    def _to_yaml_path(p: str) -> str:
        """Normalize path for YAML: forward slashes only (safe on Windows too)."""
        return p.replace("\\", "/")

    if codebases_dir:
        _v = _to_yaml_path(codebases_dir)
        content = _re.sub(r'(codebases_dir:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{_v}"', content)
    if datasets_dir:
        _v = _to_yaml_path(datasets_dir)
        content = _re.sub(r'(datasets_dir:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{_v}"', content)
    if checkpoints_dir:
        _v = _to_yaml_path(checkpoints_dir)
        content = _re.sub(r'(checkpoints_dir:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{_v}"', content)

    configs_dir = Path(state.runs_base_dir) / "project_configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    config_path = configs_dir / f"{project_id}.yaml"
    config_path.write_text(content, encoding="utf-8")
    return str(config_path)


def _safe_reference_upload_name(filename: str) -> str:
    base = Path(filename or "reference.pdf").name
    cleaned = re.sub(r"[^\w.\-]+", "_", base).strip("._")
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned or 'reference'}.pdf"
    return cleaned


_WORKSPACE_LATEX_EXT  = {".tex", ".bib", ".sty", ".cls", ".bst"}
_WORKSPACE_CODE_EXT   = {".py", ".m", ".ipynb", ".r", ".jl", ".sh", ".cpp", ".c", ".h"}
_WORKSPACE_DATA_EXT   = {".mat", ".csv", ".tsv", ".json", ".npz", ".npy", ".hdf5", ".h5", ".xlsx"}
_WORKSPACE_PDF_EXT    = {".pdf"}
_WORKSPACE_SKIP_DIRS  = {".git", "__pycache__", "node_modules", ".venv", "scholar_output", "latex_input"}


def _scan_workspace_dir(workspace_dir: str, main_tex_file: str = "") -> dict:
    """Recursively scan a user workspace folder and classify all relevant files.

    Returns a dict with keys:
      tex_files   : list[Path]  — .tex/.bib/.sty/.cls/.bst
      pdf_files   : list[Path]  — .pdf
      code_files  : list[Path]  — .py/.m/.ipynb etc.
      data_files  : list[Path]  — .mat/.csv etc.
      main_tex    : Path | None — the identified main .tex file
      has_latex   : bool
      has_pdf     : bool
      has_code    : bool
      has_data    : bool
    """
    root = Path(workspace_dir)
    result: dict = {
        "tex_files": [], "pdf_files": [], "code_files": [],
        "data_files": [], "main_tex": None,
        "has_latex": False, "has_pdf": False, "has_code": False, "has_data": False,
    }
    if not root.exists() or not root.is_dir():
        return result

    for item in root.rglob("*"):
        if not item.is_file():
            continue
        # Skip hidden dirs and known irrelevant dirs
        parts_set = set(item.relative_to(root).parts[:-1])
        if parts_set & _WORKSPACE_SKIP_DIRS:
            continue
        if any(p.startswith(".") for p in item.relative_to(root).parts[:-1]):
            continue

        ext = item.suffix.lower()
        if ext in _WORKSPACE_LATEX_EXT:
            result["tex_files"].append(item)
        elif ext in _WORKSPACE_PDF_EXT:
            result["pdf_files"].append(item)
        elif ext in _WORKSPACE_CODE_EXT:
            result["code_files"].append(item)
        elif ext in _WORKSPACE_DATA_EXT:
            result["data_files"].append(item)

    result["has_latex"] = bool(result["tex_files"])
    result["has_pdf"]   = bool(result["pdf_files"])
    result["has_code"]  = bool(result["code_files"])
    result["has_data"]  = bool(result["data_files"])

    # Identify main .tex file
    tex_files: list[Path] = result["tex_files"]
    if tex_files:
        if main_tex_file:
            for tf in tex_files:
                if tf.name == main_tex_file or str(tf).endswith(main_tex_file):
                    result["main_tex"] = tf
                    break
        if result["main_tex"] is None:
            # Auto-detect: prefer main.tex, then longest file (usually the master doc)
            candidates = [tf for tf in tex_files if tf.suffix == ".tex"]
            named_main = next((tf for tf in candidates if tf.stem == "main"), None)
            if named_main:
                result["main_tex"] = named_main
            elif candidates:
                result["main_tex"] = max(candidates, key=lambda f: f.stat().st_size)

    return result


def _setup_workspace(
    project_dir: Path,
    workspace_dir: str,
    main_tex_file: str = "",
) -> dict:
    """Scan workspace, copy LaTeX files into latex_input/, write workspace.json.

    Returns a dict with resolved path overrides and scan summary for logging.
    """
    scan = _scan_workspace_dir(workspace_dir, main_tex_file)
    overrides: dict[str, str] = {}
    summary: list[str] = []

    # ── LaTeX files → copy into project's latex_input/ ──
    if scan["has_latex"]:
        latex_dir = project_dir / "latex_input"
        latex_dir.mkdir(parents=True, exist_ok=True)
        for tf in scan["tex_files"]:
            dest = latex_dir / tf.name
            if not dest.exists():
                dest.write_bytes(tf.read_bytes())
        # README for agents
        main_tex_name = scan["main_tex"].name if scan["main_tex"] else "(auto)"
        readme = latex_dir / "_EXISTING_DRAFT_README.md"
        readme.write_text(
            "# Existing LaTeX Draft (from workspace)\n\n"
            f"**Workspace:** `{workspace_dir}`\n"
            f"**Main file:** `{main_tex_name}`\n\n"
            "**IMPORTANT INSTRUCTIONS FOR AGENTS:**\n"
            "- Read all `.tex` files in this directory before writing the paper.\n"
            "- PRESERVE all existing content. Only ADD, EXPAND, and COMPLETE unfinished sections.\n"
            "- Maintain consistent notation, terminology, and writing style with the existing draft.\n"
            "- If a `.bib` file is present, use its citations and add new ones as needed.\n\n"
            f"**LaTeX files copied:** {', '.join(tf.name for tf in scan['tex_files'])}\n",
            encoding="utf-8",
        )
        overrides["codebases_dir"] = str(latex_dir.resolve())
        summary.append(f"LaTeX: {len(scan['tex_files'])} 个文件 (主文件: {main_tex_name})")

    # ── Code/sim files (.py/.m) → point codebases_dir to workspace root ──
    if scan["has_code"] and not overrides.get("codebases_dir"):
        overrides["codebases_dir"] = workspace_dir
        summary.append(f"代码: {len(scan['code_files'])} 个文件 (.py/.m 等)")
    elif scan["has_code"]:
        summary.append(f"代码: {len(scan['code_files'])} 个文件 (.py/.m 等, 工作区可访问)")

    # ── Data files → datasets_dir ──
    if scan["has_data"]:
        overrides["datasets_dir"] = workspace_dir
        summary.append(f"数据: {len(scan['data_files'])} 个文件 (.mat/.csv 等)")

    # ── PDF files → treat as reference uploads (read bytes) ──
    ref_uploads: list[dict[str, str]] = []
    for pdf in scan["pdf_files"][:10]:  # cap at 10 PDFs to avoid huge payloads
        try:
            raw = pdf.read_bytes()
            ref_uploads.append({
                "name": pdf.name,
                "contentBase64": base64.b64encode(raw).decode("ascii"),
            })
        except Exception:
            pass
    if ref_uploads:
        summary.append(f"PDF 参考: {len(ref_uploads)} 个文件")

    # ── Save workspace metadata into project ──
    ws_meta = {
        "workspace_dir": workspace_dir,
        "main_tex_file": scan["main_tex"].name if scan["main_tex"] else "",
        "scan_summary": summary,
        "tex_files": [str(f) for f in scan["tex_files"]],
        "pdf_count": len(scan["pdf_files"]),
        "code_count": len(scan["code_files"]),
        "data_count": len(scan["data_files"]),
    }
    (project_dir / "workspace.json").write_text(
        json.dumps(ws_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "overrides": overrides,
        "ref_uploads": ref_uploads,
        "summary": summary,
    }


def _sync_outputs_to_workspace(project_dir: Path, workspace_dir: str) -> list[str]:
    """Copy final output artifacts to workspace/scholar_output/.

    Returns list of copied file names.
    """
    root = Path(workspace_dir)
    if not root.exists():
        return []
    out_dir = root / "scholar_output"
    out_dir.mkdir(exist_ok=True)

    OUTPUT_GLOBS = ["**/*.zip", "**/paper_revised.md", "**/paper_draft.md",
                    "**/outline.md", "**/latex_package.zip"]
    copied: list[str] = []
    for pattern in OUTPUT_GLOBS:
        for src in project_dir.glob(pattern):
            if "latex_input" in src.parts:
                continue
            dest = out_dir / src.name
            try:
                import shutil as _shutil
                _shutil.copy2(src, dest)
                copied.append(src.name)
            except Exception:
                pass
    return list(dict.fromkeys(copied))  # deduplicate


def _persist_latex_uploads(
    project_dir: Path,
    latex_uploads: list[dict[str, str]] | None,
) -> str | None:
    """Save uploaded LaTeX source files to project_dir/latex_input/.

    Returns the absolute path to the latex_input directory, or None if nothing was saved.
    """
    if not latex_uploads:
        return None

    latex_dir = project_dir / "latex_input"
    latex_dir.mkdir(parents=True, exist_ok=True)

    tex_ext = {".tex", ".bib", ".sty", ".cls", ".bst"}
    saved: list[str] = []

    for item in latex_uploads:
        if not isinstance(item, dict):
            continue
        name = Path(str(item.get("name", "main.tex"))).name
        suffix = Path(name).suffix.lower()
        if suffix not in tex_ext:
            continue
        content_b64 = str(item.get("contentBase64", "")).strip()
        if not content_b64:
            continue
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except Exception:
            continue
        target = latex_dir / name
        # Avoid overwriting — add suffix if needed
        if target.exists():
            stem = Path(name).stem
            target = latex_dir / f"{stem}-{uuid.uuid4().hex[:4]}{suffix}"
        target.write_bytes(raw)
        saved.append(name)

    if not saved:
        return None

    # Write an instruction file so agents know this is an existing draft
    readme = latex_dir / "_EXISTING_DRAFT_README.md"
    readme.write_text(
        "# Existing LaTeX Draft\n\n"
        "This directory contains an existing paper draft uploaded by the user.\n"
        "**IMPORTANT INSTRUCTIONS FOR AGENTS:**\n"
        "- Read all `.tex` files in this directory before writing the paper.\n"
        "- The paper outline and draft must PRESERVE all existing content.\n"
        "- Only ADD, EXPAND, and COMPLETE unfinished sections — never remove or shorten existing content.\n"
        "- Maintain consistent notation, terminology, and writing style with the existing draft.\n"
        "- If a `.bib` file is present, use its citations and add new ones as needed.\n\n"
        f"**Uploaded files:** {', '.join(saved)}\n",
        encoding="utf-8",
    )

    return str(latex_dir.resolve())


def _persist_reference_uploads(
    project_dir: Path,
    reference_uploads: list[dict[str, str]] | None,
) -> list[str]:
    if not reference_uploads:
        return []

    upload_dir = project_dir / "reference_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []

    for item in reference_uploads:
        if not isinstance(item, dict):
            continue
        name = _safe_reference_upload_name(str(item.get("name", "reference.pdf")))
        content_b64 = str(item.get("contentBase64", "")).strip()
        if not content_b64:
            continue
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except Exception:
            continue
        stem = Path(name).stem
        suffix = Path(name).suffix or ".pdf"
        target = upload_dir / f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"
        target.write_bytes(raw)
        saved_paths.append(str(target.resolve()))

    return saved_paths


KNOWN_LAB_ANGLES: dict[str, str] = {
    "CV": (
        "你是实验室的「计算机视觉 (CV)」方向研究员。"
        "你的专长是图像识别、目标检测、语义分割、图像生成、视频理解。"
        "请从 CV 的视角进行深入调研，"
        "重点关注: 视觉骨干网络（ViT/CNN/Mamba）、"
        "自监督/对比学习、生成模型（Diffusion/GAN/Flow Matching）、"
        "3D 视觉、视频时序建模、以及 CV 在多模态与具身场景中的应用。"
    ),
    "VLM": (
        "你是实验室的「视觉语言模型 (VLM)」方向研究员。"
        "你的专长是多模态理解、视觉-语言对齐、图文推理、视觉 Grounding。"
        "请从 VLM 的视角进行深入调研，"
        "重点关注: 视觉编码器选型、跨模态融合架构、指令微调策略、"
        "视觉推理能力评估、以及 VLM 在具身场景中的感知与决策应用。"
    ),
    "World Model": (
        "你是实验室的「世界模型 (World Model)」方向研究员。"
        "你的专长是环境建模、视频预测、物理仿真、因果推理。"
        "请从 World Model 的视角进行深入调研，"
        "重点关注: 世界模型的架构设计（自回归/扩散/状态空间）、"
        "时空表征学习、动力学建模、长时序预测、"
        "以及世界模型在具身智能中的规划与想象能力。"
    ),
    "VLA": (
        "你是实验室的「视觉-语言-动作模型 (VLA)」方向研究员。"
        "你的专长是端到端策略学习、动作生成、机器人操作、模仿学习。"
        "请从 VLA 的视角进行深入调研，"
        "重点关注: VLA 模型架构（RT-2、OpenVLA、π₀ 等）、"
        "动作 tokenization 与解码策略、多任务泛化、"
        "sim-to-real 迁移、以及 VLA 在真实机器人上的部署与评估。"
    ),
}

DEFAULT_LAB_ANGLES: list[dict[str, str]] = [
    {"name": "CV", "prompt": KNOWN_LAB_ANGLES["CV"]},
]


def _build_role_prompt(angle_name: str, main_topic: str) -> str:
    """Build a role prompt for a Lab mode agent. Uses predefined prompts for known
    angles, otherwise generates a reasonable prompt from the angle name."""
    if angle_name in KNOWN_LAB_ANGLES:
        return KNOWN_LAB_ANGLES[angle_name]
    return (
        f"你是实验室的「{angle_name}」方向研究员。"
        f"请从 {angle_name} 的专业视角对研究主题进行深入调研，"
        f"重点关注该方向最相关的理论、方法、数据集和最新进展。"
    )


def quick_submit_project(
    state: BridgeState, topic: str, project_id: str = "",
    mode: str = "lab",
    research_angles: list[str] | None = None,
    reference_papers: list[str] | None = None,
    reference_uploads: list[dict[str, str]] | None = None,
    path_overrides: dict[str, str] | None = None,
    latex_uploads: list[dict[str, str]] | None = None,
    workspace_dir: str = "",
    main_tex_file: str = "",
    layer_models: dict[str, str] | None = None,
) -> list[dict]:
    """Create a project from a topic string.

    Modes:
      - "lab": Multi-angle parallel research (default). If no angles provided,
        uses 3 default perspectives. Each agent gets a specialized role prompt.
      - "reproduce": Single-agent focused pipeline for paper reproduction.
    """
    messages: list[dict] = []
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    if not topic.strip():
        messages.append(msg_log(sys_agent, "请输入研究主题", "error"))
        return messages

    base_id = project_id or _slugify(topic)

    # Workspace-level dedup: if this folder already has a project, reuse its ID
    if workspace_dir:
        existing_pid = _find_existing_project_by_workspace(state, workspace_dir)
        if existing_pid:
            base_id = existing_pid

    # ── Reproduce mode: single-agent standard pipeline ──
    if mode == "reproduce":
        if not project_id and not (workspace_dir and _find_existing_project_by_workspace(state, workspace_dir)):
            existing = state.projects_dir() / base_id
            if existing.exists():
                base_id = f"{base_id}-{_uid()[:4]}"
        project_dir = state.projects_dir() / base_id
        project_dir.mkdir(parents=True, exist_ok=True)
        _po = dict(path_overrides or {})

        # ── Workspace folder scan (takes priority over manual path fields) ──
        ws_ref_uploads: list[dict[str, str]] = []
        if workspace_dir:
            ws = _setup_workspace(project_dir, workspace_dir, main_tex_file)
            for k, v in ws["overrides"].items():
                if v and not _po.get(k):
                    _po[k] = v
            ws_ref_uploads = ws["ref_uploads"]
            for line in ws["summary"]:
                messages.append(msg_log(sys_agent, f"[工作区] {line}", "info"))

        saved_reference_paths = _persist_reference_uploads(
            project_dir, list(reference_uploads or []) + ws_ref_uploads
        )
        all_reference_papers = [*(reference_papers or []), *saved_reference_paths]

        latex_dir = _persist_latex_uploads(project_dir, latex_uploads)
        if latex_dir and not _po.get("codebases_dir"):
            _po["codebases_dir"] = latex_dir

        try:
            config_path = _generate_config_from_template(
                state, base_id, topic.strip(),
                reference_papers=all_reference_papers,
                codebases_dir=_po.get("codebases_dir", ""),
                datasets_dir=_po.get("datasets_dir", ""),
                checkpoints_dir=_po.get("checkpoints_dir", ""),
            )
        except Exception as e:
            messages.append(msg_log(sys_agent, f"配置生成失败: {e}", "error"))
            return messages

        # Store workspace dir and layer_models in meta for output sync
        _save_project_meta(str(project_dir), base_id, config_path, topic.strip(),
                           mode="reproduce", layer_models=layer_models,
                           workspace_dir=workspace_dir or "")

        if saved_reference_paths:
            messages.append(msg_log(sys_agent, f"已接收 {len(saved_reference_paths)} 个本地 PDF 参考文件", "info"))
        if latex_dir:
            messages.append(msg_log(sys_agent, f"已接收 LaTeX 草稿，Agent 将基于现有内容继续写作", "info"))
        if workspace_dir:
            messages.append(msg_log(sys_agent, f"工作区: {workspace_dir} — 完成后产出将写入 scholar_output/", "info"))
        messages.append(msg_log(sys_agent, f"复现模式: 项目 [{base_id}] 单 Agent 全流程启动", "success"))
        messages.extend(submit_new_project(state, base_id, config_path, topic.strip(), mode="reproduce"))
        return messages

    # ── Lab mode: parallel research (ONE project, N agents — or 1 agent if single direction) ──
    angles: list[dict[str, str]]
    if research_angles and len(research_angles) >= 1:
        angles = [
            {"name": a.strip(), "prompt": _build_role_prompt(a.strip(), topic.strip())}
            for a in research_angles if a.strip()
        ]
    if not research_angles or not angles:
        angles = DEFAULT_LAB_ANGLES

    # Deduplicate project id (skip if reusing existing workspace project)
    if not (workspace_dir and _find_existing_project_by_workspace(state, workspace_dir)):
        existing = state.projects_dir() / base_id
        if existing.exists():
            base_id = f"{base_id}-{_uid()[:4]}"

    project_dir = state.projects_dir() / base_id
    project_dir.mkdir(parents=True, exist_ok=True)
    _po = dict(path_overrides or {})

    # ── Workspace folder scan ──
    ws_ref_uploads: list[dict[str, str]] = []
    if workspace_dir:
        ws = _setup_workspace(project_dir, workspace_dir, main_tex_file)
        for k, v in ws["overrides"].items():
            if v and not _po.get(k):
                _po[k] = v
        ws_ref_uploads = ws["ref_uploads"]
        for line in ws["summary"]:
            messages.append(msg_log(sys_agent, f"[工作区] {line}", "info"))

    saved_reference_paths = _persist_reference_uploads(
        project_dir, list(reference_uploads or []) + ws_ref_uploads
    )
    all_reference_papers = [*(reference_papers or []), *saved_reference_paths]

    latex_dir = _persist_latex_uploads(project_dir, latex_uploads)
    if latex_dir and not _po.get("codebases_dir"):
        _po["codebases_dir"] = latex_dir

    try:
        project_config_path = _generate_config_from_template(
            state, base_id, topic.strip(),
            reference_papers=all_reference_papers,
            codebases_dir=_po.get("codebases_dir", ""),
            datasets_dir=_po.get("datasets_dir", ""),
            checkpoints_dir=_po.get("checkpoints_dir", ""),
        )
    except Exception as e:
        messages.append(msg_log(sys_agent, f"配置生成失败: {e}", "error"))
        return messages

    _save_project_meta(str(project_dir), base_id, project_config_path, topic.strip(),
                       mode="lab", layer_models=layer_models,
                       workspace_dir=workspace_dir or "")

    messages.append(msg_log(
        sys_agent,
        f"Lab 模式: 项目 [{base_id}] — {len(angles)} 个方向并行调研",
        "info",
    ))
    if saved_reference_paths:
        messages.append(msg_log(sys_agent, f"已接收 {len(saved_reference_paths)} 个本地 PDF 参考文件", "info"))
    if latex_dir:
        messages.append(msg_log(sys_agent, f"已接收 LaTeX 草稿，Agent 将基于现有内容继续写作", "info"))
    if workspace_dir:
        messages.append(msg_log(sys_agent, f"工作区: {workspace_dir} — 完成后产出将写入 scholar_output/", "info"))

    task_count = 0
    for i, angle in enumerate(angles):
        name = angle["name"]
        role_prompt = angle["prompt"]
        slug = _slugify(name, 20)

        # Each direction gets its own run sub-directory within the project
        run_dir = str(project_dir / f"run-{slug}")
        os.makedirs(run_dir, exist_ok=True)

        _po = path_overrides or {}
        try:
            config_path = _generate_config_from_template(
                state, f"{base_id}--{slug}", topic.strip(), role_prompt,
                reference_papers=all_reference_papers,
                codebases_dir=_po.get("codebases_dir", ""),
                datasets_dir=_po.get("datasets_dir", ""),
                checkpoints_dir=_po.get("checkpoints_dir", ""),
            )
        except Exception as e:
            messages.append(msg_log(sys_agent, f"配置生成失败 [{name}]: {e}", "error"))
            continue

        task = Task(
            id=f"task-{_uid()}",
            project_id=base_id,
            run_dir=run_dir,
            config_path=config_path,
            topic=f"[{name}] {topic.strip()}",
            source_layer="init",
            target_layer="idea",
            created_at=_now_ms(),
        )
        state.queues["init_to_idea"].push(task)
        task_count += 1

        messages.append(msg_log(
            sys_agent,
            f"  方向 {i+1}/{len(angles)}: {name}",
            "info",
        ))

    # Register Lab batch: same project_id, expect N agents to finish S7
    if task_count >= 2:
        state.lab_batches[base_id] = task_count

    messages.append(msg_queue_update(state.queues))
    messages.append(msg_project_list(list_all_projects(state)))
    messages.append(msg_log(
        sys_agent,
        f"{task_count} 个方向 agent S7 完成后将自动讨论 → 合并为统一假设 → 进入 L2",
        "success",
    ))
    return messages


def _create_model_config(base_config_path: str, model_name: str, output_dir: str,
                         base_url: str = "", api_key: str = "") -> str:
    """Create a per-agent config file with overridden LLM settings.

    Uses regex replacement on the raw YAML text to preserve file structure,
    comments, and fields that yaml.dump would reorder or drop.
    """
    import re as _re

    with open(base_config_path, "r", encoding="utf-8") as f:
        content = f.read()

    def _yaml_replace(text: str, key: str, value: str) -> str:
        """Replace a YAML value in-place: `key: "old"` → `key: "new"`."""
        # Match key followed by colon, optional spaces, then a quoted or unquoted value
        pattern = rf'({key}:\s*)("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\n#]*)'
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return _re.sub(pattern, rf'\1"{escaped}"', text, count=1)

    if model_name:
        content = _yaml_replace(content, "primary_model", model_name)
        content = _yaml_replace(content, "coding_model", model_name)
        content = _yaml_replace(content, "image_model", model_name)
    if base_url:
        content = _yaml_replace(content, "base_url", base_url)
    if api_key:
        content = _yaml_replace(content, "api_key", api_key)

    safe_name = (model_name or "custom").replace("/", "_").replace(":", "_")
    agent_config_path = str(Path(output_dir) / f"config_{safe_name}.yaml")
    with open(agent_config_path, "w", encoding="utf-8") as f:
        f.write(content)
    return agent_config_path


def _launch_idea_factory_run(state: BridgeState, agent: LobsterAgent, s7_only: bool = False, model_override: str = "") -> list[dict]:
    """Launch L1 agent to produce ideas. s7_only=True runs only S7 (for discussion mode)."""
    messages: list[dict] = []

    idea_id = f"idea-{_uid()}"
    run_dir = str(Path(state.runs_base_dir).parent / "shared_results" / "idea_runs" / idea_id)
    os.makedirs(run_dir, exist_ok=True)

    _s6_seed = Path(run_dir) / "stage-06"
    _s6_seed.mkdir(parents=True, exist_ok=True)
    (_s6_seed / "cards").mkdir(exist_ok=True)

    _s7_seed = Path(run_dir) / "stage-07"
    _s7_seed.mkdir(parents=True, exist_ok=True)

    config_path = state.idea_factory_config
    if model_override:
        try:
            config_path = _create_model_config(state.idea_factory_config, model_override, run_dir)
        except Exception as e:
            messages.append(msg_log(agent, f"模型配置创建失败 ({model_override}): {e}，使用默认配置", "warning"))
            config_path = state.idea_factory_config

    task = Task(
        id=f"task-{_uid()}",
        project_id=idea_id,
        run_dir=run_dir,
        config_path=config_path,
        topic=state.idea_factory_topic,
        source_layer="idea_factory",
        target_layer="idea",
        created_at=_now_ms(),
    )

    _assign_task_to_agent(agent, task)
    agent._is_idea_factory = True  # type: ignore[attr-defined]
    agent._is_idea_factory_s7_only = s7_only  # type: ignore[attr-defined]

    if s7_only:
        layer_range = (7, 7)
    else:
        layer_range = (7, 8)
    fs, ts = layer_range

    cmd = [
        state.python_path, "-m", "researchclaw", "run",
        "--config", config_path,
        "--output", task.run_dir,
        "--from-stage", STAGE_NAMES.get(fs, str(fs)),
        "--to-stage", STAGE_NAMES.get(ts, str(ts)),
        "--auto-approve",
        "--skip-preflight",
        "--topic", state.idea_factory_topic,
    ]

    try:
        log_path = Path(run_dir) / f"agent_{agent.id}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env=_agent_subprocess_env(state, agent),
        )
        agent.process = proc
        n = state.idea_factory_produced + 1
        model_tag = f" [{model_override}]" if model_override else ""
        agent.current_task = f"Idea 工厂 #{n}{model_tag}" + (" (S7 综合)" if s7_only else " (S7→S8)")
        messages.append(msg_agent_update(agent))
        label = f"Idea 工厂 #{n}: 知识综合中 (S7){model_tag}" if s7_only else f"Idea 工厂 #{n}: 生成假设中 (S7→S8){model_tag}"
        messages.append(msg_log(agent, label, "info"))
    except Exception as e:
        agent.status = "error"
        agent.current_task = f"Idea 工厂启动失败: {e}"
        messages.append(msg_agent_update(agent))

    return messages


def _on_idea_factory_done(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """Handle idea factory run completion: extract hypotheses, push to idea pool + L2 queue."""
    messages: list[dict] = []
    run_dir = Path(agent.run_dir)

    # Read hypotheses
    hyp_file = None
    for sd in sorted(run_dir.glob("stage-08*"), reverse=True):
        f = sd / "hypotheses.md"
        if f.exists():
            hyp_file = f
            break

    if hyp_file:
        hyp_text = hyp_file.read_text(encoding="utf-8")

        # Write to idea pool
        pool_dir = Path(state.runs_base_dir).parent / "shared_results" / "idea_pool"
        pool_dir.mkdir(parents=True, exist_ok=True)
        pool_file = pool_dir / "ideas.jsonl"

        entry = {
            "id": agent.project_id,
            "topic": state.idea_factory_topic,
            "hypotheses": hyp_text[:2000],
            "timestamp": _now_ms(),
            "status": "pending",
        }
        with open(pool_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Create L2 task from this idea
        idea_run_dir = str(Path(state.runs_base_dir) / "projects" / agent.project_id)
        os.makedirs(idea_run_dir, exist_ok=True)

        # Copy hypotheses to the project run dir so L2 can find it
        s8_dir = Path(idea_run_dir) / "stage-08"
        s8_dir.mkdir(parents=True, exist_ok=True)
        (s8_dir / "hypotheses.md").write_text(hyp_text, encoding="utf-8")

        # Also copy synthesis if available
        for sd in sorted(run_dir.glob("stage-07*"), reverse=True):
            sf = sd / "synthesis.md"
            if sf.exists():
                s7_dir = Path(idea_run_dir) / "stage-07"
                s7_dir.mkdir(parents=True, exist_ok=True)
                (s7_dir / "synthesis.md").write_text(sf.read_text(encoding="utf-8"), encoding="utf-8")
                break

        # Push to idea_to_experiment queue
        queue = state.queues.get("idea_to_experiment")
        if queue:
            follow_task = Task(
                id=f"task-{_uid()}",
                project_id=agent.project_id,
                run_dir=idea_run_dir,
                config_path=state.idea_factory_config,
                topic=state.idea_factory_topic,
                source_layer="idea",
                target_layer="experiment",
                created_at=_now_ms(),
            )
            queue.push(follow_task)
            messages.append(msg_log(agent, f"Idea #{state.idea_factory_produced + 1} → 实验设计队列", "success"))

        state.idea_factory_produced += 1
        if state.idea_factory_remaining > 0:
            state.idea_factory_remaining -= 1

        messages.append(msg_log(
            agent,
            f"Idea 工厂: 已产出 {state.idea_factory_produced} 个, 剩余 {'无限' if state.idea_factory_remaining == -1 else state.idea_factory_remaining}",
            "info",
        ))
    else:
        messages.append(msg_log(agent, "Idea 工厂: 未生成假设", "warning"))

    # Reset agent
    _reset_agent_idle(agent)
    agent._is_idea_factory = False  # type: ignore[attr-defined]
    agent._is_idea_factory_s7_only = False  # type: ignore[attr-defined]
    agent._is_discussion_s8 = False  # type: ignore[attr-defined]
    agent._idea_factory_batch_id = None  # type: ignore[attr-defined]
    messages.append(msg_agent_update(agent))

    return messages


def _on_idea_factory_s7_done(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """Handle idea factory S7-only completion: enter discussion flow."""
    messages: list[dict] = []
    agent._is_idea_factory_s7_only = False  # type: ignore[attr-defined]

    batch_id = getattr(agent, '_idea_factory_batch_id', None)
    if not batch_id or batch_id not in state.discussion_groups:
        messages.append(msg_log(agent, "S7 完成但无沟通讨论组，回退到非讨论模式", "warning"))
        agent._is_idea_factory = False  # type: ignore[attr-defined]
        _reset_agent_idle(agent)
        messages.append(msg_agent_update(agent))
        return messages

    group = state.discussion_groups[batch_id]
    group.completed_s7.add(agent.id)
    agent.status = "waiting_discussion"
    agent.current_stage = DISCUSSION_STAGE
    agent.current_task = f"等待沟通讨论 ({len(group.completed_s7)}/{len(group.agent_ids)})"
    agent.stage_progress[DISCUSSION_STAGE] = "running"
    messages.append(msg_agent_update(agent))
    messages.append(msg_stage_update(agent.id, DISCUSSION_STAGE, "running"))
    messages.append(msg_log(agent, f"S7 完成，等待沟通讨论 ({len(group.completed_s7)}/{len(group.agent_ids)})", "info", DISCUSSION_STAGE))

    if group.all_ready():
        messages.extend(_trigger_discussion(state, group))

    return messages


def _trigger_discussion(state: BridgeState, group: DiscussionGroup) -> list[dict]:
    """Launch the discussion runner when all agents in a group have completed S7."""
    messages: list[dict] = []
    group.status = "discussing"

    disc_dir = str(state.projects_dir() / group.project_id / "discussion")
    os.makedirs(disc_dir, exist_ok=True)
    group.discussion_output_dir = disc_dir

    for aid in group.agent_ids:
        agent = state.agents.get(aid)
        if agent:
            agent.status = "discussing"
            agent.current_stage = DISCUSSION_STAGE
            agent.current_task = "多 Agent 沟通讨论中..."
            messages.append(msg_agent_update(agent))

    synthesis_dirs = group.synthesis_dirs()
    mirror_run_dirs = [rd for rd in group.run_dirs.values() if rd]
    runner_path = str(Path(__file__).resolve().parent / "discussion_runner.py")
    cmd = [
        state.python_path, runner_path,
        "--config", group.config_path,
        "--synthesis-dirs", *synthesis_dirs,
        "--output", disc_dir,
        "--rounds", str(state.discussion_rounds),
    ]
    if mirror_run_dirs:
        cmd.append("--mirror-run-dirs")
        cmd.extend(mirror_run_dirs)
    if group.topic:
        cmd.extend(["--topic", group.topic])

    try:
        log_path = Path(disc_dir) / "discussion.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env=_agent_subprocess_env(state, agent),
        )
        group.discussion_process = proc
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(
            sys_agent,
            f"项目 [{group.project_id}] 沟通讨论开始: {len(group.agent_ids)} 个 agent, {state.discussion_rounds} 轮 (PID={proc.pid})",
            "info", DISCUSSION_STAGE,
        ))
    except Exception as e:
        group.status = "done"
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(sys_agent, f"沟通讨论启动失败: {e}", "error", DISCUSSION_STAGE))
        for aid in group.agent_ids:
            agent = state.agents.get(aid)
            if agent:
                agent.status = "error"
                agent.current_task = f"沟通讨论启动失败: {e}"
                messages.append(msg_agent_update(agent))

    return messages


def _trigger_cross_project_discussion(
    state: BridgeState, agent1: LobsterAgent, agent2: LobsterAgent,
) -> list[dict]:
    """Launch a discussion between two agents from different projects."""
    messages: list[dict] = []

    for a in (agent1, agent2):
        a.status = "discussing"
        a.current_task = f"跨 project 讨论: {agent1.project_id} × {agent2.project_id}"
        messages.append(msg_agent_update(a))

    p1_id = agent1.project_id or agent1.name
    p2_id = agent2.project_id or agent2.name
    disc_name = f"{p1_id}_x_{p2_id}"
    disc_dir = str(state.projects_dir() / "_cross_discussions" / disc_name)
    os.makedirs(disc_dir, exist_ok=True)

    synthesis_dirs = []
    for a in (agent1, agent2):
        s7 = Path(a.run_dir) / "stage-07" if a.run_dir else None
        if s7 and s7.exists():
            synthesis_dirs.append(str(s7))

    group = DiscussionGroup(
        project_id=disc_name,
        topic=f"{p1_id} | {p2_id}",
        config_path=getattr(agent1, '_base_config_path', agent1.config_path),
    )
    group.agent_ids = [agent1.id, agent2.id]
    group.run_dirs = {agent1.id: agent1.run_dir, agent2.id: agent2.run_dir}
    group.status = "discussing"
    group.discussion_output_dir = disc_dir
    group._cross_project = True  # type: ignore[attr-defined]

    runner_path = str(Path(__file__).resolve().parent / "discussion_runner.py")
    mirror_run_dirs = [rd for rd in (agent1.run_dir, agent2.run_dir) if rd]
    cmd = [
        state.python_path, runner_path,
        "--config", agent1.config_path,
        "--synthesis-dirs", *synthesis_dirs,
        "--output", disc_dir,
        "--rounds", str(state.discussion_rounds),
        "--topic", group.topic,
    ]
    if mirror_run_dirs:
        cmd.append("--mirror-run-dirs")
        cmd.extend(mirror_run_dirs)

    try:
        log_path = Path(disc_dir) / "discussion.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env=_agent_subprocess_env(state, agent1),
        )
        group.discussion_process = proc
        state.discussion_groups[disc_name] = group

        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(
            sys_agent,
            f"跨 project 讨论开始: [{agent1.project_id}] × [{agent2.project_id}], {state.discussion_rounds} 轮 (PID={proc.pid})",
            "info", DISCUSSION_STAGE,
        ))
    except Exception as e:
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(sys_agent, f"跨 project 讨论启动失败: {e}", "error", DISCUSSION_STAGE))
        for a in (agent1, agent2):
            state.discussion_waiting.pop(a.id, None)
        messages.extend(_skip_discussion_proceed_s8(state, agent1))
        messages.extend(_skip_discussion_proceed_s8(state, agent2))

    return messages


def _skip_discussion_proceed_s8(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """Skip discussion and proceed directly to S8 for a single agent."""
    messages: list[dict] = []
    state.discussion_waiting.pop(agent.id, None)

    agent.stage_progress[DISCUSSION_STAGE] = "completed"
    messages.append(msg_stage_update(agent.id, DISCUSSION_STAGE, "completed"))

    node_stage_override: tuple[int, int] | None = None
    if agent.project_id and agent.assigned_task_id:
        _g = state.task_graphs.get(agent.project_id)
        if _g:
            _nd = _g.nodes.get(agent.assigned_task_id)
            if _nd:
                node_stage_override = (_nd.stage_from, _nd.stage_to)
    _tm_s8 = _read_project_meta(agent.run_dir) if agent.run_dir else None
    if not _tm_s8 and agent.run_dir:
        _tm_s8 = _read_project_meta(str(Path(agent.run_dir).parent))
    fs, ts = _effective_stage_range_for_launch(
        state, agent, None, _tm_s8,
        is_discussion_s8=True,
        node_stage_override=node_stage_override,
    )
    agent.status = "working"
    agent.current_task = f"项目 {agent.project_id} · S8 假设生成 (跳过讨论)"
    agent.stage_progress[8] = "running"
    messages.append(msg_agent_update(agent))
    messages.append(msg_stage_update(agent.id, 8, "running"))
    messages.append(msg_log(agent, "跳过讨论 → 直接启动 S8 假设生成", "info", 8))

    cmd = [
        state.python_path, "-m", "researchclaw", "run",
        "--config", agent.config_path,
        "--output", agent.run_dir,
        "--from-stage", STAGE_NAMES.get(fs, str(fs)),
        "--to-stage", STAGE_NAMES.get(ts, str(ts)),
        "--auto-approve",
        "--skip-preflight",
    ]
    if agent.project_id:
        cmd.extend(["--topic", agent.project_id])

    try:
        log_path = Path(agent.run_dir) / f"agent_{agent.id}_s8.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        agent.process = proc
        agent._is_discussion_s8 = True  # type: ignore[attr-defined]
        messages.append(msg_log(agent, f"S8 启动 (PID={proc.pid})", "info", 8))
    except Exception as e:
        agent.status = "error"
        agent.current_task = f"S8 启动失败: {e}"
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"S8 启动失败: {e}", "error"))

    return messages


def _poll_discussion(state: BridgeState, group: DiscussionGroup) -> list[dict]:
    """Check if a discussion subprocess has finished and handle completion."""
    messages: list[dict] = []
    if group.status != "discussing" or group.discussion_process is None:
        return messages

    retcode = group.discussion_process.poll()
    if retcode is None:
        return messages

    group.discussion_process = None
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    is_cross = getattr(group, "_cross_project", False)

    if retcode != 0:
        group.status = "done"
        messages.append(msg_log(sys_agent, f"讨论 [{group.project_id}] 失败 (exit={retcode})", "error", DISCUSSION_STAGE))
        for aid in group.agent_ids:
            agent = state.agents.get(aid)
            if agent:
                state.discussion_waiting.pop(aid, None)
                if is_cross:
                    messages.append(msg_log(agent, "讨论失败，跳过讨论直接进入 S8", "warning", DISCUSSION_STAGE))
                    messages.extend(_skip_discussion_proceed_s8(state, agent))
                else:
                    agent.status = "error"
                    agent.current_task = f"沟通讨论失败 (exit={retcode})"
                    agent.stage_progress[DISCUSSION_STAGE] = "failed"
                    messages.append(msg_agent_update(agent))
                    messages.append(msg_stage_update(agent.id, DISCUSSION_STAGE, "failed"))
        return messages

    consensus_file = Path(group.discussion_output_dir) / "consensus_synthesis.md"
    if not consensus_file.exists():
        messages.append(msg_log(sys_agent, f"讨论 [{group.project_id}] 完成但未产生共识", "warning", DISCUSSION_STAGE))
        group.status = "done"
        for aid in group.agent_ids:
            agent = state.agents.get(aid)
            if agent:
                state.discussion_waiting.pop(aid, None)
                has_project = bool(agent.run_dir and (Path(agent.run_dir) / "stage-07").exists())
                if has_project:
                    messages.extend(_skip_discussion_proceed_s8(state, agent))
                else:
                    _reset_agent_idle(agent)
                    agent.current_stage = 0
                    messages.append(msg_agent_update(agent))
        return messages

    consensus_text = consensus_file.read_text(encoding="utf-8")
    messages.append(msg_log(sys_agent, f"讨论 [{group.project_id}] 完成，共识已生成，启动假设生成", "success", DISCUSSION_STAGE))
    for aid in group.agent_ids:
        agent = state.agents.get(aid)
        if agent:
            agent.stage_progress[DISCUSSION_STAGE] = "completed"
            messages.append(msg_stage_update(agent.id, DISCUSSION_STAGE, "completed"))

    transcript_file = Path(group.discussion_output_dir) / "discussion_transcript.md"
    if transcript_file.exists():
        messages.append(msg_artifact(
            "knowledge", "discussion_transcript.md",
            "沟通讨论", f"{transcript_file.stat().st_size / 1024:.1f} KB",
            group.project_id,
        ))

    group.status = "done"

    # Collect pre-discussion syntheses from all agents for ablation data
    pre_discussion_parts: list[str] = []
    for i, _aid in enumerate(group.agent_ids):
        _ag = state.agents.get(_aid)
        if not _ag:
            continue
        _s7_synth = Path(_ag.run_dir) / "stage-07" / "synthesis.md"
        if _s7_synth.exists():
            _text = _s7_synth.read_text(encoding="utf-8")
            pre_discussion_parts.append(f"## Agent {i+1} ({_aid[:8]})\n\n{_text}")

    for aid in group.agent_ids:
        agent = state.agents.get(aid)
        if not agent:
            continue
        state.discussion_waiting.pop(aid, None)

        has_project = bool(agent.run_dir and (Path(agent.run_dir) / "stage-07").exists())

        if has_project:
            s7_dir = Path(agent.run_dir) / "stage-07"
            s7_dir.mkdir(parents=True, exist_ok=True)
            existing_synthesis = s7_dir / "synthesis.md"
            if existing_synthesis.exists():
                original = existing_synthesis.read_text(encoding="utf-8")
                enriched = (
                    f"{original}\n\n"
                    f"---\n\n"
                    f"# {'Cross-Project' if is_cross else 'Multi-Agent'} Discussion Consensus\n\n"
                    f"{consensus_text}"
                )
                existing_synthesis.write_text(enriched, encoding="utf-8")
            else:
                (s7_dir / "synthesis.md").write_text(consensus_text, encoding="utf-8")

            # Save discussion artifacts for L5 paper ablation study
            disc_artifact_dir = Path(agent.run_dir) / "discussion"
            disc_artifact_dir.mkdir(parents=True, exist_ok=True)
            if pre_discussion_parts:
                (disc_artifact_dir / "pre_discussion_syntheses.md").write_text(
                    "\n\n---\n\n".join(pre_discussion_parts), encoding="utf-8"
                )
            (disc_artifact_dir / "consensus_synthesis.md").write_text(
                consensus_text, encoding="utf-8"
            )
            if transcript_file.exists():
                shutil.copy2(str(transcript_file), str(disc_artifact_dir / "discussion_transcript.md"))

            messages.extend(_launch_s8_for_agent(state, agent, group))
        else:
            _reset_agent_idle(agent)
            agent.current_stage = 0
            agent.stage_progress[DISCUSSION_STAGE] = "completed"
            messages.append(msg_agent_update(agent))
            messages.append(msg_log(agent, "讨论评审完成，恢复空闲", "info", DISCUSSION_STAGE))

    return messages


def _launch_s8_for_agent(state: BridgeState, agent: LobsterAgent, group: DiscussionGroup) -> list[dict]:
    """Launch S8 (HYPOTHESIS_GEN) for a single agent after discussion."""
    messages: list[dict] = []
    fs, ts = LAYER_RANGE_PHASE2["idea"]

    agent.status = "working"
    agent.current_task = f"项目 {group.project_id} · S8 假设生成"
    agent.stage_progress[8] = "running"
    messages.append(msg_agent_update(agent))
    messages.append(msg_stage_update(agent.id, 8, "running"))
    messages.append(msg_log(agent, "沟通讨论完成 → 开始假设生成", "info", 8))

    cmd = [
        state.python_path, "-m", "researchclaw", "run",
        "--config", group.config_path,
        "--output", agent.run_dir,
        "--from-stage", STAGE_NAMES.get(fs, str(fs)),
        "--to-stage", STAGE_NAMES.get(ts, str(ts)),
        "--auto-approve",
        "--skip-preflight",
    ]
    if group.topic:
        cmd.extend(["--topic", group.topic])

    try:
        log_path = Path(agent.run_dir) / f"agent_{agent.id}_s8.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        agent.process = proc
        agent._is_discussion_s8 = True  # type: ignore[attr-defined]
        messages.append(msg_log(agent, f"S8 启动 (PID={proc.pid})", "info", 8))
    except Exception as e:
        agent.status = "error"
        agent.current_task = f"S8 启动失败: {e}"
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"S8 启动失败: {e}", "error"))

    return messages


def _select_best_hypothesis(state: BridgeState, group: DiscussionGroup) -> str:
    """Pick the best agent perspective based on hypothesis quality heuristics.

    Scores each agent's hypotheses.md by: number of hypotheses found,
    total length (richer detail = better), and presence of novelty_report.
    Returns the agent_id with the highest score.
    """
    best_id = group.agent_ids[0]
    best_score = -1
    for aid in group.agent_ids:
        rd = group.run_dirs.get(aid, "")
        if not rd:
            continue
        score = 0
        hypo_file = Path(rd) / "stage-08" / "hypotheses.md"
        if hypo_file.exists():
            text = hypo_file.read_text(encoding="utf-8", errors="replace")
            score += len(text)
            score += text.lower().count("hypothesis") * 500
            score += text.lower().count("## ") * 300
        novelty_file = Path(rd) / "stage-08" / "novelty_report.json"
        if novelty_file.exists():
            score += 2000
        if score > best_score:
            best_score = score
            best_id = aid
    return best_id


def _on_discussion_s8_done(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """Handle S8 completion — wait for all agents, then pick the best hypothesis
    and create only ONE downstream task to avoid duplicate experiments."""
    messages: list[dict] = []
    agent._is_discussion_s8 = False  # type: ignore[attr-defined]

    project_id = agent.project_id
    group = state.discussion_groups.get(project_id)
    if not group:
        for g in state.discussion_groups.values():
            if agent.id in g.agent_ids:
                group = g
                break

    if group:
        group.completed_s8.add(agent.id)
        messages.append(msg_log(
            agent,
            f"S8 完成 ({len(group.completed_s8)}/{len(group.agent_ids)})，等待其他 agent...",
            "info",
        ))

    # Not all agents done yet — park this agent, wait for peers
    if group and not group.all_s8_done():
        _reset_agent_idle(agent)
        messages.append(msg_agent_update(agent))
        return messages

    # All S8 done — select the best hypothesis and create ONE downstream task
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    merged_project_id = group.project_id if group else project_id
    if group:
        best_id = _select_best_hypothesis(state, group)
        group.best_agent_id = best_id
        best_run_dir = group.run_dirs.get(best_id, agent.run_dir)
        best_config = group.config_path
        if not best_config:
            best_agent = state.agents.get(best_id)
            best_config = (getattr(best_agent, '_base_config_path', best_agent.config_path) if best_agent else "") or getattr(agent, '_base_config_path', agent.config_path)
        other_ids = [a for a in group.agent_ids if a != best_id]
        messages.append(msg_log(
            sys_agent,
            f"项目 [{merged_project_id}] 所有 agent S8 完成 → 选择最优假设 (agent {best_id})，"
            f"合并为单一实验路径 → 进入 L2（淘汰 {', '.join(other_ids)}）",
            "success",
        ))
    else:
        best_run_dir = agent.run_dir
        best_config = agent.config_path

    output_queue_name = LAYER_OUTPUT_QUEUE.get("idea")
    if output_queue_name and output_queue_name in state.queues and merged_project_id:
        _, target_layer = QUEUE_NAMES[output_queue_name]
        follow_task = Task(
            id=f"task-{_uid()}",
            project_id=merged_project_id,
            run_dir=best_run_dir,
            config_path=best_config,
            topic=getattr(agent, '_topic', '') or (group.topic if group else ''),
            source_layer="idea",
            target_layer=target_layer,
            created_at=_now_ms(),
        )
        state.queues[output_queue_name].push(follow_task)
        messages.append(msg_log(
            sys_agent,
            f"项目 [{merged_project_id}] 最优假设已加入 {output_queue_name} 队列 → 进入 L2 实验设计",
            "success",
        ))

    # Reset this agent (peers were already reset when they finished earlier)
    _reset_agent_idle(agent)
    messages.append(msg_agent_update(agent))
    messages.append(msg_queue_update(state.queues))

    return messages


def schedule_idle_agents(state: BridgeState) -> list[dict]:
    """Assign pending tasks to idle agents (FIFO pull)."""
    messages: list[dict] = []

    for agent in state.agents.values():
        if agent.status not in ("idle",) or agent.process is not None:
            continue
        if agent.status in ("waiting_discussion", "discussing"):
            continue
        if agent.assigned_task_id:
            continue

        # L4 execution layer: skip if no GPU available
        if agent.layer == "execution" and not state.gpu_allocator.can_allocate():
            continue

        # Idea agents pull from init_to_idea, and optionally execution_feedback (auto-loop)
        if agent.layer == "idea":
            candidate_queues = ["init_to_idea"]
            if state.auto_loop:
                candidate_queues.append("execution_feedback")
        else:
            q_name = LAYER_INPUT_QUEUE.get(agent.layer, "")
            candidate_queues = [q_name] if q_name else []

        # v2.0: Check TaskGraph for ready tasks first
        assigned = False
        for _pid, _graph in state.task_graphs.graphs.items():
            ready = _graph.get_ready_tasks(agent.layer)
            if not ready:
                continue
            _tnode = ready[0]
            _graph.mark_running(_tnode.id, agent.id)
            _tg_task = Task(
                id=_tnode.id, project_id=_pid, run_dir=_tnode.run_dir,
                config_path=_tnode.config_path,
                source_layer=_LAYER_ORDER[max(0, _LAYER_ORDER.index(_tnode.layer) - 1)] if _tnode.layer != "idea" else "init",
                target_layer=_tnode.layer, topic=_tnode.title,
                stage_from=_tnode.stage_from,
                stage_to=_tnode.stage_to,
            )
            messages.extend(launch_agent_for_task(state, agent, _tg_task))
            proj_dir = state.projects_dir() / _pid
            state.task_graphs.save_to_disk(_pid, proj_dir)
            messages.append(msg_queue_update(state.queues))
            messages.append({"type": "task_graph_update", "payload": {
                "projectId": _pid, **_graph.to_dict(),
            }})
            assigned = True
            break

        if assigned:
            continue

        # Legacy: check old-style queues
        for queue_name in candidate_queues:
            queue = state.queues.get(queue_name)
            if not queue:
                continue
            task = queue.peek_pending()
            if not task or task.target_layer != agent.layer:
                continue
            if state._fail_counts.get(task.project_id, 0) >= 3:
                continue

            state._fail_counts.pop(task.project_id, None)
            queue.assign(task.id, agent.id)
            messages.extend(launch_agent_for_task(state, agent, task))
            messages.append(msg_queue_update(state.queues))
            assigned = True
            break

        # Idea factory: L1 idle with no queued tasks → produce ideas
        if not assigned and agent.layer == "idea" and state.idea_factory_remaining != 0:
            if state.idea_factory_topic and state.idea_factory_config:
                if not state.discussion_mode:
                    messages.extend(_launch_idea_factory_run(state, agent))
                continue

    # Discussion-mode idea factory: batch-launch when 2+ L1 agents are idle
    if state.discussion_mode and state.idea_factory_remaining != 0 and state.idea_factory_topic and state.idea_factory_config:
        idle_idea_agents = [
            a for a in state.agents.values()
            if a.layer == "idea" and a.status == "idle" and a.process is None
            and not a.assigned_task_id
            and a.status not in ("waiting_discussion", "discussing")
        ]
        if len(idle_idea_agents) >= 2:
            batch_id = f"idea-batch-{_uid()}"
            group = DiscussionGroup(
                project_id=batch_id,
                topic=state.idea_factory_topic,
                config_path=state.idea_factory_config,
            )
            models = state.discussion_models
            for i, agent in enumerate(idle_idea_agents[:2]):
                model = models[i % len(models)] if models else ""
                messages.extend(_launch_idea_factory_run(state, agent, s7_only=True, model_override=model))
                group.agent_ids.append(agent.id)
                group.run_dirs[agent.id] = agent.run_dir
                agent._idea_factory_batch_id = batch_id  # type: ignore[attr-defined]
            state.discussion_groups[batch_id] = group
            model_list = ", ".join(models[:2]) if models else "默认"
            sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
            messages.append(msg_log(
                sys_agent,
                f"Idea 工厂沟通讨论模式: {len(group.agent_ids)} 个 agent 开始独立综合 (S7) — 模型: {model_list}",
                "info", DISCUSSION_STAGE,
            ))

    return messages


# ── WebSocket server ────────────────────────────────────────────────────────

# 未列于此集合的命令在启用 control_token 时均视为需鉴权的控制命令
_BRIDGE_READ_ONLY_COMMANDS: frozenset[str] = frozenset({
    "list_agents",
    "list_projects",
    "get_queues",
    "get_shared_results",
    "query_status",
    "get_task_graph",
    "get_coordination",
    "list_archives",
    "tail_agent_log",
    "list_diffs",
    "get_diff",
    "list_kb_entries",
    "search_kb",
    "kb_stats",
    "get_stage_detail",
    "get_node_detail",
    "get_artifact_preview",
    "get_metaprompt",
    "get_metaprompt_versions",
    "get_prompt",
    "get_prompt_versions",
})


def _is_bridge_control_command(cmd: str) -> bool:
    if not cmd:
        return False
    return cmd not in _BRIDGE_READ_ONLY_COMMANDS


def _bridge_control_token_authorized(state: BridgeState, data: dict) -> bool:
    if not (state.control_token or "").strip():
        return True
    got = (data.get("controlToken") or data.get("authToken") or "").strip()
    a = state.control_token
    b = got
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _ab_path_under_root(path: Path, root: Path) -> bool:
    try:
        p = path.resolve()
        r = root.resolve()
    except (OSError, ValueError):
        return False
    if p == r:
        return True
    try:
        p.relative_to(r)
        return True
    except ValueError:
        return False


def _browse_allowed_roots(state: BridgeState) -> list[Path]:
    roots: list[Path] = []
    try:
        roots.append(Path(state.runs_base_dir).resolve())
    except (OSError, ValueError):
        pass
    try:
        roots.append(state.projects_dir().resolve())
    except (OSError, ValueError):
        pass
    extra = (os.environ.get("AGENT_BRIDGE_BROWSE_ROOTS") or "").replace(";", ",")
    for part in extra.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).resolve())
        except (OSError, ValueError):
            pass
    out: list[Path] = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


def _http_download_token_ok(state: BridgeState, request_headers, query_token: str = "") -> bool:
    if not (state.control_token or "").strip():
        return True
    t = (query_token or request_headers.get("x-bridge-token") or request_headers.get("x-api-key") or "").strip()
    auth = request_headers.get("authorization") or ""
    if not t and auth.lower().startswith("bearer "):
        t = auth[7:].strip()
    a = state.control_token
    b = t
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _project_detail_dirs(proj_dir: Path) -> list[Path]:
    """Resolve directories that may contain stage-* folders."""
    detail_dirs: list[Path] = []
    ws_link = _read_json(proj_dir / "_workspace_link.json")
    if ws_link and ws_link.get("scholar_dir"):
        detail_dirs.append(Path(ws_link["scholar_dir"]))
    detail_dirs.append(proj_dir)
    for angle in sorted(proj_dir.glob("run-*")):
        if angle.is_dir():
            detail_dirs.append(angle)
    return detail_dirs


def _collect_stage_files_for_detail(detail_dirs: list[Path], stage: int) -> list[dict]:
    """List files under stage-XX for the first detail root that contains it."""
    stage_dir_name = f"stage-{stage:02d}"
    files_info: list[dict] = []
    for detail_dir in detail_dirs:
        stage_dir = detail_dir / stage_dir_name
        if not stage_dir.is_dir():
            continue
        for file_path in sorted(stage_dir.rglob("*")):
            if not file_path.is_file():
                continue
            rel = str(file_path.relative_to(stage_dir)).replace("\\", "/")
            try:
                size = file_path.stat().st_size
                modified = file_path.stat().st_mtime
            except OSError:
                size, modified = 0, 0
            files_info.append({
                "name": rel,
                "size": size,
                "modified": modified,
                "dir": str(stage_dir),
            })
        break
    return files_info


def _stage_detail_completion_status(files_info: list[dict], stage: int) -> str:
    if not files_info:
        return "pending"
    expected = STAGE_OUTPUTS.get(stage, [])
    if not expected:
        return "completed"
    found = {fi["name"] for fi in files_info}
    for output in expected:
        clean = output.rstrip("/")
        if output.endswith("/"):
            if not any(name.startswith(clean) for name in found):
                return "incomplete"
        elif clean not in found:
            return "incomplete"
    return "completed"


def _input_path_resolves(prior_output_names: set[str], path: str) -> bool:
    clean = path.rstrip("/")
    if path.endswith("/"):
        return any(name == clean or name.startswith(f"{clean}/") for name in prior_output_names)
    return clean in prior_output_names


def _read_checkpoint_for_node(proj_dir: Path, node: TaskNode) -> dict | None:
    candidates = [Path(node.run_dir) / "checkpoint.json" if node.run_dir else None, proj_dir / "checkpoint.json"]
    for candidate in candidates:
        if candidate and candidate.is_file():
            data = _read_json(candidate)
            if data:
                return data
    return None


def _light_agent_log_summary(state: BridgeState, node: TaskNode, max_lines: int = 40) -> dict:
    agent_id = (node.assigned_agent or "").strip()
    run_dir = ""
    agent = state.agents.get(agent_id) if agent_id else None
    if agent_id and agent and agent.run_dir:
        run_dir = agent.run_dir
    if not run_dir and node.run_dir:
        run_dir = node.run_dir
    lines: list[str] = []
    if not run_dir or not agent_id:
        return {"agentId": agent_id, "lines": lines, "truncated": False}
    log_path = Path(run_dir) / f"agent_{agent_id}.log"
    if log_path.is_file():
        try:
            all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            lines = all_lines[-max_lines:]
            return {"agentId": agent_id, "lines": lines, "truncated": len(all_lines) > max_lines}
        except OSError:
            pass
    return {"agentId": agent_id, "lines": lines, "truncated": False}


def _build_node_detail_payload(state: BridgeState, project_id: str, node_id: str) -> dict:
    graph = state.task_graphs.get(project_id)
    if not graph:
        return {
            "ok": False,
            "error": "no_task_graph",
            "message": "该项目没有 TaskGraph（可能尚未生成或未加载）",
            "projectId": project_id,
            "nodeId": node_id,
        }
    node = graph.nodes.get(node_id)
    if not node:
        return {
            "ok": False,
            "error": "node_not_found",
            "message": "TaskGraph 中不存在该节点",
            "projectId": project_id,
            "nodeId": node_id,
        }

    proj_dir = state.projects_dir() / project_id
    detail_dirs = _project_detail_dirs(proj_dir)
    stage_from, stage_to = node.stage_from, node.stage_to
    if stage_from > stage_to:
        stage_from, stage_to = stage_to, stage_from

    prior_names: set[str] = set()
    for prior_stage in range(1, stage_from):
        for file_info in _collect_stage_files_for_detail(detail_dirs, prior_stage):
            prior_names.add(file_info["name"])

    stages_detail: list[dict] = []
    output_files: list[dict] = []
    expected_union: list[str] = []

    for stage in range(stage_from, stage_to + 1):
        input_hints = [
            {
                "path": input_path,
                "forStage": stage,
                "present": _input_path_resolves(prior_names, input_path),
            }
            for input_path in STAGE_INPUTS.get(stage, [])
        ]
        files = _collect_stage_files_for_detail(detail_dirs, stage)
        status = _stage_detail_completion_status(files, stage)
        expected = STAGE_OUTPUTS.get(stage, [])
        expected_union.extend(expected)

        stages_detail.append({
            "stage": stage,
            "stageName": STAGE_NAMES.get(stage, f"S{stage}"),
            "status": status,
            "expectedOutputs": expected,
            "inputs": input_hints,
            "files": files,
        })

        for file_info in files:
            row = dict(file_info)
            row["stage"] = stage
            output_files.append(row)
            prior_names.add(file_info["name"])

    contract = ""
    if isinstance(node.config_overrides, dict):
        contract = str(node.config_overrides.get("contract") or "")

    checkpoint = _read_checkpoint_for_node(proj_dir, node)
    execution_history: list[dict] = []
    if isinstance(checkpoint, dict) and checkpoint.get("last_completed_stage") is not None:
        execution_history.append({
            "kind": "checkpoint",
            "lastCompletedStage": checkpoint.get("last_completed_stage"),
            "lastCompletedName": checkpoint.get("last_completed_name"),
            "runId": checkpoint.get("run_id"),
            "timestamp": checkpoint.get("timestamp"),
        })

    input_files: list[dict] = []
    for stage_block in stages_detail:
        input_files.extend(stage_block["inputs"])

    expected_outputs: list[str] = []
    seen_expected: set[str] = set()
    for output in expected_union:
        if output not in seen_expected:
            seen_expected.add(output)
            expected_outputs.append(output)

    agent_log_summary = _light_agent_log_summary(state, node)

    return {
        "ok": True,
        "projectId": project_id,
        "nodeId": node_id,
        "taskId": node_id,
        "node": node.to_dict(),
        "stageRange": {"from": node.stage_from, "to": node.stage_to},
        "dependencies": list(node.dependencies),
        "status": node.status,
        "contract": contract,
        "stages": stages_detail,
        "inputFiles": input_files,
        "inputs": input_files,
        "outputFiles": output_files,
        "outputs": output_files,
        "expectedOutputs": expected_outputs,
        "checkpoint": checkpoint,
        "executionHistory": execution_history,
        "currentPrompt": "",
        "defaultPrompt": "",
        "promptDraft": "",
        "logs": "\n".join(agent_log_summary.get("lines", [])),
        "agentLogSummary": agent_log_summary,
    }


def _task_graph_ws_payload(project_id: str, graph: TaskGraph) -> dict:
    return {"type": "task_graph_update", "payload": {"projectId": project_id, **graph.to_dict()}}


def _handle_skip_task(state: BridgeState, project_id: str, task_id: str) -> list[dict]:
    messages: list[dict] = []
    _graph = state.task_graphs.get(project_id) if project_id else None
    if _graph and task_id in _graph.nodes:
        for agent in list(state.agents.values()):
            if (
                agent.project_id == project_id
                and agent.assigned_task_id == task_id
                and agent.process is not None
                and agent.process.poll() is None
            ):
                messages.extend(stop_agent(agent))
        _graph.mark_skipped(task_id)
        _proj_dir = state.projects_dir() / project_id
        state.task_graphs.save_to_disk(project_id, _proj_dir)
        messages.append(_task_graph_ws_payload(project_id, _graph))
        _sys_a = LobsterAgent(id="system", name="System", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(_sys_a, f"Task {task_id} skipped", "warning"))
        messages.extend(schedule_idle_agents(state))
    else:
        messages.append({"type": "system", "payload": {"message": f"Task {task_id} not found"}})
    return messages


def _handle_retry_task(state: BridgeState, project_id: str, task_id: str) -> list[dict]:
    messages: list[dict] = []
    _graph = state.task_graphs.get(project_id) if project_id else None
    if _graph and task_id in _graph.nodes:
        dependents = _graph.dependent_ids(task_id)
        for agent in list(state.agents.values()):
            if (
                agent.project_id == project_id
                and agent.assigned_task_id in dependents
                and agent.process is not None
                and agent.process.poll() is None
            ):
                messages.extend(stop_agent(agent))
        _graph.reset_node(task_id)
        _proj_dir = state.projects_dir() / project_id
        state.task_graphs.save_to_disk(project_id, _proj_dir)
        messages.append(_task_graph_ws_payload(project_id, _graph))
        _sys_a = LobsterAgent(id="system", name="System", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(_sys_a, f"Task {task_id} reset for retry", "info"))
        messages.extend(schedule_idle_agents(state))
    else:
        messages.append({"type": "system", "payload": {"message": f"Task {task_id} not found"}})
    return messages


def _apply_node_rollback_checkpoint(state: BridgeState, project_id: str, node: TaskNode) -> None:
    rd = Path(node.run_dir) if node.run_dir else state.projects_dir() / project_id
    if not rd.is_dir():
        return
    cp_path = rd / "checkpoint.json"
    target_done = max(0, int(node.stage_from) - 1)
    data = _read_json(cp_path) or {}
    data["last_completed_stage"] = target_done
    data["bridge_rollback"] = {
        "node_id": node.id,
        "ts": _now_ms(),
        "note": "Non-destructive rollback: research artifacts were not deleted; re-execute from node stage_from.",
    }
    _write_json(cp_path, data)


def _handle_node_action(state: BridgeState, data: dict) -> list[dict]:
    messages: list[dict] = []
    project_id = str(data.get("projectId", "") or "").strip()
    node_id = str(data.get("nodeId") or data.get("taskId", "") or "").strip()
    action = str(data.get("action", "") or "").lower().strip()
    if not project_id or not node_id:
        messages.append({
            "type": "system",
            "payload": {"message": "node_action requires projectId and nodeId or taskId"},
        })
        return messages
    _graph = state.task_graphs.get(project_id)
    if not _graph or node_id not in _graph.nodes:
        messages.append({"type": "system", "payload": {"message": f"node_action: node {node_id} not found"}})
        return messages
    _sys = LobsterAgent(id="system", name="System", layer="idea", run_id="", run_dir="", config_path="")

    if action == "skip":
        return _handle_skip_task(state, project_id, node_id)
    if action == "retry":
        return _handle_retry_task(state, project_id, node_id)

    if action in ("run", "resume"):
        node = _graph.nodes[node_id]
        if node.status == "paused":
            _graph.resume_node(node_id)
        elif node.status == "failed":
            _graph.reset_node(node_id)
        elif node.status == "skipped":
            _graph.reset_node(node_id)
        else:
            messages.append(msg_log(_sys, f"node_action {action}: node {node_id} unchanged ({node.status})", "info"))
        _proj_dir = state.projects_dir() / project_id
        state.task_graphs.save_to_disk(project_id, _proj_dir)
        messages.append(_task_graph_ws_payload(project_id, _graph))
        messages.extend(schedule_idle_agents(state))
        return messages

    if action == "pause":
        for ag in list(state.agents.values()):
            if (
                ag.assigned_task_id == node_id
                and ag.project_id == project_id
                and ag.process is not None
                and ag.process.poll() is None
            ):
                messages.extend(stop_agent(ag))
        _graph.mark_paused(node_id)
        _proj_dir = state.projects_dir() / project_id
        state.task_graphs.save_to_disk(project_id, _proj_dir)
        messages.append(_task_graph_ws_payload(project_id, _graph))
        messages.append(msg_log(_sys, f"node {node_id} paused", "warning"))
        messages.extend(schedule_idle_agents(state))
        return messages

    if action == "rollback":
        if _graph.nodes[node_id].status == "running":
            for ag in list(state.agents.values()):
                if (
                    ag.assigned_task_id == node_id
                    and ag.project_id == project_id
                    and ag.process is not None
                    and ag.process.poll() is None
                ):
                    messages.extend(stop_agent(ag))
        dependents = _graph.dependent_ids(node_id)
        for ag in list(state.agents.values()):
            if (
                ag.project_id == project_id
                and ag.assigned_task_id in dependents
                and ag.process is not None
                and ag.process.poll() is None
            ):
                messages.extend(stop_agent(ag))
        _graph.rollback_node(node_id)
        _apply_node_rollback_checkpoint(state, project_id, _graph.nodes[node_id])
        _proj_dir = state.projects_dir() / project_id
        state.task_graphs.save_to_disk(project_id, _proj_dir)
        messages.append(_task_graph_ws_payload(project_id, _graph))
        messages.append(msg_log(_sys, f"node {node_id} rolled back (checkpoint pointer only)", "info"))
        messages.extend(schedule_idle_agents(state))
        return messages

    messages.append({"type": "system", "payload": {"message": f"Unknown node_action: {action}"}})
    return messages


_METAPROMPT_LAYERS: frozenset[str] = frozenset({"system", "domain", "project", "node"})
_METAPROMPT_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _safe_metaprompt_node_id(raw: str) -> str | None:
    node_id = (raw or "").strip()
    if not node_id or not _METAPROMPT_NODE_ID_RE.fullmatch(node_id):
        return None
    if node_id in (".", ".."):
        return None
    return node_id


def _bridge_metaprompt_allowed(state: BridgeState, path: Path) -> bool:
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    return any(_ab_path_under_root(resolved, root) for root in _browse_allowed_roots(state))


def _bridge_metaprompt_project_run_paths(
    state: BridgeState, data: dict
) -> tuple[Path | None, Path | None, str]:
    project_id = str(data.get("projectId", "") or "").strip()
    if not project_id:
        return None, None, "projectId is required"
    workspace = _get_workspace_dir(state, project_id) or str(state.projects_dir() / project_id)
    project_dir = Path(workspace)
    if not _bridge_metaprompt_allowed(state, project_dir):
        return None, None, "project path not allowed"
    run_id = str(data.get("runId") or "").strip()
    if not run_id:
        return project_dir, None, ""
    run_dir = project_dir / run_id
    if not run_dir.is_dir():
        return project_dir, None, f"run directory not found: {run_dir}"
    if not _bridge_metaprompt_allowed(state, run_dir):
        return None, None, "run path not allowed"
    return project_dir, run_dir, ""


def _bridge_metaprompt_write_target(state: BridgeState, data: dict) -> tuple[Path, str]:
    """Directory to read/write metaprompt files (project root or run-* subdir)."""
    scope = str(data.get("scope", "project") or "").strip().lower()
    project_dir, run_dir, err = _bridge_metaprompt_project_run_paths(state, data)
    if err:
        return Path(), err
    if scope in ("run", "run_dir", "rundir"):
        if run_dir is None:
            return Path(), "runId is required when scope is run"
        return run_dir, ""
    return project_dir, ""  # type: ignore[return-value]


def _bridge_metaprompt_delete_layer_files(
    target: Path, layer: str, *, node_id: str | None = None
) -> list[str]:
    removed: list[str] = []
    for base in (target / ".researchclaw" / "metaprompts", target / "metaprompts"):
        if not base.is_dir():
            continue
        if layer == "node" and node_id:
            for ext in (".yaml", ".yml", ".json"):
                path = base / "nodes" / f"{node_id}{ext}"
                if path.is_file():
                    try:
                        path.unlink()
                        removed.append(str(path))
                    except OSError:
                        pass
            continue
        for ext in (".yaml", ".yml", ".json"):
            path = base / f"{layer}{ext}"
            if path.is_file():
                try:
                    path.unlink()
                    removed.append(str(path))
                except OSError:
                    pass
    return removed


async def broadcast(state: BridgeState, messages: list[dict]):
    if not messages or not state.clients:
        return
    dead = set()
    for msg in messages:
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(state.clients):
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                dead.add(ws)
    state.clients -= dead


async def handle_command(state: BridgeState, data: dict) -> list[dict]:
    cmd = data.get("command")
    messages: list[dict] = []

    if state.control_token and _is_bridge_control_command(str(cmd or "")) and not _bridge_control_token_authorized(state, data):
        return [{
            "type": "system",
            "payload": {
                "message": "需要有效的 controlToken 才能执行该控制命令",
                "code": "AUTH_REQUIRED",
            },
        }]

    if cmd == "list_agents":
        for a in state.agents.values():
            messages.append(msg_agent_update(a))
        messages.append(msg_queue_update(state.queues))
        messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "add_lobster":
        name = data.get("name", f"龙虾-{_uid()}")
        layer = data.get("layer", "idea")
        agent = create_agent(state, name, layer)
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"龙虾已加入 {layer} 层", "info"))

    elif cmd == "remove_lobster":
        agent_id = data.get("agentId")
        agent = state.agents.pop(agent_id, None)
        if agent:
            messages.extend(stop_agent(agent))

    elif cmd == "submit_project":
        project_id = data.get("projectId") or f"proj-{_uid()}"
        config_path = data.get("configPath", "")
        topic = data.get("topic", "")
        messages.extend(submit_new_project(state, project_id, config_path, topic))
        messages.extend(schedule_idle_agents(state))
        messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "list_projects":
        messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "open_project_folder":
        project_id = str(data.get("projectId", "") or "").strip()
        target = str(data.get("target", "auto") or "auto").strip()
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        if project_id:
            try:
                folder = _resolve_project_folder(state, project_id, target)
                _open_folder_in_file_manager(folder)
                messages.append({
                    "type": "project_folder_opened",
                    "payload": {"projectId": project_id, "path": str(folder)},
                })
                messages.append(msg_log(sys_agent, f"已打开项目文件夹: {folder}", "success"))
            except Exception as e:
                messages.append(msg_log(sys_agent, f"打开项目 [{project_id}] 文件夹失败: {e}", "error"))

    elif cmd == "list_archives":
        messages.append({
            "type": "archive_list",
            "payload": {"archives": list_project_archives(state)},
        })

    elif cmd == "archive_project":
        project_id = str(data.get("projectId", "") or "").strip()
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        if project_id:
            try:
                archive = archive_project(state, project_id)
                messages.append({"type": "archive_created", "payload": archive})
                messages.append({"type": "archive_list", "payload": {"archives": list_project_archives(state)}})
                messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已存档: {archive['archiveId']}", "success"))
            except Exception as e:
                messages.append(msg_log(sys_agent, f"项目 [{project_id}] 存档失败: {e}", "error"))

    elif cmd == "restore_archive":
        archive_id = str(data.get("archiveId", "") or "").strip()
        overwrite = bool(data.get("overwrite", False))
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        if archive_id:
            try:
                restored = restore_project_archive(state, archive_id, overwrite=overwrite)
                messages.append({"type": "archive_restored", "payload": restored})
                messages.append(msg_project_list(list_all_projects(state)))
                messages.append({"type": "archive_list", "payload": {"archives": list_project_archives(state)}})
                messages.append(msg_log(sys_agent, f"存档 [{archive_id}] 已恢复为项目 [{restored['projectId']}]", "success"))
            except Exception as e:
                messages.append(msg_log(sys_agent, f"存档 [{archive_id}] 恢复失败: {e}", "error"))

    elif cmd == "resume_project":
        project_id = data.get("projectId", "")
        if project_id:
            messages.extend(resume_project(state, project_id))
            messages.extend(schedule_idle_agents(state))
            messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "quick_submit":
        topic = data.get("topic", "")
        project_id = data.get("projectId", "")
        mode = data.get("mode", "lab")
        angles = data.get("researchAngles")
        if isinstance(angles, str) and angles.strip():
            angles = [a.strip() for a in re.split(r"[,，、;；]", angles) if a.strip()]
        elif not isinstance(angles, list):
            angles = None
        ref_papers = data.get("referencePapers")
        if isinstance(ref_papers, str) and ref_papers.strip():
            ref_papers = [p.strip() for p in re.split(r"[\n,，;；]", ref_papers) if p.strip()]
        elif not isinstance(ref_papers, list):
            ref_papers = None
        reference_files = data.get("referenceFiles")
        if not isinstance(reference_files, list):
            reference_files = None
        latex_files = data.get("latexFiles")
        if not isinstance(latex_files, list):
            latex_files = None
        workspace_dir = str(data.get("workspaceDir", "") or "").strip()
        main_tex_file = str(data.get("mainTexFile", "") or "").strip()
        path_overrides = {
            "codebases_dir": data.get("codebasesDir", ""),
            "datasets_dir": data.get("datasetsDir", ""),
            "checkpoints_dir": data.get("checkpointsDir", ""),
        }
        raw_layer_models = data.get("layerModels")
        layer_models = (
            {k: v for k, v in raw_layer_models.items() if v}
            if isinstance(raw_layer_models, dict) else None
        )
        messages.extend(quick_submit_project(
            state, topic, project_id, mode, angles, ref_papers,
            reference_files, path_overrides, latex_files,
            workspace_dir, main_tex_file, layer_models,
        ))
        messages.extend(schedule_idle_agents(state))
        messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "stop_agent":
        agent_id = data.get("agentId")
        agent = state.agents.get(agent_id)
        if agent:
            messages.extend(stop_agent(agent))

    elif cmd == "skip_task":
        messages.extend(_handle_skip_task(state, str(data.get("projectId", "") or ""), str(data.get("taskId", "") or "")))

    elif cmd == "retry_task":
        messages.extend(_handle_retry_task(state, str(data.get("projectId", "") or ""), str(data.get("taskId", "") or "")))

    elif cmd == "node_action":
        messages.extend(_handle_node_action(state, data))

    elif cmd == "approval_response":
        req_id = data.get("requestId", "")
        approved = data.get("approved", True)
        comment = data.get("comment", "")
        for agent in state.agents.values():
            if agent.status != "awaiting_approval" or not agent.run_dir:
                continue
            approval_file = Path(agent.run_dir) / "pending_approval.json"
            if not approval_file.exists():
                continue
            try:
                ap_data = json.loads(approval_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if ap_data.get("request_id") == req_id:
                result_path = Path(agent.run_dir) / "approval_result.json"
                result_path.write_text(json.dumps({
                    "request_id": req_id, "approved": approved, "comment": comment,
                }, ensure_ascii=False), encoding="utf-8")
                try:
                    approval_file.unlink()
                except OSError:
                    pass
                agent.status = "working"
                messages.append(msg_agent_update(agent))
                _action = "approved" if approved else "rejected"
                messages.append(msg_log(agent, f"Approval {_action}: {ap_data.get('description', req_id)}", "info" if approved else "warning"))
                break

    elif cmd == "set_approval_mode":
        mode = data.get("mode", "auto")
        if mode in ("auto", "confirm_writes", "confirm_all"):
            state.approval_mode = mode
            messages.append({"type": "system", "payload": {"message": f"Approval mode set to: {mode}"}})

    elif cmd == "steer_agent":
        project_id = data.get("projectId", "")
        layer = data.get("layer", "all")
        instruction = data.get("instruction", "")
        if project_id and instruction:
            count = _save_steering(project_id, layer, instruction, state)
            messages.append(msg_feedback_ack(
                f"steer-{_uid()}",
                f"Steering instruction sent to {count} agent(s) in {layer}: {instruction[:80]}",
                layer,
            ))

    elif cmd == "get_queues":
        messages.append(msg_queue_update(state.queues))

    elif cmd == "get_shared_results":
        if state.result_registry:
            messages.append({
                "type": "system",
                "payload": {"message": json.dumps(state.result_registry.summary(), ensure_ascii=False)},
            })

    elif cmd == "start_idea_factory":
        state.idea_factory_topic = data.get("topic", "")
        state.idea_factory_config = data.get("configPath", "")
        state.idea_factory_remaining = int(data.get("ideaCount", 0))
        state.idea_factory_produced = 0
        _sys_a = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        label = "无限" if state.idea_factory_remaining == -1 else str(state.idea_factory_remaining)
        messages.append(msg_log(_sys_a, f"Idea 工厂已启动: topic={state.idea_factory_topic[:50]}... count={label}", "info"))

    elif cmd == "stop_idea_factory":
        state.idea_factory_remaining = 0
        _sys_a = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(_sys_a, f"Idea 工厂已停止 (已产出 {state.idea_factory_produced} 个)", "info"))

    elif cmd == "set_discussion_mode":
        enabled = bool(data.get("enabled", False))
        state.discussion_mode = enabled
        rounds = data.get("rounds")
        if rounds is not None:
            state.discussion_rounds = int(rounds)

    elif cmd == "chat_input":
        content = data.get("content", "").strip()
        target_layer = data.get("targetLayer", "all")

        # P0: Slash commands for direct control
        if content.startswith("/"):
            parts = content[1:].split(None, 1)
            slash_cmd = parts[0].lower() if parts else ""
            slash_arg = parts[1] if len(parts) > 1 else ""

            if slash_cmd == "help":
                help_text = (
                    "Available commands:\n"
                    "  /stop <agent-name> - Stop an agent\n"
                    "  /skip <task-id> - Skip a task\n"
                    "  /retry <task-id> - Retry a failed task\n"
                    "  /focus <layer> <instruction> - Set focus direction\n"
                    "  /status - Show project status\n"
                    "  /pause - Pause current project\n"
                    "  /resume - Resume paused project\n"
                )
                messages.append(msg_feedback_ack(f"help-{_uid()}", help_text, target_layer))

            elif slash_cmd == "stop" and slash_arg:
                found = None
                for a in state.agents.values():
                    if slash_arg.lower() in a.name.lower() or slash_arg == a.id:
                        found = a
                        break
                if found:
                    messages.extend(stop_agent(found))
                    messages.append(msg_feedback_ack(f"stop-{_uid()}", f"Stopped agent: {found.name}", target_layer))
                else:
                    messages.append(msg_feedback_ack(f"stop-{_uid()}", f"Agent not found: {slash_arg}", target_layer))

            elif slash_cmd == "skip" and slash_arg:
                messages.extend(await handle_command(
                    state,
                    {**data, "command": "skip_task", "taskId": slash_arg, "projectId": data.get("projectId", "")},
                ))

            elif slash_cmd == "retry" and slash_arg:
                messages.extend(await handle_command(
                    state,
                    {**data, "command": "retry_task", "taskId": slash_arg, "projectId": data.get("projectId", "")},
                ))

            elif slash_cmd == "focus" and slash_arg:
                focus_parts = slash_arg.split(None, 1)
                focus_layer = focus_parts[0] if focus_parts else "all"
                focus_instr = focus_parts[1] if len(focus_parts) > 1 else slash_arg
                pid = data.get("projectId", "")
                if not pid:
                    for p in state.agents.values():
                        if p.project_id:
                            pid = p.project_id
                            break
                if pid:
                    messages.extend(await handle_command(
                        state,
                        {**data, "command": "steer_agent", "projectId": pid, "layer": focus_layer, "instruction": focus_instr},
                    ))
                else:
                    messages.append(msg_feedback_ack(f"focus-{_uid()}", "No active project found", target_layer))

            elif slash_cmd == "status":
                reply = _build_status_summary(state, target_layer)
                messages.append(msg_feedback_ack(f"qs-{_uid()}", reply, target_layer))

            elif slash_cmd in ("pause", "resume"):
                pid = data.get("projectId", "")
                if not pid:
                    for p in state.agents.values():
                        if p.project_id:
                            pid = p.project_id
                            break
                if pid:
                    sub_cmd = "pause_project" if slash_cmd == "pause" else "resume_project"
                    messages.extend(await handle_command(state, {**data, "command": sub_cmd, "projectId": pid}))
                else:
                    messages.append(msg_feedback_ack(f"{slash_cmd}-{_uid()}", "No active project", target_layer))
            else:
                messages.append(msg_feedback_ack(f"cmd-{_uid()}", f"Unknown command: /{slash_cmd}. Type /help for available commands.", target_layer))
        else:
            intent = await _classify_chat_intent(content, state)
            if intent == "query":
                reply = _build_status_summary(state, target_layer)
                messages.append(msg_feedback_ack(f"qs-{_uid()}", reply, target_layer))
            elif intent == "steer":
                pid = data.get("projectId", "")
                if not pid:
                    for p in state.agents.values():
                        if p.project_id:
                            pid = p.project_id
                            break
                if pid:
                    count = _save_steering(pid, target_layer, content, state)
                    messages.append(msg_feedback_ack(f"steer-{_uid()}", f"Direction sent to {count} agent(s): {content[:80]}", target_layer))
                else:
                    data["command"] = "human_feedback"
                    messages.extend(await handle_command(state, data))
            else:
                data["command"] = "human_feedback"
                messages.extend(await handle_command(state, data))

    elif cmd == "query_status":
        target_layer = data.get("targetLayer", "all")
        reply = _build_status_summary(state, target_layer)
        messages.append(msg_feedback_ack(f"qs-{_uid()}", reply, target_layer))

    elif cmd == "pause_project":
        project_id = data.get("projectId", "")
        if project_id:
            messages.extend(_pause_project(state, project_id))
            messages.extend(schedule_idle_agents(state))
            messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "restart_project":
        project_id = data.get("projectId", "")
        if project_id:
            messages.extend(_restart_project(state, project_id))
            messages.extend(schedule_idle_agents(state))
            messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "delete_project":
        project_id = data.get("projectId", "")
        messages.extend(_delete_project(state, project_id))
        state.lab_batches.pop(project_id, None)
        messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "human_feedback":
        content = data.get("content", "")
        target_layer = data.get("targetLayer", "all")
        message_id = data.get("messageId", f"fb-{_uid()}")

        sys_agent = LobsterAgent(
            id="system", name="系统", layer=target_layer if target_layer != "all" else "idea",
            run_id="", run_dir="", config_path="",
        )
        sys_agent.project_id = str(data.get("projectId", "") or "")
        messages.append(msg_log(sys_agent, f"收到人工反馈: {content[:80]}{'...' if len(content) > 80 else ''}", "info"))
        messages.append(msg_activity(
            sys_agent,
            "human_feedback",
            f"人工反馈 → {target_layer}: {content[:120]}{'...' if len(content) > 120 else ''}",
            detail=content,
            stage=None,
        ))

        _save_feedback(state, content, target_layer, message_id)

        injected_projects = []
        for agent in state.agents.values():
            if not agent.run_dir or agent.status not in ("working", "idle"):
                continue
            if target_layer != "all" and agent.layer != target_layer:
                continue
            if agent.project_id:
                injected_projects.append(agent.project_id)

        if injected_projects:
            unique = sorted(set(injected_projects))
            plan_hint = (
                f"已将反馈注入 {len(unique)} 个项目的 prompt 上下文中 "
                f"({', '.join(unique)})。"
                f"当前阶段完成后，下一个阶段的 LLM 将读取并参考你的反馈来调整执行计划。"
            )
        else:
            plan_hint = (
                f"已记录反馈。当前无匹配的运行中项目，反馈将在新任务启动时生效。"
            )
        messages.append(msg_feedback_ack(message_id, plan_hint, target_layer))

    elif cmd in ("get_metaprompt", "get_prompt"):
        scope = str(data.get("promptScope", "") or "").lower()
        if cmd == "get_prompt" and scope != "metaprompt":
            messages.append({
                "type": "metaprompt_error",
                "payload": {"error": "get_prompt requires promptScope=metaprompt"},
            })
        else:
            try:
                from researchclaw.metaprompt import resolve_metaprompt_overlay
            except ImportError as exc:
                messages.append({
                    "type": "metaprompt_error",
                    "payload": {"error": f"metaprompt unavailable: {exc}"},
                })
            else:
                project_dir, run_dir, err = _bridge_metaprompt_project_run_paths(state, data)
                if err:
                    messages.append({"type": "metaprompt_error", "payload": {"error": err}})
                else:
                    raw_node_id = str(data.get("nodeId") or "").strip()
                    node_id = _safe_metaprompt_node_id(raw_node_id) if raw_node_id else None
                    if raw_node_id and node_id is None:
                        messages.append({
                            "type": "metaprompt_error",
                            "payload": {"error": "invalid nodeId for node metaprompt"},
                        })
                        return messages
                    resolved = resolve_metaprompt_overlay(
                        project_dir=project_dir,
                        run_dir=run_dir,
                        node_id=node_id,
                    )
                    messages.append({
                        "type": "metaprompt_resolved",
                        "payload": {
                            "projectId": str(data.get("projectId", "") or "").strip(),
                            "runId": str(data.get("runId") or "").strip(),
                            "nodeId": node_id or "",
                            "versionHash": resolved.version_hash if resolved else "",
                            "sources": list(resolved.sources) if resolved else [],
                            "appendSystemPreview": (resolved.append_system[:2000] if resolved else ""),
                            "appendUserPreview": (resolved.append_user[:2000] if resolved else ""),
                        },
                    })

    elif cmd in ("save_metaprompt", "save_prompt"):
        scope = str(data.get("promptScope", "") or "").lower()
        if cmd == "save_prompt" and scope != "metaprompt":
            messages.append({
                "type": "metaprompt_error",
                "payload": {"error": "save_prompt requires promptScope=metaprompt"},
            })
        else:
            try:
                from researchclaw.metaprompt import (
                    append_metaprompt_version_record,
                    resolve_metaprompt_overlay,
                )
            except ImportError as exc:
                messages.append({
                    "type": "metaprompt_error",
                    "payload": {"error": f"metaprompt unavailable: {exc}"},
                })
            else:
                target, err = _bridge_metaprompt_write_target(state, data)
                layer = str(data.get("layer", "") or "").strip().lower()
                if err:
                    messages.append({"type": "metaprompt_error", "payload": {"error": err}})
                elif layer not in _METAPROMPT_LAYERS:
                    messages.append({
                        "type": "metaprompt_error",
                        "payload": {"error": f"invalid layer (expected one of {_METAPROMPT_LAYERS})"},
                    })
                elif layer == "node" and not _safe_metaprompt_node_id(str(data.get("nodeId") or "")):
                    messages.append({
                        "type": "metaprompt_error",
                        "payload": {"error": "invalid nodeId for node metaprompt"},
                    })
                else:
                    mp_root = target / ".researchclaw" / "metaprompts"
                    mp_root.mkdir(parents=True, exist_ok=True)
                    body = {
                        "system": str(data.get("system", "") or ""),
                        "user": str(data.get("user", "") or ""),
                    }
                    node_id_raw = str(data.get("nodeId") or "").strip()
                    safe_node_id = _safe_metaprompt_node_id(node_id_raw)
                    if layer == "node" and not safe_node_id:
                        messages.append({
                            "type": "metaprompt_error",
                            "payload": {"error": "invalid nodeId for node metaprompt"},
                        })
                    if layer == "node" and safe_node_id:
                        node_dir = mp_root / "nodes"
                        node_dir.mkdir(parents=True, exist_ok=True)
                        out = node_dir / f"{safe_node_id}.json"
                    else:
                        out = mp_root / f"{layer}.json"
                    out.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

                    project_dir, run_dir, err2 = _bridge_metaprompt_project_run_paths(state, data)
                    version_hash = ""
                    if not err2:
                        node_id = safe_node_id if layer == "node" else (node_id_raw or None)
                        resolved = resolve_metaprompt_overlay(
                            project_dir=project_dir,
                            run_dir=run_dir,
                            node_id=node_id,
                        )
                        version_hash = resolved.version_hash if resolved else ""
                        if resolved and str(data.get("recordVersion", "")).lower() in ("1", "true", "yes"):
                            append_metaprompt_version_record(
                                run_dir if run_dir is not None else target,
                                version_hash=resolved.version_hash,
                                layers_snapshot=body | {"layer": layer},
                            )
                    messages.append({
                        "type": "metaprompt_saved",
                        "payload": {
                            "path": str(out),
                            "versionHash": version_hash,
                            "layer": layer,
                        },
                    })
                    _sys = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
                    _sys.project_id = str(data.get("projectId", "") or "")
                    messages.append(msg_activity(
                        _sys,
                        "metaprompt_update",
                        f"保存 {layer} MetaPrompt",
                        detail=f"path={out}\nversion={version_hash}",
                        nodeId=safe_node_id if layer == "node" else "",
                        promptHash=version_hash,
                    ))

    elif cmd in ("reset_metaprompt", "reset_prompt"):
        scope = str(data.get("promptScope", "") or "").lower()
        if cmd == "reset_prompt" and scope != "metaprompt":
            messages.append({
                "type": "metaprompt_error",
                "payload": {"error": "reset_prompt requires promptScope=metaprompt"},
            })
        else:
            target, err = _bridge_metaprompt_write_target(state, data)
            layer = str(data.get("layer", "") or "").strip().lower()
            node_id_raw = str(data.get("nodeId") or "").strip()
            node_id = _safe_metaprompt_node_id(node_id_raw) if layer == "node" else (node_id_raw or None)
            if err:
                messages.append({"type": "metaprompt_error", "payload": {"error": err}})
            elif layer not in _METAPROMPT_LAYERS:
                messages.append({
                    "type": "metaprompt_error",
                    "payload": {"error": f"invalid layer (expected one of {_METAPROMPT_LAYERS})"},
                })
            elif layer == "node" and node_id is None:
                messages.append({
                    "type": "metaprompt_error",
                    "payload": {"error": "invalid nodeId for node metaprompt"},
                })
            else:
                removed = _bridge_metaprompt_delete_layer_files(target, layer, node_id=node_id)
                messages.append({
                    "type": "metaprompt_reset",
                    "payload": {"removed": removed, "layer": layer},
                })
                _sys = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
                _sys.project_id = str(data.get("projectId", "") or "")
                messages.append(msg_activity(
                    _sys,
                    "metaprompt_update",
                    f"重置 {layer} MetaPrompt",
                    detail="\n".join(removed),
                    nodeId=node_id or "",
                ))

    elif cmd in ("get_metaprompt_versions", "get_prompt_versions"):
        scope = str(data.get("promptScope", "") or "").lower()
        if cmd == "get_prompt_versions" and scope != "metaprompt":
            messages.append({
                "type": "metaprompt_error",
                "payload": {"error": "get_prompt_versions requires promptScope=metaprompt"},
            })
        else:
            try:
                from researchclaw.metaprompt import read_metaprompt_versions
            except ImportError as exc:
                messages.append({
                    "type": "metaprompt_error",
                    "payload": {"error": f"metaprompt unavailable: {exc}"},
                })
            else:
                target, err = _bridge_metaprompt_write_target(state, data)
                if err:
                    messages.append({"type": "metaprompt_error", "payload": {"error": err}})
                else:
                    messages.append({
                        "type": "metaprompt_versions",
                        "payload": {
                            "entries": read_metaprompt_versions(target),
                            "runDir": str(target),
                        },
                    })

    elif cmd == "get_download_url":
        project_id = data.get("projectId", "")
        filename = data.get("filename", "latex_package.zip")
        if project_id:
            messages.append({
                "type": "download_url",
                "payload": {
                    "projectId": project_id,
                    "filename": filename,
                    "url": f"/download/{project_id}/{filename}",
                },
            })

    elif cmd == "browse_path":
        req_path = str(data.get("path", "") or "").strip()
        messages.append(_browse_path(state, req_path))

    elif cmd == "scan_project":
        scan_dir = str(data.get("workspaceDir", "") or "").strip()
        main_tex_hint = str(data.get("mainTexFile", "") or "").strip()
        project_id = str(data.get("projectId", "") or "").strip()
        if scan_dir:
            # Check if this workspace already has an existing project
            existing_pid = _find_existing_project_by_workspace(state, scan_dir)
            ws_cfg = _load_workspace_config(scan_dir) if scan_dir else None

            scan_result = _deep_scan_project(scan_dir, main_tex_hint)
            payload: dict = {
                "projectId": existing_pid or project_id,
                "workspaceDir": scan_dir,
                **scan_result.to_dict(),
            }
            if existing_pid:
                payload["existingProjectId"] = existing_pid
                payload["existingConfig"] = ws_cfg or {}
            messages.append({
                "type": "project_scan_result",
                "payload": payload,
            })
        else:
            messages.append({
                "type": "project_scan_result",
                "payload": {
                    "projectId": project_id,
                    "workspaceDir": "",
                    "error": "workspaceDir is required",
                },
            })

    elif cmd == "planner_start":
        pid = str(data.get("projectId", "") or "").strip()
        scan_dir = str(data.get("workspaceDir", "") or "").strip()
        main_tex = str(data.get("mainTexFile", "") or "").strip()
        llm_cfg = data.get("llmConfig", {}) or {}
        base_url = str(llm_cfg.get("base_url", "") or "")
        api_key_val = str(llm_cfg.get("api_key", "") or "")
        model_name = str(llm_cfg.get("model", "") or "")
        if base_url or api_key_val or model_name:
            state.global_llm_config = {"base_url": base_url, "api_key": api_key_val, "model": model_name}
        # Reuse existing project ID if the workspace already has one
        if scan_dir:
            existing_pid = _find_existing_project_by_workspace(state, scan_dir)
            if existing_pid:
                pid = existing_pid
        scan_result = _deep_scan_project(scan_dir, main_tex) if scan_dir else None
        session = state.planner.get_or_create(
            pid, scan_result, base_url, api_key_val, model_name, main_tex, scan_dir,
        )
        messages.append({
            "type": "planner_status",
            "payload": session.to_status_dict(),
        })
        if scan_result:
            messages.append({
                "type": "project_scan_result",
                "payload": {
                    "projectId": pid,
                    "workspaceDir": scan_dir,
                    **scan_result.to_dict(),
                },
            })
        # Generate project name asynchronously via LLM
        from project_planner import _generate_project_name as _gen_name
        async def _do_gen_name(_s=session, _p=pid):
            try:
                name = await _gen_name(_s)
                if name:
                    _s.project_name = name
                    _name_msg = json.dumps({
                        "type": "project_name",
                        "payload": {"projectId": _p, "projectName": name},
                    })
                    for _c in state.clients:
                        try:
                            await _c.send(_name_msg)
                        except Exception:
                            pass
            except Exception:
                pass
        asyncio.create_task(_do_gen_name())

    elif cmd == "planner_chat":
        pid = str(data.get("projectId", "") or "").strip()
        user_msg = str(data.get("message", "") or "").strip()
        if pid and user_msg:
            async def _run_planner_chat(_pid=pid, _msg=user_msg):
                try:
                    async def _on_chunk(text: str):
                        await broadcast(state, [{
                            "type": "planner_chunk",
                            "payload": {"projectId": _pid, "text": text},
                        }])

                    reply, session = await state.planner.chat_stream(
                        _pid, _msg, _on_chunk,
                    )
                    final_msgs: list[dict] = [{
                        "type": "planner_status",
                        "payload": session.to_status_dict(),
                    }]
                    if session.proposals:
                        final_msgs.append({
                            "type": "planner_proposals",
                            "payload": {
                                "projectId": _pid,
                                "proposals": [p.to_dict() for p in session.proposals],
                            },
                        })
                    await broadcast(state, final_msgs)
                except Exception as e:
                    await broadcast(state, [{
                        "type": "planner_status",
                        "payload": {"projectId": _pid, "error": str(e)},
                    }])

            asyncio.create_task(_run_planner_chat())

    elif cmd == "planner_select":
        pid = str(data.get("projectId", "") or "").strip()
        proposal_ids = data.get("proposalIds", [])
        agent_counts = data.get("layerAgentCounts")
        if pid and proposal_ids:
            plan = state.planner.select_proposals(pid, proposal_ids, agent_counts)
            if plan:
                proj_dir = state.projects_dir() / pid
                proj_dir.mkdir(parents=True, exist_ok=True)
                state.planner.save_session(pid, proj_dir)
                messages.append({
                    "type": "planner_plan",
                    "payload": {
                        "projectId": pid,
                        **plan.to_dict(),
                    },
                })
            session = state.planner.get(pid)
            if session:
                messages.append({
                    "type": "planner_status",
                    "payload": session.to_status_dict(),
                })

    elif cmd == "planner_confirm":
        pid = str(data.get("projectId", "") or "").strip()
        if pid:
            plan = state.planner.confirm_plan(pid)
            if plan:
                proj_dir = state.projects_dir() / pid
                proj_dir.mkdir(parents=True, exist_ok=True)
                state.planner.save_session(pid, proj_dir)
                _planner_session = state.planner.get(pid)
                _plan_topic = plan.narrative or "学术规划项目"
                _ws_dir = _planner_session.workspace_dir if _planner_session else ""
                _ws_dir_parent = str(Path(_planner_session.main_tex_file).parent) if _planner_session and _planner_session.main_tex_file else _ws_dir
                # Use workspace .scholar/ as run_dir so all outputs go into user's project folder
                if _ws_dir:
                    _scholar_dir = Path(_ws_dir) / ".scholar"
                    _scholar_dir.mkdir(parents=True, exist_ok=True)
                    (_scholar_dir / "logs").mkdir(exist_ok=True)
                    (_scholar_dir / "backups").mkdir(exist_ok=True)
                    (_scholar_dir / "diffs").mkdir(exist_ok=True)
                    _plan_run_dir = str(_scholar_dir)
                    _pointer = proj_dir / "_workspace_link.json"
                    _write_json(_pointer, {"workspace_dir": _ws_dir, "scholar_dir": str(_scholar_dir)})
                else:
                    _plan_run_dir = str(proj_dir)
                _plan_config = _generate_config_from_template(
                    state, pid, _plan_topic,
                    codebases_dir=_ws_dir_parent or "",
                )
                _g_llm = state.global_llm_config
                if _g_llm and _g_llm.get("model") and Path(_plan_config).exists():
                    try:
                        _plan_config = _create_model_config(
                            _plan_config, _g_llm["model"], _plan_run_dir,
                            base_url=_g_llm.get("base_url", ""),
                            api_key=_g_llm.get("api_key", ""),
                        )
                    except Exception:
                        pass
                _save_project_meta(_plan_run_dir, pid, _plan_config, _plan_topic,
                                   mode="planner", workspace_dir=_ws_dir)
                _meta_path = Path(_plan_run_dir) / "project_meta.json"
                _meta = _read_json(_meta_path) or {}
                if _ws_dir:
                    _meta["workspace_dir"] = _ws_dir
                _plan_name = _planner_session.project_name if _planner_session else ""
                if _plan_name:
                    _meta["project_name"] = _plan_name
                _write_json(_meta_path, _meta)
                # Also write a copy of project_meta.json into runs/projects/<pid>/
                # so restart/resume can find config_path even when run_dir is .scholar/
                if _plan_run_dir != str(proj_dir):
                    _proj_meta = dict(_meta)
                    _write_json(proj_dir / "project_meta.json", _proj_meta)
                graph = state.task_graphs.create_from_plan(
                    pid, plan.to_dict(),
                    run_dir=_plan_run_dir,
                    config_path=_plan_config,
                )
                state.task_graphs.save_to_disk(pid, proj_dir)
                messages.append({
                    "type": "planner_plan",
                    "payload": {
                        "projectId": pid,
                        "confirmed": True,
                        **plan.to_dict(),
                    },
                })
                messages.append({
                    "type": "task_graph_update",
                    "payload": {"projectId": pid, **graph.to_dict()},
                })
                if plan.layer_agent_counts:
                    messages.extend(resize_agent_pool(state, plan.layer_agent_counts))
                messages.extend(schedule_idle_agents(state))
                messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "get_coordination":
        pid = str(data.get("projectId", "") or "").strip()
        if pid:
            sessions = state.coordinator.get_project_sessions(pid)
            messages.append({
                "type": "coordination_update",
                "payload": {
                    "projectId": pid,
                    "sessions": [s.to_dict() for s in sessions],
                },
            })

    elif cmd == "get_task_graph":
        pid = str(data.get("projectId", "") or "").strip()
        if pid:
            graph = state.task_graphs.get(pid)
            if graph:
                messages.append({
                    "type": "task_graph_update",
                    "payload": {"projectId": pid, **graph.to_dict()},
                })

    elif cmd == "tail_agent_log":
        agent_id = str(data.get("agentId", "") or "").strip()
        max_lines = int(data.get("maxLines", 200) or 200)
        agent = state.agents.get(agent_id)
        if agent and agent.run_dir:
            log_path = Path(agent.run_dir) / f"agent_{agent_id}.log"
            lines: list[str] = []
            if log_path.exists():
                try:
                    text = log_path.read_text(encoding="utf-8", errors="replace")
                    lines = text.splitlines()[-max_lines:]
                except Exception:
                    lines = ["[读取日志失败]"]
            messages.append({
                "type": "agent_log_tail",
                "payload": {
                    "agentId": agent_id,
                    "lines": lines,
                    "total": len(lines),
                },
            })
        else:
            messages.append({
                "type": "agent_log_tail",
                "payload": {"agentId": agent_id, "lines": [], "total": 0},
            })

    elif cmd == "agent_chat":
        agent_id = str(data.get("agentId", "") or "").strip()
        user_msg = str(data.get("message", "") or "").strip()
        if agent_id and user_msg:
            agent = state.agents.get(agent_id)
            run_dir = agent.run_dir if agent else ""
            if not run_dir and agent and agent.project_id:
                _proj_dir = state.projects_dir() / agent.project_id
                if _proj_dir.is_dir():
                    run_dir = str(_proj_dir)
            if not run_dir:
                for a in state.agents.values():
                    if a.id == agent_id:
                        if a.run_dir:
                            run_dir = a.run_dir
                            break
                        if a.project_id:
                            _pd = state.projects_dir() / a.project_id
                            if _pd.is_dir():
                                run_dir = str(_pd)
                                break
            if run_dir:
                msg_file = Path(run_dir) / "user_messages.jsonl"
                entry = json.dumps({
                    "role": "user",
                    "content": user_msg,
                    "timestamp": time.time(),
                    "agent_id": agent_id,
                }, ensure_ascii=False)
                try:
                    msg_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(msg_file, "a", encoding="utf-8") as f:
                        f.write(entry + "\n")
                except Exception as _chat_err:
                    logger.warning("agent_chat: failed to write %s: %s", msg_file, _chat_err)
                # Broadcast as a properly-formatted ActivityEvent
                _sys = LobsterAgent(
                    id=agent_id, name=agent.name if agent else agent_id,
                    layer=agent.layer if agent else "idea",
                    run_id="", run_dir="", config_path="",
                    project_id=agent.project_id if agent else "",
                )
                await broadcast(state, [
                    msg_activity(_sys, "user_message", user_msg),
                ])
                messages.append({
                    "type": "agent_chat_ack",
                    "payload": {"agentId": agent_id, "ok": True},
                })
            else:
                messages.append({
                    "type": "agent_chat_ack",
                    "payload": {"agentId": agent_id, "ok": False, "error": "Agent run_dir not found"},
                })

    elif cmd == "test_model_config":
        test_cfg = data.get("config", {})
        request_id = data.get("requestId", "")
        result = await _test_model_config(
            test_cfg.get("base_url", ""),
            test_cfg.get("api_key", ""),
            test_cfg.get("model", ""),
        )
        messages.append({
            "type": "test_model_result",
            "payload": {"requestId": request_id, **result},
        })

    elif cmd == "update_layer_models":
        project_id = data.get("projectId", "")
        new_layer_models = data.get("layerModels", {})
        if project_id:
            _update_ok = _update_project_layer_models(state, project_id, new_layer_models)
            messages.append({
                "type": "update_layer_models_result",
                "payload": {"projectId": project_id, "ok": _update_ok},
            })
            if _update_ok:
                messages.append(msg_project_list(list_all_projects(state)))

    elif cmd == "list_diffs":
        pid = str(data.get("projectId", "") or "").strip()
        diff_list: list[dict] = []
        if pid:
            for _scan_dir in [state.projects_dir() / pid, ]:
                _diff_dir = _scan_dir / "diffs"
                if not _diff_dir.exists():
                    _pointer = _scan_dir / "_workspace_link.json"
                    _ptr = _read_json(_pointer)
                    if _ptr and _ptr.get("scholar_dir"):
                        _diff_dir = Path(_ptr["scholar_dir"]) / "diffs"
                if _diff_dir.exists():
                    for fp in sorted(_diff_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                        try:
                            rec = json.loads(fp.read_text(encoding="utf-8"))
                            rec["_id"] = fp.stem
                            diff_list.append(rec)
                        except Exception:
                            pass
        messages.append({
            "type": "diff_list",
            "payload": {"projectId": pid, "diffs": diff_list},
        })

    elif cmd == "get_diff":
        pid = str(data.get("projectId", "") or "").strip()
        diff_id = str(data.get("diffId", "") or "").strip()
        diff_data: dict | None = None
        if pid and diff_id:
            for _scan_dir in [state.projects_dir() / pid, ]:
                _diff_dir = _scan_dir / "diffs"
                if not _diff_dir.exists():
                    _pointer = _scan_dir / "_workspace_link.json"
                    _ptr = _read_json(_pointer)
                    if _ptr and _ptr.get("scholar_dir"):
                        _diff_dir = Path(_ptr["scholar_dir"]) / "diffs"
                fp = _diff_dir / f"{diff_id}.json"
                if fp.exists():
                    try:
                        diff_data = json.loads(fp.read_text(encoding="utf-8"))
                    except Exception:
                        pass
        messages.append({
            "type": "diff_detail",
            "payload": {"projectId": pid, "diffId": diff_id, "data": diff_data},
        })

    elif cmd == "resize_pool":
        counts = data.get("layerCounts", {})
        if counts:
            messages.extend(resize_agent_pool(state, counts))
            for a in state.agents.values():
                messages.append(msg_agent_update(a))

    elif cmd == "list_kb_entries":
        pid = str(data.get("projectId", "") or "").strip()
        category = str(data.get("category", "") or "").strip()
        ws_dir = _get_workspace_dir(state, pid)
        entries = state.kb.list_entries(ws_dir, category) if ws_dir else []
        messages.append({
            "type": "kb_entries",
            "payload": {"projectId": pid, "entries": entries},
        })

    elif cmd == "search_kb":
        pid = str(data.get("projectId", "") or "").strip()
        query = str(data.get("query", "") or "").strip()
        ws_dir = _get_workspace_dir(state, pid)
        results = state.kb.search(ws_dir, query) if ws_dir and query else []
        messages.append({
            "type": "kb_search_results",
            "payload": {"projectId": pid, "query": query, "results": results},
        })

    elif cmd == "kb_stats":
        pid = str(data.get("projectId", "") or "").strip()
        ws_dir = _get_workspace_dir(state, pid)
        stats = state.kb.get_stats(ws_dir) if ws_dir else {"total": 0, "by_category": {}}
        messages.append({
            "type": "kb_stats",
            "payload": {"projectId": pid, **stats},
        })

    elif cmd == "get_stage_detail":
        pid = str(data.get("projectId", "") or "").strip()
        stage = int(data.get("stage", 0))
        if pid and stage:
            _detail_dirs = []
            proj_dir = state.projects_dir() / pid
            _ws_link = _read_json(proj_dir / "_workspace_link.json")
            if _ws_link and _ws_link.get("scholar_dir"):
                _detail_dirs.append(Path(_ws_link["scholar_dir"]))
            _detail_dirs.append(proj_dir)
            for angle in sorted(proj_dir.glob("run-*")):
                if angle.is_dir():
                    _detail_dirs.append(angle)

            _files_info: list[dict] = []
            _stage_dir_name = f"stage-{stage:02d}"
            for _dd in _detail_dirs:
                sd = _dd / _stage_dir_name
                if not sd.is_dir():
                    continue
                for f in sorted(sd.rglob("*")):
                    if f.is_file():
                        rel = str(f.relative_to(sd)).replace("\\", "/")
                        try:
                            _sz = f.stat().st_size
                            _mt = f.stat().st_mtime
                        except OSError:
                            _sz, _mt = 0, 0
                        _files_info.append({
                            "name": rel,
                            "size": _sz,
                            "modified": _mt,
                            "dir": str(sd),
                        })
                break

            _expected = STAGE_OUTPUTS.get(stage, [])
            _found = {fi["name"] for fi in _files_info}
            _status = "completed"
            for _eo in _expected:
                _eo_clean = _eo.rstrip("/")
                if _eo.endswith("/"):
                    if not any(fn.startswith(_eo_clean) for fn in _found):
                        _status = "incomplete"
                        break
                elif _eo_clean not in _found:
                    _status = "incomplete"
                    break
            if not _files_info:
                _status = "pending"

            messages.append({
                "type": "stage_detail",
                "payload": {
                    "projectId": pid,
                    "stage": stage,
                    "stageName": STAGE_NAMES.get(stage, f"S{stage}"),
                    "status": _status,
                    "expectedOutputs": _expected,
                    "files": _files_info,
                },
            })

    elif cmd == "get_node_detail":
        pid = str(data.get("projectId", "") or "").strip()
        node_id = str(data.get("nodeId", "") or data.get("taskId", "") or "").strip()
        if not pid or not node_id:
            messages.append({
                "type": "node_detail",
                "payload": {
                    "ok": False,
                    "error": "bad_request",
                    "message": "get_node_detail requires projectId and nodeId",
                    "projectId": pid,
                    "nodeId": node_id,
                },
            })
        else:
            messages.append({
                "type": "node_detail",
                "payload": _build_node_detail_payload(state, pid, node_id),
            })

    elif cmd == "get_artifact_preview":
        pid = str(data.get("projectId", "") or "").strip()
        stage = int(data.get("stage", 0))
        filename = str(data.get("filename", "") or "").strip()
        file_dir = str(data.get("dir", "") or "").strip()
        if pid and stage and filename:
            _preview_content = ""
            _preview_type = "text"
            _target = Path(file_dir) / filename if file_dir else None
            if not _target or not _target.exists():
                proj_dir = state.projects_dir() / pid
                _target = proj_dir / f"stage-{stage:02d}" / filename
            if _target and _target.exists() and _target.is_file():
                _sz = _target.stat().st_size
                _ext = _target.suffix.lower()
                if _ext in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
                    _preview_type = "image"
                    if _ext == ".svg":
                        try:
                            _preview_content = _target.read_text(encoding="utf-8", errors="replace")[:50000]
                        except Exception:
                            _preview_content = "(无法读取)"
                    else:
                        try:
                            _img_data = base64.b64encode(_target.read_bytes()[:2_000_000]).decode("ascii")
                            _preview_content = f"data:image/{_ext.lstrip('.')};base64,{_img_data}"
                        except Exception:
                            _preview_content = "(无法读取)"
                elif _ext in (".json", ".jsonl"):
                    _preview_type = "json"
                    try:
                        _raw = _target.read_text(encoding="utf-8", errors="replace")
                        if _ext == ".jsonl":
                            _lines = _raw.splitlines()[:50]
                            _preview_content = "\n".join(_lines)
                        else:
                            _preview_content = _raw[:50000]
                    except Exception:
                        _preview_content = "(无法读取)"
                elif _ext in (".yaml", ".yml"):
                    _preview_type = "yaml"
                    try:
                        _preview_content = _target.read_text(encoding="utf-8", errors="replace")[:50000]
                    except Exception:
                        _preview_content = "(无法读取)"
                else:
                    _preview_type = "markdown" if _ext == ".md" else "text"
                    try:
                        _preview_content = _target.read_text(encoding="utf-8", errors="replace")[:50000]
                    except Exception:
                        _preview_content = "(无法读取)"
            else:
                _preview_content = "(文件不存在)"

            messages.append({
                "type": "artifact_preview",
                "payload": {
                    "projectId": pid,
                    "stage": stage,
                    "filename": filename,
                    "contentType": _preview_type,
                    "content": _preview_content,
                    "size": _target.stat().st_size if _target and _target.exists() else 0,
                },
            })

    elif cmd == "set_global_llm":
        g_cfg = data.get("config", {}) or {}
        g_base = str(g_cfg.get("base_url", "") or "")
        g_key = str(g_cfg.get("api_key", "") or "")
        g_model = str(g_cfg.get("model", "") or "")
        if g_base or g_key or g_model:
            state.global_llm_config = {"base_url": g_base, "api_key": g_key, "model": g_model}
            messages.append({
                "type": "set_global_llm_result",
                "payload": {"ok": True},
            })

    return messages


def _test_model_config_sync(base_url: str, api_key: str, model: str) -> bool:
    """Synchronous connectivity test — returns True only if the endpoint
    responds with valid JSON containing an 'id' or 'choices' field."""
    import urllib.request
    import urllib.error

    if not base_url or not model:
        return False
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode("utf-8")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return isinstance(data, dict) and ("id" in data or "choices" in data)
    except Exception:
        return False


_LAYER_ORDER = ["idea", "experiment", "coding", "execution", "writing"]


def _get_workspace_dir(state: BridgeState, project_id: str) -> str:
    """Resolve the user's workspace directory for a project."""
    if not project_id:
        return ""
    # Check planner session first
    session = state.planner.get(project_id)
    if session and session.workspace_dir:
        return session.workspace_dir
    # Check project meta
    proj_dir = state.projects_dir() / project_id
    meta = _read_project_meta(str(proj_dir))
    if meta and meta.get("workspace_dir"):
        return meta["workspace_dir"]
    # Check workspace pointer
    pointer = _read_json(proj_dir / "_workspace_link.json")
    if pointer and pointer.get("workspace_dir"):
        return pointer["workspace_dir"]
    return ""


def _extract_layer_model_fields(cfg) -> tuple[str, str, str]:
    """Extract (model, base_url, api_key) from a layer_models entry (str or dict)."""
    if isinstance(cfg, str):
        return cfg, "", ""
    return cfg.get("model", ""), cfg.get("base_url", ""), cfg.get("api_key", "")


async def _test_model_config(base_url: str, api_key: str, model: str) -> dict:
    """Send a minimal chat completion request to verify the model endpoint is reachable."""
    import urllib.request
    import urllib.error

    if not base_url or not model:
        return {"ok": False, "error": "需要填写 API 地址和模型名称"}

    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        loop = asyncio.get_event_loop()
        def _do_request():
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        status, resp_text = await loop.run_in_executor(None, _do_request)
        if 200 <= status < 300:
            try:
                data = json.loads(resp_text)
                if isinstance(data, dict) and ("id" in data or "choices" in data):
                    return {"ok": True, "error": ""}
                return {"ok": False, "error": "API 返回格式异常（非标准 chat completion 响应）"}
            except (json.JSONDecodeError, ValueError):
                return {"ok": False, "error": "API 返回非 JSON 内容，请检查 base_url 是否需要加 /v1"}
        return {"ok": False, "error": f"HTTP {status}: {resp_text[:200]}"}
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body_text or e.reason}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _browse_path(state: BridgeState, req_path: str) -> dict:
    """Return directory listing for the folder picker UI (restricted to run/project roots)."""
    allow_roots = _browse_allowed_roots(state)
    if not allow_roots:
        return {"type": "browse_result", "payload": {
            "path": req_path, "parent": None, "entries": [], "error": "无可用根目录 (runs base 未配置)",
        }}

    if not req_path:
        entries: list[dict] = []
        for r in allow_roots:
            if r.is_dir():
                entries.append({
                    "name": r.name or str(r),
                    "path": str(r),
                    "type": "dir",
                })
        if not entries:
            return {"type": "browse_result", "payload": {
                "path": "", "parent": None, "entries": [],
                "error": "允许浏览的根目录尚不存在，请先确保 runs 目录已创建",
            }}
        return {"type": "browse_result", "payload": {
            "path": "", "parent": None, "entries": entries, "error": None,
        }}

    target = Path(req_path)
    if not target.exists():
        return {"type": "browse_result", "payload": {
            "path": req_path, "parent": None, "entries": [], "error": "路径不存在",
        }}
    if not target.is_dir():
        target = target.parent

    try:
        t_res = target.resolve()
    except (OSError, ValueError):
        return {"type": "browse_result", "payload": {
            "path": req_path, "parent": None, "entries": [], "error": "无法解析该路径",
        }}
    if not any(_ab_path_under_root(t_res, r) for r in allow_roots):
        return {"type": "browse_result", "payload": {
            "path": str(target), "parent": None, "entries": [],
            "error": "路径不在允许范围内（仅 runs/、projects/ 及 AGENT_BRIDGE_BROWSE_ROOTS）",
        }}

    parent = str(target.parent) if target.parent != target else None
    if parent:
        try:
            p_res = Path(parent).resolve()
            if not any(_ab_path_under_root(p_res, r) for r in allow_roots):
                parent = None
        except (OSError, ValueError):
            parent = None
    out_entries: list[dict] = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for item in items:
            if item.name.startswith("."):
                continue
            try:
                ir = item.resolve()
            except (OSError, ValueError):
                continue
            if not any(_ab_path_under_root(ir, r) for r in allow_roots):
                continue
            out_entries.append({
                "name": item.name,
                "path": str(item),
                "type": "dir" if item.is_dir() else "file",
            })
    except PermissionError:
        return {"type": "browse_result", "payload": {
            "path": str(target), "parent": parent, "entries": [], "error": "无权限访问",
        }}

    return {"type": "browse_result", "payload": {
        "path": str(target), "parent": parent, "entries": out_entries, "error": None,
    }}


def _scan_existing_artifacts(state: BridgeState) -> list[dict]:
    """Scan all project directories for completed stage artifacts to send on connect."""
    messages: list[dict] = []
    projects_dir = state.projects_dir()
    if not projects_dir.exists():
        return messages

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue
        project_id = proj_dir.name

        # Collect all run dirs: project root + any run-* sub-dirs (Lab mode)
        run_dirs: list[Path] = []
        angle_dirs = list(proj_dir.glob("run-*"))
        if angle_dirs:
            run_dirs.extend(d for d in angle_dirs if d.is_dir())
        else:
            run_dirs.append(proj_dir)

        seen: set[str] = set()
        for run_dir in run_dirs:
            for s, outputs in STAGE_OUTPUTS.items():
                stage_dir = run_dir / f"stage-{s:02d}"
                if not stage_dir.is_dir():
                    continue
                for expected in outputs:
                    if expected not in DISPLAY_ARTIFACTS:
                        continue
                    artifact_path = stage_dir / expected.rstrip("/")
                    dedup_key = f"{project_id}:{s}:{expected}"
                    if dedup_key in seen or not artifact_path.exists():
                        continue
                    seen.add(dedup_key)
                    size = "dir" if artifact_path.is_dir() else f"{artifact_path.stat().st_size / 1024:.1f} KB"
                    content = _extract_artifact_summary(artifact_path, expected)
                    messages.append(msg_artifact(
                        REPO_FOR_STAGE.get(s, "knowledge"), expected, project_id, size, project_id, content, stage=s,
                    ))
            discussion_path = run_dir / "discussion" / "discussion_transcript.md"
            dedup_key = f"{project_id}:discussion:discussion_transcript.md"
            if dedup_key not in seen and discussion_path.is_file():
                seen.add(dedup_key)
                size = f"{discussion_path.stat().st_size / 1024:.1f} KB"
                content = _extract_artifact_summary(discussion_path, "discussion_transcript.md")
                messages.append(msg_artifact(
                    "knowledge", "discussion_transcript.md", "沟通讨论", size, project_id, content
                ))
    return messages


async def ws_handler(state: BridgeState, websocket: websockets.ServerConnection):
    state.clients.add(websocket)
    print(f"[+] Client connected (total: {len(state.clients)})")
    for agent in state.agents.values():
        try:
            await websocket.send(json.dumps(msg_agent_update(agent), ensure_ascii=False))
        except websockets.ConnectionClosed:
            break
    try:
        await websocket.send(json.dumps(msg_queue_update(state.queues), ensure_ascii=False))
    except websockets.ConnectionClosed:
        pass

    # Send existing artifacts from completed stages
    try:
        for msg in _scan_existing_artifacts(state):
            await websocket.send(json.dumps(msg, ensure_ascii=False))
    except websockets.ConnectionClosed:
        pass

    try:
        async for raw in websocket:
            try:
                responses = await handle_command(state, json.loads(raw))
                await broadcast(state, responses)
            except json.JSONDecodeError:
                pass
    except websockets.ConnectionClosed:
        pass
    finally:
        state.clients.discard(websocket)
        print(f"[-] Client disconnected (total: {len(state.clients)})")


async def poll_loop(state: BridgeState, interval: float):
    while True:
        await asyncio.sleep(interval)
        all_messages: list[dict] = []

        # Drain EventBus events (real-time streaming from turn loops)
        if _HAS_EVENT_BUS:
            _drained_projects: set[str] = set()
            for agent in list(state.agents.values()):
                pid = agent.project_id
                if pid and pid not in _drained_projects:
                    _drained_projects.add(pid)
                    try:
                        bus = get_event_bus(pid)
                        events = bus.drain(max_events=50)
                        for evt in events:
                            all_messages.append(_event_to_ws_message(evt, agent))
                    except Exception:
                        pass

        for agent in list(state.agents.values()):
            prev_status = agent.status
            msgs = poll_agent(agent, state)
            all_messages.extend(msgs)

            # Detect layer completion → feed task queue
            if prev_status == "working" and agent.status == "done":
                # Auto-ingest completed stage artifacts into knowledge base
                _ws_dir = _get_workspace_dir(state, agent.project_id)
                if _ws_dir and agent.run_dir:
                    try:
                        _rd = Path(agent.run_dir)
                        for _sd in _rd.glob("stage-*"):
                            if _sd.is_dir():
                                for _f in _sd.iterdir():
                                    if _f.is_file() and _f.stat().st_size < 5_000_000:
                                        state.kb.ingest_file(
                                            _ws_dir, str(_f),
                                            source="pipeline",
                                            agent_id=agent.id,
                                            stage=agent.current_stage or 0,
                                        )
                    except Exception:
                        pass

                if getattr(agent, '_is_idea_factory_s7_only', False):
                    all_messages.extend(_on_idea_factory_s7_done(state, agent))
                elif getattr(agent, '_is_idea_factory', False):
                    all_messages.extend(_on_idea_factory_done(state, agent))
                elif getattr(agent, '_is_discussion_s8', False):
                    all_messages.extend(_on_discussion_s8_done(state, agent))
                else:
                    all_messages.extend(on_agent_done(state, agent))

            # Detect failure → diagnose, auto-recover if possible, track retry count
            if prev_status == "working" and agent.status == "error":
                _fail_pid = agent.project_id or "unknown"
                state._fail_counts[_fail_pid] = state._fail_counts.get(_fail_pid, 0) + 1
                _n_fails = state._fail_counts[_fail_pid]
                _MAX_RETRIES = 3

                # Diagnose failure category
                _diag_cat, _diag_detail = _diagnose_failure(agent)
                _cat_labels = {
                    "config_not_found": "配置文件缺失",
                    "missing_input": "前置产物缺失",
                    "api_error": "API/网络错误",
                    "timeout": "超时",
                    "import_error": "模块导入错误",
                    "unknown": "未知错误",
                }
                _cat_label = _cat_labels.get(_diag_cat, _diag_cat)
                all_messages.append(msg_log(
                    agent,
                    f"错误诊断: [{_cat_label}] {_diag_detail[:120]}",
                    "warning",
                ))

                # Attempt auto-recovery
                _orig_task = None
                if agent.assigned_task_id:
                    for q in state.queues.values():
                        for t in q.tasks:
                            if t.id == agent.assigned_task_id:
                                _orig_task = t
                                break
                        if _orig_task:
                            break

                _recovered, _action = _auto_recover(state, agent, _diag_cat, _orig_task)
                if _recovered and _n_fails < _MAX_RETRIES and _orig_task:
                    all_messages.append(msg_log(
                        agent,
                        f"自动恢复: {_action} (第 {_n_fails}/{_MAX_RETRIES} 次重试)",
                        "info",
                    ))
                    if _diag_cat == "api_error":
                        import asyncio as _aio
                        _aio.get_event_loop().call_later(10, lambda: None)
                    # Re-queue the task for retry
                    if agent.assigned_task_id:
                        for q in state.queues.values():
                            q.fail(agent.assigned_task_id)
                    _retry_task = Task(
                        id=f"task-{_uid()}",
                        project_id=_orig_task.project_id,
                        run_dir=_orig_task.run_dir,
                        config_path=_orig_task.config_path,
                        topic=_orig_task.topic,
                        source_layer=_orig_task.source_layer,
                        target_layer=_orig_task.target_layer,
                        created_at=_now_ms(),
                        stage_from=_orig_task.stage_from,
                        stage_to=_orig_task.stage_to,
                    )
                    _q_name = _queue_for_layer(_orig_task.target_layer)
                    if _q_name in state.queues:
                        state.queues[_q_name].push(_retry_task)
                        all_messages.append(msg_queue_update(state.queues))
                else:
                    if agent.assigned_task_id:
                        for q in state.queues.values():
                            q.fail(agent.assigned_task_id)

                # S12 sanity check failure → pause project and notify user
                if agent.layer == "coding" and agent.project_id:
                    _s12_err_msgs = _check_s12_sanity_failure(state, agent)
                    if _s12_err_msgs:
                        all_messages.extend(_s12_err_msgs)
                        continue

                if agent.layer == "execution" and agent.project_id:
                    released = state.gpu_allocator.release(agent.project_id)
                    if released:
                        all_messages.append(msg_log(agent, f"GPU {released} 已释放 (错误后)", "warning"))

                if _n_fails >= _MAX_RETRIES:
                    all_messages.append(msg_log(
                        agent,
                        f"项目 [{_fail_pid}] 连续失败 {_n_fails} 次 [{_cat_label}]，已停止自动重试。请检查日志后手动恢复。",
                        "error",
                    ))

                # Clean up discussion group if agent failed
                _batch_id = getattr(agent, '_idea_factory_batch_id', None)
                _disc_key = _batch_id or (agent.project_id if agent.project_id in state.discussion_groups else None)
                if _disc_key and _disc_key in state.discussion_groups:
                    _grp = state.discussion_groups[_disc_key]
                    if agent.id in _grp.agent_ids:
                        _grp.agent_ids.remove(agent.id)
                        _grp.run_dirs.pop(agent.id, None)
                        _grp.completed_s7.discard(agent.id)
                    remaining = [state.agents.get(a) for a in _grp.agent_ids if state.agents.get(a)]
                    waiting = [a for a in remaining if a.status == "waiting_discussion"]
                    if waiting and len(_grp.agent_ids) < 2:
                        sole = waiting[0]
                        all_messages.append(msg_log(
                            sole,
                            f"伙伴 agent 失败，跳过讨论 → 直接进入 S8 假设生成",
                            "warning", DISCUSSION_STAGE,
                        ))
                        sole.stage_progress[DISCUSSION_STAGE] = "skipped"
                        all_messages.append(msg_stage_update(sole.id, DISCUSSION_STAGE, "skipped"))
                        all_messages.extend(_launch_s8_for_agent(state, sole, _grp))
                        _grp.status = "done"
                    elif not remaining:
                        del state.discussion_groups[_disc_key]
                _reset_agent_idle(agent)
                all_messages.append(msg_agent_update(agent))

        # Poll active discussions
        for group in list(state.discussion_groups.values()):
            all_messages.extend(_poll_discussion(state, group))

        # Schedule idle agents
        sched_msgs = schedule_idle_agents(state)
        all_messages.extend(sched_msgs)

        # Periodically broadcast task graph updates (every ~5 poll cycles)
        if not hasattr(state, '_tg_broadcast_counter'):
            state._tg_broadcast_counter = 0  # type: ignore[attr-defined]
        state._tg_broadcast_counter += 1  # type: ignore[attr-defined]
        if state._tg_broadcast_counter >= 5:  # type: ignore[attr-defined]
            state._tg_broadcast_counter = 0  # type: ignore[attr-defined]
            for _tg_pid, _tg_graph in state.task_graphs.graphs.items():
                _has_active = any(
                    n.status in ("running", "ready")
                    for n in _tg_graph.nodes.values()
                )
                if _has_active:
                    all_messages.append({"type": "task_graph_update", "payload": {
                        "projectId": _tg_pid, **_tg_graph.to_dict(),
                    }})

        # Periodically broadcast project list (every ~10 poll cycles)
        if not hasattr(state, '_project_list_counter'):
            state._project_list_counter = 0  # type: ignore[attr-defined]
        state._project_list_counter += 1  # type: ignore[attr-defined]
        if state._project_list_counter >= 10:  # type: ignore[attr-defined]
            state._project_list_counter = 0  # type: ignore[attr-defined]
            all_messages.append(msg_project_list(list_all_projects(state)))

        await broadcast(state, all_messages)


# ── Startup ─────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace):
    _ct = (args.control_token or os.environ.get("AGENT_BRIDGE_CONTROL_TOKEN") or "").strip()
    state = BridgeState(
        python_path=args.python,
        agent_package_dir=args.agent_dir,
        runs_base_dir=args.runs_dir,
        gpu_allocator=GpuAllocator(args.total_gpus, args.gpus_per_project),
        auto_loop=args.auto_loop,
        discussion_mode=args.discussion_mode,
        discussion_rounds=args.discussion_rounds,
        discussion_models=[m.strip() for m in args.discussion_models.split(",") if m.strip()],
        idea_factory_topic=args.idea_topic,
        idea_factory_config=args.idea_config,
        idea_factory_remaining=args.idea_count,
        control_token=_ct,
    )

    # Initialize shared results registry
    _shared_results_path = Path(state.runs_base_dir).parent / "shared_results"
    try:
        from result_registry import ResultRegistry
        state.result_registry = ResultRegistry(str(_shared_results_path))
    except Exception:
        pass

    state.projects_dir().mkdir(parents=True, exist_ok=True)
    state.queues_dir().mkdir(parents=True, exist_ok=True)

    # Initialize queues (load from disk, clean stale tasks from prior run)
    _completed_projects: set[str] = set()
    for _pd in state.projects_dir().iterdir():
        if _pd.is_dir() and not _pd.name.startswith("_"):
            _cp = _read_json(_pd / "checkpoint.json")
            if _cp and _cp.get("last_completed_stage", 0) >= 22:
                _completed_projects.add(_pd.name)
    for queue_name in list(QUEUE_NAMES.keys()) + ["init_to_idea"]:
        q = TaskQueue(name=queue_name, path=state.queues_dir() / f"{queue_name}.json")
        q.load()
        _stale = 0
        _cleaned = 0
        for t in q.tasks:
            if t.status == "assigned":
                t.status = "failed"
                _stale += 1
        orig_len = len(q.tasks)
        q.tasks = [t for t in q.tasks if not (
            t.project_id in _completed_projects and t.status in ("pending", "assigned", "failed")
        )]
        _cleaned = orig_len - len(q.tasks)
        if _stale or _cleaned:
            q.save()
        if _stale:
            print(f"   [queue] {queue_name}: reset {_stale} stale assigned task(s)")
        if _cleaned:
            print(f"   [queue] {queue_name}: removed {_cleaned} task(s) for completed projects")
        state.queues[queue_name] = q

    # v2.0: Load existing task graphs from disk
    for _pd in state.projects_dir().iterdir():
        if _pd.is_dir() and not _pd.name.startswith("_"):
            _tg_path = _pd / "task_graph.json"
            if _tg_path.exists():
                state.task_graphs.load_from_disk(_pd.name, _pd)
                print(f"   [task_graph] loaded graph for {_pd.name}")

    # v2.0: Agent pool is created dynamically based on planning results.
    # On startup, check if any existing project has a plan with layer_agent_counts.
    _startup_counts: dict[str, int] = {}
    for _pd in state.projects_dir().iterdir():
        if not _pd.is_dir() or _pd.name.startswith("_"):
            continue
        _plan_file = _pd / "project_plan.json"
        if _plan_file.exists():
            try:
                _plan_data = json.loads(_plan_file.read_text(encoding="utf-8"))
                _lac = _plan_data.get("layer_agent_counts", {})
                for _lk, _lv in _lac.items():
                    _startup_counts[_lk] = max(_startup_counts.get(_lk, 0), int(_lv))
            except Exception:
                pass

    _pool_names = {"idea": "L1", "experiment": "L2", "coding": "L3", "execution": "L4", "writing": "L5"}
    if _startup_counts:
        for _layer_name in ["idea", "experiment", "coding", "execution", "writing"]:
            _cnt = max(1, min(_startup_counts.get(_layer_name, 1), 5))
            for _i in range(_cnt):
                _tag = chr(ord('A') + _i)
                create_agent(state, f"{_pool_names[_layer_name]}·{_tag}", _layer_name)
        print(f"   [pool] Created pool from plan: {_startup_counts}")
    else:
        # Always create a minimal pool (1 agent per layer) so cards are visible on startup
        for _layer_name in ["idea", "experiment", "coding", "execution", "writing"]:
            create_agent(state, f"{_pool_names[_layer_name]}·A", _layer_name)
        print(f"   [pool] Created default pool (1 per layer)")

    queued_tasks = sum(q.pending_count() for q in state.queues.values())

    print(f"📚 ScholarLab Agent Bridge v2 starting on ws://0.0.0.0:{args.port}")
    print(f"   Agent package: {args.agent_dir}")
    print(f"   Runs base:     {args.runs_dir}")
    print(f"   Python:        {args.python}")
    print(f"   Lobsters:      {len(state.agents)}")
    print(f"   GPUs:          {args.total_gpus}x ({args.gpus_per_project}/project, max {args.total_gpus // max(args.gpus_per_project, 1)} parallel)")
    print(f"   Auto-loop:     {'ON' if args.auto_loop else 'OFF'}")
    _disc_info = f"ON ({args.discussion_rounds} rounds, models: {args.discussion_models})" if args.discussion_mode else "OFF"
    print(f"   Discussion:    {_disc_info}")
    print(f"   Queued tasks:  {queued_tasks}")
    if _ct:
        print("   Control token: ON (set AGENT_BRIDGE_CONTROL_TOKEN or --control-token; 控制命令与 /download/ 需鉴权)")
    print()

    def _make_process_request(st: BridgeState):
        """Create HTTP request handler for file downloads."""
        from http import HTTPStatus
        from websockets.http11 import Response as WSResponse

        def _http_response(status_code: int, body: bytes, content_type: str = "text/plain",
                           extra_headers: dict | None = None) -> WSResponse:
            reason = HTTPStatus(status_code).phrase
            headers = {"Content-Type": content_type, "Content-Length": str(len(body)),
                        "Access-Control-Allow-Origin": "*"}
            if extra_headers:
                headers.update(extra_headers)
            return WSResponse(status_code, reason, websockets.Headers(headers), body)

        async def process_request(connection, request):
            if request.path.startswith("/download/"):
                from urllib.parse import unquote, parse_qs, urlparse
                raw = request.path
                q_tok = ""
                if "?" in raw:
                    u = urlparse("https://x" + raw)
                    raw = u.path
                    for k, v in parse_qs(u.query).items():
                        if k.lower() == "token" and v:
                            q_tok = (v[0] or "")[:4096]
                            break
                if st.control_token and not _http_download_token_ok(
                    st, request.headers, q_tok,
                ):
                    return _http_response(401, b"Unauthorized: missing or invalid download token\n")
                parts = unquote(raw[len("/download/"):]).split("/", 1)
                if len(parts) < 2:
                    return _http_response(404, b"Not found\n")
                project_id, filename = parts[0], parts[1]
                proj_dir = Path(st.runs_base_dir) / "projects" / project_id
                file_path = None
                search_roots: list[Path] = []
                if proj_dir.is_dir():
                    search_roots.append(proj_dir)
                cross_proj_dir = Path(st.runs_base_dir) / "projects" / "_cross_discussions" / project_id
                if cross_proj_dir.is_dir():
                    search_roots.append(cross_proj_dir)
                if not search_roots:
                    return _http_response(404, f"Project {project_id} not found\n".encode())

                for root_dir in search_roots:
                    for stage_dir in sorted(root_dir.glob("run-*/stage-*"), reverse=True):
                        candidate = stage_dir / filename
                        if candidate.is_file():
                            file_path = candidate
                            break
                    if file_path:
                        break
                    for discussion_dir in sorted(root_dir.glob("run-*/discussion"), reverse=True):
                        candidate = discussion_dir / filename
                        if candidate.is_file():
                            file_path = candidate
                            break
                    if file_path:
                        break
                    candidate = root_dir / "discussion" / filename
                    if candidate.is_file():
                        file_path = candidate
                        break
                if not file_path:
                    return _http_response(404, f"File {filename} not found\n".encode())
                try:
                    data = file_path.read_bytes()
                    return _http_response(200, data, "application/octet-stream",
                                          {"Content-Disposition": f'attachment; filename="{file_path.name}"'})
                except Exception as e:
                    return _http_response(500, f"Error: {e}\n".encode())
            return None
        return process_request

    handler = lambda ws: ws_handler(state, ws)
    async with websockets.serve(
        handler, "0.0.0.0", args.port,
        process_request=_make_process_request(state),
        max_size=64 * 1024 * 1024,
    ):
        await poll_loop(state, args.interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Bridge v2")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--agent-dir",
                        default=str(Path(__file__).resolve().parent.parent / "agent"))
    parser.add_argument("--runs-dir",
                        default=str(Path(__file__).resolve().parent.parent / "runs"))
    parser.add_argument("--pool-idea", type=int, default=2)
    parser.add_argument("--pool-exp", type=int, default=2)
    parser.add_argument("--pool-code", type=int, default=3)
    parser.add_argument("--pool-exec", type=int, default=4)
    parser.add_argument("--pool-write", type=int, default=2)
    parser.add_argument("--total-gpus", type=int, default=8,
                        help="Total number of GPUs available")
    parser.add_argument("--gpus-per-project", type=int, default=2,
                        help="GPUs allocated per project in execution layer")
    parser.add_argument("--auto-loop", action="store_true", default=False,
                        help="Enable auto-loop: L4 completion feeds back to L1 for new research cycle")
    parser.add_argument("--discussion-mode", action="store_true", default=True,
                        help="Enable L1 discussion: agents discuss after S7 before generating hypotheses")
    parser.add_argument("--no-discussion-mode", action="store_false", dest="discussion_mode",
                        help="Disable L1 discussion mode")
    parser.add_argument("--discussion-rounds", type=int, default=3,
                        help="Number of LLM discussion rounds (default: 3)")
    parser.add_argument("--discussion-models", default="gpt-5.3-codex-spark,claude-opus-4-6",
                        help="Comma-separated LLM models for discussion agents (different companies for diversity)")
    parser.add_argument("--idea-count", type=int, default=0,
                        help="Idea factory: number of ideas to produce (0=disabled, -1=infinite)")
    parser.add_argument("--idea-topic", default="",
                        help="Idea factory: research topic for idea generation")
    parser.add_argument("--idea-config", default="",
                        help="Idea factory: config file path")
    parser.add_argument(
        "--control-token", default="",
        help="非空时要求 WebSocket 控制命令与 HTTP /download/ 携带相同 token (亦可用环境变量 AGENT_BRIDGE_CONTROL_TOKEN)",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
