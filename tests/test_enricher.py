from __future__ import annotations

import httpx
import respx

from reel_pipeline.config import LlmConfig, Settings
from reel_pipeline.enricher import Enricher, render_and_split
from reel_pipeline.models import TranscriptResult


def test_render_and_split_separates_static_prefix_from_variable_content():
    template = (
        "Static instructions here.\n\n"
        "<!-- CACHE:BOUNDARY -->\n\n"
        "Source URL: {{source_url}}\n"
        "Transcript: {{transcript}}"
    )
    static_prefix, prompt = render_and_split(
        template, source_url="https://example.com", transcript="hello"
    )
    assert static_prefix == "Static instructions here."
    assert prompt == "Source URL: https://example.com\nTranscript: hello"


def test_render_and_split_without_marker_returns_empty_static_prefix():
    template = "Source URL: {{source_url}}"
    static_prefix, prompt = render_and_split(template, source_url="https://example.com")
    assert static_prefix == ""
    assert prompt == "Source URL: https://example.com"


@respx.mock
def test_enrich_uses_transcript_prompt_for_media_content(tmp_path):
    # Set up prompt files
    prompts_dir = tmp_path / "config" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "enrich_transcript.md").write_text(
        "You are an analyst turning a raw video/reel transcript into structured metadata.\n\n"
        "Source URL: {{source_url}}\n\n"
        "Transcript:\n"
        '"""\n'
        "{{transcript}}\n"
        '"""\n\n'
        "Respond with valid JSON only."
    )
    (prompts_dir / "enrich_text_capture.md").write_text(
        "You are an analyst turning a captured reference document into metadata.\n\n"
        "Source URL: {{source_url}}\n\n"
        "Content:\n"
        '"""\n'
        "{{transcript}}\n"
        '"""\n\n'
        "Respond with valid JSON only."
    )

    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="ollama"))
    captured = {}

    def capture(request):
        import json

        captured["prompt"] = json.loads(request.content)["prompt"]
        return httpx.Response(
            200,
            json={
                "response": '{"title": "T", "summary": "S", "tags": [], '
                '"tools_mentioned": [], "key_takeaways": [], "high_signal": false, '
                '"skill_candidate_reason": null}'
            },
        )

    respx.post("http://localhost:11434/api/generate").mock(side_effect=capture)

    transcript = TranscriptResult(
        content_id="cid1", text="spoken words here", content_kind="media", backend="fake"
    )
    Enricher(settings).enrich(transcript, "https://youtube.com/watch?v=abc")

    assert "video/reel transcript" in captured["prompt"]


@respx.mock
def test_enrich_uses_text_capture_prompt_for_text_content(tmp_path):
    # Set up prompt files
    prompts_dir = tmp_path / "config" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "enrich_transcript.md").write_text(
        "You are an analyst turning a raw video/reel transcript into structured metadata.\n\n"
        "Source URL: {{source_url}}\n\n"
        "Transcript:\n"
        '"""\n'
        "{{transcript}}\n"
        '"""\n\n'
        "Respond with valid JSON only."
    )
    (prompts_dir / "enrich_text_capture.md").write_text(
        "You are an analyst turning a captured reference document into metadata.\n\n"
        "Source URL: {{source_url}}\n\n"
        "Captured content:\n"
        '"""\n'
        "{{transcript}}\n"
        '"""\n\n'
        "This is reference/documentation content, not a spoken transcript.\n\n"
        "Respond with valid JSON only."
    )

    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="ollama"))
    captured = {}

    def capture(request):
        import json

        captured["prompt"] = json.loads(request.content)["prompt"]
        return httpx.Response(
            200,
            json={
                "response": '{"title": "T", "summary": "S", "tags": [], '
                '"tools_mentioned": [], "key_takeaways": [], "high_signal": false, '
                '"skill_candidate_reason": null}'
            },
        )

    respx.post("http://localhost:11434/api/generate").mock(side_effect=capture)

    transcript = TranscriptResult(
        content_id="cid2", text="# repo\n\nA CLI tool.", content_kind="text", backend="github-api"
    )
    Enricher(settings).enrich(transcript, "https://github.com/owner/repo")

    assert "reference" in captured["prompt"].lower() or "README" in captured["prompt"]


def test_extract_json_unwraps_a_single_object_list():
    # Real failure 2026-11-06: a model answered [ {...} ] and EnrichmentResult(**list)
    # crashed with a TypeError instead of a clear enrichment error.
    from reel_pipeline.enricher import _extract_json

    assert _extract_json('[{"title": "t"}]') == {"title": "t"}


def test_extract_json_rejects_non_object_json_with_enrichment_error():
    import pytest

    from reel_pipeline.enricher import EnrichmentError, _extract_json

    for raw in ('[{"a": 1}, {"b": 2}]', "[]", '"just a string"', "42"):
        with pytest.raises(EnrichmentError, match="not a JSON object"):
            _extract_json(raw)
