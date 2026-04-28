"""LLMClient.chat() must stream llm_request + llm_response activity events."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from researchclaw.llm.client import LLMClient, LLMConfig, LLMResponse


def _read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "activity.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _make_client(activity_dir: Path) -> LLMClient:
    cfg = LLMConfig(
        base_url="https://example.invalid/v1",
        api_key="sk-test",
        primary_model="dummy-model",
        fallback_models=[],
        max_retries=1,
        retry_base_delay=0.0,
        timeout_sec=1,
    )
    client = LLMClient(cfg)
    client._activity_run_dir = str(activity_dir)  # type: ignore[attr-defined]
    return client


def test_chat_writes_request_and_response_events(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    fake_response = LLMResponse(
        content="Hello world from the assistant.",
        model="dummy-model",
        prompt_tokens=42,
        completion_tokens=8,
        total_tokens=50,
    )

    with patch.object(client, "_call_with_retry", return_value=fake_response):
        result = client.chat(
            [{"role": "user", "content": "Say hello"}],
            system="You are a careful assistant.",
        )

    assert result.content == fake_response.content

    events = _read_events(tmp_path)
    types = [e["type"] for e in events]
    assert "llm_request" in types, f"missing llm_request in {types}"
    assert "llm_response" in types, f"missing llm_response in {types}"
    assert "llm_call" in types  # legacy stats event remains for backward compat

    req = next(e for e in events if e["type"] == "llm_request")
    assert "Say hello" in req["detail"]
    assert "system" in req["detail"]

    resp = next(e for e in events if e["type"] == "llm_response")
    assert resp["detail"].startswith("Hello world")
    assert resp["tokens"] == 50


def test_chat_emits_error_event_when_all_models_fail(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom: provider down")

    with patch.object(client, "_call_with_retry", side_effect=_boom):
        try:
            client.chat([{"role": "user", "content": "ping"}])
        except RuntimeError:
            pass

    events = _read_events(tmp_path)
    assert any(e["type"] == "error" for e in events), [e["type"] for e in events]


def test_chat_skips_activity_when_no_run_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SCHOLARCLAW_RUN_DIR", raising=False)
    cfg = LLMConfig(
        base_url="https://example.invalid/v1",
        api_key="sk-test",
        primary_model="dummy-model",
        fallback_models=[],
    )
    client = LLMClient(cfg)
    fake_response = LLMResponse(content="ok", model="dummy-model")

    with patch.object(client, "_call_with_retry", return_value=fake_response):
        client.chat([{"role": "user", "content": "hi"}])

    # Without _activity_run_dir, no activity.jsonl is written anywhere we can
    # easily inspect; the contract here is "doesn't crash".
    assert not getattr(client, "_activity_run_dir", "")


def test_chat_auto_binds_run_dir_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCHOLARCLAW_RUN_DIR", str(tmp_path))
    cfg = LLMConfig(
        base_url="https://example.invalid/v1",
        api_key="sk-test",
        primary_model="dummy-model",
        fallback_models=[],
    )
    client = LLMClient(cfg)
    assert getattr(client, "_activity_run_dir", "") == str(tmp_path)

    fake_response = LLMResponse(content="bound", model="dummy-model", total_tokens=3)
    with patch.object(client, "_call_with_retry", return_value=fake_response):
        client.chat([{"role": "user", "content": "ping"}])

    events = _read_events(tmp_path)
    assert any(e["type"] == "llm_response" for e in events)
