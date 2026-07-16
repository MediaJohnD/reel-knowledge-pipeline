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
from datetime import UTC, datetime

from reel_pipeline.config import Settings
from reel_pipeline.models import ItemStatus, QueueSource, StateRecord
from reel_pipeline.validators import classify_url_kind, validate_url


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

    # -- state.json persistence -------------------------------------------------

    def load_state(self) -> dict[str, StateRecord]:
        if not self.state_file.exists():
            return {}
        raw = json.loads(self.state_file.read_text(encoding="utf-8") or "{}")
        return {cid: StateRecord(**record) for cid, record in raw.get("items", {}).items()}

    def save_state(self, state: dict[str, StateRecord]) -> None:
        items = {cid: json.loads(record.model_dump_json()) for cid, record in state.items()}
        payload = {"items": items}
        tmp_path = self.state_file.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.state_file)

    def update_record(self, record: StateRecord) -> None:
        state = self.load_state()
        record.updated_at = datetime.now(UTC)
        state[record.content_id] = record
        self.save_state(state)

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
        state = self.load_state()
        record = self._register(url, source, state)
        self.save_state(state)
        return record

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

        record = StateRecord(
            content_id=result.content_id,
            url=url,
            normalized_url=result.normalized_url,
            source=source,
            status=ItemStatus.PENDING,
            content_kind="media",
            added_at=now,
            updated_at=now,
        )
        state[result.content_id] = record
        return record

    def sync_queue_file_into_state(self) -> list[StateRecord]:
        """Drain queue.txt into state.json, returning newly-registered records."""
        lines = self.queue_file.read_text(encoding="utf-8").splitlines()
        state = self.load_state()
        newly_registered: list[StateRecord] = []
        kept_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                kept_lines.append(line)
                continue
            before = set(state.keys())
            record = self._register(stripped, QueueSource.QUEUE_FILE, state)
            if record.content_id not in before:
                newly_registered.append(record)
            # Line is consumed either way: state.json (or needs-attention.txt for
            # blocked/invalid URLs) is now the durable record of this URL.

        self.save_state(state)
        remaining = "\n".join(kept_lines) + ("\n" if kept_lines else "")
        self.queue_file.write_text(remaining, encoding="utf-8")
        return newly_registered

    def get_actionable_items(self) -> list[StateRecord]:
        """All records not yet in a terminal status - includes crash-interrupted items."""
        state = self.load_state()
        return [
            record
            for record in state.values()
            if record.status not in (ItemStatus.DONE, ItemStatus.BLOCKED)
        ]
