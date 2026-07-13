from __future__ import annotations

import json

import httpx
import pytest
import respx

from reel_pipeline.config import (
    PROJECT_ROOT,
    ImageDescriptionConfig,
    LlmConfig,
    PromptsConfig,
    Settings,
)
from reel_pipeline.image_describer import ImageDescriptionError, LlmImageDescriber


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        project_root=tmp_path,
        prompts=PromptsConfig(
            describe_image_post=str(PROJECT_ROOT / "config" / "prompts" / "describe_image_post.md")
        ),
        **overrides,
    )


def test_describe_calls_ollama_vision_and_returns_transcript(tmp_path):
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
        image_description=ImageDescriptionConfig(model="mistral-small3.1"),
    )
    image_path = tmp_path / "post_1.jpg"
    image_path.write_bytes(b"fake-jpg-bytes")

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "A screenshot of a tweet about AI."})
        )
        result = LlmImageDescriber(settings).describe([image_path], "cid1")

    assert result.content_id == "cid1"
    assert result.text == "A screenshot of a tweet about AI."
    assert result.backend == "vision:ollama:mistral-small3.1"
    assert result.language is None


def test_describe_raises_clear_error_on_failure(tmp_path):
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    image_path = tmp_path / "post_1.jpg"
    image_path.write_bytes(b"fake-jpg-bytes")

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(500, json={"error": "model not found"})
        )
        with pytest.raises(ImageDescriptionError):
            LlmImageDescriber(settings).describe([image_path], "cid2")


def test_describe_handles_multiple_carousel_images(tmp_path):
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    paths = []
    for i in range(3):
        p = tmp_path / f"post_{i}.jpg"
        p.write_bytes(f"fake-jpg-{i}".encode())
        paths.append(p)

    with respx.mock:
        route = respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "carousel description"})
        )
        result = LlmImageDescriber(settings).describe(paths, "cid3")

    assert result.text == "carousel description"
    sent_payload = json.loads(route.calls[0].request.content)
    assert len(sent_payload["images"]) == 3
