"""Typed data models shared across pipeline stages."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ItemStatus(StrEnum):
    """Lifecycle status of a queued content item, persisted in state.json."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    ENRICHING = "enriching"
    WRITING_NOTE = "writing_note"
    DONE = "done"
    FAILED = "failed"
    # Distinct from FAILED: attempt_count has hit retry.max_attempts, so
    # get_actionable_items() stops retrying it. Distinct from BLOCKED (a policy
    # decision made before any processing was attempted) so the reason a
    # never-succeeding item stopped being retried is clear at a glance.
    FAILED_PERMANENT = "failed_permanent"
    BLOCKED = "blocked"


class ItemStage(StrEnum):
    """Durably-completed pipeline stage, used to resume a crash-interrupted item
    without redoing already-finished work. Distinct from ItemStatus, which
    describes what's currently happening (or last happened); this describes
    what's confirmed finished and safe to skip on retry.
    """

    DOWNLOADED = "downloaded"
    TRANSCRIBED = "transcribed"
    ENRICHED = "enriched"
    NOTE_WRITTEN = "note_written"


class QueueSource(StrEnum):
    """Where a URL entered the system from."""

    QUEUE_FILE = "queue_file"
    WEBHOOK = "webhook"


class StateRecord(BaseModel):
    """Persisted record for one content item, keyed by content_id in state.json."""

    content_id: str
    url: str
    normalized_url: str
    source: QueueSource
    status: ItemStatus
    content_kind: Literal["media", "text"] = "media"
    added_at: datetime
    updated_at: datetime
    error: str | None = None
    note_path: str | None = None
    skill_path: str | None = None
    # Reliability fields - see docs/superpowers/specs/2026-07-29-state-reliability-design.md.
    # All default so pre-existing state.json records (written before this change)
    # deserialize unchanged.
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    last_completed_stage: ItemStage | None = None
    # Set only by an optional-stage (skill generation) failure - never changes
    # `status` away from DONE, since the required stages already succeeded.
    skill_error: str | None = None


class MediaType(StrEnum):
    """What kind of media a download produced - determines whether the worker
    routes to Transcriber (video/audio) or ImageDescriber (photo posts/carousels).
    """

    VIDEO = "video"
    IMAGE = "image"


class DownloadResult(BaseModel):
    """Output of the downloader module."""

    content_id: str
    media_type: MediaType = MediaType.VIDEO
    media_paths: list[str]
    platform: str
    source_title: str | None = None
    duration_seconds: float | None = None


class TranscriptResult(BaseModel):
    """Text content extracted from the downloaded media - a literal transcript
    for video/audio (from Transcriber), a vision-model description for image
    posts (from ImageDescriber), or fetched page text for GitHub/Notion links
    (from TextFetcher). Downstream stages (enrichment, note writing) treat
    `.text` identically regardless of source; `content_kind` only changes
    which enrichment prompt is used.
    """

    content_id: str
    text: str
    content_kind: Literal["media", "text"] = "media"
    language: str | None = None
    backend: str
    duration_seconds: float | None = None


class EnrichmentResult(BaseModel):
    """Structured metadata produced by the enrichment module.

    This is the output contract required by the pipeline: every field here
    must be present in the final Obsidian note frontmatter/body.
    """

    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    tools_mentioned: list[str] = Field(default_factory=list)
    key_takeaways: list[str] = Field(default_factory=list)
    high_signal: bool = False
    skill_candidate_reason: str | None = None


class ContentItem(BaseModel):
    """Fully assembled record passed to the note/skill writers."""

    content_id: str
    source_url: str
    created_at: datetime
    transcript: TranscriptResult
    enrichment: EnrichmentResult
