from __future__ import annotations

from datetime import UTC, datetime

from reel_pipeline import skill_writer as skill_writer_module
from reel_pipeline.config import PROJECT_ROOT, PromptsConfig, Settings, SkillWriterConfig
from reel_pipeline.models import ContentItem, EnrichmentResult, TranscriptResult
from reel_pipeline.skill_writer import SkillWriter, should_generate_skill, skill_path


def make_settings_with_real_prompts(tmp_path, **overrides) -> Settings:
    """Settings with data dirs isolated under tmp_path but prompts pointing at the
    real repo's config/prompts/ (prompts are part of the source tree, not per-run data).
    """
    return Settings(
        project_root=tmp_path,
        prompts=PromptsConfig(
            enrich_transcript=str(PROJECT_ROOT / "config" / "prompts" / "enrich_transcript.md"),
            create_skill=str(PROJECT_ROOT / "config" / "prompts" / "create_skill.md"),
        ),
        **overrides,
    )


def make_item(high_signal: bool, reason: str | None) -> ContentItem:
    return ContentItem(
        content_id="abc123",
        source_url="https://www.youtube.com/watch?v=abc123",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript=TranscriptResult(content_id="abc123", text="transcript text", backend="fake"),
        enrichment=EnrichmentResult(
            title="Reusable Build Workflow",
            summary="Summary.",
            tags=["howto"],
            tools_mentioned=["Widget"],
            key_takeaways=["Step one", "Step two"],
            high_signal=high_signal,
            skill_candidate_reason=reason,
        ),
    )


def test_should_generate_skill_requires_high_signal_and_reason(tmp_path):
    settings = Settings(project_root=tmp_path)

    assert should_generate_skill(make_item(True, "reusable workflow"), settings) is True
    assert should_generate_skill(make_item(False, None), settings) is False
    assert should_generate_skill(make_item(False, "reason but not high signal"), settings) is False
    assert should_generate_skill(make_item(True, None), settings) is False


def test_should_generate_skill_respects_enabled_flag(tmp_path):
    settings = Settings(project_root=tmp_path, skill_writer=SkillWriterConfig(enabled=False))
    assert should_generate_skill(make_item(True, "reusable workflow"), settings) is False


def test_generate_returns_none_and_makes_no_api_call_when_low_signal(tmp_path, monkeypatch):
    settings = make_settings_with_real_prompts(tmp_path)
    calls = []
    monkeypatch.setattr(
        skill_writer_module, "call_llm", lambda *a, **k: calls.append(1) or "unused"
    )

    writer = SkillWriter(settings)
    result = writer.generate(make_item(False, None))

    assert result is None
    assert calls == []


def test_generate_writes_deterministic_skill_path_for_high_signal_item(tmp_path, monkeypatch):
    settings = make_settings_with_real_prompts(tmp_path)
    fake_skill_md = (
        "---\nname: reusable-build-workflow\ndescription: test\n---\n\n# Reusable Build Workflow\n"
    )
    monkeypatch.setattr(skill_writer_module, "call_llm", lambda *a, **k: fake_skill_md)

    writer = SkillWriter(settings)
    item = make_item(True, "Teaches a reusable workflow")
    result = writer.generate(item)

    expected_path = skill_path(settings, item.enrichment.title)
    assert result == expected_path
    assert expected_path.exists()
    assert "reusable-build-workflow" in expected_path.read_text(encoding="utf-8")
    # Generated skills must live under skills_dir, separate from .claude/skills/.
    assert settings.skills_dir in expected_path.parents
