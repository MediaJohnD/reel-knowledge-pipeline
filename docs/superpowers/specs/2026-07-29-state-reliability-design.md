# State.json reliability rework — design

Date: 2026-07-29
Status: proposed (pending user review)

## Problem

A code audit (see conversation history / `git log` around this date) surfaced six
findings that all trace back to one root cause: `StateRecord` and the worker's
processing loop have no concept of "how many times has this been attempted" or
"which stage actually finished," and `state.json` writes are not consistently
guarded by the cross-process lock.

1. `queue_manager.add_url()` (webhook path) does an unlocked load-modify-save,
   so a webhook arriving mid-`run_once()` can clobber state written during that
   pass.
2. A race in `webhook_server.py` around `_rerun_requested` can leave an event
   set with no worker left to consume it.
3. `get_actionable_items()` treats every non-terminal status (including
   `FAILED`) as retryable forever — a permanently broken URL gets fully
   re-downloaded/re-transcribed/re-enriched on every single `run_once()`.
4. `process_item()` does not branch on the record's current status at entry —
   a crash after `TRANSCRIBING` still re-runs `download()` on the next pass,
   discarding already-completed (and possibly paid) work.
5. A `skill_writer.generate()` failure marks an already-successfully-written
   note as `FAILED`, causing the *entire* pipeline (download → transcribe →
   enrich → write note) to re-run on the next pass for something that already
   succeeded.
6. `downloader.py` retries can reuse the same temp directory across attempts,
   risking picking up a stale file left by an earlier failed attempt.

(A seventh finding — `state.json` being fully reparsed and rewritten on every
stage transition, making a backlog pass O(n²) in state I/O — is addressed below
under "Deferred," not fixed.)

## Goals

- A permanently-failing item stops burning API/download calls after a bounded
  number of attempts, without silently dropping off the user's radar.
- A crash or restart resumes from the last *durably completed* stage instead
  of redoing already-finished work.
- Webhook registration and `run_once()` can interleave safely without either
  blocking the other for the length of a full backlog pass.
- An optional-stage failure (skill generation) never discards an
  already-successful required stage (note writing).
- Every change ships with a regression test per the repo's
  `validating-release-readiness` skill.

## Non-goals

- Migrating `state.json` off plain JSON (SQLite/WAL was researched and
  rejected for this project's scale — see "Deferred").
- Fixing the O(n²) full-file-rewrite pattern (`#8` above) — real in
  complexity-theory terms, not worth it at "a handful of reels" volume, and a
  batched-write fix would reintroduce the staleness risk the locking change
  below removes. Revisit only if a real backlog-size performance complaint
  shows up.
- Full artifact caching (persisting transcript/enrichment text so a resume
  never redoes work even after `data/tmp/` is cleared) — checkpoint-only
  resumability (below) covers the common case at much less schema/complexity
  cost.
- Fixing `webhook_server.py`'s `_rerun_requested` race (#2) as its own item —
  it's actually resolved as a side effect of the locking change below, since
  the underlying problem is "no shared, short-held lock around state
  mutations"; no separate fix needed.

## Schema changes (`models.py`)

Add to `StateRecord`:

```python
attempt_count: int = 0
next_retry_at: datetime | None = None
last_completed_stage: ItemStage | None = None
skill_error: str | None = None
```

(`skill_error` is set only by the skill-generation decoupling change below —
independent of the required-stage `error` field, which continues to describe
a failure of the required pipeline: download/transcribe/enrich/write.)

New `ItemStage` enum (distinct from `ItemStatus`, which describes what's
*currently* happening; `ItemStage` records what has *durably finished*):

```python
class ItemStage(StrEnum):
    DOWNLOADED = "downloaded"
    TRANSCRIBED = "transcribed"
    ENRICHED = "enriched"
    NOTE_WRITTEN = "note_written"
```

New terminal `ItemStatus`:

```python
FAILED_PERMANENT = "failed_permanent"
```

`get_actionable_items()` excludes `DONE`, `BLOCKED`, and `FAILED_PERMANENT`.
`FAILED` remains actionable but is now retried on a schedule (below), not
every single pass.

All three new `StateRecord` fields get defaults, so existing `state.json`
records deserialize unchanged — no migration script needed.

## Retry / backoff policy (`queue_manager.py`, `worker.py`)

- Config (new `RetryConfig` in `config.py`, mirroring the existing
  `MaintenanceConfig` pattern): `max_attempts: int = 5`,
  `backoff_schedule_minutes: list[int] = [1, 5, 30, 120, 480]`.
- On each `FAILED` outcome in `process_item()`: increment `attempt_count`,
  set `next_retry_at = now + backoff_schedule_minutes[min(attempt_count, len(schedule)) - 1]`
  minutes. If `attempt_count >= max_attempts`, set status `FAILED_PERMANENT`
  instead of `FAILED` (still logs to `needs-attention.txt` once, on the
  transition, same as today's "only log when the error changes" rule).
- `get_actionable_items()` additionally filters out `FAILED` records whose
  `next_retry_at` is still in the future.
- A successful stage completion resets `attempt_count` to 0 (a transient
  failure followed by success shouldn't count against a later, unrelated
  failure).

## Locking model (`queue_manager.py`, `worker.py`)

Reuse the existing `FileLock` file/class — no new lock primitive. Change what
it wraps:

- `run_once()` stops holding the lock for the entire backlog pass. Instead,
  every individual state mutation — `update_record()`, `add_url()`,
  `sync_queue_file_into_state()`'s final save — acquires the same
  `state.json.lock` for just the duration of its own load-modify-save, with a
  short timeout (5s; these are always fast local-disk operations, unlike the
  full backlog pass).
- The expensive work in `process_item()` (download/transcribe/enrich/write)
  happens with no lock held at all — only the before/after `update_record()`
  calls bracketing each stage take the lock, briefly.
- This is a deliberate minimize-critical-section change: `filelock`'s own
  docs flag long-held locks as needing special handling (`heartbeat_interval`,
  `stale_threshold`, `lifetime`) specifically because they're a known risk
  area in crash-prone, multi-process scenarios — exactly what the current
  600s-held lock is exposed to.

## Resumability / checkpointing (`worker.py`)

`process_item()` starts by checking `record.last_completed_stage` and the
corresponding artifact under `data/tmp/<content_id>/`:

- If `last_completed_stage` is `DOWNLOADED` (or later) and the downloaded
  media file(s) still exist on disk, skip straight to transcription/
  description using the existing files instead of re-downloading.
- Same pattern for `TRANSCRIBED` → skip to enrichment if a cached transcript
  artifact is present; `ENRICHED` → skip to note writing.
- If the expected artifact is missing (tmp dir was cleaned by
  `_sweep_stale_tmp_dirs()` or manually), fall back to redoing *only that one
  stage* — not the whole item — by treating it as if `last_completed_stage`
  were the one before it.
- `last_completed_stage` is set (and persisted via `update_record()`,
  lock-guarded per above) immediately after each stage succeeds, before
  moving to the next.

This is the standard checkpoint-recovery pattern for pipeline reliability
(store a durable marker of last-completed-step; on resume, verify the
step's output is actually present before trusting the marker).

## Skill generation decoupling (`worker.py`)

Split `process_item()`'s single try/except so that `write_note()` succeeding
is committed independently of `skill_writer.generate()`:

- After `write_note()` succeeds, immediately set `status = DONE`,
  `note_path`, `last_completed_stage = NOTE_WRITTEN`, and persist via
  `update_record()`.
- Call `skill_writer.generate()` in its own try/except *after* that commit.
  On failure, log it and record the error in a new `skill_error` field on
  `StateRecord` (does not change `status` away from `DONE`) — visible for
  debugging but never causes a re-run of the whole pipeline.
- A future `run_once()` can independently retry just skill generation for
  `DONE` records with a `skill_error` set and no `skill_path` — out of scope
  for this rework, but the schema (`skill_error` as its own field) leaves room
  for it without another migration.

## Downloader temp-dir isolation (`downloader.py`)

Each internal download attempt writes to
`data/tmp/<content_id>/.attempt-<n>/` instead of directly into
`data/tmp/<content_id>/`. Only the successful attempt's files are moved
(promoted) into the canonical `data/tmp/<content_id>/` path; failed attempts'
partial files are left in their own `.attempt-<n>/` dir for
`_sweep_stale_tmp_dirs()` to eventually reclaim, and are never picked up by a
subsequent attempt or by the resumability check above (which only looks at
the canonical path).

## Deferred (researched, explicitly not doing now)

- **SQLite/WAL migration** — the actual current best-practice answer for
  "avoid full-file rewrite, get atomic per-record updates, no custom
  lock needed" (confirmed via Context7/web research). Would fix findings
  #1/#2/#8 as one mechanism instead of three patches, using stdlib `sqlite3`
  (no new dependency). Rejected for now because it changes `state.json` from
  a human-readable/greppable file into a binary DB, contradicting
  `CLAUDE.md`'s current framing, and requires a one-time data migration —
  bigger than this project's "handful of reels" scale justifies today.
  Revisit if item volume grows enough that #8's O(n²) cost becomes real.
- **Full artifact caching** (persisting transcript/enrichment text in
  `state.json` itself) — checkpoint-only resumability above gets the same
  practical benefit (no redundant paid API calls on resume) at much lower
  schema/complexity cost, as long as `data/tmp/` isn't cleared between the
  crash and the resume.

## Testing plan

Per `validating-release-readiness`: each behavior change gets a test using
the existing fakes-satisfying-Protocols pattern (`tests/test_worker_flow.py`).
New tests needed:

- Retry/backoff: a fake that fails N times then succeeds; assert
  `attempt_count`/`next_retry_at` progression and the `FAILED_PERMANENT`
  transition at the configured max.
- Resumability: a fake downloader/transcriber where the test pre-populates
  `data/tmp/<content_id>/` and a `last_completed_stage`, then asserts the
  corresponding fake stage method is never called.
- Locking: a test acquiring the lock manually in one thread while
  `add_url()` runs in another, asserting it waits rather than corrupting
  state (adapting the existing lock test pattern referenced in the recent
  "add cross-process file lock" commit).
- Skill decoupling: a fake `skill_writer` that raises; assert `status` is
  still `DONE` with `note_path` set and `skill_error` populated.
- Downloader isolation: assert a second attempt's directory is fresh (no
  leftover file from a first, failed, mocked attempt).

Full `make check` (lint/typecheck/test/audit) plus a manual `run-once`
verification per the repo's standard completion checklist.

## Open question surfaced during design, resolved

Whether to fix the O(n²) full-file-rewrite (#8) via SQLite migration was
raised explicitly and researched (Context7 `filelock` docs + web search on
embedded local state stores) before being deferred — see "Deferred" above.
