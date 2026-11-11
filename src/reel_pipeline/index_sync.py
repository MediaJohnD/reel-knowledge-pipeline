"""Ask a local semantic-search index (Khoj) to pick up newly written notes.

Without this, a note written after the index's scheduled run (daily at 02:00)
can't be found by meaning until the next day. The call is best-effort and never
raises: the notes are already saved, so a down index must not fail a run.

Safety: only loopback hosts are contacted, one call per run at most, and at
least ``min_interval_minutes`` apart (a state file records the last call), so
a burst of webhook batches can't pile reindex work onto a busy machine.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

import httpx

from reel_pipeline.config import Settings
from reel_pipeline.logging_setup import log_context

logger = logging.getLogger(__name__)

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def notify_index(settings: Settings, notes_written: int, *, now: float | None = None) -> bool:
    """Returns True if the index was asked to update. Never raises."""
    cfg = settings.search_index_sync
    if not cfg.enabled or notes_written <= 0 or not cfg.update_url:
        return False
    host = urlparse(cfg.update_url).hostname or ""
    if host not in _LOOPBACK:
        log_context(logger, 30, "index sync skipped: non-loopback url", host=host)
        return False
    now = time.time() if now is None else now
    stamp = settings.state_file.with_name(".index_sync.json")
    try:
        last = float(json.loads(stamp.read_text(encoding="utf-8")).get("last_call", 0))
    except (OSError, ValueError):
        last = 0.0
    if now - last < cfg.min_interval_minutes * 60:
        log_context(logger, 20, "index sync throttled", notes=notes_written)
        return False
    try:
        # trust_env=False: ignore proxy variables, this is a loopback call.
        with httpx.Client(timeout=cfg.timeout_seconds, trust_env=False) as client:
            client.get(cfg.update_url).raise_for_status()
        stamp.write_text(json.dumps({"last_call": now}), encoding="utf-8")
        log_context(logger, 20, "index sync requested", notes=notes_written)
        return True
    except (httpx.HTTPError, OSError) as exc:
        log_context(logger, 30, "index sync failed", error=str(exc) or repr(exc))
        return False
