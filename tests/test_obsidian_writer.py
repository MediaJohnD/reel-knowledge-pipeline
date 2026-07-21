from __future__ import annotations

from datetime import UTC, datetime

import yaml

from reel_pipeline.config import Settings
from reel_pipeline.models import ContentItem, EnrichmentResult, TranscriptResult
from reel_pipeline.obsidian_writer import note_filename, note_path, slugify, write_note


def make_item(content_id="abc123", title="How To Build A Thing") -> ContentItem:
    return ContentItem(
        content_id=content_id,
        source_url="https://www.youtube.com/watch?v=abc123",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript=TranscriptResult(
            content_id=content_id, text="full transcript text here", backend="fake"
        ),
        enrichment=EnrichmentResult(
            title=title,
            summary="A short summary.",
            tags=["howto", "building"],
            tools_mentioned=["Widget"],
            key_takeaways=["Do the thing", "Then do the other thing"],
            high_signal=True,
            skill_candidate_reason="Teaches a repeatable build process.",
        ),
    )


def test_slugify_produces_url_safe_lowercase():
    assert slugify("How To Build A Thing!") == "how-to-build-a-thing"


def test_filename_is_the_title_slug_with_no_content_id_prefix(tmp_path):
    name1 = note_filename(tmp_path, "abc123", "How To Build A Thing")
    name2 = note_filename(tmp_path, "abc123", "How To Build A Thing")
    assert name1 == name2 == "how-to-build-a-thing.md"


def test_filename_disambiguates_slug_collision_between_different_content_ids(tmp_path):
    write_note(Settings(project_root=tmp_path), make_item(content_id="aaa111", title="Same Title"))

    name = note_filename(tmp_path / "data" / "notes", "bbb222", "Same Title")

    assert name == "same-title-2.md"


def test_filename_reuses_own_file_on_reprocessing_same_content_id(tmp_path):
    settings = Settings(project_root=tmp_path)
    write_note(settings, make_item(content_id="abc123", title="Same Title"))

    name = note_filename(settings.vault_dir, "abc123", "Same Title")

    assert name == "same-title.md"


def test_write_note_creates_file_with_required_frontmatter_fields(tmp_path):
    settings = Settings(project_root=tmp_path)
    item = make_item()

    path = write_note(settings, item)

    assert path.exists()
    assert path == note_path(settings, item.content_id, item.enrichment.title)

    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    frontmatter_block = raw.split("---\n")[1]
    frontmatter = yaml.safe_load(frontmatter_block)

    assert frontmatter["title"] == item.enrichment.title
    assert frontmatter["source_url"] == item.source_url
    assert frontmatter["content_id"] == item.content_id
    assert frontmatter["created_at"] == item.created_at.isoformat()
    assert frontmatter["tags"] == item.enrichment.tags
    assert frontmatter["tools_mentioned"] == item.enrichment.tools_mentioned

    assert item.enrichment.summary in raw
    assert "Do the thing" in raw
    assert "full transcript text here" in raw


def test_write_note_is_idempotent_and_overwrites_same_path(tmp_path):
    settings = Settings(project_root=tmp_path)
    item = make_item()

    first_path = write_note(settings, item)
    second_path = write_note(settings, item)

    assert first_path == second_path
    matching_files = list(settings.vault_dir.glob(f"{slugify(item.enrichment.title)}*.md"))
    assert len(matching_files) == 1
