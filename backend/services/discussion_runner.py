#!/usr/bin/env python3
"""
L1 Discussion Runner — Multi-round LLM debate between agent perspectives.

Collects synthesis.md from multiple L1 agents who independently researched the
same topic, runs a structured multi-round debate via the LLM, and produces a
consensus synthesis that enriches all agents' context for hypothesis generation.

Usage:
    python discussion_runner.py \
        --config /path/to/config.arc.yaml \
        --synthesis-dirs /path/to/agent-a/stage-07 /path/to/agent-b/stage-07 \
        --output /path/to/discussion_output \
        --rounds 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

AGENT_PACKAGE_DIR = str(Path(__file__).resolve().parent.parent / "agent")
if AGENT_PACKAGE_DIR not in sys.path:
    sys.path.insert(0, AGENT_PACKAGE_DIR)

from researchclaw.config import RCConfig
from researchclaw.llm import create_llm_client

try:
    from researchclaw.pipeline.activity_writer import write_event as _aw_write_event
except Exception:  # noqa: BLE001
    _aw_write_event = None


_MIRROR_DIRS: list[Path] = []


def _mirror_event(event_type: str, summary: str, detail: str = "", **extra) -> None:
    """Write an activity event to every mirrored run dir + the discussion output."""
    if _aw_write_event is None:
        return
    for d in _MIRROR_DIRS:
        try:
            _aw_write_event(d, event_type, summary, detail=detail, **extra)
        except Exception:  # noqa: BLE001
            pass


def _read_synthesis(stage_dir: Path) -> str | None:
    for name in ("synthesis.md", "synthesis.txt"):
        f = stage_dir / name
        if f.is_file():
            return f.read_text(encoding="utf-8")
    return None


def _label(index: int) -> str:
    return chr(ord("A") + index)


SYSTEM_PRESENT = """\
You are a senior research coordinator facilitating a multi-agent research discussion.
Multiple research agents have independently studied the same topic and produced their
own literature syntheses. Your task is to analyze each agent's perspective and identify:
1. Key findings unique to each perspective
2. Common themes across perspectives
3. Knowledge gaps that no perspective addressed
4. Contradictions or disagreements between perspectives

Be specific and cite which perspective (Agent A, Agent B, etc.) each point comes from.
Respond in the same language as the syntheses (Chinese if they are in Chinese)."""

SYSTEM_CRITIQUE = """\
You are a critical research reviewer. Given the initial analysis of multiple research
perspectives, your task is to:
1. Evaluate the strength of evidence for each key finding
2. Identify potential biases in individual perspectives
3. Find complementary findings that could be combined for stronger conclusions
4. Highlight the most promising research directions that emerge from combining perspectives
5. Note any methodological differences that explain contradictions

Be rigorous and constructive. Respond in the same language as the input."""

SYSTEM_CONSENSUS = """\
You are a research synthesis expert. Based on the multi-round discussion of independent
research perspectives, produce a unified consensus synthesis that:
1. Integrates the strongest findings from all perspectives
2. Resolves contradictions with reasoned explanations
3. Preserves novel insights that appeared in only one perspective
4. Identifies the most promising hypotheses suggested by the combined evidence
5. Notes remaining uncertainties and open questions

Format the output as a well-structured markdown document with clear sections.
This consensus will be used by all agents to generate research hypotheses.
Respond in the same language as the input."""


def run_discussion(
    llm,
    topic: str,
    syntheses: dict[str, str],
    num_rounds: int,
    output_dir: Path,
) -> tuple[str, str]:
    """Run the multi-round discussion and return (transcript, consensus)."""
    transcript_parts: list[str] = []
    agent_labels = list(syntheses.keys())

    perspectives_block = "\n\n".join(
        f"## {label}\n\n{text}" for label, text in syntheses.items()
    )

    # ── Round 1: Present and analyze perspectives ──
    print(f"  [Round 1/{num_rounds}] Presenting perspectives...")
    user_r1 = (
        f"Research topic: {topic}\n\n"
        f"The following are independent literature syntheses from {len(syntheses)} "
        f"research agents who studied this topic independently:\n\n"
        f"{perspectives_block}\n\n"
        f"Please analyze these perspectives following your instructions."
    )
    resp_r1 = llm.chat(
        [{"role": "user", "content": user_r1}],
        system=SYSTEM_PRESENT,
        max_tokens=4096,
    )
    analysis = resp_r1.content
    transcript_parts.append(f"# Discussion Transcript\n\nTopic: {topic}\n")
    transcript_parts.append(f"Participants: {', '.join(agent_labels)}\n")
    transcript_parts.append(f"## Round 1: Perspective Analysis\n\n{analysis}\n")
    print(f"    Done ({resp_r1.total_tokens} tokens)")

    # ── Round 2: Critical review ──
    if num_rounds >= 2:
        print(f"  [Round 2/{num_rounds}] Critical review...")
        user_r2 = (
            f"Original perspectives:\n\n{perspectives_block}\n\n"
            f"---\n\nInitial analysis:\n\n{analysis}\n\n"
            f"Please provide your critical review following your instructions."
        )
        resp_r2 = llm.chat(
            [{"role": "user", "content": user_r2}],
            system=SYSTEM_CRITIQUE,
            max_tokens=4096,
        )
        critique = resp_r2.content
        transcript_parts.append(f"## Round 2: Critical Review\n\n{critique}\n")
        print(f"    Done ({resp_r2.total_tokens} tokens)")
    else:
        critique = analysis

    # ── Round 3+: Consensus synthesis ──
    if num_rounds >= 3:
        print(f"  [Round 3/{num_rounds}] Building consensus...")
        user_r3 = (
            f"Research topic: {topic}\n\n"
            f"Original perspectives:\n\n{perspectives_block}\n\n"
            f"---\n\nPerspective analysis:\n\n{analysis}\n\n"
            f"---\n\nCritical review:\n\n{critique}\n\n"
            f"Please produce the consensus synthesis following your instructions."
        )
        resp_r3 = llm.chat(
            [{"role": "user", "content": user_r3}],
            system=SYSTEM_CONSENSUS,
            max_tokens=8192,
        )
        consensus = resp_r3.content
        transcript_parts.append(f"## Round 3: Consensus Synthesis\n\n{consensus}\n")
        print(f"    Done ({resp_r3.total_tokens} tokens)")
    else:
        consensus_prompt = (
            f"Research topic: {topic}\n\n"
            f"Perspectives:\n\n{perspectives_block}\n\n"
            f"Discussion so far:\n\n{critique}\n\n"
            f"Please produce a unified consensus synthesis."
        )
        resp_c = llm.chat(
            [{"role": "user", "content": consensus_prompt}],
            system=SYSTEM_CONSENSUS,
            max_tokens=8192,
        )
        consensus = resp_c.content
        transcript_parts.append(f"## Consensus\n\n{consensus}\n")

    transcript = "\n---\n\n".join(transcript_parts)
    return transcript, consensus


def main():
    parser = argparse.ArgumentParser(description="L1 Discussion Runner")
    parser.add_argument("--config", required=True, help="Path to config.arc.yaml")
    parser.add_argument(
        "--synthesis-dirs", nargs="+", required=True,
        help="Paths to stage-07 dirs containing synthesis.md",
    )
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--rounds", type=int, default=3, help="Number of discussion rounds")
    parser.add_argument("--topic", default="", help="Research topic override")
    parser.add_argument(
        "--mirror-run-dirs", nargs="*", default=[],
        help="Additional run dirs to mirror activity events into (so each agent's "
             "supervisor timeline sees the discussion).",
    )
    args = parser.parse_args()

    # Build the mirror set: the discussion output + all participant run_dirs.
    output_dir_path = Path(args.output)
    mirror_targets: list[Path] = [output_dir_path]
    for d in args.mirror_run_dirs:
        if d:
            mirror_targets.append(Path(d))
    # Stage-07 dirs are typically <agent_run_dir>/stage-07; the parent is the run dir.
    for sd in args.synthesis_dirs:
        try:
            mirror_targets.append(Path(sd).parent)
        except Exception:  # noqa: BLE001
            pass
    env_run_dir = os.environ.get("SCHOLARCLAW_RUN_DIR", "")
    if env_run_dir:
        mirror_targets.append(Path(env_run_dir))
    seen: set[str] = set()
    deduped: list[Path] = []
    for d in mirror_targets:
        try:
            key = str(d.resolve())
        except Exception:  # noqa: BLE001
            key = str(d)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)
    _MIRROR_DIRS.extend(deduped)
    # Make LLMClient stream its own llm_request/llm_response into the discussion
    # output dir so the supervisor sees full prompt + response of each round.
    os.environ["SCHOLARCLAW_RUN_DIR"] = str(output_dir_path)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write heartbeat so the bridge can track progress
    heartbeat = {"status": "starting", "started_at": int(time.time() * 1000)}
    (output_dir / "heartbeat.json").write_text(
        json.dumps(heartbeat, ensure_ascii=False), encoding="utf-8",
    )

    print(f"🦞 Discussion Runner starting")
    print(f"   Config:     {args.config}")
    print(f"   Dirs:       {args.synthesis_dirs}")
    print(f"   Rounds:     {args.rounds}")
    print(f"   Output:     {args.output}")

    # Load config and create LLM client
    config = RCConfig.load(args.config, check_paths=False)
    llm = create_llm_client(config)

    topic = args.topic or config.research.topic
    print(f"   Topic:      {topic}")

    # Collect syntheses
    syntheses: dict[str, str] = {}
    for i, sd in enumerate(args.synthesis_dirs):
        label = f"Agent {_label(i)}"
        text = _read_synthesis(Path(sd))
        if text:
            syntheses[label] = text
            print(f"   ✓ {label}: {len(text)} chars from {sd}")
        else:
            print(f"   ✗ {label}: no synthesis.md found in {sd}")

    if len(syntheses) < 1:
        print("ERROR: No syntheses available for discussion")
        heartbeat["status"] = "failed"
        heartbeat["error"] = "no syntheses available"
        (output_dir / "heartbeat.json").write_text(
            json.dumps(heartbeat, ensure_ascii=False), encoding="utf-8",
        )
        sys.exit(1)

    if len(syntheses) == 1:
        print("   ℹ Single synthesis — will run critical self-review instead of multi-agent debate")

    heartbeat["status"] = "discussing"
    heartbeat["participants"] = len(syntheses)
    (output_dir / "heartbeat.json").write_text(
        json.dumps(heartbeat, ensure_ascii=False), encoding="utf-8",
    )

    _mirror_event(
        "stage_transition",
        f"🗣️ 跨 agent 讨论开始 ({len(syntheses)} 个视角, {args.rounds} 轮)",
        detail=f"Topic: {topic}\nParticipants: {', '.join(syntheses.keys())}",
    )

    # Run discussion
    print(f"\n🗣️  Starting {args.rounds}-round discussion with {len(syntheses)} perspectives...\n")
    transcript, consensus = run_discussion(llm, topic, syntheses, args.rounds, output_dir)
    _mirror_event(
        "stage_transition",
        f"✅ 讨论完成 ({len(consensus)} chars consensus)",
        detail=consensus[:8 * 1024],
    )

    # Write outputs
    (output_dir / "discussion_transcript.md").write_text(transcript, encoding="utf-8")
    (output_dir / "consensus_synthesis.md").write_text(consensus, encoding="utf-8")

    heartbeat["status"] = "completed"
    heartbeat["completed_at"] = int(time.time() * 1000)
    (output_dir / "heartbeat.json").write_text(
        json.dumps(heartbeat, ensure_ascii=False), encoding="utf-8",
    )

    print(f"\n✅ Discussion complete")
    print(f"   Transcript: {output_dir / 'discussion_transcript.md'}")
    print(f"   Consensus:  {output_dir / 'consensus_synthesis.md'}")


if __name__ == "__main__":
    main()
