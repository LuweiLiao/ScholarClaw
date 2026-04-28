"""
ProjectPlanner — Interactive planning for ScholarLab v2.0.

Manages multi-turn LLM conversations to understand user goals,
generates 3 academic proposals, and produces a confirmed ProjectPlan
with task graph and per-layer agent counts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from project_scanner import ProjectScanResult

logger = logging.getLogger(__name__)

_RE_THINK = re.compile(r"<think>[\s\S]*?</think>", re.DOTALL)


def _strip_think(text: str) -> str:
    """Remove <think>…</think> reasoning blocks from model output."""
    return _RE_THINK.sub("", text).strip()

# ── Data classes ─────────────────────────────────────────────────────────────

LAYERS = ["idea", "experiment", "coding", "execution", "writing"]
LAYER_ZH = {
    "idea": "调研",
    "experiment": "实验设计",
    "coding": "编码",
    "execution": "执行",
    "writing": "写作",
}


@dataclass
class TaskSpec:
    id: str
    layer: str
    title: str
    description: str
    stage_from: int
    stage_to: int
    dependencies: list[str] = field(default_factory=list)


@dataclass
class AcademicProposal:
    id: str
    title: str
    summary: str
    approach: str
    estimated_effort: dict[str, int]
    task_breakdown: list[TaskSpec]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "approach": self.approach,
            "estimated_effort": self.estimated_effort,
            "task_breakdown": [
                {
                    "id": t.id,
                    "layer": t.layer,
                    "title": t.title,
                    "description": t.description,
                    "stage_from": t.stage_from,
                    "stage_to": t.stage_to,
                    "dependencies": t.dependencies,
                }
                for t in self.task_breakdown
            ],
        }


@dataclass
class ProjectPlan:
    narrative: str
    proposals_used: list[str]
    task_specs: list[TaskSpec]
    layer_agent_counts: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "narrative": self.narrative,
            "proposals_used": self.proposals_used,
            "task_specs": [
                {
                    "id": t.id,
                    "layer": t.layer,
                    "title": t.title,
                    "description": t.description,
                    "stage_from": t.stage_from,
                    "stage_to": t.stage_to,
                    "dependencies": t.dependencies,
                }
                for t in self.task_specs
            ],
            "layer_agent_counts": self.layer_agent_counts,
        }


@dataclass
class PlannerSession:
    project_id: str
    scan_result: ProjectScanResult | None = None
    chat_history: list[dict] = field(default_factory=list)
    proposals: list[AcademicProposal] | None = None
    selected_plan: ProjectPlan | None = None
    status: str = "chatting"  # chatting | proposing | confirmed
    main_tex_file: str = ""
    workspace_dir: str = ""
    project_name: str = ""

    # LLM config
    base_url: str = ""
    api_key: str = ""
    model: str = ""

    def to_status_dict(self) -> dict:
        return {
            "projectId": self.project_id,
            "projectName": self.project_name,
            "status": self.status,
            "chatHistory": self.chat_history,
            "proposals": [p.to_dict() for p in self.proposals] if self.proposals else None,
            "plan": self.selected_plan.to_dict() if self.selected_plan else None,
        }


# ── PlannerManager ───────────────────────────────────────────────────────────

class PlannerManager:
    """Manages planner sessions across multiple projects."""

    def __init__(self) -> None:
        self.sessions: dict[str, PlannerSession] = {}

    def get_or_create(
        self,
        project_id: str,
        scan_result: ProjectScanResult | None = None,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        main_tex_file: str = "",
        workspace_dir: str = "",
    ) -> PlannerSession:
        if project_id not in self.sessions:
            self.sessions[project_id] = PlannerSession(
                project_id=project_id,
                scan_result=scan_result,
                base_url=base_url,
                api_key=api_key,
                model=model,
                main_tex_file=main_tex_file,
                workspace_dir=workspace_dir,
            )
        session = self.sessions[project_id]
        if scan_result:
            session.scan_result = scan_result
        if base_url:
            session.base_url = base_url
        if api_key:
            session.api_key = api_key
        if model:
            session.model = model
        if main_tex_file:
            session.main_tex_file = main_tex_file
        if workspace_dir:
            session.workspace_dir = workspace_dir
        return session

    def get(self, project_id: str) -> PlannerSession | None:
        return self.sessions.get(project_id)

    def remove(self, project_id: str) -> None:
        self.sessions.pop(project_id, None)

    async def chat(
        self,
        project_id: str,
        user_message: str,
    ) -> tuple[str, PlannerSession]:
        """Send a user message and get AI reply. May trigger proposal generation."""
        session = self.sessions.get(project_id)
        if not session:
            raise ValueError(f"No planner session for project {project_id}")

        session.chat_history.append({"role": "user", "content": user_message})

        system_prompt = _build_system_prompt(session)
        messages_for_llm = [{"role": "system", "content": system_prompt}]
        messages_for_llm.extend(session.chat_history)

        should_propose = _should_generate_proposals(session)
        if should_propose:
            messages_for_llm.append({
                "role": "system",
                "content": (
                    "用户已经充分描述了目标。现在请生成3个学术方案。"
                    "严格按以下JSON格式输出，不要添加其他文字：\n"
                    "```json\n"
                    '{"proposals": [\n'
                    '  {"title": "方案名称", "summary": "一句话摘要", '
                    '"approach": "详细方法描述(3-5句)", '
                    '"estimated_effort": {"idea": 2, "experiment": 1, "coding": 3, "execution": 2, "writing": 2}, '
                    '"tasks": [\n'
                    '    {"layer": "idea", "title": "任务名", "description": "具体描述", '
                    '"stage_from": 1, "stage_to": 8},\n'
                    '    {"layer": "coding", "title": "任务名", "description": "具体描述", '
                    '"stage_from": 10, "stage_to": 13}\n'
                    "  ]}\n"
                    "]}\n"
                    "```\n"
                    "stage_from/stage_to 范围: idea=1-8, experiment=9, coding=10-13, execution=14-18, writing=19-22"
                ),
            })

        reply = await _call_llm(
            session.base_url,
            session.api_key,
            session.model,
            messages_for_llm,
            max_tokens=4096,
        )

        clean_reply = _strip_think(reply)

        if should_propose:
            proposals = _parse_proposals(reply)
            if proposals:
                session.proposals = proposals
                session.status = "proposing"
                session.chat_history.append({
                    "role": "assistant",
                    "content": "我已根据你的需求生成了3个学术方案，请在方案面板中查看和选择。",
                })
                return "我已根据你的需求生成了3个学术方案，请在方案面板中查看和选择。", session
            else:
                session.chat_history.append({"role": "assistant", "content": clean_reply or reply})
                return clean_reply or reply, session
        else:
            session.chat_history.append({"role": "assistant", "content": clean_reply or reply})
            return clean_reply or reply, session

    async def chat_stream(
        self,
        project_id: str,
        user_message: str,
        on_chunk,
    ) -> tuple[str, "PlannerSession"]:
        """Like chat(), but streams AI reply via *on_chunk(text)* callback."""
        session = self.sessions.get(project_id)
        if not session:
            raise ValueError(f"No planner session for project {project_id}")

        session.chat_history.append({"role": "user", "content": user_message})

        system_prompt = _build_system_prompt(session)
        messages_for_llm = [{"role": "system", "content": system_prompt}]
        messages_for_llm.extend(session.chat_history)

        should_propose = _should_generate_proposals(session)
        if should_propose:
            messages_for_llm.append({
                "role": "system",
                "content": (
                    "用户已经充分描述了目标。现在请生成3个学术方案。"
                    "严格按以下JSON格式输出，不要添加其他文字：\n"
                    "```json\n"
                    '{"proposals": [\n'
                    '  {"title": "方案名称", "summary": "一句话摘要", '
                    '"approach": "详细方法描述(3-5句)", '
                    '"estimated_effort": {"idea": 2, "experiment": 1, "coding": 3, "execution": 2, "writing": 2}, '
                    '"tasks": [\n'
                    '    {"layer": "idea", "title": "任务名", "description": "具体描述", '
                    '"stage_from": 1, "stage_to": 8},\n'
                    '    {"layer": "coding", "title": "任务名", "description": "具体描述", '
                    '"stage_from": 10, "stage_to": 13}\n'
                    "  ]}\n"
                    "]}\n"
                    "```\n"
                    "stage_from/stage_to 范围: idea=1-8, experiment=9, coding=10-13, execution=14-18, writing=19-22"
                ),
            })

        reply = await _call_llm_stream(
            session.base_url,
            session.api_key,
            session.model,
            messages_for_llm,
            on_chunk,
            max_tokens=4096,
        )

        clean_reply = _strip_think(reply)

        if should_propose:
            proposals = _parse_proposals(reply)
            if proposals:
                session.proposals = proposals
                session.status = "proposing"
                session.chat_history.append({
                    "role": "assistant",
                    "content": "我已根据你的需求生成了3个学术方案，请在方案面板中查看和选择。",
                })
                return "我已根据你的需求生成了3个学术方案，请在方案面板中查看和选择。", session
            else:
                session.chat_history.append({"role": "assistant", "content": clean_reply or reply})
                return clean_reply or reply, session
        else:
            session.chat_history.append({"role": "assistant", "content": clean_reply or reply})
            return clean_reply or reply, session

    def select_proposals(
        self,
        project_id: str,
        proposal_ids: list[str],
        layer_agent_counts: dict[str, int] | None = None,
    ) -> ProjectPlan | None:
        """User selects one or more proposals to combine into a plan."""
        session = self.sessions.get(project_id)
        if not session or not session.proposals:
            return None

        selected = [p for p in session.proposals if p.id in proposal_ids]
        if not selected:
            return None

        all_tasks: list[TaskSpec] = []
        for p in selected:
            all_tasks.extend(p.task_breakdown)

        _dedup: dict[str, TaskSpec] = {}
        for t in all_tasks:
            key = f"{t.layer}:{t.title}"
            if key not in _dedup:
                _dedup[key] = t
        merged_tasks = list(_dedup.values())

        _assign_dependencies(merged_tasks)

        counts = layer_agent_counts or {}
        for layer in LAYERS:
            if layer not in counts:
                layer_tasks = [t for t in merged_tasks if t.layer == layer]
                counts[layer] = max(1, min(len(layer_tasks), 5))

        for layer in counts:
            counts[layer] = max(1, min(counts[layer], 5))

        narratives = [p.summary for p in selected]
        narrative = " + ".join(narratives)

        plan = ProjectPlan(
            narrative=narrative,
            proposals_used=[p.id for p in selected],
            task_specs=merged_tasks,
            layer_agent_counts=counts,
        )
        session.selected_plan = plan
        session.status = "confirmed"
        return plan

    def confirm_plan(self, project_id: str) -> ProjectPlan | None:
        """Mark the plan as confirmed and return it for execution."""
        session = self.sessions.get(project_id)
        if not session or not session.selected_plan:
            return None
        session.status = "confirmed"
        return session.selected_plan

    def save_session(self, project_id: str, project_dir: Path) -> None:
        """Persist session to disk."""
        session = self.sessions.get(project_id)
        if not session:
            return
        chat_dir = project_dir / "chat_history"
        chat_dir.mkdir(parents=True, exist_ok=True)
        out = chat_dir / "planning_session.json"
        data = {
            "project_id": session.project_id,
            "status": session.status,
            "chat_history": session.chat_history,
            "proposals": [p.to_dict() for p in session.proposals] if session.proposals else None,
            "plan": session.selected_plan.to_dict() if session.selected_plan else None,
        }
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        if session.selected_plan:
            plan_path = project_dir / "project_plan.json"
            plan_path.write_text(
                json.dumps(session.selected_plan.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


# ── Prompt construction ──────────────────────────────────────────────────────

def _build_system_prompt(session: PlannerSession) -> str:
    parts: list[str] = [
        "你是一位顶级学术研究助手，正在帮助用户规划科研项目。",
        "你的任务是通过对话了解用户的学术目标，然后生成可执行的研究方案。",
        "",
        "对话策略：",
        "1. 首先确认你对项目现状的理解是否正确",
        "2. 追问用户的具体目标（补充实验？完善论文？探索新方向？）",
        "3. 了解目标期刊/会议级别、时间限制等约束",
        "4. 当你收集到足够信息后（通常2-3轮对话），告诉用户你将生成方案",
        "",
        "【重要】回复格式要求：",
        "- 使用中文回复",
        "- 简洁专业，每次回复不超过300字",
        "- 主动提出建设性建议和追问",
        "- 每当你提出问题时，必须给出3个推荐选项供用户选择，格式如下：",
        '  [选项A] 具体方案描述',
        '  [选项B] 具体方案描述',
        '  [选项C] 具体方案描述',
        "  用户也可以自己输入其他答案。",
        "- 如果一次提出多个问题，每个问题都要各自附带3个选项。",
    ]

    if session.main_tex_file:
        parts.append("")
        parts.append(f"【主论文文件】{session.main_tex_file}")
        parts.append("请围绕这篇论文进行工作规划，所有改动最终要体现在这个 .tex 文件中。")

    if session.workspace_dir:
        parts.append("")
        parts.append(f"【工作目录】{session.workspace_dir}")
        parts.append("【重要原则】必须在用户已有文件上进行修改，不要创建新的独立文件。"
                     "修改前自动备份原文件，所有产物输出到用户的项目文件夹中。")

    if session.scan_result:
        parts.append("\n" + "=" * 40)
        parts.append("【项目现状分析】")
        parts.append(session.scan_result.summary_text)
        parts.append("=" * 40)

    return "\n".join(parts)


def _should_generate_proposals(session: PlannerSession) -> bool:
    """Heuristic: generate proposals after 2+ user messages."""
    user_msgs = [m for m in session.chat_history if m["role"] == "user"]
    if len(user_msgs) < 2:
        return False
    if session.proposals is not None:
        return False
    last_msg = user_msgs[-1]["content"].lower()
    trigger_words = ["可以了", "生成方案", "开始", "确认", "就这样", "够了", "ok", "好的"]
    if any(w in last_msg for w in trigger_words):
        return True
    return len(user_msgs) >= 3


async def _generate_project_name(
    session: PlannerSession,
) -> str:
    """Generate a concise project name using LLM based on workspace and scan info."""
    from pathlib import Path as _Path

    folder_name = _Path(session.workspace_dir).name if session.workspace_dir else ""
    tex_name = _Path(session.main_tex_file).stem if session.main_tex_file else ""

    scan_info = ""
    if session.scan_result:
        sr = session.scan_result
        scan_info = f"论文标题: {sr.title}\n" if sr.title else ""
        if sr.sections:
            section_titles = [s.title for s in sr.sections[:5]]
            scan_info += f"章节: {', '.join(section_titles)}\n"
        if sr.abstract:
            scan_info += f"摘要: {sr.abstract[:200]}\n"

    prompt = (
        "根据以下项目信息，生成一个简洁的中文项目名称（10-20个字），要求能准确概括研究内容。\n"
        "只输出项目名称，不要加引号或其他标点。\n\n"
        f"项目文件夹: {folder_name}\n"
        f"主文件: {tex_name}\n"
        f"{scan_info}"
    )

    if not session.base_url or not session.model:
        # Fallback: derive name from folder/tex name
        if tex_name and tex_name.lower() not in ("main", "paper", "manuscript", "draft", "article"):
            return tex_name.replace("-", " ").replace("_", " ").title()
        if folder_name:
            return folder_name.replace("-", " ").replace("_", " ").title()
        return ""

    try:
        result = await _call_llm(
            session.base_url, session.api_key, session.model,
            [{"role": "user", "content": prompt}],
            max_tokens=50, temperature=0.3,
        )
        name = result.strip().strip('"\'').strip()
        if len(name) > 40:
            name = name[:40]
        return name
    except Exception:
        if tex_name and tex_name.lower() not in ("main", "paper", "manuscript", "draft", "article"):
            return tex_name.replace("-", " ").replace("_", " ").title()
        if folder_name:
            return folder_name.replace("-", " ").replace("_", " ").title()
        return ""


# ── LLM calling ──────────────────────────────────────────────────────────────

async def _call_llm(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    if not base_url or not model:
        return "LLM 未配置。请先在项目设置中配置模型信息（base_url、api_key、model）。"

    url = base_url.rstrip("/") + "/chat/completions"
    # Some providers (MiniMax) don't support the "system" role
    _no_sys = "minimax" in base_url.lower() or "minimaxi" in base_url.lower()
    if _no_sys:
        converted = []
        for m in messages:
            if m.get("role") == "system":
                converted.append({"role": "user", "content": f"[System Instructions]\n{m['content']}"})
                converted.append({"role": "assistant", "content": "Understood."})
            else:
                converted.append(m)
        messages = converted
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8", errors="replace")

    try:
        raw = await loop.run_in_executor(None, _do)
        data = json.loads(raw)
        if isinstance(data, dict) and "choices" in data:
            return data["choices"][0]["message"]["content"]
        return raw[:2000]
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        logger.error("LLM HTTP error %s: %s", e.code, err_body)
        return f"LLM 调用失败 (HTTP {e.code}): {err_body[:200]}"
    except Exception as e:
        logger.error("LLM call error: %s", e)
        return f"LLM 调用失败: {e}"


_STREAM_SENTINEL = object()


async def _call_llm_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    on_chunk,
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    """Streaming LLM call using requests.iter_lines() — the proven SSE approach."""
    if not base_url or not model:
        fallback = "LLM 未配置。请先在项目设置中配置模型信息（base_url、api_key、model）。"
        await on_chunk(fallback)
        return fallback

    import requests as _requests

    url = base_url.rstrip("/") + "/chat/completions"
    _no_sys = "minimax" in base_url.lower() or "minimaxi" in base_url.lower()
    if _no_sys:
        converted = []
        for m in messages:
            if m.get("role") == "system":
                converted.append({"role": "user", "content": f"[System Instructions]\n{m['content']}"})
                converted.append({"role": "assistant", "content": "Understood."})
            else:
                converted.append(m)
        messages = converted
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    req_headers = {"Content-Type": "application/json"}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _blocking_read():
        """Thread: use requests with stream=True + iter_lines for real-time SSE."""
        resp = None
        try:
            resp = _requests.post(
                url,
                json=payload,
                headers=req_headers,
                stream=True,
                timeout=180,
            )

            if resp.status_code != 200:
                resp.encoding = "utf-8"
                err = resp.text[:500]
                logger.error("LLM stream HTTP %s: %s", resp.status_code, err)
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    f"LLM 调用失败 (HTTP {resp.status_code}): {err[:200]}",
                )
                return

            # iter_lines() returns bytes; we decode as utf-8 ourselves
            # because requests defaults to ISO-8859-1 when charset is missing
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith(":"):
                    continue
                if line == "data: [DONE]":
                    break
                if line.startswith("data: "):
                    try:
                        chunk_data = json.loads(line[6:])
                        delta = (chunk_data.get("choices") or [{}])[0].get("delta", {})
                        text = delta.get("content", "")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, text)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        pass

        except Exception as e:
            logger.error("LLM stream error: %s", e)
            loop.call_soon_threadsafe(
                queue.put_nowait, f"LLM 调用失败: {e}"
            )
        finally:
            if resp:
                resp.close()
            loop.call_soon_threadsafe(queue.put_nowait, _STREAM_SENTINEL)

    reader_future = loop.run_in_executor(None, _blocking_read)

    collected: list[str] = []
    try:
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=180)
            if item is _STREAM_SENTINEL:
                break
            collected.append(item)
            await on_chunk(item)
    except asyncio.TimeoutError:
        logger.error("LLM stream timed out after 180s")
        if not collected:
            fallback = "LLM 流式响应超时（180秒无新数据）。"
            await on_chunk(fallback)
            return fallback

    await reader_future
    return "".join(collected)


# ── Proposal parsing ─────────────────────────────────────────────────────────

def _parse_proposals(text: str) -> list[AcademicProposal] | None:
    """Extract proposals JSON from LLM output text."""
    cleaned = text.strip()
    if "```json" in cleaned:
        start = cleaned.index("```json") + len("```json")
        end = cleaned.index("```", start) if "```" in cleaned[start:] else len(cleaned)
        cleaned = cleaned[start:start + (end - start)].strip()
    elif "```" in cleaned:
        start = cleaned.index("```") + 3
        end = cleaned.index("```", start) if "```" in cleaned[start:] else len(cleaned)
        cleaned = cleaned[start:start + (end - start)].strip()

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        brace_start = cleaned.find("{")
        brace_end = cleaned.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                data = json.loads(cleaned[brace_start:brace_end + 1])
            except (json.JSONDecodeError, ValueError):
                logger.warning("Failed to parse proposals JSON")
                return None
        else:
            return None

    raw_proposals = data.get("proposals", []) if isinstance(data, dict) else []
    if not raw_proposals:
        return None

    proposals: list[AcademicProposal] = []
    for i, rp in enumerate(raw_proposals[:3]):
        tasks: list[TaskSpec] = []
        for j, rt in enumerate(rp.get("tasks", [])):
            tasks.append(TaskSpec(
                id=f"task-{uuid.uuid4().hex[:8]}",
                layer=rt.get("layer", "idea"),
                title=rt.get("title", f"任务{j + 1}"),
                description=rt.get("description", ""),
                stage_from=int(rt.get("stage_from", 1)),
                stage_to=int(rt.get("stage_to", 8)),
            ))
        proposals.append(AcademicProposal(
            id=f"proposal-{chr(65 + i)}",
            title=rp.get("title", f"方案{chr(65 + i)}"),
            summary=rp.get("summary", ""),
            approach=rp.get("approach", ""),
            estimated_effort=rp.get("estimated_effort", {}),
            task_breakdown=tasks,
        ))

    return proposals if proposals else None


def _assign_dependencies(tasks: list[TaskSpec]) -> None:
    """Auto-assign cross-layer dependencies based on layer order.

    Each task depends on ALL tasks from the nearest preceding layer that
    actually has tasks (not necessarily the immediate previous layer).
    """
    layer_order = {layer: i for i, layer in enumerate(LAYERS)}
    by_layer: dict[str, list[TaskSpec]] = {}
    for t in tasks:
        by_layer.setdefault(t.layer, []).append(t)

    for t in tasks:
        t_order = layer_order.get(t.layer, 0)
        if t_order == 0:
            continue
        deps: list[str] = []
        for prev_idx in range(t_order - 1, -1, -1):
            prev_layer = LAYERS[prev_idx]
            prev_tasks = by_layer.get(prev_layer, [])
            if prev_tasks:
                deps = [pt.id for pt in prev_tasks]
                break
        t.dependencies = deps
