"""Re-capture Instagram carousel deep links saved before 817a059.

Until that commit the downloader added 1 to a link's 1-based img_index, so every
"?img_index=N" link saved earlier captured slide N+1 instead of the slide shared. The
note may describe the wrong slide. This re-runs those records, slowly, on the owner's
real account: at most MAX_PER_DAY posts per local day, and any failure halts it for
HALT_HOURS and puts the record back the way it was.

What a re-run must not lose:
  - a copy of the old note, in data/review/carousel-recapture-backup/
  - the note's folder, since write_note always writes to the vault_dir root
  - wikilinks to a note whose title (and so filename) changed
  - sections the pipeline does not write, such as "Gated content (DM)"

The old "Review (auto, ...)" section is dropped and the reviewed.json entry popped, since
it judged a different slide; the next review run re-reviews the note. Archived notes
(outside vault_dir) stay as they are.

Owner-run only, never the worker or webhook:
  uv run python scripts/recapture_carousels.py            list what is left
  uv run python scripts/recapture_carousels.py --apply    re-capture up to two posts
  uv run python scripts/recapture_carousels.py --self-check
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reel_pipeline.config import get_settings
from reel_pipeline.models import ItemStatus, StateRecord
from reel_pipeline.queue_manager import QueueManager
from reel_pipeline.validators import parse_img_index
from reel_pipeline.worker import build_worker

MAX_PER_DAY = 2
HALT_HOURS = 24
# 817a059 landed 2026-11-01; anything added since already captures the right slide.
FIXED_ON = datetime(2026, 11, 1, tzinfo=UTC)
# Sections write_note renders itself; every other section is someone else's to keep.
WRITER_SECTIONS = {"Summary", "Key Takeaways", "Tools Mentioned", "Skill Candidate", "Transcript"}
SECTION = re.compile(r"^## (.+)$", re.M)


def _vault_root(vault_dir: Path) -> Path:
    for parent in (vault_dir, *vault_dir.parents):
        if (parent / ".obsidian").exists():
            return parent
    return vault_dir


def _load_json(path: Path, default: dict) -> dict:
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def candidates(state: dict[str, StateRecord], ledger: dict, vault_dir: Path) -> list[StateRecord]:
    """Done, deep-linked, saved before the fix, note still live, not yet attempted. Oldest first."""
    out = []
    for record in state.values():
        if (
            record.status is ItemStatus.DONE
            and parse_img_index(record.url) is not None
            and record.added_at < FIXED_ON
            and record.note_path
            and Path(record.note_path).is_file()
            and Path(record.note_path).is_relative_to(vault_dir)
            and record.content_id not in ledger["items"]
        ):
            out.append(record)
    return sorted(out, key=lambda record: record.added_at)


def kept_sections(old_text: str) -> str:
    """Sections of the old note the pipeline did not write, Review excepted."""
    marks = list(SECTION.finditer(old_text))
    kept = []
    for index, mark in enumerate(marks):
        name = mark.group(1).strip()
        if name in WRITER_SECTIONS or name.startswith("Review (auto,"):
            continue
        end = marks[index + 1].start() if index + 1 < len(marks) else len(old_text)
        kept.append(old_text[mark.start() : end].strip())
    return "\n\n".join(kept)


def repair_links(root: Path, old_stem: str, new_stem: str) -> list[str]:
    """Point [[old]], [[old|label]], [[old#h]] and [[path/old]] at the new filename."""
    pattern = re.compile(r"\[\[((?:[^\]|#\n]*/)?)" + re.escape(old_stem) + r"(?=[\]|#])")
    changed = []
    for path in root.rglob("*.md"):
        if ".obsidian" in path.parts:
            continue
        raw = path.read_bytes()
        if old_stem.encode("utf-8") not in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        new = pattern.sub(lambda m: f"[[{m.group(1)}{new_stem}", text)
        if new != text:
            path.write_bytes(new.encode("utf-8"))
            changed.append(path.relative_to(root).as_posix())
    return changed


def recapture_one(settings, worker, queue, record: StateRecord, backup_dir: Path) -> dict:
    """Re-run one record. Returns its ledger entry; status "failed" means it was restored."""
    old_path = Path(record.note_path or "")
    old_text = old_path.read_bytes().decode("utf-8")
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / f"{record.content_id}.md").write_bytes(old_text.encode("utf-8"))
    saved = record.model_copy(deep=True)

    fresh = record.model_copy(deep=True)
    fresh.last_completed_stage = None
    result = worker.process_item(fresh)
    now = datetime.now(UTC).isoformat()
    if result.status is not ItemStatus.DONE:
        queue.mutate_state(lambda state: state.__setitem__(record.content_id, saved))
        return {"status": "failed", "at": now, "error": result.error}

    new_path = Path(result.note_path or "")
    if new_path.parent != old_path.parent:
        moved = old_path.parent / new_path.name
        if not moved.exists():  # never overwrite a different note that holds that name
            shutil.move(str(new_path), str(moved))
            new_path = moved

    def keep_path(state: dict[str, StateRecord]) -> None:
        state[record.content_id].note_path = str(new_path)

    queue.mutate_state(keep_path)

    extra = kept_sections(old_text)
    if extra:
        text = new_path.read_bytes().decode("utf-8").rstrip("\n")
        new_path.write_bytes(f"{text}\n\n{extra}\n".encode())

    links = []
    if new_path.stem != old_path.stem:
        links = repair_links(_vault_root(settings.vault_dir), old_path.stem, new_path.stem)

    reviewed_path = settings.resolve("data/review/reviewed.json")
    reviewed = _load_json(reviewed_path, {"version": 1, "items": {}})
    reviewed["items"].pop(record.content_id, None)
    _save_json(reviewed_path, reviewed)

    return {
        "status": "done",
        "at": now,
        "old_note": old_path.name,
        "new_note": new_path.name,
        "links_repaired": links,
        "sections_kept": [m.group(1).strip() for m in SECTION.finditer(extra)],
    }


def run(settings, worker, queue, *, apply: bool, today: str | None = None) -> int:
    ledger_path = settings.resolve("data/review/carousel-recapture.json")
    backup_dir = settings.resolve("data/review/carousel-recapture-backup")
    ledger = _load_json(ledger_path, {"halted_until": None, "items": {}})
    pending = candidates(queue.load_state(), ledger, settings.vault_dir)
    print(f"{len(pending)} left, {len(ledger['items'])} attempted")
    if not apply:
        for record in pending[:MAX_PER_DAY]:
            print(f"  next: {record.content_id} {Path(record.note_path or '').name}")
        return 0

    now = datetime.now(UTC)
    if ledger["halted_until"] and now < datetime.fromisoformat(ledger["halted_until"]):
        print(f"halted until {ledger['halted_until']}")
        return 1
    today = today or datetime.now().astimezone().date().isoformat()
    used = sum(1 for item in ledger["items"].values() if item.get("day") == today)
    for record in pending[: max(MAX_PER_DAY - used, 0)]:
        entry = recapture_one(settings, worker, queue, record, backup_dir)
        entry["day"] = today
        ledger["items"][record.content_id] = entry
        print(record.content_id, json.dumps(entry, ensure_ascii=False))
        if entry["status"] != "done":
            ledger["halted_until"] = (now + timedelta(hours=HALT_HOURS)).isoformat()
            _save_json(ledger_path, ledger)
            print(f"halted until {ledger['halted_until']}")
            return 1
        _save_json(ledger_path, ledger)
    if used >= MAX_PER_DAY:
        print(f"already did {used} today")
    return 0


def self_check() -> int:
    """Real files and a real QueueManager; only the worker is faked."""
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".obsidian").mkdir()
        vault_dir = root / "20-Resources" / "Reels"
        sorted_dir = vault_dir / "Sorted"
        sorted_dir.mkdir(parents=True)
        hub = vault_dir / "hub.md"
        hub.write_text(
            "- [[old-title]]\n- [[a/old-title|label]]\n- [[old-title-longer]]\n", "utf-8"
        )
        old = sorted_dir / "old-title.md"
        old.write_text(
            "---\ntitle: old\n---\n# old\n\n## Summary\nold\n\n## Transcript\nold\n\n"
            "## Review (auto, 2026-09-24)\nstale\n\n## Gated content (DM)\nhttps://kept.example\n",
            "utf-8",
        )

        settings = SimpleNamespace(vault_dir=vault_dir, resolve=lambda rel: root / rel)
        added = datetime(2026, 10, 1, tzinfo=UTC)

        def rec(cid: str, url: str, note: Path, when: datetime = added) -> StateRecord:
            return StateRecord(
                content_id=cid,
                url=url,
                normalized_url=url,
                source="queue_file",
                status=ItemStatus.DONE,
                content_kind="media",
                added_at=when,
                updated_at=when,
                note_path=str(note),
            )

        state = {
            "one": rec("one", "https://instagram.com/p/a?img_index=2", old),
            "plain": rec("plain", "https://instagram.com/p/b", old),
            "later": rec("later", "https://instagram.com/p/c?img_index=2", old, FIXED_ON),
            "arch": rec("arch", "https://instagram.com/p/d?img_index=2", root / "gone.md"),
        }

        class Queue:
            def load_state(self):
                return state

            def mutate_state(self, fn):
                return fn(state)

        class Worker:
            fail = False

            def process_item(self, record):
                assert record.last_completed_stage is None
                if self.fail:
                    record.status = ItemStatus.FAILED
                    record.error = "HttpError 400"
                    return record
                new = vault_dir / "new-title.md"
                new.write_text("---\ntitle: new\n---\n# new\n\n## Transcript\nnew\n", "utf-8")
                old.unlink()  # what _cleanup_stale_note does
                record.status = ItemStatus.DONE
                record.note_path = str(new)
                return record

        reviewed = root / "data/review/reviewed.json"
        _save_json(reviewed, {"version": 1, "items": {"one": {"verdict": "skip"}, "x": {}}})

        ledger_path = root / "data/review/carousel-recapture.json"
        worker = Worker()
        worker.fail = True
        assert run(settings, worker, Queue(), apply=True, today="d1") == 1
        assert old.is_file(), "a failed re-run must leave the note"
        assert state["one"].status is ItemStatus.DONE and state["one"].note_path == str(old)
        assert run(settings, worker, Queue(), apply=True, today="d1") == 1, "must stay halted"
        assert "one" in _load_json(ledger_path, {})["items"]

        ledger_path.unlink()
        worker.fail = False
        assert run(settings, worker, Queue(), apply=True, today="d2") == 0
        moved = sorted_dir / "new-title.md"
        assert moved.is_file() and not (vault_dir / "new-title.md").exists(), "folder kept"
        assert state["one"].note_path == str(moved)
        text = moved.read_text("utf-8")
        assert "https://kept.example" in text and "stale" not in text, text
        assert "- [[new-title]]\n- [[a/new-title|label]]\n- [[old-title-longer]]" in hub.read_text(
            "utf-8"
        )
        assert "stale" in (root / "data/review/carousel-recapture-backup/one.md").read_text("utf-8")
        items = _load_json(reviewed, {})["items"]
        assert "one" not in items and "x" in items
        ledger = _load_json(ledger_path, {})
        assert sorted(ledger["items"]) == ["one"], "plain, later and arch are not candidates"

        # The daily cap holds across runs.
        ledger["items"] = {
            "p": {"day": "d3", "status": "done"},
            "q": {"day": "d3", "status": "done"},
        }
        _save_json(ledger_path, ledger)
        state["two"] = rec("two", "https://instagram.com/p/e?img_index=2", moved)
        assert run(settings, worker, Queue(), apply=True, today="d3") == 0
        assert "two" not in _load_json(ledger_path, {})["items"], "cap must stop a third post"
    print("self-check ok")
    return 0


def main(argv: list[str]) -> int:
    if "--self-check" in argv:
        return self_check()
    settings = get_settings()
    return run(settings, build_worker(settings), QueueManager(settings), apply="--apply" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
