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

import httpx

from reel_pipeline.config import Settings

_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
_CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_IMAGE_MEDIA_TYPE = "image/jpeg"

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


class LlmCallError(RuntimeError):
    """Raised when an LLM call fails or returns no usable text."""


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
        response = owned_client.post(
            _ANTHROPIC_ENDPOINT,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LlmCallError(f"Claude API request failed: {exc}") from exc
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
    owned_client = client or httpx.Client(timeout=300.0)
    owns_client = client is None
    try:
        response = owned_client.post(
            f"{settings.llm.ollama_host}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "num_ctx": settings.llm.ollama_num_ctx},
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Ollama request to {settings.llm.ollama_host!r} failed: {exc}. "
            f"Is Ollama running, and is {model!r} pulled (ollama pull {model})?"
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
    try:
        response = owned_client.post(
            _GROQ_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LlmCallError(f"Groq API request failed: {exc}") from exc
    finally:
        if owns_client:
            owned_client.close()

    choices = payload.get("choices", [])
    text = choices[0].get("message", {}).get("content", "") if choices else ""
    if not text:
        raise LlmCallError(f"Groq API response had no text content: {payload!r}")
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
        response = owned_client.post(
            _CEREBRAS_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LlmCallError(f"Cerebras API request failed: {exc}") from exc
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
        response = owned_client.post(
            f"{_GEMINI_ENDPOINT}/{model}:generateContent",
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LlmCallError(f"Gemini API request failed: {exc}") from exc
    finally:
        if owns_client:
            owned_client.close()

    candidates = payload.get("candidates", [])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise LlmCallError(f"Gemini API response had no text content: {payload!r}")
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
        response = owned_client.post(
            _ANTHROPIC_ENDPOINT,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LlmCallError(f"Claude API vision request failed: {exc}") from exc
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
    owned_client = client or httpx.Client(timeout=300.0)
    owns_client = client is None
    try:
        response = owned_client.post(
            f"{settings.llm.ollama_host}/api/generate",
            json={
                "model": model,
                "prompt": combined_prompt,
                "images": [_encode_image_b64(path) for path in image_paths],
                "stream": False,
                "options": {"num_predict": max_tokens, "num_ctx": settings.llm.ollama_num_ctx},
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LlmCallError(
            f"Ollama vision request to {settings.llm.ollama_host!r} failed: {exc}. "
            f"Is Ollama running, and is {model!r} pulled (ollama pull {model})? "
            "It must also be a vision-capable model."
        ) from exc
    finally:
        if owns_client:
            owned_client.close()

    text = payload.get("response", "")
    if not text:
        raise LlmCallError(f"Ollama vision response had no text content: {payload!r}")
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
    """provider overrides settings.llm.provider for this call - only "ollama" and
    "anthropic" support vision (Groq/Gemini/Cerebras have no vision path here), so
    callers whose text provider is one of those pass settings.image_description.provider
    (or default to "ollama") instead of falling through to the text provider.
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
    raise ValueError(
        f"Unknown or vision-incapable llm provider: {resolved_provider!r}. "
        "Vision only supports 'ollama' or 'anthropic' - set image_description.provider "
        "in settings.yaml if llm.provider is 'groq'/'gemini'/'cerebras'."
    )
