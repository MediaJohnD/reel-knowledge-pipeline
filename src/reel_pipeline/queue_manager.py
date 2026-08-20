"""Queue ingestion, deduplication, and restart-safe state persistence.

state.json is the single source of truth for "have we handled this content_id
before, and how far did we get." Both ingestion paths converge on it:

- `sync_queue_file_into_state()` drains data/inbox/queue.txt line by line into
  state.json (new PENDING records) and data/inbox/needs-attention.txt (invalid
  or blocked URLs), then empties consumed lines out of queue.txt.
- `add_url()` is called directly by the webhook server for the same effect,
  without going through queue.txt.

Processing is restart-safe: `get_actionable_items()` returns every record not
yet in a terminal status (DONE/BLOCKED), including ones left mid-flight by a
crashed previous run, regardless of which path added them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from textwrap import indent
from typing import TypeVar

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from reel_pipeline.config import Settings
from reel_pipeline.models import ItemStatus, QueueSource, StateRecord
from reel_pipeline.validators import classify_url_kind, validate_url

_T = TypeVar("_T")


class StateLockTimeout(RuntimeError):
    """Raised when a state.json mutation can't acquire its lock within the
    timeout - another process is holding it far longer than a single
    read-modify-write should ever take, which likely means it's stuck.
    """


class QueueManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue_file = settings.queue_file
        self.state_file = settings.state_file
        self.needs_attention_file = settings.needs_attention_file
        for path in (self.queue_file, self.state_file, self.needs_attention_file):
            path.parent.mkdir(parents=True, exist_ok=True)
        if not self.queue_file.exists():
            self.queue_file.write_text("", encoding="utf-8")
        if not self.needs_attention_file.exists():
            self.needs_attention_file.write_text("", encoding="utf-8")
        self._cached_state: dict[str, StateRecord] | None = None
        self._cached_stamp: tuple[int, int] | None = None
        self._record_json: dict[str, str] = {}

    # -- state.json persistence -------------------------------------------------

    def _disk_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.state_file.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def load_state(self) -> dict[str, StateRecord]:
        """Parsed state.json, reusing the last parse when the file on disk hasn't
        changed since we wrote it.

        Every mutation used to reparse the whole file (O(items) pydantic
        validation per stage transition, ~6 per item per run_once() pass). The
        cache is invalidated by (mtime_ns, size), so another process's write -
        a webhook add_url(), a concurrent CLI run - is always picked up; we
        only skip the reparse when the bytes are provably the ones we wrote.

        The returned dict is the live cache, not a copy: mutating a StateRecord
        in it must be followed by update_record()/mutate_state(), which is what
        tells save_state() to re-serialize that record.
        """
        stamp = self._disk_stamp()
        if self._cached_state is not None and stamp == self._cached_stamp:
            return self._cached_state
        if stamp is None:
            state: dict[str, StateRecord] = {}
        else:
            raw = json.loads(self.state_file.read_text(encoding="utf-8") or "{}")
            state = {cid: StateRecord(**record) for cid, record in raw.get("items", {}).items()}
        self._record_json.clear()
        self._cached_state = state
        self._cached_stamp = stamp
        return state

    def save_state(self, state: dict[str, StateRecord]) -> None:
        """Atomically rewrite state.json, re-serializing only records that
        actually changed since the last write.

        Byte-identical to the previous `json.dumps(payload, indent=2,
        sort_keys=True)` output - each record is rendered at indent=2 and
        re-indented by one level - so the on-disk format is unchanged; only the
        cost is. The file itself is still rewritten whole (it is a single JSON
        document), but that's a memcpy of cached text rather than an
        O(items) pydantic dump on every stage transition.
        """
        cached = self._cached_state
        parts: list[str] = []
        for cid, record in sorted(state.items()):
            if cached is None or cached.get(cid) is not record:
                # A record object we've never serialized, or one swapped in
                # under an existing content_id - can't trust the cached text.
                self._record_json.pop(cid, None)
            text = self._record_json.get(cid)
            if text is None:
                text = json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True)
                self._record_json[cid] = text
            parts.append(f"    {json.dumps(cid)}: {indent(text, '    ').lstrip()}")
        body = ",\n".join(parts)
        payload = '{\n  "items": {' + (f"\n{body}\n  " if body else "") + "}\n}"
        tmp_path = self.state_file.with_suffix(".json.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self.state_file)
        self._cached_state = state
        self._cached_stamp = self._disk_stamp()
        for stale in set(self._record_json) - set(state):
            del self._record_json[stale]

    def _locked_mutate(self, mutate_fn: Callable[[dict[str, StateRecord]], _T]) -> _T:
        """Acquire the cross-process state.json lock briefly, load current state,
        call mutate_fn(state) to modify it in place, save, and return mutate_fn's
        result. Every state.json mutation (update_record, add_url,
        sync_queue_file_into_state) goes through this, so two processes'
        mutations serialize on the smallest possible critical section instead of
        one holding the lock for an entire run_once() pass - see worker.py's
        run_once() docstring for the corruption this prevents, and the design
        doc's "Locking model" section for why this is scoped this tightly.
        """
        lock_path = self.state_file.with_suffix(".lock")
        try:
            with FileLock(str(lock_path), timeout=5):
                state = self.load_state()
                result = mutate_fn(state)
                self.save_state(state)
                return result
        except FileLockTimeout as exc:
            raise StateLockTimeout(
                f"Could not acquire the state.json lock ({lock_path}) within 5s - "
                "another process appears to be stuck mid-mutation."
            ) from exc

    def mutate_state(self, mutate_fn: Callable[[dict[str, StateRecord]], _T]) -> _T:
        """Public entry point for callers (e.g. vault_organizer) that need to
        read-modify-write multiple records under the same state.json lock
        worker.py's own mutations use, rather than one record at a time via
        update_record().
        """

        def mutate(state: dict[str, StateRecord]) -> _T:
            result = mutate_fn(state)
            # mutate_fn can touch any record in place, and we can't tell which -
            # drop every cached serialization rather than risk writing stale
            # text. Rare path (vault_organizer), so the full re-dump is fine.
            self._record_json.clear()
            return result

        return self._locked_mutate(mutate)

    def update_record(self, record: StateRecord) -> None:
        def mutate(state: dict[str, StateRecord]) -> None:
            record.updated_at = datetime.now(UTC)
            state[record.content_id] = record
            self._record_json.pop(record.content_id, None)

        self._locked_mutate(mutate)

    # -- needs-attention.txt ------------------------------------------------------

    def append_needs_attention(self, url: str, reason: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with self.needs_attention_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{timestamp}\t{url}\t{reason}\n")

    def prune_needs_attention(self, retention_days: int) -> int:
        """Drop lines older than retention_days. Returns the number of lines removed.

        A retention_days <= 0 disables pruning entirely (treated as "keep forever").
        Malformed lines (missing/unparseable leading timestamp) are always kept,
        since we can't judge their age.
        """
        if retention_days <= 0:
            return 0
        lines = self.needs_attention_file.read_text(encoding="utf-8").splitlines()
        cutoff = datetime.now(UTC).timestamp() - retention_days * 86400
        kept: list[str] = []
        removed = 0
        for line in lines:
            timestamp_str = line.split("\t", 1)[0] if line else ""
            try:
                age = datetime.fromisoformat(timestamp_str).timestamp()
            except ValueError:
                kept.append(line)
                continue
            if age >= cutoff:
                kept.append(line)
            else:
                removed += 1
        if removed:
            remaining = "\n".join(kept) + ("\n" if kept else "")
            self.needs_attention_file.write_text(remaining, encoding="utf-8")
        return removed

    # -- ingestion ----------------------------------------------------------------

    def add_url(self, url: str, source: QueueSource) -> StateRecord:
        """Validate and register a single URL directly into state.json (webhook path)."""

        def mutate(state: dict[str, StateRecord]) -> StateRecord:
            return self._register(url, source, state)

        return self._locked_mutate(mutate)

    def _register(
        self, url: str, source: QueueSource, state: dict[str, StateRecord]
    ) -> StateRecord:
        result = validate_url(url, self.settings)
        now = datetime.now(UTC)

        # validate_url() only knows about download.allowed_domains - a URL it
        # rejects for "not in the configured allow-list" may still be a valid
        # text-capture URL, so re-check via classify_url_kind() before treating
        # it as genuinely blocked.
        if not result.ok and not result.blocked:
            kind = classify_url_kind(url, self.settings)
            if kind == "text":
                content_id = result.content_id
                existing = state.get(content_id)
                if existing is not None:
                    return existing
                record = StateRecord(
                    content_id=content_id,
                    url=url,
                    normalized_url=result.normalized_url,
                    source=source,
                    status=ItemStatus.PENDING,
                    content_kind="text",
                    added_at=now,
                    updated_at=now,
                )
                state[content_id] = record
                return record

        if not result.ok:
            content_id = result.content_id or url
            existing = state.get(content_id)
            if existing is None:
                record = StateRecord(
                    content_id=content_id,
                    url=url,
                    normalized_url=result.normalized_url,
                    source=source,
                    status=ItemStatus.BLOCKED,
                    added_at=now,
                    updated_at=now,
                    error=result.reason,
                )
                state[content_id] = record
                self.append_needs_attention(url, result.reason or "validation failed")
                return record
            return existing

        existing = state.get(result.content_id)
        if existing is not None:
            return existing

        # Every download.allowed_domains host is unambiguously "media" except
        # drive.google.com, which hosts both video files and arbitrary shared
        # documents - classify_url_kind() runs a cheap yt-dlp probe for that
        # one host to tell them apart; every other host returns "media"
        # immediately with no network call, same as hardcoding it here would.
        content_kind = classify_url_kind(url, self.settings) or "media"
        record = StateRecord(
            content_id=result.content_id,
            url=url,
            normalized_url=result.normalized_url,
            source=source,
            status=ItemStatus.PENDING,
            content_kind=content_kind,
            added_at=now,
            updated_at=now,
        )
        state[result.content_id] = record
        return record

    def sync_queue_file_into_state(self) -> list[StateRecord]:
        """Drain queue.txt into state.json, returning newly-registered records."""
        lines = self.queue_file.read_text(encoding="utf-8").splitlines()
        newly_registered: list[StateRecord] = []
        kept_lines: list[str] = []

        def mutate(state: dict[str, StateRecord]) -> None:
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    kept_lines.append(line)
                    continue
                before = set(state.keys())
                record = self._register(stripped, QueueSource.QUEUE_FILE, state)
                if record.content_id not in before:
                    newly_registered.append(record)
                # Line is consumed either way: state.json (or needs-attention.txt
                # for blocked/invalid URLs) is now the durable record of this URL.

        self._locked_mutate(mutate)
        remaining = "\n".join(kept_lines) + ("\n" if kept_lines else "")
        self.queue_file.write_text(remaining, encoding="utf-8")
        return newly_registered

    def reset_for_retry(
        self, content_id: str | None = None, all_failed_permanent: bool = False
    ) -> list[str]:
        """Reset FAILED_PERMANENT record(s) back to PENDING with a clean retry
        slate (attempt_count/next_retry_at/error cleared) so the next
        run_once() picks them up via get_actionable_items(). Deliberately only
        touches records currently in FAILED_PERMANENT status - this is a
        give-up recovery mechanism, not a generic force-reprocess, so a
        content_id that's DONE, still FAILED (mid-backoff), etc. is left
        untouched. `last_completed_stage` is preserved so a retry still skips
        already-completed stages per the checkpoint/resume logic in worker.py.
        """

        def mutate(state: dict[str, StateRecord]) -> list[str]:
            if all_failed_permanent:
                targets = [
                    record
                    for record in state.values()
                    if record.status is ItemStatus.FAILED_PERMANENT
                ]
            elif content_id is not None:
                record = state.get(content_id)
                targets = (
                    [record]
                    if record is not None and record.status is ItemStatus.FAILED_PERMANENT
                    else []
                )
            else:
                targets = []
            now = datetime.now(UTC)
            for target in targets:
                target.status = ItemStatus.PENDING
                target.attempt_count = 0
                target.next_retry_at = None
                target.error = None
                target.updated_at = now
                self._record_json.pop(target.content_id, None)
            return [target.content_id for target in targets]

        return self._locked_mutate(mutate)

    def get_actionable_items(self) -> list[StateRecord]:
        """All records not yet in a terminal status - includes crash-interrupted
        items, and FAILED items whose retry backoff has elapsed. Excludes
        DONE/BLOCKED/FAILED_PERMANENT (all terminal) and FAILED items still
        waiting out their next_retry_at.
        """
        state = self.load_state()
        now = datetime.now(UTC)
        return [
            record
            for record in state.values()
            if record.status
            not in (ItemStatus.DONE, ItemStatus.BLOCKED, ItemStatus.FAILED_PERMANENT)
            and (record.next_retry_at is None or record.next_retry_at <= now)
        ]
