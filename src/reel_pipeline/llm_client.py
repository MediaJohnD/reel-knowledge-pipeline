"""Shared LLM call abstraction, used by the enrichment, skill-writer, and
image-description modules so the HTTP/auth/error-handling logic lives in
exactly one place.

Dispatches by settings.llm.provider:
- "anthropic": Claude Messages API (requires ANTHROPIC_API_KEY).
- "ollama": a local Ollama instance (settings.llm.ollama_host), no API key -
  just requires Ollama running and the configured model already pulled
  (`ollama pull <model>`).
- "groq": Groq's OpenAI-compatible chat completions API (requires GROQ_API_KEY).
- "gemini": Google's Gemini generateContent API (requires GEMINI_API_KEY).
- "cerebras": Cerebras' OpenAI-compatible chat completions API (requires
  CEREBRAS_API_KEY). Free tier: 1M tokens/day. Non-Chinese-operated - see
  CLAUDE.md's Chinese provider blocklist; stick to non-Chinese-origin models
  (e.g. gpt-oss-120b) on this provider for the same reason.

call_llm() is text-only (enrichment, skill generation). describe_images() adds
image input for vision-capable models (image-post/carousel description) - the
model configured must actually support vision (see ImageDescriptionConfig).

Prompt caching: call_llm()'s static_prefix param carries the shared,
repeated instruction text (see the <!-- CACHE:BOUNDARY --> marker in
config/prompts/*.md, split out by enricher.render_and_split()) separately
from the per-call variable content, so it can actually be cached rather than
invalidated by a differing prefix on every call - see static_prefix's
docstring below for the per-provider mechanism.
"""

from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path
from typing import Any

import httpx

from reel_pipeline.config import Settings

_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
_CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
# Gemini's key goes in the x-goog-api-key header, never the ?key= query param
# Google also accepts. httpx puts the full request URL in every HTTPStatusError
# message, which this module wraps into LlmCallError -> record.error ->
# data/inbox/needs-attention.txt, so a query-param key gets written to disk in
# plaintext on any Gemini failure (it did, twice, on 2026-08-12). A header
# cannot leak that way.
_DEFAULT_IMAGE_MEDIA_TYPE = "image/jpeg"
_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"

# Per-provider timestamp (time.monotonic()) of the last call made by this
# process - checked by _throttle() before every provider call so a run_once()
# pass processing many items back-to-back can't burst past a hosted
# provider's rate limit. Process-local only: separate worker/webhook
# processes don't share it, but within one run_once() call (the actual
# source of the 2026-08-12 Cerebras 429s) it's exactly what's needed.
_last_call_at: dict[str, float] = {}


def _seconds_to_wait(min_interval: float, elapsed_since_last_call: float | None) -> float:
    """Pure helper for _throttle(), split out for testing without real sleeps."""
    if min_interval <= 0 or elapsed_since_last_call is None:
        return 0.0
    return max(0.0, min_interval - elapsed_since_last_call)


def _throttle(settings: Settings, provider: str) -> None:
    min_interval = settings.llm.min_interval_seconds.get(provider, 0.0)
    now = time.monotonic()
    last = _last_call_at.get(provider)
    wait = _seconds_to_wait(min_interval, None if last is None else now - last)
    if wait > 0:
        time.sleep(wait)
    _last_call_at[provider] = time.monotonic()


# A 429 that survives this many retries is left to the item-level retry
# (settings.retry.backoff_schedule_minutes) - by then it's a quota that won't
# clear in seconds. Waits are Retry-After when the vendor sends one, else
# 5s/10s/20s; anything longer than _RATE_LIMIT_MAX_WAIT isn't slept through at
# all, so a daily-quota Retry-After can't stall a whole run_once() pass.
_RATE_LIMIT_STATUS = 429
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BASE_WAIT = 5.0
_RATE_LIMIT_MAX_WAIT = 60.0


def _rate_limit_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait after a 429, or a value above _RATE_LIMIT_MAX_WAIT to
    mean "don't wait, give up". Pure helper, so it's testable without sleeps.

    Retry-After may also be an HTTP-date rather than a delay in seconds; that
    parses as a float failure and falls back to the exponential schedule,
    which is the conservative direction (we may wait less than asked, but
    never longer than _RATE_LIMIT_MAX_WAIT).
    """
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return _RATE_LIMIT_BASE_WAIT * 2**attempt


# How much of a provider's error body is quoted into the raised error. Long
# enough for the provider's own message (Ollama's context-overflow text runs
# ~120 chars), short enough that an HTML error page cannot swamp
# needs-attention.txt.
_ERROR_BODY_LIMIT = 500
# Request headers whose value is a credential. Everything sent from this module
# travels as a header (never a query param - see the 2026-08-12 Gemini leak),
# so redacting these covers every secret that could come back echoed in an
# error body. Matched on the name so ordinary headers ("content-type",
# "anthropic-version") are left readable.
_SECRET_HEADER_HINTS = ("key", "auth", "token", "secret")


def _error_detail(response: httpx.Response, headers: Any) -> str:
    """The provider's own error text, flattened to one line, credential-scrubbed
    and truncated - or "" when there is no body to report.

    httpx's HTTPStatusError message stops at the status line, so without this
    the reason never reaches the caller. That is not merely vague: the Ollama
    vision path appended its own guess about the model not being pulled, and on
    2026-08-21 that sent an investigation down the wrong path while the real
    cause (a context overflow on a 20-image carousel) sat unreported in a body
    nothing read.
    """
    try:
        body = response.text
    except Exception:  # noqa: BLE001 - an unreadable body must not mask the HTTP error
        return ""
    body = " ".join(body.split())
    if not body:
        return ""
    for name, value in (headers or {}).items():
        if not any(hint in str(name).lower() for hint in _SECRET_HEADER_HINTS):
            continue
        # Split so a scheme prefix ("Bearer <key>") does not stop the key
        # itself from matching - providers echo the bare credential, not the
        # whole header value.
        for token in str(value).split():
            if len(token) >= 8:
                body = body.replace(token, "[redacted]")
    if len(body) > _ERROR_BODY_LIMIT:
        body = body[:_ERROR_BODY_LIMIT] + "..."
    return body


def _post_json(client: httpx.Client, url: str, **kwargs: Any) -> dict:
    """POST and return the parsed JSON body, retrying 429s in place.

    Without this, one 429 failed the whole item and pushed it into the retry
    backlog to be picked up a day later (the recurring ~2/day Cerebras entries
    in needs-attention.txt through 2026-08-14). Those were isolated single
    requests, not bursts - _throttle() already spaces bursts out - so retrying
    the request is the only thing that clears them.
    """
    response = client.post(url, **kwargs)
    for attempt in range(_RATE_LIMIT_RETRIES):
        if response.status_code != _RATE_LIMIT_STATUS:
            break
        wait = _rate_limit_wait(response, attempt)
        if wait > _RATE_LIMIT_MAX_WAIT:
            break
        time.sleep(wait)
        response = client.post(url, **kwargs)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _error_detail(response, kwargs.get("headers"))
        if not detail:
            raise
        # Re-raised rather than handled here so every provider's own wrapper
        # keeps producing its own message; .response is carried over so
        # _http_status() (and LlmCallError.is_account_level) still work.
        raise httpx.HTTPStatusError(
            f"{exc}: {detail}", request=exc.request, response=exc.response
        ) from exc
    return response.json()


def _strip_groq_reasoning(text: str) -> str:
    """Some reasoning-capable Groq models put their internal reasoning inline
    in message.content, before a closing </think> tag, with the real answer
    after it. Still true as of 2026-08-15 for qwen/qwen3.6-27b - this account's
    only vision-capable model, so the vision path always needs this - even
    though gpt-oss-120b now separates reasoning into message.reasoning
    server-side and returns clean content (see _call_groq).

    If max_tokens ran out mid-reasoning before a closing tag ever appeared
    (found live 2026-08-12), there's no confirmed answer text - return "" so
    the caller's existing empty-text check reports a real, diagnosable failure
    instead of silently leaking a chopped-off reasoning fragment as if it were
    the model's answer.
    """
    if _THINK_CLOSE_TAG in text:
        return text.rsplit(_THINK_CLOSE_TAG, 1)[1].strip()
    if _THINK_OPEN_TAG in text:
        return ""
    return text.strip()


# Provider HTTP statuses that describe the *account*, not this request: no
# credit left (402), key rejected (401), key not entitled to the resource
# (403). Every item in a pass fails identically on these, so they say nothing
# about any one URL and must not consume its retry budget - see
# WorkerPipeline.process_item(). 429 is deliberately absent: _post_json()
# already retries it in place and it is genuinely transient.
_ACCOUNT_LEVEL_STATUSES = frozenset({401, 402, 403})

# Client-error statuses that ARE worth retrying, so they stay on the normal
# backoff rather than being treated as terminal below: 408 is a timeout and 429
# is rate limiting (_post_json() already retries 429 in place).
_TRANSIENT_CLIENT_STATUSES = frozenset({408, 429})


def _http_status(exc: httpx.HTTPError) -> int | None:
    """The provider's HTTP status, or None if the request never got a response
    (connection error, timeout). httpx only sets .response on HTTPStatusError.
    """
    response = getattr(exc, "response", None)
    return None if response is None else response.status_code


class LlmCallError(RuntimeError):
    """Raised when an LLM call fails or returns no usable text.

    status_code carries the provider's HTTP status when the failure came back
    as a response, and is None otherwise (connection failures, and the
    empty-text errors raised after a successful 200). It exists so callers can
    tell "this account is dead" from "this item is bad" - the distinction the
    2026-08-16 Cerebras 402 outage needed and did not have, which walked 22
    perfectly good URLs to FAILED_PERMANENT.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_account_level(self) -> bool:
        """True when the failure is about the account rather than this request."""
        return self.status_code in _ACCOUNT_LEVEL_STATUSES

    @property
    def is_terminal(self) -> bool:
        """True when retrying this exact request can only fail the same way.

        A request-level 4xx - a malformed prompt, an image the model won't
        accept, an unknown model name - is deterministic: the same bytes get
        the same answer in eight hours. Spending the full retry budget on it
        only delays the inevitable (the 2026-08-21 vision 400 took ~27h to
        reach a verdict it could have reached immediately) and keeps a dead
        item sitting in the queue meanwhile.

        Account-level statuses are excluded - they are not about this request
        at all, see is_account_level - as are the transient client errors.
        status_code is None for connection failures and for empty-text errors
        raised after a successful 200, both of which are worth retrying.
        """
        if self.status_code is None or self.is_account_level:
            return False
        return 400 <= self.status_code < 500 and self.status_code not in _TRANSIENT_CLIENT_STATUSES


def _gemini_empty_text_error(payload: dict) -> LlmCallError:
    """Gemini 2.5 models spend part of max_tokens on internal 'thinking' before
    the visible answer - a tight max_tokens (found live 2026-08-12, testing the
    new vision path at max_tokens=50) can exhaust the whole budget on thinking
    and return finishReason='MAX_TOKENS' with zero answer text. Surfacing that
    reason directly, instead of dumping the raw payload, makes "raise
    max_tokens" the obvious fix rather than something to reverse-engineer.
    """
    candidates = payload.get("candidates", [])
    finish_reason = candidates[0].get("finishReason") if candidates else None
    if finish_reason == "MAX_TOKENS":
        return LlmCallError(
            "Gemini response had no text content: max_tokens was exhausted "
            "(likely by the model's internal 'thinking' before any answer text) "
            "- try raising max_tokens."
        )
    return LlmCallError(f"Gemini API response had no text content: {payload!r}")


def _call_claude(
    settings: Settings,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    static_prefix: str = "",
    client: httpx.Client | None = None,
) -> str:
    api_key = settings.require_anthropic_api_key()
    owned_client = client or httpx.Client(timeout=120.0)
    owns_client = client is None
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if static_prefix:
        body["system"] = [
            {
                "type": "text",
                "text": static_prefix,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    try:
        payload = _post_json(
            owned_client,
            _ANTHROPIC_ENDPOINT,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Claude API request failed: {exc}", status_code=_http_status(exc)
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    blocks = payload.get("content", [])
    text_blocks = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    if not text_blocks:
        raise LlmCallError(f"Claude API response had no text content: {payload!r}")
    return "".join(text_blocks)


def _call_ollama(
    settings: Settings,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    client: httpx.Client | None = None,
) -> str:
    owned_client = client or httpx.Client(timeout=600.0)
    owns_client = client is None
    try:
        payload = _post_json(
            owned_client,
            f"{settings.llm.ollama_host}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "num_ctx": settings.llm.ollama_num_ctx},
            },
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Ollama request to {settings.llm.ollama_host!r} failed: {exc}. "
            f"Is Ollama running, and is {model!r} pulled (ollama pull {model})?",
            status_code=_http_status(exc),
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    text = payload.get("response", "")
    if not text:
        raise LlmCallError(f"Ollama response had no text content: {payload!r}")
    return text


def _call_groq(
    settings: Settings,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    json_mode: bool = False,
    client: httpx.Client | None = None,
) -> str:
    api_key = settings.require_groq_api_key()
    owned_client = client or httpx.Client(timeout=120.0)
    owns_client = client is None
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    # No reasoning_format is sent at all. It used to be "raw" here so
    # _strip_groq_reasoning() could see the <think> block; Groq has since
    # narrowed which models accept the parameter, and as of 2026-08-15 every
    # value 400s on non-reasoning models (llama-3.1-8b-instant) and "raw"
    # specifically 400s on gpt-oss-120b - which had made this whole provider
    # unusable. Omitting it is the only setting all models accept, and it
    # loses nothing: gpt-oss now splits reasoning into message.reasoning
    # server-side (clean content, and an exhausted budget still surfaces as
    # the empty-content error below), while models that do inline <think>
    # still do so without the parameter, for _strip_groq_reasoning() to cut.
    try:
        payload = _post_json(
            owned_client,
            _GROQ_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json=body,
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Groq API request failed: {exc}", status_code=_http_status(exc)
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    choices = payload.get("choices", [])
    text = choices[0].get("message", {}).get("content", "") if choices else ""
    text = _strip_groq_reasoning(text)
    if not text:
        raise LlmCallError(f"Groq API response had no text content: {payload!r}")
    return text


def _call_groq_vision(
    settings: Settings,
    prompt: str,
    image_paths: list[Path],
    *,
    model: str,
    max_tokens: int,
    static_prefix: str = "",
    client: httpx.Client | None = None,
) -> str:
    """Groq's OpenAI-compatible endpoint takes images as data-URI image_url
    parts alongside text, same shape OpenAI's vision API uses. Only some Groq
    models support image input (check input_modalities via GET
    /openai/v1/models) - a text-only model here just errors like any other
    unsupported request, same as calling it with an oversized prompt would.
    """
    combined_prompt = f"{static_prefix}\n\n{prompt}" if static_prefix else prompt
    api_key = settings.require_groq_api_key()
    content: list[dict] = [{"type": "text", "text": combined_prompt}]
    for path in image_paths:
        media_type = mimetypes.guess_type(path.name)[0] or _DEFAULT_IMAGE_MEDIA_TYPE
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{_encode_image_b64(path)}"},
            }
        )
    owned_client = client or httpx.Client(timeout=180.0)
    owns_client = client is None
    try:
        payload = _post_json(
            owned_client,
            _GROQ_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": content}],
                # No reasoning_format - see _call_groq. qwen/qwen3.6-27b, the
                # only vision-capable model on this account, inlines <think>
                # either way, so _strip_groq_reasoning() below still applies.
            },
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Groq API vision request failed: {exc}", status_code=_http_status(exc)
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    choices = payload.get("choices", [])
    text = choices[0].get("message", {}).get("content", "") if choices else ""
    text = _strip_groq_reasoning(text)
    if not text:
        raise LlmCallError(f"Groq API vision response had no text content: {payload!r}")
    return text


def _call_cerebras(
    settings: Settings,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    json_mode: bool = False,
    client: httpx.Client | None = None,
) -> str:
    api_key = settings.require_cerebras_api_key()
    owned_client = client or httpx.Client(timeout=120.0)
    owns_client = client is None
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        payload = _post_json(
            owned_client,
            _CEREBRAS_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json=body,
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Cerebras API request failed: {exc}", status_code=_http_status(exc)
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    choices = payload.get("choices", [])
    text = choices[0].get("message", {}).get("content", "") if choices else ""
    if not text:
        raise LlmCallError(f"Cerebras API response had no text content: {payload!r}")
    return text


def _call_gemini(
    settings: Settings,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    json_mode: bool = False,
    client: httpx.Client | None = None,
) -> str:
    api_key = settings.require_gemini_api_key()
    owned_client = client or httpx.Client(timeout=120.0)
    owns_client = client is None
    generation_config: dict = {"maxOutputTokens": max_tokens}
    if json_mode:
        generation_config["response_mime_type"] = "application/json"
    try:
        payload = _post_json(
            owned_client,
            f"{_GEMINI_ENDPOINT}/{model}:generateContent",
            headers={"x-goog-api-key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
            },
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Gemini API request failed: {exc}", status_code=_http_status(exc)
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    candidates = payload.get("candidates", [])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise _gemini_empty_text_error(payload)
    return text


def call_llm(
    settings: Settings,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    json_mode: bool = False,
    static_prefix: str = "",
    client: httpx.Client | None = None,
) -> str:
    """json_mode requests provider-native JSON-only output (Groq's and
    Cerebras' response_format, Gemini's response_mime_type) instead of relying
    solely on enricher._extract_json's best-effort fenced-JSON parsing. Only
    meaningful for groq/gemini/cerebras; ignored by anthropic and ollama,
    whose JSON reliability already comes from prompting alone.

    static_prefix is the shared, repeated instruction text that precedes the
    per-call variable content (source URL, transcript, etc). Anthropic gets it
    as a separate cache_control-annotated system block (explicit prompt
    caching - see _call_claude). Groq/Gemini/Ollama get it prepended to the
    prompt text instead, since their prefix-caching (automatic for Groq and
    Gemini, none for Ollama) only pays off if the shared text is a literal
    prefix of the request.
    """
    _throttle(settings, settings.llm.provider)
    if settings.llm.provider == "ollama":
        combined = f"{static_prefix}\n\n{prompt}" if static_prefix else prompt
        return _call_ollama(settings, combined, model=model, max_tokens=max_tokens, client=client)
    if settings.llm.provider == "anthropic":
        return _call_claude(
            settings,
            prompt,
            model=model,
            max_tokens=max_tokens,
            static_prefix=static_prefix,
            client=client,
        )
    if settings.llm.provider == "groq":
        combined = f"{static_prefix}\n\n{prompt}" if static_prefix else prompt
        return _call_groq(
            settings,
            combined,
            model=model,
            max_tokens=max_tokens,
            json_mode=json_mode,
            client=client,
        )
    if settings.llm.provider == "gemini":
        combined = f"{static_prefix}\n\n{prompt}" if static_prefix else prompt
        return _call_gemini(
            settings,
            combined,
            model=model,
            max_tokens=max_tokens,
            json_mode=json_mode,
            client=client,
        )
    if settings.llm.provider == "cerebras":
        combined = f"{static_prefix}\n\n{prompt}" if static_prefix else prompt
        return _call_cerebras(
            settings,
            combined,
            model=model,
            max_tokens=max_tokens,
            json_mode=json_mode,
            client=client,
        )
    raise ValueError(f"Unknown llm.provider: {settings.llm.provider!r}")


def _encode_image_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _call_claude_vision(
    settings: Settings,
    prompt: str,
    image_paths: list[Path],
    *,
    model: str,
    max_tokens: int,
    static_prefix: str = "",
    client: httpx.Client | None = None,
) -> str:
    api_key = settings.require_anthropic_api_key()
    content: list[dict] = []
    for path in image_paths:
        media_type = mimetypes.guess_type(path.name)[0] or _DEFAULT_IMAGE_MEDIA_TYPE
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": _encode_image_b64(path),
                },
            }
        )
    content.append({"type": "text", "text": prompt})

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if static_prefix:
        body["system"] = [
            {
                "type": "text",
                "text": static_prefix,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    owned_client = client or httpx.Client(timeout=180.0)
    owns_client = client is None
    try:
        payload = _post_json(
            owned_client,
            _ANTHROPIC_ENDPOINT,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Claude API vision request failed: {exc}", status_code=_http_status(exc)
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    blocks = payload.get("content", [])
    text_blocks = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    if not text_blocks:
        raise LlmCallError(f"Claude API vision response had no text content: {payload!r}")
    return "".join(text_blocks)


def _call_ollama_vision(
    settings: Settings,
    prompt: str,
    image_paths: list[Path],
    *,
    model: str,
    max_tokens: int,
    static_prefix: str = "",
    client: httpx.Client | None = None,
) -> str:
    combined_prompt = f"{static_prefix}\n\n{prompt}" if static_prefix else prompt
    owned_client = client or httpx.Client(timeout=600.0)
    owns_client = client is None
    try:
        payload = _post_json(
            owned_client,
            f"{settings.llm.ollama_host}/api/generate",
            json={
                "model": model,
                "prompt": combined_prompt,
                "images": [_encode_image_b64(path) for path in image_paths],
                "stream": False,
                "options": {"num_predict": max_tokens, "num_ctx": settings.llm.ollama_num_ctx},
            },
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Ollama vision request to {settings.llm.ollama_host!r} failed: {exc}. "
            f"Is Ollama running, and is {model!r} pulled (ollama pull {model})? "
            "It must also be a vision-capable model.",
            status_code=_http_status(exc),
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    text = payload.get("response", "")
    if not text:
        raise LlmCallError(f"Ollama vision response had no text content: {payload!r}")
    return text


def _call_gemini_vision(
    settings: Settings,
    prompt: str,
    image_paths: list[Path],
    *,
    model: str,
    max_tokens: int,
    static_prefix: str = "",
    client: httpx.Client | None = None,
) -> str:
    """Every current Gemini model is natively multimodal - unlike Ollama/Anthropic,
    there's no separate 'vision-capable' model tier to pick here.
    """
    api_key = settings.require_gemini_api_key()
    combined_prompt = f"{static_prefix}\n\n{prompt}" if static_prefix else prompt
    parts: list[dict] = []
    for path in image_paths:
        media_type = mimetypes.guess_type(path.name)[0] or _DEFAULT_IMAGE_MEDIA_TYPE
        parts.append({"inline_data": {"mime_type": media_type, "data": _encode_image_b64(path)}})
    parts.append({"text": combined_prompt})

    owned_client = client or httpx.Client(timeout=180.0)
    owns_client = client is None
    try:
        payload = _post_json(
            owned_client,
            f"{_GEMINI_ENDPOINT}/{model}:generateContent",
            headers={"x-goog-api-key": api_key},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
        )
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Gemini API vision request failed: {exc}", status_code=_http_status(exc)
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    candidates = payload.get("candidates", [])
    resp_parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "".join(p.get("text", "") for p in resp_parts)
    if not text:
        raise _gemini_empty_text_error(payload)
    return text


def describe_images(
    settings: Settings,
    prompt: str,
    image_paths: list[Path],
    *,
    model: str,
    max_tokens: int,
    static_prefix: str = "",
    client: httpx.Client | None = None,
    provider: str | None = None,
) -> str:
    """provider overrides settings.llm.provider for this call - only "ollama",
    "anthropic", "gemini", and "groq" support vision here (Cerebras has no
    vision path), so callers whose text provider is cerebras pass
    settings.image_description.provider (or default to "ollama") instead of
    falling through to the text provider. Note Groq vision requires a model
    that actually supports image input - check `input_modalities` via GET
    /openai/v1/models, since most Groq text models don't.
    """
    if not image_paths:
        raise ValueError("describe_images requires at least one image path")
    resolved_provider = provider or settings.llm.provider
    _throttle(settings, resolved_provider)
    if resolved_provider == "ollama":
        return _call_ollama_vision(
            settings,
            prompt,
            image_paths,
            model=model,
            max_tokens=max_tokens,
            static_prefix=static_prefix,
            client=client,
        )
    if resolved_provider == "anthropic":
        return _call_claude_vision(
            settings,
            prompt,
            image_paths,
            model=model,
            max_tokens=max_tokens,
            static_prefix=static_prefix,
            client=client,
        )
    if resolved_provider == "gemini":
        return _call_gemini_vision(
            settings,
            prompt,
            image_paths,
            model=model,
            max_tokens=max_tokens,
            static_prefix=static_prefix,
            client=client,
        )
    if resolved_provider == "groq":
        return _call_groq_vision(
            settings,
            prompt,
            image_paths,
            model=model,
            max_tokens=max_tokens,
            static_prefix=static_prefix,
            client=client,
        )
    raise ValueError(
        f"Unknown or vision-incapable llm provider: {resolved_provider!r}. "
        "Vision only supports 'ollama', 'anthropic', 'gemini', or 'groq' - set "
        "image_description.provider in settings.yaml if llm.provider is 'cerebras'."
    )
