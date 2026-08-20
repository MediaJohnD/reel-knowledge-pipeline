# State.json Reliability Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the pipeline's `state.json`-backed processing loop resilient to permanent failures, crashes mid-item, and concurrent webhook/CLI access, without changing the storage format.

**Architecture:** Extend `StateRecord` with attempt-tracking and checkpoint fields; shrink the existing cross-process `FileLock` to wrap each individual state mutation instead of an entire `run_once()` pass; add exponential backoff with a new terminal status; make `process_item()` skip stages whose output is still on disk; decouple optional skill generation from the required note-writing stage; make the downloader idempotent across retries.

**Tech Stack:** Python 3.12, pydantic v2, `filelock` (already a dependency), pytest.

## Global Constraints

- Every behavior change ships with a regression test using this repo's existing fakes-satisfying-Protocols pattern (see `tests/test_worker_flow.py`) — no real network/API calls in tests.
- New `StateRecord` fields must all have defaults so existing `state.json` files on disk deserialize unchanged — no migration script.
- Run `uv run ruff check .`, `uv run pyright`, and `uv run pytest -q` after every task; all three must be clean before moving to the next task.
- No new dependencies — `filelock` and stdlib `datetime`/`enum` cover everything.
- Design source of truth: `docs/superpowers/specs/2026-07-29-state-reliability-design.md`.

---

### Task 1: Schema — new StateRecord fields, ItemStage enum, FAILED_PERMANENT status

**Files:**
- Modify: `src/reel_pipeline/models.py:12-45`
- Test: `tests/test_queue_manager.py`

**Interfaces:**
- Produces: `ItemStage` enum (`DOWNLOADED`, `TRANSCRIBED`, `ENRICHED`, `NOTE_WRITTEN`), `ItemStatus.FAILED_PERMANENT`, and four new `StateRecord` fields — `attempt_count: int`, `next_retry_at: datetime | None`, `last_completed_stage: ItemStage | None`, `skill_error: str | None` — all consumed by Tasks 3–6.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_manager.py` (near the other `StateRecord`-construction tests, after `test_get_actionable_items_excludes_done_and_blocked_but_includes_crash_interrupted`):

```python
def test_state_record_new_reliability_fields_have_safe_defaults(tmp_path):
    """Regression test: existing state.json files on disk predate attempt_count/
    next_retry_at/last_completed_stage/skill_error - they must deserialize with
    safe defaults, not raise a validation error.
    """
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.state_file.write_text(
        '{"items": {"legacy1": {'
        '"content_id": "legacy1", "url": "https://youtube.com/1", '
        '"normalized_url": "https://youtube.com/1", "source": "queue_file", '
        '"status": "pending", "added_at": "2026-01-01T00:00:00+00:00", '
        '"updated_at": "2026-01-01T00:00:00+00:00"}}}',
        encoding="utf-8",
    )

    state = qm.load_state()

    record = state["legacy1"]
    assert record.attempt_count == 0
    assert record.next_retry_at is None
    assert record.last_completed_stage is None
    assert record.skill_error is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_manager.py::test_state_record_new_reliability_fields_have_safe_defaults -v`
Expected: FAIL with `pydantic_core._pydantic_core.ValidationError` — no, actually with `AttributeError: 'StateRecord' object has no attribute 'attempt_count'` since the field doesn't exist yet.

- [ ] **Step 3: Add ItemStage enum and FAILED_PERMANENT status**

In `src/reel_pipeline/models.py`, replace lines 12-22:

```python
class ItemStatus(StrEnum):
    """Lifecycle status of a queued content item, persisted in state.json."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    ENRICHING = "enriching"
    WRITING_NOTE = "writing_note"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
```

with:

```python
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
```

- [ ] **Step 4: Add the four new fields to StateRecord**

In `src/reel_pipeline/models.py`, replace the `StateRecord` class body (currently lines 32-45):

```python
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
```

with:

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_queue_manager.py::test_state_record_new_reliability_fields_have_safe_defaults -v`
Expected: PASS

- [ ] **Step 6: Run full verification**

Run: `uv run ruff check . && uv run pyright && uv run pytest -q`
Expected: all clean, 121 passed (120 existing + 1 new)

- [ ] **Step 7: Commit**

```bash
git add src/reel_pipeline/models.py tests/test_queue_manager.py
git commit -m "feat: add attempt-tracking and checkpoint fields to StateRecord"
```

---

### Task 2: RetryConfig

**Files:**
- Modify: `src/reel_pipeline/config.py:102-108` (add class near `MaintenanceConfig`), `:110-127` (register on `Settings`), `:326-348` (wire into `load_settings`)
- Modify: `config/settings.yaml` (document the new section, matching the `maintenance:` section's style)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.retry.backoff_schedule_minutes: list[int]`, `Settings.retry.max_attempts: int` — consumed by Task 4.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (it already imports `DEFAULT_SETTINGS_PATH, load_settings` from `reel_pipeline.config` — no new imports needed):

```python
def test_loads_retry_defaults_from_settings_yaml():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    assert settings.retry.max_attempts == 5
    assert settings.retry.backoff_schedule_minutes == [1, 5, 30, 120, 480]


def test_retry_config_overridable_from_yaml(tmp_path):
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "retry:\n  max_attempts: 3\n  backoff_schedule_minutes: [2, 10]\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=config_path, env={})

    assert settings.retry.max_attempts == 3
    assert settings.retry.backoff_schedule_minutes == [2, 10]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -k retry_config -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'retry'`

- [ ] **Step 3: Add RetryConfig class**

In `src/reel_pipeline/config.py`, after the `MaintenanceConfig` class (currently lines 102-107), add:

```python
class RetryConfig(BaseModel):
    # Exponential backoff in minutes, indexed by (attempt_count - 1); clamped to
    # the last entry once attempt_count exceeds the schedule's length (only
    # possible if max_attempts > len(backoff_schedule_minutes)).
    backoff_schedule_minutes: list[int] = Field(default_factory=lambda: [1, 5, 30, 120, 480])
    # After this many failed attempts, an item's status becomes FAILED_PERMANENT
    # instead of FAILED, so run_once() stops retrying it automatically.
    max_attempts: int = 5
```

- [ ] **Step 4: Register on Settings and wire into load_settings**

In `src/reel_pipeline/config.py`, in the `Settings` class, add after the `maintenance` field (currently line 127):

```python
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
```

In `load_settings()`, add after the `maintenance=MaintenanceConfig(...)` line (currently line 338):

```python
maintenance = (MaintenanceConfig(**raw.get("maintenance", {})),)
retry = (RetryConfig(**raw.get("retry", {})),)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -k retry_config -v`
Expected: PASS

- [ ] **Step 6: Document the section in settings.yaml**

In `config/settings.yaml`, after the `maintenance:` section at the end of the file, add:

```yaml

# Retry policy for items that fail processing (download/transcribe/enrich/write
# errors - not validation/blocked-domain rejections, which never retry).
# Exponential backoff: after each failure, the item waits backoff_schedule_minutes[N]
# minutes before the next run_once() will pick it up again. After max_attempts
# failures, the item's status becomes failed_permanent and stops being retried
# automatically - it's still visible in state.json/needs-attention.txt for
# manual review.
retry:
  backoff_schedule_minutes: [1, 5, 30, 120, 480]
  max_attempts: 5
```

- [ ] **Step 7: Run full verification**

Run: `uv run ruff check . && uv run pyright && uv run pytest -q`
Expected: all clean, 123 passed (121 + 2 new)

- [ ] **Step 8: Commit**

```bash
git add src/reel_pipeline/config.py config/settings.yaml tests/test_config.py
git commit -m "feat: add configurable retry/backoff policy"
```

---

### Task 3: Locking — a fine-grained per-mutation lock alongside run_once()'s own serialization lock

> **Design correction (post pre-implementation attempt):** the original version of
> this task removed `run_once()`'s lock entirely, relying only on a shared
> per-mutation lock. An implementer attempt found this allows two concurrent
> `run_once()` calls to double-process the same item: there is no way to
> distinguish "another live process is actively working this right now" from
> "a prior process crashed mid-item and this is legitimately resumable,"
> because that ambiguity never existed under the old design (a single
> whole-pass lock made concurrent `run_once()` calls physically impossible).
> The corrected design below uses **two separate lock files**: a fine-grained
> per-mutation lock (this task's original goal — `add_url()` never blocks on
> it for long) and a second, distinct, coarse lock that only `run_once()`
> itself holds for its entire pass (restoring the no-double-processing
> guarantee). Confirmed with the user before re-implementing.

**Files:**
- Modify: `src/reel_pipeline/queue_manager.py:1-60` (imports, `save_state`/`update_record`), `:100-105` (`add_url`), `:173-195` (`sync_queue_file_into_state`)
- Modify: `src/reel_pipeline/worker.py:284-303` (only the `lock_path` line and docstrings in `run_once()` change — the `run_once()`/`_run_once_locked()` split and `RunOnceLockError` are kept, not removed)
- Test: `tests/test_queue_manager.py` (new lock-contention test)

**Interfaces:**
- Produces: `QueueManager._locked_mutate(mutate_fn)` (private helper, uses lock file `state.lock`), `StateLockTimeout` exception. `worker.py`'s existing `RunOnceLockError` and its lock (now at `state.run_once.lock`, a distinct file) are unchanged in purpose, just documented more precisely.
- Consumes: nothing new from earlier tasks.

**Why this shape:** `filelock`'s own docs flag long-held locks as needing special handling (`heartbeat_interval`/`stale_threshold`/`lifetime` params exist specifically for that risk) — confirmed via Context7 during design. The *mutation-safety* problem (a webhook's `add_url()` doing an unlocked load-modify-save that a concurrent `run_once()` can clobber) is fixed by giving every state.json mutation its own brief, separate lock (`state.lock`). The *no-double-processing* problem is a distinct concern with a distinct, already-correct mechanism: `run_once()`'s own whole-pass lock, which must stay — it just needs to live on a different lock file (`state.run_once.lock`) than the new fine-grained one, so `run_once()` holding its own lock while internally calling `update_record()` (which briefly acquires the *other* lock file) never self-deadlocks.

**Note:** because `run_once()` keeps its own whole-pass lock, two concurrent `run_once()` calls still cannot overlap — the existing `test_concurrent_run_once_calls_never_interleave` in `tests/test_worker_flow.py` needs no changes and should be left exactly as-is (do not modify it in this task).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_manager.py`:

```python
def test_add_url_waits_for_a_concurrently_held_state_lock_instead_of_racing(tmp_path):
    """Regression test: add_url() used to do an unlocked load-modify-save, so a
    webhook arriving while another process held the state.json lock could read a
    stale snapshot and clobber that process's write. Now it must wait for the
    lock (briefly) instead of proceeding unguarded.
    """
    import threading
    import time

    from filelock import FileLock

    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.youtube.com/watch?v=held1\n", encoding="utf-8")
    qm.sync_queue_file_into_state()  # ensure state.json exists on disk

    lock_path = qm.state_file.with_suffix(".lock")
    held = threading.Event()
    release = threading.Event()

    def hold_lock():
        with FileLock(str(lock_path), timeout=5):
            held.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    held.wait(timeout=5)

    start = time.monotonic()
    release_soon = threading.Timer(0.3, release.set)
    release_soon.start()
    qm.add_url("https://www.youtube.com/watch?v=new1", source=QueueSource.WEBHOOK)
    elapsed = time.monotonic() - start

    holder.join(timeout=5)
    assert elapsed >= 0.25  # actually waited for the externally-held lock
    assert len(qm.load_state()) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_manager.py::test_add_url_waits_for_a_concurrently_held_state_lock_instead_of_racing -v`
Expected: FAIL — `add_url()` currently has no locking at all, so `elapsed` will be near-zero (it doesn't wait), failing the `elapsed >= 0.25` assertion.

- [ ] **Step 3: Add the lock-guarded mutation helper to QueueManager**

In `src/reel_pipeline/queue_manager.py`, add to the imports (currently lines 17-25):

```python
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Callable, TypeVar

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
```

Then, in the `QueueManager` class, replace `save_state` and `update_record` (currently lines 49-60) with:

```python
def save_state(self, state: dict[str, StateRecord]) -> None:
    items = {cid: json.loads(record.model_dump_json()) for cid, record in state.items()}
    payload = {"items": items}
    tmp_path = self.state_file.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, self.state_file)


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


def update_record(self, record: StateRecord) -> None:
    def mutate(state: dict[str, StateRecord]) -> None:
        record.updated_at = datetime.now(UTC)
        state[record.content_id] = record

    self._locked_mutate(mutate)
```

- [ ] **Step 4: Route add_url and sync_queue_file_into_state through _locked_mutate**

In `src/reel_pipeline/queue_manager.py`, replace `add_url` (currently lines 100-105):

```python
    def add_url(self, url: str, source: QueueSource) -> StateRecord:
        """Validate and register a single URL directly into state.json (webhook path)."""

        def mutate(state: dict[str, StateRecord]) -> StateRecord:
            return self._register(url, source, state)

        return self._locked_mutate(mutate)
```

Replace `sync_queue_file_into_state` (currently lines 173-195):

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_queue_manager.py::test_add_url_waits_for_a_concurrently_held_state_lock_instead_of_racing -v`
Expected: PASS

- [ ] **Step 6: Point run_once()'s existing lock at a separate lock file**

`worker.py`'s `run_once()` already has its own `FileLock`/`RunOnceLockError` mechanism, holding a lock for the entire pass — this stays, and does NOT need removing. The only change: its lock file must be different from the fine-grained one `_locked_mutate` now uses (`state.lock`), so that `run_once()` holding its own lock while internally calling `update_record()` (which briefly acquires the *other* lock file inside the pass) never self-deadlocks.

In `src/reel_pipeline/worker.py`, find `run_once()` (currently lines 284-303):

```python
    def run_once(self) -> RunSummary:
        """Acquires a cross-process lock on state.json before doing any work, so
        this run_once() can't interleave with another one in a different
        process (a concurrent CLI invocation, or the webhook server's own
        background run) - see this module's docstring for what goes wrong
        without it. Blocks up to 10 minutes for the lock (a full pass over a
        large backlog can legitimately take a while); past that, something is
        very likely stuck rather than just busy, so this raises instead of
        blocking forever.
        """
        lock_path = self.settings.state_file.with_suffix(".lock")
        try:
            with FileLock(str(lock_path), timeout=600):
                return self._run_once_locked()
        except FileLockTimeout as exc:
            raise RunOnceLockError(
                f"Could not acquire the state.json lock ({lock_path}) within 600s - "
                "another run_once() (this process, another CLI invocation, or the "
                "webhook server) appears to be stuck rather than just busy."
            ) from exc
```

Replace it with:

```python
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
```

Also update the module docstring's second paragraph (currently part of lines 1-18) to mention both locks - find:

```
run_once() is guarded by a cross-process file lock (see RunOnceLockError
below). webhook_server.py's threading.Lock only prevents overlapping
webhook-triggered runs *within that one long-lived process* - it does
nothing for a manually-invoked `run-once` CLI call racing against the
webhook server, or two manual CLI calls racing against each other. Without
this, concurrent run_once() calls read/mutate/write state.json independently
via QueueManager.load_state()/save_state(), and the loser's write silently
clobbers the winner's - observed in practice as a StateRecord field (e.g.
content_kind) reverting to its default value, or an item being processed
twice.
```

Replace with:

```
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
```

Do not touch `_run_once_locked()`'s body, the imports (`FileLock`/`FileLockTimeout` stay), or `RunOnceLockError` (it stays, unchanged) - only the two blocks shown above change.

- [ ] **Step 7: Run full verification**

Run: `uv run ruff check . && uv run pyright && uv run pytest -q`
Expected: all clean, 124 passed (123 + 1 new). `test_concurrent_run_once_calls_never_interleave` in `tests/test_worker_flow.py` must still pass UNCHANGED - two run_once() calls still cannot overlap, since run_once() keeps its own whole-pass lock (just on a different file now). Do not modify that test.

- [ ] **Step 8: Commit**

```bash
git add src/reel_pipeline/queue_manager.py src/reel_pipeline/worker.py tests/test_queue_manager.py
git commit -m "fix: add a per-mutation state.json lock separate from run_once()'s whole-pass lock"
```

---

### Task 4: Retry / backoff policy

**Files:**
- Modify: `src/reel_pipeline/queue_manager.py:197-205` (`get_actionable_items`)
- Modify: `src/reel_pipeline/worker.py` (the `except Exception` block in `process_item`)
- Test: `tests/test_queue_manager.py`, `tests/test_worker_flow.py`

**Interfaces:**
- Consumes: `Settings.retry.max_attempts`, `Settings.retry.backoff_schedule_minutes` (Task 2); `StateRecord.attempt_count`/`next_retry_at`/`ItemStatus.FAILED_PERMANENT` (Task 1).
- Produces: `get_actionable_items()` also excludes `FAILED_PERMANENT` and any `FAILED` record whose `next_retry_at` is still in the future — consumed by nothing further in this plan, but is the behavior Task 8's manual verification checks.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_queue_manager.py`:

```python
def test_get_actionable_items_excludes_failed_permanent(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)

    from reel_pipeline.models import StateRecord

    permanent = StateRecord(
        content_id="perm1",
        url="https://youtube.com/1",
        normalized_url="https://youtube.com/1",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED_PERMANENT,
        added_at=now,
        updated_at=now,
    )
    qm.save_state({"perm1": permanent})

    assert qm.get_actionable_items() == []


def test_get_actionable_items_excludes_failed_item_whose_backoff_has_not_elapsed(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)

    from reel_pipeline.models import StateRecord

    waiting = StateRecord(
        content_id="wait1",
        url="https://youtube.com/1",
        normalized_url="https://youtube.com/1",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED,
        added_at=now,
        updated_at=now,
        attempt_count=1,
        next_retry_at=now + timedelta(minutes=5),
    )
    ready = StateRecord(
        content_id="ready1",
        url="https://youtube.com/2",
        normalized_url="https://youtube.com/2",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED,
        added_at=now,
        updated_at=now,
        attempt_count=1,
        next_retry_at=now - timedelta(minutes=1),
    )
    qm.save_state({"wait1": waiting, "ready1": ready})

    actionable_ids = {r.content_id for r in qm.get_actionable_items()}
    assert actionable_ids == {"ready1"}
```

Add to `tests/test_worker_flow.py` (needs a fake that fails a configurable number of times):

```python
class FailNTimesThenSucceedDownloader:
    def __init__(self, fail_count: int):
        self.fail_count = fail_count
        self.calls = 0

    def download(self, url: str, content_id: str) -> DownloadResult:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise RuntimeError(f"simulated failure #{self.calls}")
        return DownloadResult(
            content_id=content_id,
            media_type=MediaType.VIDEO,
            media_paths=[f"/fake/{content_id}.mp3"],
            platform="youtube",
        )


def test_failed_item_gets_backoff_and_attempt_count_increment(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = build_pipeline(settings, FailingDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=backoff1\n", encoding="utf-8"
    )

    before = datetime.now(UTC)
    pipeline.run_once()

    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.FAILED
    assert record.attempt_count == 1
    assert record.next_retry_at is not None
    assert record.next_retry_at > before + timedelta(seconds=30)  # first backoff step is 1 minute


def test_item_becomes_failed_permanent_after_max_attempts(tmp_path):
    settings = make_settings(tmp_path)
    settings.retry.max_attempts = 2
    settings.retry.backoff_schedule_minutes = [0, 0]  # no actual waiting needed for this test
    pipeline = build_pipeline(settings, FailingDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=permfail1\n", encoding="utf-8"
    )

    pipeline.run_once()  # attempt 1 -> FAILED
    pipeline.run_once()  # attempt 2 -> FAILED_PERMANENT

    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.FAILED_PERMANENT
    assert record.attempt_count == 2

    third = pipeline.run_once()
    assert third.processed == 0  # get_actionable_items() no longer returns it


def test_successful_retry_resets_attempt_count(tmp_path):
    settings = make_settings(tmp_path)
    settings.retry.backoff_schedule_minutes = [0, 0, 0, 0, 0]
    downloader = FailNTimesThenSucceedDownloader(fail_count=1)
    pipeline = build_pipeline(settings, downloader)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=retry1\n", encoding="utf-8"
    )

    pipeline.run_once()  # fails once
    pipeline.run_once()  # succeeds (next_retry_at already elapsed at 0-minute backoff)

    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.DONE
    assert record.attempt_count == 0
```

Note: `Settings` fields are pydantic `BaseModel` fields — confirm in Step 2 whether `settings.retry.max_attempts = 2` requires `model_config = {"frozen": False}` (pydantic v2's default is mutable, so this should work as-is; if `pyright`/`pydantic` complain, construct a fresh `Settings(..., retry=RetryConfig(max_attempts=2, backoff_schedule_minutes=[0, 0]))` instead in Step 3's actual edit).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_manager.py tests/test_worker_flow.py -k "failed_permanent or backoff or attempt_count" -v`
Expected: FAIL — `get_actionable_items()` doesn't filter on these fields yet, and `process_item()` never sets `attempt_count`/`next_retry_at`.

- [ ] **Step 3: Update get_actionable_items**

In `src/reel_pipeline/queue_manager.py`, replace `get_actionable_items` (currently lines 197-205):

```python
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
            if record.status not in (ItemStatus.DONE, ItemStatus.BLOCKED, ItemStatus.FAILED_PERMANENT)
            and (record.next_retry_at is None or record.next_retry_at <= now)
        ]
```

- [ ] **Step 4: Update process_item's failure handling in worker.py**

In `src/reel_pipeline/worker.py`, replace the `except Exception as exc:` block (find it near the end of `process_item` — currently lines 175-188):

```python
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
            # Only log a fresh needs-attention line when this is a new failure (first
            # occurrence, or the error changed) - not on every identical retry, which
            # would otherwise grow needs-attention.txt by one line per run-once forever.
            if new_error != previous_error:
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
```

Add `timedelta` to the existing `from datetime import UTC, datetime` import line near the top of `worker.py`:

```python
from datetime import UTC, datetime, timedelta
```

Add a line resetting `attempt_count`/`next_retry_at` on success — in the success path, right after `record.error = None` (find this line a few lines above the try block's end, in the success branch, before `self._cleanup_tmp_dir(...)`):

```python
            record.error = None
            record.attempt_count = 0
            record.next_retry_at = None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_manager.py tests/test_worker_flow.py -k "failed_permanent or backoff or attempt_count" -v`
Expected: PASS

- [ ] **Step 6: Run full verification**

Run: `uv run ruff check . && uv run pyright && uv run pytest -q`
Expected: all clean

- [ ] **Step 7: Commit**

```bash
git add src/reel_pipeline/queue_manager.py src/reel_pipeline/worker.py tests/test_queue_manager.py tests/test_worker_flow.py
git commit -m "feat: exponential backoff and FAILED_PERMANENT terminal status for repeatedly-failing items"
```

---

### Task 5: Checkpoint resumability

**Files:**
- Modify: `src/reel_pipeline/worker.py` (`process_item`)
- Test: `tests/test_worker_flow.py`

**Interfaces:**
- Consumes: `StateRecord.last_completed_stage`, `ItemStage` (Task 1).
- Produces: `process_item()` sets `record.last_completed_stage` after each stage and skips re-running a stage whose output artifact is still present.

**Design note:** Only the media-download path is resumable at the artifact-check level in this task (checking `data/tmp/<content_id>/` for existing files before re-downloading) — this is the stage that's actually expensive to redo (network + disk I/O) and safe to verify via file existence. Transcription/enrichment results aren't cached to disk in this rework (see the design doc's "Deferred: full artifact caching"), so a resume after a crash mid-transcription still re-transcribes, but does *not* re-download if the media file is still there. This is the single highest-value fix from finding #4 at the lowest schema/complexity cost.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_worker_flow.py`:

```python
def test_resuming_a_crash_interrupted_item_does_not_redownload_if_media_still_on_disk(tmp_path):
    """Regression test: process_item() used to always call downloader.download()
    regardless of the record's last_completed_stage, so a crash after a
    successful download (e.g. during transcription) re-downloaded on the next
    run_once() - discarding already-completed, possibly-costly work.
    """
    settings = make_settings(tmp_path)
    downloader = FakeDownloaderWithRealTmpFile(settings)
    pipeline = build_pipeline(settings, downloader)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=resume1\n", encoding="utf-8"
    )
    pipeline.queue_manager.sync_queue_file_into_state()
    (record,) = pipeline.queue_manager.load_state().values()

    # Simulate a crash right after a successful download: the media file exists
    # on disk and last_completed_stage reflects it, but status never advanced
    # past DOWNLOADING (the crash happened before the next update_record() call).
    from reel_pipeline.models import ItemStage

    media_path = settings.tmp_dir / record.content_id / "audio.mp3"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"pre-existing audio from a completed download")
    record.status = ItemStatus.DOWNLOADING
    record.last_completed_stage = ItemStage.DOWNLOADED
    pipeline.queue_manager.update_record(record)

    class CountingDownloader:
        def __init__(self):
            self.calls = 0

        def download(self, url, content_id):
            self.calls += 1
            raise AssertionError("download() should not be called - media already on disk")

    pipeline.downloader = CountingDownloader()

    result = pipeline.process_item(record)

    assert result.status == ItemStatus.DONE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_flow.py::test_resuming_a_crash_interrupted_item_does_not_redownload_if_media_still_on_disk -v`
Expected: FAIL with `AssertionError: download() should not be called - media already on disk`

- [ ] **Step 3: Implement the resumability check**

In `src/reel_pipeline/worker.py`, this task modifies the start of the `else:` branch inside `process_item` (the media path — the `if record.content_kind == "text":` branch is untouched, since text capture has no equivalent large-artifact-to-skip). Find the current media branch:

```python
            else:
                record.status = ItemStatus.DOWNLOADING
                self.queue_manager.update_record(record)
                download_result = self.downloader.download(record.url, record.content_id)
                log_context(
                    logger, 20, "downloaded", content_id=record.content_id, url=record.url
                )

                record.status = ItemStatus.TRANSCRIBING
                self.queue_manager.update_record(record)
```

Replace it with:

```python
            else:
                if record.last_completed_stage in (
                    ItemStage.DOWNLOADED,
                    ItemStage.TRANSCRIBED,
                    ItemStage.ENRICHED,
                ) and self._cached_download_result(record.content_id) is not None:
                    download_result = self._cached_download_result(record.content_id)
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

                record.status = ItemStatus.TRANSCRIBING
                self.queue_manager.update_record(record)
```

Add the helper method `_cached_download_result` right after `process_item` (before `_transcribe_media_paths`):

```python
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
```

- [ ] **Step 4: Add ItemStage to worker.py's imports**

`worker.py`'s `else:` branch above now references `ItemStage.DOWNLOADED`/`TRANSCRIBED`/`ENRICHED`, but the module doesn't import `ItemStage` yet. In `src/reel_pipeline/worker.py`, replace the existing `from reel_pipeline.models import (...)` block:

```python
from reel_pipeline.models import (
    ContentItem,
    EnrichmentResult,
    ItemStatus,
    MediaType,
    StateRecord,
    TranscriptResult,
)
```

with:

```python
from reel_pipeline.models import (
    ContentItem,
    EnrichmentResult,
    ItemStage,
    ItemStatus,
    MediaType,
    StateRecord,
    TranscriptResult,
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_flow.py::test_resuming_a_crash_interrupted_item_does_not_redownload_if_media_still_on_disk -v`
Expected: PASS

- [ ] **Step 6: Run full verification**

Run: `uv run ruff check . && uv run pyright && uv run pytest -q`
Expected: all clean — pay attention to whether existing tests using `FakeDownloader` (which doesn't write real files to `settings.tmp_dir`) still pass; they should, since `_cached_download_result` returns `None` when the tmp dir doesn't exist on disk, falling through to a normal `download()` call exactly as before.

- [ ] **Step 7: Commit**

```bash
git add src/reel_pipeline/worker.py tests/test_worker_flow.py
git commit -m "feat: skip re-downloading media when a crash-interrupted item's file is still cached"
```

---

### Task 6: Decouple skill generation from note-writing success

**Files:**
- Modify: `src/reel_pipeline/worker.py` (`process_item`)
- Test: `tests/test_worker_flow.py`

**Interfaces:**
- Consumes: `StateRecord.skill_error` (Task 1).
- Produces: `write_note()` succeeding always results in `status = DONE`, regardless of `skill_writer.generate()`'s outcome.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_worker_flow.py`:

```python
def test_skill_generation_failure_does_not_undo_a_successful_note(tmp_path):
    """Regression test: a skill_writer.generate() exception used to be caught by
    the same try/except as the whole pipeline, marking an already-written note
    as FAILED - causing the entire pipeline to re-run on the next pass even
    though the note had already succeeded.
    """
    settings = make_settings(tmp_path)

    class FailingSkillWriter:
        def generate(self, item):
            raise RuntimeError("simulated skill generation failure")

    pipeline = WorkerPipeline(
        settings=settings,
        queue_manager=QueueManager(settings),
        downloader=FakeDownloader(),
        transcriber=FakeTranscriber(),
        image_describer=FakeImageDescriber(),
        text_fetcher=FakeTextFetcher(),
        enricher=FakeEnricher(),
        skill_writer=FailingSkillWriter(),
    )
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=skillfail1\n", encoding="utf-8"
    )

    summary = pipeline.run_once()

    assert summary.done == 1
    assert summary.failed == 0
    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.DONE
    assert record.note_path is not None
    assert Path(record.note_path).exists()
    assert record.skill_path is None
    assert record.skill_error is not None
    assert "simulated skill generation failure" in record.skill_error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_flow.py::test_skill_generation_failure_does_not_undo_a_successful_note -v`
Expected: FAIL — currently `summary.failed == 1` and `record.status == ItemStatus.FAILED`, since the exception propagates out of the whole try block.

- [ ] **Step 3: Split note-writing and skill-generation into separate try blocks**

In `src/reel_pipeline/worker.py`, find the section (inside `process_item`, after enrichment):

```python
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
            record.attempt_count = 0
            record.next_retry_at = None
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
```

Replace it with:

```python
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
```

(`ItemStage` is already imported in `worker.py` as of Task 5 — no import change needed here.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_flow.py::test_skill_generation_failure_does_not_undo_a_successful_note -v`
Expected: PASS

- [ ] **Step 5: Run full verification**

Run: `uv run ruff check . && uv run pyright && uv run pytest -q`
Expected: all clean — check specifically that `test_happy_path_produces_note_and_skill_and_marks_done` still passes (it asserts `record.skill_path == summary.note_paths[0]`-adjacent behavior on the success path, which is unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/reel_pipeline/worker.py tests/test_worker_flow.py
git commit -m "fix: skill generation failure no longer marks an already-written note as FAILED"
```

---

### Task 7: Downloader idempotency across retries

**Files:**
- Modify: `src/reel_pipeline/downloader.py:67-75` (`YtDlpDownloader.download`), `:157-161` (`GalleryDlDownloader.download`)
- Test: `tests/test_downloader.py`

**Interfaces:**
- No new interfaces — internal behavior change only (each `download()` call starts from a clean `out_dir`).

**Design note (deviates slightly from the design doc's wording):** The design doc described a per-attempt `.attempt-<n>/` subdirectory scheme, written before reading `downloader.py` in full. In practice there is no internal retry loop inside `download()` at all — the only way `download()` gets called twice for the same `content_id` is a cross-run retry (Task 5's resumability check already prevents this when the media is still cached; this task covers the case where it *isn't* cached, e.g. the previous attempt failed before producing a complete file). The simpler, equally-correct fix: clear `out_dir` before writing, every time `download()` is called. Since Task 5 already skips calling `download()` at all when a complete cached result exists, this only ever clears genuinely stale/partial data from a previous failed attempt.

- [ ] **Step 1: Write the failing test**

`tests/test_downloader.py` already has a `_install_fake_yt_dlp(monkeypatch, extract_info_impl=None)` helper (module-level, near the top of the file) that fakes the `yt_dlp` module via `monkeypatch.setitem(sys.modules, ...)` — reuse it rather than introducing a new mocking style. Add:

```python
def test_yt_dlp_downloader_clears_stale_files_from_a_previous_failed_attempt(tmp_path, monkeypatch):
    """Regression test: a retry after a failed download attempt used to write
    into the same out_dir without clearing it first, so a stale partial file
    from the earlier failed attempt could be picked up as if it were this
    attempt's output.
    """
    settings = Settings(project_root=tmp_path)
    out_dir = settings.tmp_dir / "content123"
    out_dir.mkdir(parents=True)
    stale_file = out_dir / "audio.wav"  # wrong extension - simulates a partial leftover
    stale_file.write_bytes(b"stale partial data from a failed attempt")

    def extract_info_impl(url):
        # Simulate yt-dlp producing this attempt's real output file.
        (out_dir / "audio.mp3").write_bytes(b"real audio from this attempt")
        return {"extractor_key": "Fake", "title": "Fake Title", "duration": 12.0}

    _install_fake_yt_dlp(monkeypatch, extract_info_impl=extract_info_impl)

    result = YtDlpDownloader(settings).download("https://www.youtube.com/watch?v=x", "content123")

    assert not stale_file.exists()
    assert result.media_paths == [str(out_dir / "audio.mp3")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_downloader.py -k stale_files -v`
Expected: FAIL — `stale_file.exists()` is still `True` since nothing clears `out_dir` today.

- [ ] **Step 3: Clear out_dir before writing in both downloaders**

In `src/reel_pipeline/downloader.py`, in `YtDlpDownloader.download`, replace:

```python
        out_dir = self.settings.tmp_dir / content_id
        out_dir.mkdir(parents=True, exist_ok=True)
```

with:

```python
        out_dir = self.settings.tmp_dir / content_id
        # Clear any stale/partial files from a previous failed attempt at this same
        # content_id - download() is only ever called again for an item whose
        # cached result Task 5's resumability check found incomplete or missing,
        # so anything already here is leftover junk, never data worth keeping.
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
```

Add `import shutil` to the top of `downloader.py` (alongside the existing `import subprocess`).

In `GalleryDlDownloader.download`, replace:

```python
        out_dir = self.settings.tmp_dir / content_id
        out_dir.mkdir(parents=True, exist_ok=True)
```

with the same pattern:

```python
        out_dir = self.settings.tmp_dir / content_id
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_downloader.py -k stale_files -v`
Expected: PASS

- [ ] **Step 5: Run full verification**

Run: `uv run ruff check . && uv run pyright && uv run pytest -q`
Expected: all clean

- [ ] **Step 6: Commit**

```bash
git add src/reel_pipeline/downloader.py tests/test_downloader.py
git commit -m "fix: downloader clears stale files from a previous failed attempt before writing"
```

---

### Task 8: Final integration verification

**Files:** none modified — verification only.

- [ ] **Step 1: Full check suite**

Run: `uv lock --check && uv run ruff check . && uv run pyright && uv run pytest -q && uv audit`
Expected: lockfile in sync (no `pyproject.toml` changes in this plan, so this should be a no-op check), ruff clean, pyright clean, all tests passing, no new vulnerabilities.

- [ ] **Step 2: Manual worker-flow verification**

Run: `uv run python -m reel_pipeline.cli run-once`
Expected: exits cleanly with a `processed=... done=... failed=...` summary line. If no items are queued, `processed=0` is expected and fine — this confirms the CLI entry point still wires together cleanly after the `worker.py`/`queue_manager.py` changes, not that real content was processed (that requires real credentials/network per this repo's `validating-release-readiness` skill notes).

- [ ] **Step 3: Re-read the diff for scope creep**

Run: `git log --oneline -8` and `git diff main --stat` (or the equivalent range covering this plan's commits) and confirm every changed file traces back to one of the seven findings this plan addresses. No unrelated files should appear.

- [ ] **Step 4: Report**

Summarize for the user: files changed per task, exact verification commands run and their pass/fail outcome, and explicitly restate the two items from the design doc that remain deferred (SQLite/WAL migration, full artifact caching) so they aren't mistaken for having been done.
