from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

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

    # Qwen (found live 2026-08-12, still true 2026-08-15 with no
    # reasoning_format sent at all) inlines a <think>...</think> reasoning
    # block before the real answer - this must not leak into the note as if it
    # were part of the description.
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
        # Coverage gap found live (2026-08-21): the same positively-phrased
        # refusal, but with four words between the verb and the noun ("a
        # description of the images") where the pattern allowed at most three.
        # Produced by feeding the model a WebP it could not decode; the reply
        # reached the vault as request-for-image-descriptions-to-generate-transcript.md.
        "Sure, I can help with that! Please provide a description of the images "
        "or copy and paste the text you see.",
        "Of course - please paste a transcription of each of the screenshots below.",
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


def _carousel(tmp_path, count: int) -> list:
    paths = []
    for i in range(count):
        p = tmp_path / f"carousel_{i}.jpg"
        p.write_bytes(f"fake-jpg-{i}".encode())
        paths.append(p)
    return paths


def test_describe_splits_a_carousel_that_exceeds_the_per_call_image_cap(tmp_path):
    """Every image went into one request against a fixed num_ctx, so a large
    carousel overflowed the model's context and returned HTTP 400 forever -
    reproduced 2026-08-21 with a real 20-image post (20667 tokens vs 16384).
    Retrying could never clear it, so the item burned its whole attempt budget
    and re-stranded its media in data/tmp/ each pass. Fan the images out over
    several calls instead, mirroring worker._transcribe_media_paths.
    """
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
        image_description=ImageDescriptionConfig(model="mistral-small3.1", max_images_per_call=3),
    )
    paths = _carousel(tmp_path, 7)

    with respx.mock:
        route = respx.post("http://localhost:11434/api/generate").mock(
            side_effect=[
                httpx.Response(200, json={"response": "first three"}),
                httpx.Response(200, json={"response": "second three"}),
                httpx.Response(200, json={"response": "last one"}),
            ]
        )
        result = LlmImageDescriber(settings).describe(paths, "cid-big")

    assert route.call_count == 3
    batch_sizes = [len(json.loads(c.request.content)["images"]) for c in route.calls]
    assert batch_sizes == [3, 3, 1]
    # Every image is sent exactly once, in the post's original order.
    sent = [img for c in route.calls for img in json.loads(c.request.content)["images"]]
    assert len(sent) == 7
    assert len(set(sent)) == 7
    for text in ("first three", "second three", "last one"):
        assert text in result.text


def test_describe_sends_a_carousel_at_the_cap_as_one_unlabelled_call(tmp_path):
    """The boundary: at exactly the cap there is still only one call, and the
    text is the model's description verbatim - no batch labelling, so the
    common single-batch case is byte-identical to the pre-batching behaviour.
    """
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
        image_description=ImageDescriptionConfig(model="mistral-small3.1", max_images_per_call=3),
    )
    paths = _carousel(tmp_path, 3)

    with respx.mock:
        route = respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "one whole carousel"})
        )
        result = LlmImageDescriber(settings).describe(paths, "cid-cap")

    assert route.call_count == 1
    assert len(json.loads(route.calls[0].request.content)["images"]) == 3
    assert result.text == "one whole carousel"


def test_describe_rejects_a_refusal_returned_by_a_later_batch(tmp_path):
    """The refusal guard has to run per batch. Checking only the joined text
    would let a real description in batch one carry a refusal in batch two
    through to the enricher and into the vault.
    """
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
        image_description=ImageDescriptionConfig(model="mistral-small3.1", max_images_per_call=2),
    )
    paths = _carousel(tmp_path, 4)

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            side_effect=[
                httpx.Response(200, json={"response": "a genuine description"}),
                httpx.Response(
                    200,
                    json={"response": "I cannot view the images you have attached."},
                ),
            ]
        )
        with pytest.raises(ImageDescriptionError, match="refused"):
            LlmImageDescriber(settings).describe(paths, "cid-refuse")


def test_describe_re_encodes_webp_before_sending_it_to_the_vision_model(tmp_path, monkeypatch):
    """Ollama/mistral-small3.1 does not decode WebP. It does not error either -
    it returns HTTP 200 with the image silently dropped and answers as though
    nothing were attached (measured 2026-08-21: 2.7s for a WebP against 70.3s
    for the same picture as JPEG). That reply then passed enrichment and landed
    in the vault as a normal note. Instagram commonly serves WebP.

    Re-encoding happens here rather than in downloader.py because there are two
    image-selection sites - downloader.py's gallery-dl branch and
    worker._cached_download_result's resume-from-tmp rescan - and both accept
    ".webp". This is where they converge before the vision call.
    """
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    webp = tmp_path / "carousel_0.webp"
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 fake-webp-payload")

    ffmpeg_calls = []

    def fake_run(command, **kwargs):
        assert command[0] == "ffmpeg", command
        ffmpeg_calls.append(command)
        Path(command[-1]).write_bytes(b"\x89PNG\r\n\x1a\nfake-png-payload")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with respx.mock:
        route = respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "a real description"})
        )
        result = LlmImageDescriber(settings).describe([webp], "cid-webp")

    assert len(ffmpeg_calls) == 1
    assert str(webp) in ffmpeg_calls[0]
    sent = base64.b64decode(json.loads(route.calls[0].request.content)["images"][0])
    assert sent.startswith(b"\x89PNG"), "the model must receive PNG, not the original WebP"
    assert b"WEBP" not in sent
    assert result.text == "a real description"


def test_describe_does_not_re_encode_a_format_the_model_already_reads(tmp_path, monkeypatch):
    """Only WebP is broken. Re-encoding JPEG/PNG would burn an ffmpeg process
    per image per attempt and lose quality for nothing.
    """
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    jpg = tmp_path / "carousel_0.jpg"
    jpg.write_bytes(b"\xff\xd8\xff-fake-jpeg-payload")

    def fail_run(command, **kwargs):
        pytest.fail(f"no re-encode should run for a .jpg: {command}")

    monkeypatch.setattr(subprocess, "run", fail_run)

    with respx.mock:
        route = respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "a real description"})
        )
        LlmImageDescriber(settings).describe([jpg], "cid-jpg")

    sent = base64.b64decode(json.loads(route.calls[0].request.content)["images"][0])
    assert sent == jpg.read_bytes()


def test_describe_reports_a_failed_webp_re_encode_instead_of_sending_it_anyway(
    tmp_path, monkeypatch
):
    """A failed conversion must not fall back to posting the WebP - that is the
    exact silent-success path this fix exists to close.
    """
    settings = make_settings(
        tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    webp = tmp_path / "carousel_0.webp"
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 fake-webp-payload")

    def failing_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="ffmpeg: boom")

    monkeypatch.setattr(subprocess, "run", failing_run)

    with respx.mock:
        route = respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "unreachable"})
        )
        with pytest.raises(ImageDescriptionError, match="webp"):
            LlmImageDescriber(settings).describe([webp], "cid-webp-fail")

    assert route.call_count == 0
