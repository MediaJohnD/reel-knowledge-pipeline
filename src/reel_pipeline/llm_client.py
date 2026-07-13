"""Shared LLM call abstraction, used by the enrichment, skill-writer, and
image-description modules so the HTTP/auth/error-handling logic lives in
exactly one place.

Dispatches by settings.llm.provider:
- "anthropic": Claude Messages API (requires ANTHROPIC_API_KEY).
- "ollama": a local Ollama instance (settings.llm.ollama_host), no API key -
  just requires Ollama running and the configured model already pulled
  (`ollama pull <model>`).

call_llm() is text-only (enrichment, skill generation). describe_images() adds
image input for vision-capable models (image-post/carousel description) - the
model configured must actually support vision (see ImageDescriptionConfig).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import httpx

from reel_pipeline.config import Settings

_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_IMAGE_MEDIA_TYPE = "image/jpeg"


class LlmCallError(RuntimeError):
    """Raised when an LLM call fails or returns no usable text."""


def _call_claude(
    settings: Settings,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    client: httpx.Client | None = None,
) -> str:
    api_key = settings.require_anthropic_api_key()
    owned_client = client or httpx.Client(timeout=120.0)
    owns_client = client is None
    try:
        response = owned_client.post(
            _ANTHROPIC_ENDPOINT,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
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


def call_llm(
    settings: Settings,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    client: httpx.Client | None = None,
) -> str:
    if settings.llm.provider == "ollama":
        return _call_ollama(settings, prompt, model=model, max_tokens=max_tokens, client=client)
    if settings.llm.provider == "anthropic":
        return _call_claude(settings, prompt, model=model, max_tokens=max_tokens, client=client)
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
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": content}],
            },
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
    client: httpx.Client | None = None,
) -> str:
    if not image_paths:
        raise ValueError("describe_images requires at least one image path")
    if settings.llm.provider == "ollama":
        return _call_ollama_vision(
            settings, prompt, image_paths, model=model, max_tokens=max_tokens, client=client
        )
    if settings.llm.provider == "anthropic":
        return _call_claude_vision(
            settings, prompt, image_paths, model=model, max_tokens=max_tokens, client=client
        )
    raise ValueError(f"Unknown llm.provider: {settings.llm.provider!r}")
