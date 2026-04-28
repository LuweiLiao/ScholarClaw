"""
ScholarLab full pipeline E2E example.

This script exercises scan -> plan -> run -> monitor flows without hard-coding
private credentials. Configure the LLM through environment variables:

  SCHOLARLAB_LLM_BASE_URL=https://api.example.com/v1
  SCHOLARLAB_LLM_API_KEY=...
  SCHOLARLAB_LLM_MODEL=...

Optional:
  SCHOLARLAB_WS_URL=ws://localhost:8906
  SCHOLARLAB_E2E_PROJECT_DIR=examples/llm-research-project
  SCHOLARLAB_E2E_MAIN_TEX=main.tex
  SCHOLARLAB_E2E_TIMEOUT=180
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError as exc:  # pragma: no cover - setup guard for manual runs
    raise SystemExit("Install websockets first: python -m pip install websockets") from exc


ROOT = Path(__file__).resolve().parents[1]
WS_URL = os.environ.get("SCHOLARLAB_WS_URL", "ws://localhost:8906")
PROJECT_DIR = Path(
    os.environ.get("SCHOLARLAB_E2E_PROJECT_DIR", ROOT / "examples" / "llm-research-project")
).resolve()
MAIN_TEX = os.environ.get("SCHOLARLAB_E2E_MAIN_TEX", "main.tex")
TIMEOUT = int(os.environ.get("SCHOLARLAB_E2E_TIMEOUT", "180"))

LLM_CONFIG = {
    "base_url": os.environ.get("SCHOLARLAB_LLM_BASE_URL", ""),
    "api_key": os.environ.get("SCHOLARLAB_LLM_API_KEY", ""),
    "model": os.environ.get("SCHOLARLAB_LLM_MODEL", ""),
}


def require_llm_config() -> None:
    missing = [key for key, value in LLM_CONFIG.items() if not value]
    if missing:
        names = ", ".join(f"SCHOLARLAB_LLM_{key.upper()}" for key in missing)
        raise SystemExit(f"Missing required LLM environment variables: {names}")


def log_message(msg: dict) -> None:
    msg_type = msg.get("type", "?")
    payload = msg.get("payload", {})
    if msg_type == "agent_update":
        print(
            "  [agent] "
            f"{payload.get('name', '?')} status={payload.get('status')} "
            f"stage={payload.get('currentStage')} "
            f"task={payload.get('currentTask', '')[:50]}"
        )
    elif msg_type == "agent_activity":
        print(
            "  [activity] "
            f"{payload.get('agentName', '?')} "
            f"[{payload.get('activityType')}] {payload.get('summary', '')[:60]}"
        )
    elif msg_type == "task_graph_update":
        nodes = payload.get("nodes", {})
        status_counts: dict[str, int] = {}
        for node in nodes.values():
            status = node.get("status", "?")
            status_counts[status] = status_counts.get(status, 0) + 1
        print(f"  [taskgraph] {len(nodes)} nodes: {status_counts}")
    elif msg_type == "approval_request":
        print(
            "  [approval] "
            f"{payload.get('agentName', '?')} wants to "
            f"{payload.get('actionType')}: {payload.get('description', '')[:60]}"
        )
    elif msg_type == "planner_status":
        print(
            "  [planner] "
            f"status={payload.get('status')} "
            f"proposals={len(payload.get('proposals') or [])} "
            f"chatHistory={len(payload.get('chatHistory') or [])}"
        )
    elif msg_type == "project_scan_result":
        paper = payload.get("paper")
        experiment = payload.get("experiment", {})
        if paper:
            print(
                "  [scan] "
                f"tex_files={len(paper.get('tex_files', []))} "
                f"sections={len(paper.get('sections', []))} "
                f"completeness={paper.get('completeness_pct', 0)}%"
            )
        print(
            "  [scan] "
            f"code_files={len(experiment.get('code_files', []))} "
            f"frameworks={experiment.get('frameworks', [])}"
        )
    elif msg_type in {"log", "chat_message", "system"}:
        print(f"  [{msg_type}] {str(payload)[:120]}")


async def recv_until(ws, received: list[dict], msg_type: str, timeout: int = 60) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            received.append(msg)
            if msg.get("type") in {
                "agent_update",
                "agent_activity",
                "task_graph_update",
                "approval_request",
                "log",
                "planner_status",
                "project_scan_result",
                "system",
                "chat_message",
            }:
                log_message(msg)
            if msg.get("type") == msg_type:
                return msg
        except asyncio.TimeoutError:
            continue
    return None


async def drain(ws, received: list[dict], seconds: int = 3) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1)
            msg = json.loads(raw)
            received.append(msg)
            log_message(msg)
        except asyncio.TimeoutError:
            continue


async def test_pipeline() -> None:
    require_llm_config()
    print("=" * 70)
    print("ScholarLab Full Pipeline E2E Example")
    print(f"  WebSocket: {WS_URL}")
    print(f"  Project:   {PROJECT_DIR}")
    print(f"  Main TeX:  {MAIN_TEX}")
    print("=" * 70)

    received: list[dict] = []
    project_id = f"test-llm-{int(time.time()) % 100000}"

    async with websockets.connect(WS_URL, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"command": "set_global_llm", "config": LLM_CONFIG}))
        await drain(ws, received, 2)

        await ws.send(
            json.dumps(
                {
                    "command": "scan_project",
                    "workspaceDir": str(PROJECT_DIR),
                    "mainTexFile": MAIN_TEX,
                    "projectId": project_id,
                }
            )
        )
        await recv_until(ws, received, "project_scan_result", timeout=30)

        await ws.send(
            json.dumps(
                {
                    "command": "planner_start",
                    "projectId": project_id,
                    "workspaceDir": str(PROJECT_DIR),
                    "mainTexFile": MAIN_TEX,
                    "llmConfig": LLM_CONFIG,
                }
            )
        )
        await recv_until(ws, received, "planner_status", timeout=60)

        await ws.send(
            json.dumps(
                {
                    "command": "planner_chat",
                    "projectId": project_id,
                    "message": (
                        "Compare decoder-only, encoder-decoder, and MoE LLM architectures; "
                        "analyze scaling laws and alignment methods; then propose experiments "
                        "that extend the provided paper skeleton."
                    ),
                }
            )
        )

        proposal_ids: list[str] = []
        for _ in range(12):
            msg = await recv_until(ws, received, "planner_status", timeout=15)
            if not msg:
                continue
            proposals = msg.get("payload", {}).get("proposals") or []
            proposal_ids = [proposal.get("id", f"proposal_{i}") for i, proposal in enumerate(proposals)]
            if proposal_ids:
                break

        if proposal_ids:
            await ws.send(
                json.dumps(
                    {
                        "command": "planner_select",
                        "projectId": project_id,
                        "proposalIds": [proposal_ids[0]],
                        "layerAgentCounts": {
                            "idea": 2,
                            "experiment": 1,
                            "coding": 1,
                            "execution": 1,
                            "writing": 1,
                        },
                    }
                )
            )
            await drain(ws, received, 3)

        await ws.send(json.dumps({"command": "planner_confirm", "projectId": project_id}))

        start_time = time.time()
        metrics = {"agent_update": 0, "agent_activity": 0, "task_graph_update": 0}
        while time.time() - start_time < TIMEOUT:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                msg = json.loads(raw)
                received.append(msg)
                msg_type = msg.get("type", "")
                if msg_type in metrics:
                    metrics[msg_type] += 1
                if msg_type == "approval_request":
                    request_id = msg.get("payload", {}).get("requestId", "")
                    await ws.send(
                        json.dumps(
                            {
                                "command": "approval_response",
                                "requestId": request_id,
                                "approved": True,
                            }
                        )
                    )
                log_message(msg)
            except asyncio.TimeoutError:
                await ws.send(
                    json.dumps(
                        {
                            "command": "chat_input",
                            "content": "/status",
                            "targetLayer": "all",
                            "projectId": project_id,
                        }
                    )
                )

        print("\nTEST SUMMARY")
        print(f"  Total messages:     {len(received)}")
        print(f"  Agent updates:      {metrics['agent_update']}")
        print(f"  Activity events:    {metrics['agent_activity']}")
        print(f"  TaskGraph updates:  {metrics['task_graph_update']}")


if __name__ == "__main__":
    asyncio.run(test_pipeline())
