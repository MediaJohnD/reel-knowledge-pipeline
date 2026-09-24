"""Ledger vs vault drift check. Dry-run by default; --apply to write.

Done records whose note_path is missing: if exactly one vault file has the same
basename, repoint note_path there (via QueueManager's locked mutate_state);
otherwise append a line to data/inbox/needs-attention.txt. Never raises out.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def plan(records, vault_files):
    """records: {id: note_path|None} (done only). -> (moves {id: new}, lost [ids])."""
    by_name: dict[str, list[str]] = {}
    for f in vault_files:
        by_name.setdefault(Path(f).name.lower(), []).append(str(f))
    moves, lost = {}, []
    for cid, p in records.items():
        if p and Path(p).exists():
            continue
        hits = by_name.get(Path(p).name.lower(), []) if p else []
        if len(hits) == 1:
            moves[cid] = hits[0]
        else:
            lost.append(cid)
    return moves, lost


def self_check():
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        a = Path(t, "a.md"); a.write_text("x")
        b = Path(t, "sub"); b.mkdir(); (b / "b.md").write_text("x")
        m, l = plan({"1": str(a), "2": str(Path(t, "old", "b.md")), "3": str(Path(t, "gone.md"))},
                    [a, b / "b.md"])
        assert m == {"2": str(b / "b.md")} and l == ["3"], (m, l)
    print("self-check ok")


def main(argv):
    if "--self-check" in argv:
        return self_check()
    apply = "--apply" in argv
    sys.path.insert(0, str(ROOT / "src"))
    from reel_pipeline.config import load_settings
    from reel_pipeline.queue_manager import QueueManager

    s = load_settings()
    qm = QueueManager(s)
    vault_files = list(Path(s.vault_dir).rglob("*.md"))
    # vault_dir is the Reels subfolder; moved notes may sit elsewhere in the vault.
    vault_files += [f for f in Path(s.vault_dir).parents[1].rglob("*.md") if f not in set(vault_files)]
    done = {c: (str(r.note_path) if r.note_path else None)
            for c, r in qm.load_state().items() if str(r.status.value if hasattr(r.status, "value") else r.status) == "done"}
    moves, lost = plan(done, vault_files)
    for c, p in moves.items():
        print(f"moved {c}: {done[c]} -> {p}")
    for c in lost:
        print(f"lost  {c}: {done[c]}")
    print(f"done={len(done)} moved={len(moves)} lost={len(lost)} mode={'apply' if apply else 'dry-run'}")
    if not apply:
        return
    def fix(state):
        for c, p in moves.items():
            state[c].note_path = type(state[c].note_path)(p) if state[c].note_path else p
    if moves:
        qm.mutate_state(fix)
    if lost:
        na = ROOT / "data" / "inbox" / "needs-attention.txt"
        old = na.read_text(encoding="utf-8") if na.exists() else ""
        new = [f"ledger drift: done record {c} note missing: {done[c]}" for c in lost
               if f"done record {c} note missing" not in old]
        if new:
            with na.open("a", encoding="utf-8") as f:
                f.write("\n".join(new) + "\n")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:  # never break the nightly batch
        print(f"reconcile_ledger error: {e}")
