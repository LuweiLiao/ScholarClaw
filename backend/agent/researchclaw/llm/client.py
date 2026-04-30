"""Lightweight OpenAI-compatible LLM client — stdlib only.

Features:
  - Model fallback chain (gpt-5.2 → gpt-5.1 → gpt-4.1 → gpt-4o)
  - Auto-detect max_tokens vs max_completion_tokens per model
  - Cloudflare User-Agent bypass
  - Exponential backoff retry with jitter
  - JSON mode support
  - Streaming disabled (sync only)
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_PROMPT_PREVIEW_BYTES = 8 * 1024
_RESP_PREVIEW_BYTES = 8 * 1024
_MAX_TURN_NUMBER_RESET = 10_000

_turn_counter = itertools.count(1)


def _next_turn_number() -> int:
    n = next(_turn_counter)
    if n > _MAX_TURN_NUMBER_RESET:
        # reset so the counter doesn't grow unbounded across long-lived processes
        globals()["_turn_counter"] = itertools.count(1)
        n = 1
    return n


def _truncate_text(text: str, limit: int) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= limit:
        return text
    head = encoded[:limit].decode("utf-8", errors="ignore")
    return f"{head}\n[truncated {len(encoded) - limit} bytes]"


def _format_messages_preview(messages: list[dict[str, str]]) -> str:
    """Render a chat-style preview of messages for the activity timeline."""
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "")).strip() or "user"
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(p) for p in content)
        parts.append(f"### {role}\n{content}")
    joined = "\n\n".join(parts)
    return _truncate_text(joined, _PROMPT_PREVIEW_BYTES)


def _hash_messages(messages: list[dict[str, str]]) -> str:
    canon = json.dumps(messages, ensure_ascii=False, sort_keys=False)
    return hashlib.sha1(canon.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _resolve_activity_run_dir(client: "LLMClient") -> str:
    explicit = getattr(client, "_activity_run_dir", "")
    if explicit:
        return str(explicit)
    env_dir = os.environ.get("SCHOLARCLAW_RUN_DIR", "")
    if env_dir:
        try:
            client._activity_run_dir = env_dir  # type: ignore[attr-defined]
        except Exception:
            pass
        return env_dir
    return ""


def _safe_write_event(*args, **kwargs) -> None:
    try:
        from researchclaw.pipeline.activity_writer import write_event
        write_event(*args, **kwargs)
    except Exception:
        pass

# Models that require max_completion_tokens instead of max_tokens
_NEW_PARAM_MODELS = frozenset(
    {
        "o3",
        "o3-mini",
        "o4-mini",
        "gpt-5",
        "gpt-5.1",
        "gpt-5.2",
        "gpt-5.4",
    }
)

# Models routed through the Responses API that need max_output_tokens
_RESPONSES_API_MODELS = frozenset(
    {
        "gpt-5",
        "gpt-5.1",
        "gpt-5.2",
    }
)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class LLMResponse:
    """Parsed response from the LLM API."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str = ""
    truncated: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMConfig:
    """Configuration for the LLM client."""

    base_url: str
    api_key: str
    primary_model: str = "gpt-4o"
    fallback_models: list[str] = field(
        default_factory=lambda: ["gpt-4.1", "gpt-4o-mini"]
    )
    max_tokens: int = 4096
    temperature: float = 0.7
    max_retries: int = 5
    retry_base_delay: float = 3.0
    timeout_sec: int = 600
    user_agent: str = _DEFAULT_USER_AGENT
    # MetaClaw bridge: extra headers for proxy requests
    extra_headers: dict[str, str] = field(default_factory=dict)
    # MetaClaw bridge: fallback URL if primary (proxy) is unreachable
    fallback_url: str = ""
    fallback_api_key: str = ""


class LLMClient:
    """Stateless OpenAI-compatible chat completion client."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._model_chain = [config.primary_model] + list(config.fallback_models)
        self._anthropic = None  # Will be set by from_rc_config if needed
        # Auto-bind activity run_dir so subprocess-spawned LLM clients still
        # stream their request/response events to the supervisor timeline.
        env_run_dir = os.environ.get("SCHOLARCLAW_RUN_DIR", "")
        if env_run_dir:
            self._activity_run_dir = env_run_dir  # type: ignore[attr-defined]

    @classmethod
    def from_rc_config(cls, rc_config: Any) -> LLMClient:
        from researchclaw.llm import resolve_provider_base_url

        provider = getattr(rc_config.llm, "provider", "openai")
        configured_base_url = str(getattr(rc_config.llm, "base_url", "") or "")

        api_key = str(
            rc_config.llm.api_key
            or os.environ.get(rc_config.llm.api_key_env, "")
            or ""
        )

        base_url = resolve_provider_base_url(provider, configured_base_url)

        # Preserve original URL/key before MetaClaw bridge override
        # (needed for Anthropic adapter which should always talk directly
        # to the Anthropic API, not through the OpenAI-compatible proxy).
        original_base_url = base_url
        original_api_key = api_key

        # MetaClaw bridge: if enabled, point to proxy and set up fallback
        bridge = getattr(rc_config, "metaclaw_bridge", None)
        fallback_url = ""
        fallback_api_key = ""

        if bridge and getattr(bridge, "enabled", False):
            fallback_url = base_url
            fallback_api_key = api_key
            base_url = bridge.proxy_url
            if bridge.fallback_url:
                fallback_url = bridge.fallback_url
            if bridge.fallback_api_key:
                fallback_api_key = bridge.fallback_api_key

        config = LLMConfig(
            base_url=base_url,
            api_key=api_key,
            primary_model=rc_config.llm.primary_model or "gpt-4o",
            fallback_models=list(rc_config.llm.fallback_models or []),
            timeout_sec=getattr(rc_config.llm, "timeout_sec", 600),
            fallback_url=fallback_url,
            fallback_api_key=fallback_api_key,
        )
        client = cls(config)

        # Detect Anthropic provider — use original URL/key (not the
        # MetaClaw proxy URL which is OpenAI-compatible only).
        if provider == "anthropic":
            from .anthropic_adapter import AnthropicAdapter

            client._anthropic = AnthropicAdapter(
                original_base_url, original_api_key, config.timeout_sec
            )
        return client

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        system: str | None = None,
        strip_thinking: bool = False,
    ) -> LLMResponse:
        """Send a chat completion request with retry and fallback.

        Args:
            messages: List of {role, content} dicts.
            model: Override model (skips fallback chain).
            max_tokens: Override max token count.
            temperature: Override temperature.
            json_mode: Request JSON response format.
            system: Prepend a system message.
            strip_thinking: If True, strip <think>…</think> reasoning
                tags from the response content.  Use this when the
                output will be written to paper/script artifacts but
                NOT for general chat calls (to avoid corrupting
                legitimate content).

        Returns:
            LLMResponse with content and metadata.
        """
        if system:
            messages = [{"role": "system", "content": system}] + messages

        models = [model] if model else self._model_chain
        max_tok = max_tokens or self.config.max_tokens
        temp = temperature if temperature is not None else self.config.temperature

        last_error: Exception | None = None
        _t0 = time.monotonic()

        # Pre-compute the prompt preview & hash once per chat() invocation so we
        # can stream a "user" bubble to the supervisor before the model replies.
        _act_dir = _resolve_activity_run_dir(self)
        _turn_no = _next_turn_number() if _act_dir else 0
        _prompt_hash = _hash_messages(messages) if _act_dir else ""
        if _act_dir:
            _prompt_preview = _format_messages_preview(messages)
            _prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
            _safe_write_event(
                _act_dir,
                "llm_request",
                f"🧑 Turn {_turn_no} · {self.config.primary_model} · {_prompt_chars} chars",
                detail=_prompt_preview,
                tokens=0,
                elapsed_ms=0,
                stage=None,
                tool_name="",
            )

        for idx, m in enumerate(models):
            try:
                resp = self._call_with_retry(m, messages, max_tok, temp, json_mode)
                _elapsed_ms = int((time.monotonic() - _t0) * 1000)
                if _act_dir:
                    response_preview = _truncate_text(resp.content or "", _RESP_PREVIEW_BYTES)
                    _safe_write_event(
                        _act_dir,
                        "llm_response",
                        f"🤖 Turn {_turn_no} · {resp.model or m} · {resp.total_tokens} tokens ({_elapsed_ms}ms)",
                        detail=response_preview,
                        tokens=resp.total_tokens,
                        elapsed_ms=_elapsed_ms,
                    )
                    # Backward-compatible legacy event so older UI clients still
                    # see a single "llm_call" line with the stats summary.
                    _safe_write_event(
                        _act_dir,
                        "llm_call",
                        f"🤖 {resp.model or m}: {resp.total_tokens} tokens ({_elapsed_ms}ms)",
                        detail=(
                            f"prompt={resp.prompt_tokens}, completion={resp.completion_tokens}, "
                            f"content_len={len(resp.content)}, hash={_prompt_hash}"
                        ),
                        tokens=resp.total_tokens,
                        elapsed_ms=_elapsed_ms,
                    )
                if strip_thinking:
                    from researchclaw.utils.thinking_tags import strip_thinking_tags
                    resp = LLMResponse(
                        content=strip_thinking_tags(resp.content),
                        model=resp.model,
                        prompt_tokens=resp.prompt_tokens,
                        completion_tokens=resp.completion_tokens,
                        total_tokens=resp.total_tokens,
                        finish_reason=resp.finish_reason,
                        truncated=resp.truncated,
                        raw=resp.raw,
                    )
                return resp
            except Exception as exc:  # noqa: BLE001
                logger.warning("Model %s failed: %s. Trying next.", m, exc)
                last_error = exc
                _is_rate_or_conn = (
                    isinstance(exc, urllib.error.HTTPError) and exc.code == 429
                ) or isinstance(exc, (urllib.error.URLError, OSError, ConnectionError))
                if _is_rate_or_conn and idx < len(models) - 1:
                    import random
                    _backoff = self.config.retry_base_delay * (2 ** idx) + random.uniform(2, 8)
                    logger.info(
                        "Rate-limit / connection error on %s; waiting %.1fs before next model.",
                        m, _backoff,
                    )
                    time.sleep(_backoff)

        if _act_dir:
            _safe_write_event(
                _act_dir,
                "error",
                f"❌ Turn {_turn_no} · all models failed",
                detail=str(last_error)[:_RESP_PREVIEW_BYTES],
            )
        raise RuntimeError(
            f"All models failed. Last error: {last_error}"
        ) from last_error

    def preflight(self) -> tuple[bool, str]:
        """Quick connectivity check - one minimal chat call.

        Returns (success, message).
        Distinguishes: 401 (bad key), 403 (model forbidden),
                       404 (bad endpoint), 429 (rate limited), timeout.
        """
        is_reasoning = any(
            self.config.primary_model.startswith(p) for p in _NEW_PARAM_MODELS
        )
        min_tokens = 64 if is_reasoning else 1
        try:
            _ = self.chat(
                [{"role": "user", "content": "ping"}],
                max_tokens=min_tokens,
                temperature=0,
            )
            return True, f"OK - model {self.config.primary_model} responding"
        except urllib.error.HTTPError as e:
            status_map = {
                401: "Invalid API key",
                403: f"Model {self.config.primary_model} not allowed for this key",
                404: f"Endpoint not found: {self.config.base_url}",
                429: "Rate limited - try again in a moment",
            }
            msg = status_map.get(e.code, f"HTTP {e.code}")
            return False, msg
        except (urllib.error.URLError, OSError) as e:
            return False, f"Connection failed: {e}"
        except RuntimeError as e:
            # chat() wraps errors in RuntimeError; extract original HTTPError
            cause = e.__cause__
            if isinstance(cause, urllib.error.HTTPError):
                status_map = {
                    401: "Invalid API key",
                    403: f"Model {self.config.primary_model} not allowed for this key",
                    404: f"Endpoint not found: {self.config.base_url}",
                    429: "Rate limited - try again in a moment",
                }
                msg = status_map.get(cause.code, f"HTTP {cause.code}")
                return False, msg
            return False, f"All models failed: {e}"

    def _call_with_retry(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> LLMResponse:
        """Call with exponential backoff retry."""
        for attempt in range(self.config.max_retries):
            try:
                return self._raw_call(
                    model, messages, max_tokens, temperature, json_mode
                )
            except urllib.error.HTTPError as e:
                status = e.code
                body = ""
                try:
                    body = e.read().decode()[:500]
                except Exception:  # noqa: BLE001
                    pass

                # Non-retryable errors
                if status == 403 and "not allowed to use model" in body:
                    raise  # Model not available — let fallback handle

                # 400 is normally non-retryable, but some providers
                # (Azure OpenAI) return 400 during overload / rate-limit.
                # Retry if the body hints at a transient issue.
                if status == 400:
                    print(f"[LLM 400] model={model} body={body[:300]}", flush=True)
                    _transient_400 = any(
                        kw in body.lower()
                        for kw in ("rate limit", "ratelimit", "overloaded",
                                   "temporarily", "capacity", "throttl",
                                   "too many", "retry")
                    )
                    if not _transient_400:
                        raise  # Genuine bad request — don't retry

                # Retryable: 429 (rate limit), transient 400, 500, 502, 503, 504,
                # 529 (Anthropic overloaded)
                if status in (400, 429, 500, 502, 503, 504, 529):
                    delay = self.config.retry_base_delay * (2**attempt)
                    # Add jitter
                    import random

                    delay += random.uniform(0, delay * 0.3)
                    logger.info(
                        "Retry %d/%d for %s (HTTP %d). Waiting %.1fs.",
                        attempt + 1,
                        self.config.max_retries,
                        model,
                        status,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                raise  # Other HTTP errors
            except (urllib.error.URLError, OSError, ConnectionError) as e:
                if attempt < self.config.max_retries - 1:
                    import random
                    delay = self.config.retry_base_delay * (2 ** attempt)
                    delay += random.uniform(0, delay * 0.3)
                    logger.info(
                        "Retry %d/%d for %s (connection error: %s). Waiting %.1fs.",
                        attempt + 1, self.config.max_retries, model, e, delay,
                    )
                    time.sleep(delay)
                    continue
                raise

        # Should not reach here, but just in case
        return self._raw_call(model, messages, max_tokens, temperature, json_mode)

    @staticmethod
    def _stream_read(req: urllib.request.Request, timeout: int) -> dict:
        """Send a streaming request and reassemble into a standard response dict."""
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            chunks: list[str] = []
            finish_reason = None
            model_name = ""
            usage = {}
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not model_name:
                    model_name = event.get("model", "")
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        chunks.append(delta["content"])
                    # GLM-5-Turbo streams reasoning_content instead of content
                    if delta.get("reasoning_content"):
                        chunks.append(delta["reasoning_content"])
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
            content = "".join(chunks)
            return {
                "choices": [{"message": {"content": content, "role": "assistant"},
                             "finish_reason": finish_reason}],
                "model": model_name,
                "usage": usage,
            }

    def _raw_call(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> LLMResponse:
        """Make a single API call."""
        
        # Use Anthropic adapter if configured
        if self._anthropic:
            data = self._anthropic.chat_completion(model, messages, max_tokens, temperature, json_mode)
        else:
            # Original OpenAI logic
            # Copy messages to avoid mutating the caller's list (important for
            # retries and model-fallback — each attempt must start from the
            # original, un-modified messages).
            msgs = [dict(m) for m in messages]

            # Some providers (MiniMax, etc.) don't support the "system" role.
            # Convert system messages to user messages with a clear prefix.
            _no_system_role = (
                "minimax" in self.config.base_url.lower()
                or "minimaxi" in self.config.base_url.lower()
                or model.lower().startswith("minimax")
                or model.lower().startswith("abab")
            )
            if _no_system_role:
                converted = []
                for m in msgs:
                    if m.get("role") == "system":
                        converted.append({"role": "user", "content": f"[System Instructions]\n{m['content']}"})
                        converted.append({"role": "assistant", "content": "Understood. I will follow these instructions."})
                    else:
                        converted.append(m)
                msgs = converted

            body: dict[str, Any] = {
                "model": model,
                "messages": msgs,
                "temperature": temperature,
            }

            # Use correct token parameter based on model.
            # Check _NEW_PARAM_MODELS first — "gpt-5.4" must NOT fall through
            # to _RESPONSES_API_MODELS whose "gpt-5" prefix would also match.
            if any(model.startswith(prefix) for prefix in _NEW_PARAM_MODELS):
                reasoning_min = 32768
                body["max_completion_tokens"] = max(max_tokens, reasoning_min)
            elif any(model.startswith(prefix) for prefix in _RESPONSES_API_MODELS):
                body["max_output_tokens"] = max(max_tokens, 32768)
            else:
                body["max_tokens"] = max_tokens

            if json_mode:
                # Many OpenAI-compatible providers (Claude, DeepSeek, etc.)
                # don't support the response_format parameter and return 400.
                # Fall back to a system-prompt injection for non-OpenAI models.
                _use_prompt_injection = (
                    model.startswith("claude")
                    or model.startswith("deepseek")
                    or "deepseek" in self.config.base_url.lower()
                )
                if _use_prompt_injection:
                    _json_hint = (
                        "You MUST respond with valid JSON only. "
                        "Do not include any text outside the JSON object."
                    )
                    # Prepend to existing system message or add as new one
                    if msgs and msgs[0]["role"] == "system":
                        msgs[0]["content"] = (
                            _json_hint + "\n\n" + msgs[0]["content"]
                        )
                    else:
                        msgs.insert(
                            0, {"role": "system", "content": _json_hint}
                        )
                else:
                    body["response_format"] = {"type": "json_object"}

            body["stream"] = False
            payload = json.dumps(body).encode("utf-8")
            url = f"{self.config.base_url.rstrip('/')}/chat/completions"

            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": self.config.user_agent,
            }
            headers.update(self.config.extra_headers)

            req = urllib.request.Request(url, data=payload, headers=headers)

            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_sec) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError) as exc:
                if self.config.fallback_url:
                    logger.warning(
                        "Primary endpoint unreachable, falling back to %s: %s",
                        self.config.fallback_url,
                        exc,
                    )
                    fallback_url = (
                        f"{self.config.fallback_url.rstrip('/')}/chat/completions"
                    )
                    fallback_key = self.config.fallback_api_key or self.config.api_key
                    fallback_headers = {
                        "Authorization": f"Bearer {fallback_key}",
                        "Content-Type": "application/json",
                        "User-Agent": self.config.user_agent,
                    }
                    fb_body = dict(body)
                    fb_body["stream"] = False
                    fb_payload = json.dumps(fb_body).encode("utf-8")
                    fallback_req = urllib.request.Request(
                        fallback_url, data=fb_payload, headers=fallback_headers
                    )
                    with urllib.request.urlopen(fallback_req, timeout=self.config.timeout_sec) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                else:
                    raise

        # Handle API error responses
        if "error" in data:
            error_info = data["error"]
            error_msg = error_info.get("message", str(error_info))
            error_type = error_info.get("type", "api_error")
            import io
            raise urllib.error.HTTPError(
                "", 500, f"{error_type}: {error_msg}", {},
                io.BytesIO(error_msg.encode()),
            )

        # Validate response structure
        if "choices" not in data or not data["choices"]:
            raise ValueError(f"Malformed API response: missing choices. Got: {data}")

        choice = data["choices"][0]
        usage = data.get("usage", {})

        message = choice.get("message", {})
        content = message.get("content") or ""
        # GLM-5-Turbo and other reasoning models return content in reasoning_content
        if not content and message.get("reasoning_content"):
            content = message.get("reasoning_content")

        prompt_tok = usage.get("prompt_tokens", 0)
        completion_tok = usage.get("completion_tokens", 0)
        total_tok = usage.get("total_tokens", 0)
        if total_tok == 0 and (prompt_tok or completion_tok):
            total_tok = prompt_tok + completion_tok
        if total_tok == 0 and content:
            total_tok = len(content) // 4 + prompt_tok

        return LLMResponse(
            content=content,
            model=data.get("model", model),
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
            total_tokens=total_tok,
            finish_reason=choice.get("finish_reason", ""),
            truncated=(choice.get("finish_reason", "") == "length"),
            raw=data,
        )


def create_client_from_yaml(yaml_path: str | None = None) -> LLMClient:
    """Create an LLMClient from the ARC config file.

    Reads base_url and api_key from config.arc.yaml's llm section.
    """
    import yaml as _yaml

    if yaml_path is None:
        yaml_path = "config.yaml"

    with open(yaml_path, encoding="utf-8") as f:
        raw = _yaml.safe_load(f)

    llm_section = raw.get("llm", {})
    api_key = str(
        os.environ.get(
            llm_section.get("api_key_env", "OPENAI_API_KEY"),
            llm_section.get("api_key", ""),
        )
        or ""
    )

    return LLMClient(
        LLMConfig(
            base_url=llm_section.get("base_url", "https://api.openai.com/v1"),
            api_key=api_key,
            primary_model=llm_section.get("primary_model", "gpt-4o"),
            fallback_models=llm_section.get(
                "fallback_models", ["gpt-4.1", "gpt-4o-mini"]
            ),
        )
    )
