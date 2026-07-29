"""Orchestrates a single pass of the pipeline: validate/dedup -> download ->
transcribe -> enrich -> write note -> optionally write skill -> persist state.

Every stage updates state.json before moving to the next, so a crash mid-item
leaves an accurate `status` behind and the next run_once() will pick the item
back up via QueueManager.get_actionable_items() (restart-safe, idempotent).

run_once() is guarded by its own cross-process file lock (see RunOnceLockError
below), held for the entire pass so two run_once() calls (a concurrent CLI
invocation, or the webhook server's own background run) never process the
same item twice. webhook_server.py's threading.Lock only prevents overlapping
webhook-triggered runs *within that one long-lived process* - it does nothing
for a manually-invoked `run-once` CLI call racing against the webhook server,
or two manual CLI calls racing against each other; run_once()'s own lock
covers all of those cases.

Separately, every individual state.json read-modify-write (add_url,
update_record, sync_queue_file_into_state) goes through
QueueManager._locked_mutate(), which uses a *different* lock file so that
webhook registration (add_url()) is never blocked waiting behind an
in-progress backlog pass - see queue_manager.py.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from reel_pipeline.config import Settings
from reel_pipeline.downloader import Downloader, get_downloader
from reel_pipeline.enricher import Enricher
from reel_pipeline.image_describer import ImageDescriber, get_image_describer
from reel_pipeline.logging_setup import get_logger, log_context
from reel_pipeline.models import (
    ContentItem,
    DownloadResult,
    EnrichmentResult,
    ItemStage,
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


class RunOnceLockError(RuntimeError):
    """Raised when run_once() can't acquire the cross-process state.json lock
    within the timeout - another run_once() (this process, another CLI
    invocation, or the webhook server) is still running after an unexpectedly
    long time, which likely means something is stuck rather than just busy.
    """


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
            transcript: TranscriptResult | None = None
            enrichment: EnrichmentResult | None = None
            download_result: DownloadResult | None = None

            cached_transcript = (
                self._cached_transcript_result(record.content_id)
                if record.last_completed_stage is not None
                else None
            )
            cached_enrichment = (
                self._cached_enrichment_result(record.content_id)
                if record.last_completed_stage is not None
                else None
            )

            if (
                record.last_completed_stage is ItemStage.ENRICHED
                and cached_transcript is not None
                and cached_enrichment is not None
            ):
                transcript = cached_transcript
                enrichment = cached_enrichment
                log_context(
                    logger,
                    20,
                    "reusing cached enrichment from a previous attempt",
                    content_id=record.content_id,
                )
            elif (
                record.last_completed_stage in (ItemStage.TRANSCRIBED, ItemStage.ENRICHED)
                and cached_transcript is not None
            ):
                transcript = cached_transcript
                log_context(
                    logger,
                    20,
                    "reusing cached transcript from a previous attempt",
                    content_id=record.content_id,
                )
            elif record.content_kind == "text":
                record.status = ItemStatus.DOWNLOADING
                self.queue_manager.update_record(record)
                transcript = self.text_fetcher.fetch(record.url, record.content_id)
                log_context(
                    logger, 20, "text captured", content_id=record.content_id, url=record.url
                )
                self._write_transcript_cache(record.content_id, transcript)
                record.last_completed_stage = ItemStage.TRANSCRIBED
            else:
                cached_download = (
                    self._cached_download_result(record.content_id)
                    if record.last_completed_stage is not None
                    else None
                )
                if (
                    record.last_completed_stage
                    in (
                        ItemStage.DOWNLOADED,
                        ItemStage.TRANSCRIBED,
                        ItemStage.ENRICHED,
                    )
                    and cached_download is not None
                ):
                    download_result = cached_download
                    log_context(
                        logger,
                        20,
                        "reusing cached download from a previous attempt",
                        content_id=record.content_id,
                    )
                else:
                    record.status = ItemStatus.DOWNLOADING
                    self.queue_manager.update_record(record)
                    download_result = self.downloader.download(record.url, record.content_id)
                    record.last_completed_stage = ItemStage.DOWNLOADED
                    log_context(
                        logger, 20, "downloaded", content_id=record.content_id, url=record.url
                    )

            if transcript is None:
                # transcript is only still None here via the media (non-text) branch
                # above, which always assigns download_result before falling through.
                assert download_result is not None
                record.status = ItemStatus.TRANSCRIBING
                self.queue_manager.update_record(record)
                media_paths = [Path(p) for p in download_result.media_paths]
                if download_result.media_type is MediaType.IMAGE:
                    transcript = self.image_describer.describe(media_paths, record.content_id)
                else:
                    transcript = self._transcribe_media_paths(media_paths, record.content_id)
                self._write_transcript_cache(record.content_id, transcript)
                record.last_completed_stage = ItemStage.TRANSCRIBED
                log_context(
                    logger,
                    20,
                    "transcribed",
                    content_id=record.content_id,
                    media_type=download_result.media_type.value,
                )

            if enrichment is None:
                record.status = ItemStatus.ENRICHING
                self.queue_manager.update_record(record)
                enrichment = self.enricher.enrich(transcript, record.url)
                self._write_enrichment_cache(record.content_id, enrichment)
                record.last_completed_stage = ItemStage.ENRICHED
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
            record.last_completed_stage = ItemStage.NOTE_WRITTEN

            previous_note_path = record.note_path
            previous_skill_path = record.skill_path
            record.status = ItemStatus.DONE
            record.note_path = str(note_path)
            # Not yet known at this point - only the try/except below (skill
            # generation, not yet attempted) determines these. Clearing them here
            # (rather than leaving the previous attempt's values) avoids a crash
            # during skill generation permanently recording a stale skill_path/
            # skill_error pointing at an older run's outcome.
            record.skill_path = None
            record.skill_error = None
            record.error = None
            record.attempt_count = 0
            record.next_retry_at = None
            log_context(
                logger,
                20,
                "note written",
                content_id=record.content_id,
                note_path=str(note_path),
            )
            # Persist the DONE status now, before attempting skill generation - the
            # slowest, most kill-prone remaining step. Without this, a crash (hard
            # kill, OOM, power loss) during skill generation would leave state.json
            # showing the pre-DONE status, causing the next run_once() to redo the
            # entire pipeline (download/transcribe/enrich/write) for an item whose
            # note already exists on disk. See finding Important #1 in the state
            # reliability final review.
            self.queue_manager.update_record(record)

            # Skill generation is optional and independent of the note's success -
            # its failure must never mark an already-written note as FAILED (that
            # would re-run the whole pipeline next pass for something that already
            # succeeded). See docs/superpowers/specs/2026-07-29-state-reliability-design.md.
            try:
                skill_path = self.skill_writer.generate(content_item)
                record.skill_path = str(skill_path) if skill_path else None
                record.skill_error = None
            except Exception as exc:  # noqa: BLE001 - must never fail the whole item
                record.skill_path = None
                record.skill_error = str(exc)
                log_context(
                    logger,
                    30,
                    "skill generation failed",
                    content_id=record.content_id,
                    error=str(exc),
                )

            self._cleanup_tmp_dir(record.content_id)
            self._cleanup_stale_note(previous_note_path, record.note_path)
            self._cleanup_stale_skill(previous_skill_path, record.skill_path)

        except Exception as exc:  # noqa: BLE001 - any stage failure must be recorded, not raised
            new_error = str(exc)
            previous_error = record.error
            record.attempt_count += 1
            schedule = self.settings.retry.backoff_schedule_minutes
            if record.attempt_count >= self.settings.retry.max_attempts:
                record.status = ItemStatus.FAILED_PERMANENT
                record.next_retry_at = None
            else:
                record.status = ItemStatus.FAILED
                backoff_index = min(record.attempt_count, len(schedule)) - 1
                record.next_retry_at = datetime.now(UTC) + timedelta(
                    minutes=schedule[backoff_index]
                )
            record.error = new_error
            # Log a fresh needs-attention line when this is a new failure (first
            # occurrence, or the error changed) - not on every identical retry, which
            # would otherwise grow needs-attention.txt by one line per run-once forever.
            # Also always log once on the FAILED -> FAILED_PERMANENT transition, even
            # if the error text is identical to the previous attempt (the overwhelmingly
            # common case) - otherwise the moment the pipeline gives up on an item never
            # reaches the human-readable log at all.
            just_gave_up = record.status is ItemStatus.FAILED_PERMANENT
            if just_gave_up:
                reason = (
                    f"giving up after {record.attempt_count} attempts (permanent failure): "
                    f"{new_error}"
                )
                self.queue_manager.append_needs_attention(record.url, reason)
            elif new_error != previous_error:
                reason = f"processing failed: {new_error}"
                self.queue_manager.append_needs_attention(record.url, reason)
            log_context(
                logger,
                40,
                "processing failed",
                content_id=record.content_id,
                error=new_error,
                attempt_count=record.attempt_count,
                status=record.status.value,
            )

        self.queue_manager.update_record(record)
        return record

    def _cached_download_result(self, content_id: str) -> DownloadResult | None:
        """Returns a DownloadResult reconstructed from files still present in
        data/tmp/<content_id>/, or None if the directory is empty/missing (e.g.
        _sweep_stale_tmp_dirs() already reclaimed it, or this is a fresh item).
        Only media_paths/media_type are reconstructable this way - platform/
        source_title/duration_seconds are lost on resume, which is fine since
        nothing downstream of the download stage consumes them.
        """
        tmp_dir = self.settings.tmp_dir / content_id
        if not tmp_dir.is_dir():
            return None
        files = sorted(p for p in tmp_dir.rglob("*") if p.is_file())
        if not files:
            return None
        video_suffixes = (".mp3", ".mp4", ".mov", ".webm", ".mkv", ".m4a", ".wav")
        image_suffixes = (".jpg", ".jpeg", ".png", ".webp")
        videos = [p for p in files if p.suffix.lower() in video_suffixes]
        if videos:
            return DownloadResult(
                content_id=content_id,
                media_type=MediaType.VIDEO,
                media_paths=[str(p) for p in videos],
                platform="cached",
            )
        images = [p for p in files if p.suffix.lower() in image_suffixes]
        if images:
            return DownloadResult(
                content_id=content_id,
                media_type=MediaType.IMAGE,
                media_paths=[str(p) for p in images],
                platform="cached",
            )
        return None

    def _cached_transcript_result(self, content_id: str) -> TranscriptResult | None:
        """Returns a TranscriptResult cached under data/tmp/<content_id>/ by a
        previous attempt, or None if missing/unreadable. Mirrors
        _cached_download_result's "trust but verify" pattern: last_completed_stage
        alone is never enough - the artifact must actually still be present.
        """
        path = self.settings.tmp_dir / content_id / "transcript.json"
        if not path.is_file():
            return None
        try:
            return TranscriptResult.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _write_transcript_cache(self, content_id: str, transcript: TranscriptResult) -> None:
        tmp_dir = self.settings.tmp_dir / content_id
        tmp_dir.mkdir(parents=True, exist_ok=True)
        (tmp_dir / "transcript.json").write_text(
            transcript.model_dump_json(), encoding="utf-8"
        )

    def _cached_enrichment_result(self, content_id: str) -> EnrichmentResult | None:
        """Same rationale as _cached_transcript_result, for the enrichment stage."""
        path = self.settings.tmp_dir / content_id / "enrichment.json"
        if not path.is_file():
            return None
        try:
            return EnrichmentResult.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _write_enrichment_cache(self, content_id: str, enrichment: EnrichmentResult) -> None:
        tmp_dir = self.settings.tmp_dir / content_id
        tmp_dir.mkdir(parents=True, exist_ok=True)
        (tmp_dir / "enrichment.json").write_text(
            enrichment.model_dump_json(), encoding="utf-8"
        )

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
        `<title-slug>.md` (no content_id prefix). Without this, the old file
        would sit in the vault forever as an orphan duplicate, indistinguishable
        from a genuinely different note.
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
        """Acquires a lock (distinct from QueueManager's per-mutation state.json
        lock - see queue_manager.py's _locked_mutate()) before doing any work, so
        this run_once() can't interleave with another one in a different
        process (a concurrent CLI invocation, or the webhook server's own
        background run) - see this module's docstring for what goes wrong
        without it. This lock is deliberately a *different file* than the
        per-mutation one: run_once() holds this one for the whole pass while
        internally calling update_record() (via process_item()), which briefly
        acquires the per-mutation lock on every stage transition - using the
        same lock file for both would self-deadlock. Blocks up to 10 minutes
        for the lock (a full pass over a large backlog can legitimately take a
        while); past that, something is very likely stuck rather than just
        busy, so this raises instead of blocking forever. add_url() (webhook
        registration) never touches this lock, only the per-mutation one, so
        it's never blocked by an in-progress backlog pass.
        """
        lock_path = self.settings.state_file.with_suffix(".run_once.lock")
        try:
            with FileLock(str(lock_path), timeout=600):
                return self._run_once_locked()
        except FileLockTimeout as exc:
            raise RunOnceLockError(
                f"Could not acquire the run_once() lock ({lock_path}) within 600s - "
                "another run_once() (this process, another CLI invocation, or the "
                "webhook server) appears to be stuck rather than just busy."
            ) from exc

    def _run_once_locked(self) -> RunSummary:
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
