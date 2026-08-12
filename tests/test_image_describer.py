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


def test_describe_uses_image_description_provider_override_when_text_provider_lacks_vision(
    tmp_path,
):
    # llm.provider="cerebras" has no vision path (see llm_client.describe_images) -
    # image_description.provider="ollama" must override it rather than the call
    # falling through to cerebras and erroring on every photo/carousel post.
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="cerebras", ollama_host="http://localhost:11434"),
        image_description=ImageDescriptionConfig(model="mistral-small3.1", provider="ollama"),
    )
    image_path = tmp_path / "post_1.jpg"
    image_path.write_bytes(b"fake-jpg-bytes")

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "described via ollama override"})
        )
        result = LlmImageDescriber(settings).describe([image_path], "cid-override")

    assert result.text == "described via ollama override"
    assert result.backend == "vision:ollama:mistral-small3.1"


def test_describe_uses_gemini_vision(tmp_path):
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="gemini"),
        image_description=ImageDescriptionConfig(model="gemini-2.5-flash", provider="gemini"),
    )
    settings.gemini_api_key = "gemini-test-key"
    image_path = tmp_path / "post_1.jpg"
    image_path.write_bytes(b"fake-jpg-bytes")

    with respx.mock:
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "A screenshot described via Gemini."}]}}
                    ]
                },
            )
        )
        result = LlmImageDescriber(settings).describe([image_path], "cid-gemini")

    assert result.text == "A screenshot described via Gemini."
    assert result.backend == "vision:gemini:gemini-2.5-flash"


def test_describe_uses_groq_vision(tmp_path):
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="groq"),
        image_description=ImageDescriptionConfig(model="qwen/qwen3.6-27b", provider="groq"),
    )
    settings.groq_api_key = "gsk-test"
    image_path = tmp_path / "post_1.jpg"
    image_path.write_bytes(b"fake-jpg-bytes")

    # Qwen (found live 2026-08-12) inlines a <think>...</think> reasoning
    # block before the real answer when reasoning_format="raw" is requested -
    # this must not leak into the note as if it were part of the description.
    raw_content = (
        "\n<think>\nThe user wants a description.\n</think>\n\nA screenshot described via Groq."
    )

    with respx.mock:
        route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": raw_content}}]}
            )
        )
        result = LlmImageDescriber(settings).describe([image_path], "cid-groq")

    assert result.text == "A screenshot described via Groq."
    assert result.backend == "vision:groq:qwen/qwen3.6-27b"
    sent = json.loads(route.calls.last.request.content)
    assert sent["reasoning_format"] == "raw"
    content = sent["messages"][0]["content"]
    assert content[0]["type"] == "text" and content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


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


@pytest.mark.parametrize(
    "refusal",
    [
        # Both verbatim from notes that reached the Obsidian vault (2026-07-14, 2026-07-29).
        "I'm sorry, but I cannot directly view images. However, you can describe the "
        "content of the image to me, and I will help you transcribe any text and "
        "capture the subject matter as accurately as possible.",
        "I can help you transcribe and describe the images, but since I cannot view "
        "the actual images, please describe them to me.",
        "I am unable to see the image you attached.",
        # Coverage gap found by review (2026-08-10): the verb list originally
        # missed "analyze"/"interpret"/"identify" refusals.
        "I'm sorry, but I cannot analyze the image you've shared.",
        "Unfortunately I am not able to interpret the photo directly.",
        # Coverage gap found live (2026-08-12): a positively-phrased refusal that
        # never says "cannot" - the model asks to be sent the images instead of
        # describing the ones already attached. One such note reached the vault
        # before this pattern was added.
        "Certainly, please provide the images so I can analyze them for you.",
        "Sure! Please share the images and I'll describe them in detail.",
    ],
)
def test_describe_rejects_refusal_instead_of_writing_a_note(tmp_path, refusal):
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    image_path = tmp_path / "post_1.jpg"
    image_path.write_bytes(b"fake-jpg-bytes")

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": refusal})
        )
        # ImageDescriptionError propagates out of WorkerPipeline.process_item's
        # per-item try/except, which appends the URL to needs-attention.txt - see
        # test_download_failure_marks_failed_and_records_needs_attention.
        with pytest.raises(ImageDescriptionError, match="refused"):
            LlmImageDescriber(settings).describe([image_path], "cid-refusal")


def test_describe_keeps_genuine_description_that_mentions_images(tmp_path):
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    image_path = tmp_path / "post_1.jpg"
    image_path.write_bytes(b"fake-jpg-bytes")
    description = (
        "The image is a screenshot of a tweet arguing that you cannot ship a product "
        "without users. A second image shows the same text as a quote card, and the "
        "caption tells readers to view the images in order."
    )

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": description})
        )
        result = LlmImageDescriber(settings).describe([image_path], "cid-genuine")

    assert result.text == description


def test_describe_keeps_genuine_description_that_mentions_sharing_or_providing(tmp_path):
    # _REQUEST_FOR_IMAGES_RE targets an imperative request for images the model
    # doesn't have - a genuine description that merely narrates someone
    # sharing/providing something depicted in the image must still pass.
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    image_path = tmp_path / "post_1.jpg"
    image_path.write_bytes(b"fake-jpg-bytes")
    description = (
        "The screenshot shows a message where someone offers to share the photos "
        "from last weekend's trip once they upload them to the shared drive."
    )

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": description})
        )
        result = LlmImageDescriber(settings).describe([image_path], "cid-genuine-2")

    assert result.text == description


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
