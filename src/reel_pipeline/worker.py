"""Orchestrates a single pass of the pipeline: validate/dedup -> download ->
transcribe -> enrich -> write note -> optionally write skill -> persist state.

Every stage updates state.json before moving to the next, so a crash mid-item
leaves an accurate `status` behind and the next run_once() will pick the item
back up via QueueManager.get_actionable_items() (restart-safe, idempotent).
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from reel_pipeline.config import Settings
from reel_pipeline.downloader import Downloader, get_downloader
from reel_pipeline.enricher import Enricher
from reel_pipeline.image_describer import ImageDescriber, get_image_describer
from reel_pipeline.logging_setup import get_logger, log_context
from reel_pipeline.models import (
    ContentItem,
    EnrichmentResult,
    ItemStatus,
    MediaType,
    StateRecord,
    TranscriptResult,
)
from reel_pipeline.obsidian_writer import write_note
from reel_pipeline.queue_manager import QueueManager
from reel_pipeline.skill_writer import SkillWriter
from reel_pipeline.text_fetcher import TextFetcher, get_text_fetcher
from reel_pipeline.transcriber import Transcriber, get_transcriber

logger = get_logger(__name__)


class EnrichmentProvider(Protocol):
    def enrich(self, transcript: TranscriptResult, source_url: str) -> EnrichmentResult: ...


class SkillGenerator(Protocol):
    def generate(self, item: ContentItem) -> Path | None: ...


@dataclass
class RunSummary:
    processed: int = 0
    done: int = 0
    failed: int = 0
    note_paths: list[str] = field(default_factory=list)
    skill_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class WorkerPipeline:
    def __init__(
        self,
        settings: Settings,
        queue_manager: QueueManager,
        downloader: Downloader,
        transcriber: Transcriber,
        image_describer: ImageDescriber,
        text_fetcher: TextFetcher,
        enricher: EnrichmentProvider,
        skill_writer: SkillGenerator,
    ):
        self.settings = settings
        self.queue_manager = queue_manager
        self.downloader = downloader
        self.transcriber = transcriber
        self.image_describer = image_describer
        self.text_fetcher = text_fetcher
        self.enricher = enricher
        self.skill_writer = skill_writer

    def process_item(self, record: StateRecord) -> StateRecord:
        try:
            if record.content_kind == "text":
                record.status = ItemStatus.DOWNLOADING
                self.queue_manager.update_record(record)
                transcript = self.text_fetcher.fetch(record.url, record.content_id)
                log_context(
                    logger, 20, "text captured", content_id=record.content_id, url=record.url
                )
                record.status = ItemStatus.TRANSCRIBING
                self.queue_manager.update_record(record)
            else:
                record.status = ItemStatus.DOWNLOADING
                self.queue_manager.update_record(record)
                download_result = self.downloader.download(record.url, record.content_id)
                log_context(
                    logger, 20, "downloaded", content_id=record.content_id, url=record.url
                )

                record.status = ItemStatus.TRANSCRIBING
                self.queue_manager.update_record(record)
                media_paths = [Path(p) for p in download_result.media_paths]
                if download_result.media_type is MediaType.IMAGE:
                    transcript = self.image_describer.describe(media_paths, record.content_id)
                else:
                    transcript = self._transcribe_media_paths(media_paths, record.content_id)
                log_context(
                    logger,
                    20,
                    "transcribed",
                    content_id=record.content_id,
                    media_type=download_result.media_type.value,
                )

            record.status = ItemStatus.ENRICHING
            self.queue_manager.update_record(record)
            enrichment = self.enricher.enrich(transcript, record.url)
            log_context(
                logger,
                20,
                "enriched",
                content_id=record.content_id,
                high_signal=enrichment.high_signal,
            )

            record.status = ItemStatus.WRITING_NOTE
            self.queue_manager.update_record(record)
            content_item = ContentItem(
                content_id=record.content_id,
                source_url=record.url,
                created_at=datetime.now(UTC),
                transcript=transcript,
                enrichment=enrichment,
            )
            note_path = write_note(self.settings, content_item)
            skill_path = self.skill_writer.generate(content_item)

            previous_note_path = record.note_path
            previous_skill_path = record.skill_path
            record.status = ItemStatus.DONE
            record.note_path = str(note_path)
            record.skill_path = str(skill_path) if skill_path else None
            record.error = None
            log_context(
                logger,
                20,
                "note written",
                content_id=record.content_id,
                note_path=str(note_path),
            )
            self._cleanup_tmp_dir(record.content_id)
            self._cleanup_stale_note(previous_note_path, record.note_path)
            self._cleanup_stale_skill(previous_skill_path, record.skill_path)

        except Exception as exc:  # noqa: BLE001 - any stage failure must be recorded, not raised
            new_error = str(exc)
            previous_error = record.error
            record.status = ItemStatus.FAILED
            record.error = new_error
            # Only log a fresh needs-attention line when this is a new failure (first
            # occurrence, or the error changed) - not on every identical retry, which
            # would otherwise grow needs-attention.txt by one line per run-once forever.
            if new_error != previous_error:
                reason = f"processing failed: {new_error}"
                self.queue_manager.append_needs_attention(record.url, reason)
            log_context(
                logger, 40, "processing failed", content_id=record.content_id, error=new_error
            )

        self.queue_manager.update_record(record)
        return record

    def _transcribe_media_paths(
        self, media_paths: list[Path], content_id: str
    ) -> TranscriptResult:
        """Transcribes one or more video files and combines them into a single
        TranscriptResult - a multi-video Instagram carousel produces several files
        (see GalleryDlDownloader.download()), mirroring how image_describer.describe()
        already combines multiple images from a photo carousel into one description.
        """
        if len(media_paths) == 1:
            return self.transcriber.transcribe(media_paths[0], content_id)

        results = [self.transcriber.transcribe(path, content_id) for path in media_paths]
        combined_text = "\n\n".join(
            f"[Clip {i + 1}/{len(results)}] {result.text}" for i, result in enumerate(results)
        )
        total_duration = sum(result.duration_seconds or 0.0 for result in results) or None
        language = next((result.language for result in results if result.language), None)
        return TranscriptResult(
            content_id=content_id,
            text=combined_text,
            language=language,
            backend=results[0].backend,
            duration_seconds=total_duration,
        )

    def _cleanup_stale_note(self, previous_path: str | None, new_path: str) -> None:
        """Reprocessing an already-DONE item (e.g. a concurrent duplicate run, or a
        manual retry after clearing an error) can produce a different title -
        enrichment is an LLM call, not deterministic - and note filenames are
        `<content_id>-<title-slug>.md`. Without this, the old file would sit in the
        vault forever as an orphan duplicate, indistinguishable from a genuinely
        different note.
        """
        if not previous_path or previous_path == new_path:
            return
        try:
            old_file = Path(previous_path)
            if old_file.is_file():
                old_file.unlink()
        except OSError as exc:
            log_context(logger, 30, "stale note cleanup failed", path=previous_path, error=str(exc))

    def _cleanup_stale_skill(self, previous_path: str | None, new_path: str | None) -> None:
        """Same rationale as _cleanup_stale_note, for skill_writer's `<slug>/SKILL.md`
        artifacts - removes the whole stale `<slug>/` directory, not just the file.
        """
        if not previous_path or previous_path == new_path:
            return
        try:
            old_dir = Path(previous_path).parent
            if old_dir.is_dir():
                shutil.rmtree(old_dir)
        except OSError as exc:
            log_context(
                logger, 30, "stale skill cleanup failed", path=previous_path, error=str(exc)
            )

    def _cleanup_tmp_dir(self, content_id: str) -> None:
        """Remove downloaded media for a successfully-processed item - its content is
        already captured in the note, so the raw file(s) under data/tmp/ serve no
        further purpose and would otherwise accumulate on disk indefinitely.
        """
        tmp_dir = self.settings.tmp_dir / content_id
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
        except OSError as exc:
            log_context(
                logger, 30, "tmp cleanup failed", content_id=content_id, error=str(exc)
            )

    def _sweep_stale_tmp_dirs(self) -> None:
        """Reclaim data/tmp/<content_id>/ dirs left behind by items that never
        reached success (e.g. permanently-failing items) - _cleanup_tmp_dir only
        handles the success path, so these would otherwise accumulate forever.
        Age is judged by directory mtime, not item status, so it works even for
        content_ids no longer in state.json.
        """
        retention_days = self.settings.maintenance.tmp_retention_days
        if retention_days <= 0 or not self.settings.tmp_dir.exists():
            return
        cutoff = time.time() - retention_days * 86400
        for child in self.settings.tmp_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child)
            except OSError as exc:
                log_context(logger, 30, "stale tmp sweep failed", path=str(child), error=str(exc))

    def run_once(self) -> RunSummary:
        self.settings.ensure_directories()
        self._sweep_stale_tmp_dirs()
        self.queue_manager.prune_needs_attention(
            self.settings.maintenance.needs_attention_retention_days
        )
        self.queue_manager.sync_queue_file_into_state()
        actionable = self.queue_manager.get_actionable_items()

        summary = RunSummary()
        for record in actionable:
            result = self.process_item(record)
            summary.processed += 1
            if result.status is ItemStatus.DONE:
                summary.done += 1
                if result.note_path:
                    summary.note_paths.append(result.note_path)
                if result.skill_path:
                    summary.skill_paths.append(result.skill_path)
            else:
                summary.failed += 1
                if result.error:
                    summary.errors.append(f"{result.content_id}: {result.error}")
        return summary


def build_worker(settings: Settings) -> WorkerPipeline:
    """Wire up the real (non-fake) implementations for CLI/webhook use."""
    return WorkerPipeline(
        settings=settings,
        queue_manager=QueueManager(settings),
        downloader=get_downloader(settings),
        transcriber=get_transcriber(settings),
        image_describer=get_image_describer(settings),
        text_fetcher=get_text_fetcher(settings),
        enricher=Enricher(settings),
        skill_writer=SkillWriter(settings),
    )
