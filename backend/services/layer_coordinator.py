"""
LayerCoordinator — 3-Phase multi-agent collaboration for ScholarLab v2.0.

When multiple agents work on the same layer, they go through:
  Phase 1: Coordination Discussion — agree on division of labor
  Phase 2: Parallel Execution — each agent runs its assigned task
  Phase 3: Cross Review — agents review each other's work (optional)
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

LAYER_ZH = {
    "idea": "调研",
    "experiment": "实验设计",
    "coding": "编码",
    "execution": "执行",
    "writing": "写作",
}


@dataclass
class CoordinationMessage:
    agent_id: str
    agent_name: str
    content: str
    phase: str  # "discussion" | "review"
    timestamp: float = 0.0


@dataclass
class CoordinationSession:
    project_id: str
    layer: str
    agent_ids: list[str] = field(default_factory=list)
    agent_names: dict[str, str] = field(default_factory=dict)
    task_titles: dict[str, str] = field(default_factory=dict)
    messages: list[CoordinationMessage] = field(default_factory=list)
    coordination_plan: str = ""
    review_summary: str = ""
    phase: str = "pending"  # pending | discussing | executing | reviewing | done
    enable_review: bool = True

    def to_dict(self) -> dict:
        return {
            "projectId": self.project_id,
            "layer": self.layer,
            "agentIds": self.agent_ids,
            "agentNames": self.agent_names,
            "taskTitles": self.task_titles,
            "messages": [
                {
                    "agentId": m.agent_id,
                    "agentName": m.agent_name,
                    "content": m.content,
                    "phase": m.phase,
                    "timestamp": m.timestamp,
                }
                for m in self.messages
            ],
            "coordinationPlan": self.coordination_plan,
            "reviewSummary": self.review_summary,
            "phase": self.phase,
        }


class LayerCoordinator:
    """Manages multi-agent coordination within a single layer."""

    def __init__(self) -> None:
        self.sessions: dict[str, CoordinationSession] = {}

    def _session_key(self, project_id: str, layer: str) -> str:
        return f"{project_id}:{layer}"

    def get_session(self, project_id: str, layer: str) -> CoordinationSession | None:
        return self.sessions.get(self._session_key(project_id, layer))

    def get_project_sessions(self, project_id: str) -> list[CoordinationSession]:
        return [s for s in self.sessions.values() if s.project_id == project_id]

    async def run_discussion(
        self,
        project_id: str,
        layer: str,
        agent_ids: list[str],
        agent_names: dict[str, str],
        task_titles: dict[str, str],
        base_url: str,
        api_key: str,
        model: str,
        rounds: int = 2,
        on_message: Callable[[CoordinationSession], Awaitable[None]] | None = None,
    ) -> CoordinationSession:
        """Phase 1: Run a coordination discussion among agents."""
        key = self._session_key(project_id, layer)
        session = CoordinationSession(
            project_id=project_id,
            layer=layer,
            agent_ids=agent_ids,
            agent_names=agent_names,
            task_titles=task_titles,
            phase="discussing",
        )
        self.sessions[key] = session

        layer_zh = LAYER_ZH.get(layer, layer)
        tasks_desc = "\n".join(
            f"- {aid}: {task_titles.get(aid, '未分配')}"
            for aid in agent_ids
        )

        system_prompt = (
            f"你们是{layer_zh}层的{len(agent_ids)}个AI助手，需要协调分工。\n"
            f"当前任务分配：\n{tasks_desc}\n\n"
            "请讨论：\n"
            "1. 各自任务的理解和执行计划\n"
            "2. 接口约定（数据格式、文件命名等）\n"
            "3. 可能的冲突和解决方案\n\n"
            "每次回复不超过200字，简洁实用。"
        )

        discussion_history: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]

        import time
        for round_idx in range(rounds):
            for agent_id in agent_ids:
                name = agent_names.get(agent_id, agent_id)
                role_prompt = (
                    f"你是 {name}，负责：{task_titles.get(agent_id, '待定')}。"
                    f"请发言（第{round_idx + 1}轮）。"
                )
                msgs = discussion_history + [{"role": "user", "content": role_prompt}]
                reply = await _call_llm(base_url, api_key, model, msgs, max_tokens=512)

                msg = CoordinationMessage(
                    agent_id=agent_id,
                    agent_name=name,
                    content=reply,
                    phase="discussion",
                    timestamp=time.time(),
                )
                session.messages.append(msg)
                discussion_history.append({
                    "role": "assistant",
                    "content": f"[{name}]: {reply}",
                })

                if on_message:
                    await on_message(session)

        summary_prompt = (
            "请总结以上讨论，生成一份简洁的《分工协议》，包括：\n"
            "1. 各Agent的具体分工\n"
            "2. 接口约定\n"
            "3. 注意事项\n"
            "用Markdown格式，不超过300字。"
        )
        msgs = discussion_history + [{"role": "user", "content": summary_prompt}]
        coordination_plan = await _call_llm(base_url, api_key, model, msgs, max_tokens=1024)
        session.coordination_plan = coordination_plan
        session.phase = "executing"

        if on_message:
            await on_message(session)

        return session

    async def run_cross_review(
        self,
        project_id: str,
        layer: str,
        results: dict[str, str],
        base_url: str,
        api_key: str,
        model: str,
        on_message: Callable[[CoordinationSession], Awaitable[None]] | None = None,
    ) -> CoordinationSession | None:
        """Phase 3: Cross-review after execution."""
        key = self._session_key(project_id, layer)
        session = self.sessions.get(key)
        if not session:
            return None

        session.phase = "reviewing"
        if on_message:
            await on_message(session)

        import time
        review_history: list[dict] = [
            {"role": "system", "content": "你是学术代码和论文审查专家。请对同事的工作进行简洁评审。"},
        ]

        for agent_id in session.agent_ids:
            name = session.agent_names.get(agent_id, agent_id)
            other_results = {
                aid: res for aid, res in results.items() if aid != agent_id
            }
            if not other_results:
                continue

            review_prompt = (
                f"你是 {name}，请审查以下同事的工作产物：\n"
                + "\n".join(
                    f"- {session.agent_names.get(aid, aid)}: {res[:500]}"
                    for aid, res in other_results.items()
                )
                + "\n\n请给出简短评审（优点、问题、建议），不超过200字。"
            )
            msgs = review_history + [{"role": "user", "content": review_prompt}]
            reply = await _call_llm(base_url, api_key, model, msgs, max_tokens=512)

            msg = CoordinationMessage(
                agent_id=agent_id,
                agent_name=name,
                content=reply,
                phase="review",
                timestamp=time.time(),
            )
            session.messages.append(msg)

            if on_message:
                await on_message(session)

        summary_prompt = "请综合所有审查意见，生成一份简短的审查总结（不超过200字）。"
        all_reviews = "\n".join(
            f"[{m.agent_name}]: {m.content}"
            for m in session.messages if m.phase == "review"
        )
        msgs = [
            {"role": "system", "content": "你是学术审查协调人。"},
            {"role": "user", "content": f"审查意见：\n{all_reviews}\n\n{summary_prompt}"},
        ]
        review_summary = await _call_llm(base_url, api_key, model, msgs, max_tokens=512)
        session.review_summary = review_summary
        session.phase = "done"

        if on_message:
            await on_message(session)

        return session

    def save_session(self, project_id: str, layer: str, project_dir: Path) -> None:
        key = self._session_key(project_id, layer)
        session = self.sessions.get(key)
        if not session:
            return

        coord_dir = project_dir / "coordination"
        coord_dir.mkdir(parents=True, exist_ok=True)

        if session.coordination_plan:
            plan_path = coord_dir / f"L{_layer_num(layer)}_coordination.md"
            plan_path.write_text(session.coordination_plan, encoding="utf-8")

        if session.review_summary:
            review_path = coord_dir / f"L{_layer_num(layer)}_reviews.md"
            review_path.write_text(session.review_summary, encoding="utf-8")

        session_path = coord_dir / f"L{_layer_num(layer)}_session.json"
        session_path.write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def mark_executing(self, project_id: str, layer: str) -> None:
        key = self._session_key(project_id, layer)
        session = self.sessions.get(key)
        if session:
            session.phase = "executing"

    def mark_done(self, project_id: str, layer: str) -> None:
        key = self._session_key(project_id, layer)
        session = self.sessions.get(key)
        if session:
            session.phase = "done"


def _layer_num(layer: str) -> int:
    return {"idea": 1, "experiment": 2, "coding": 3, "execution": 4, "writing": 5}.get(layer, 0)


# ── LLM calling (same pattern as project_planner.py) ────────────────────────

async def _call_llm(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> str:
    if not base_url or not model:
        return "(LLM 未配置)"

    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    loop = asyncio.get_event_loop()

    def _do():
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")

    try:
        raw = await loop.run_in_executor(None, _do)
        data = json.loads(raw)
        if isinstance(data, dict) and "choices" in data:
            return data["choices"][0]["message"]["content"]
        return raw[:1000]
    except Exception as e:
        logger.error("LayerCoordinator LLM error: %s", e)
        return f"(LLM 调用失败: {e})"
