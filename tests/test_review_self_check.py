"""Run the reel scripts' built-in self-checks under pytest so CI catches regressions.

`scripts/review_new_reels.py --self-check` exercises verdict capping, the career/method/
personal relabelling against the committed example profile, and the backfill end to end
on a temp vault. `scripts/loop_digest.py --test` covers the digest parser, backlog and
session brief (including the Methods line). Both run in a subprocess: the reviewer swaps
module globals (the manifest path) while it works.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script", "flag", "ok"),
    [
        ("review_new_reels.py", "--self-check", "self-check ok"),
        ("loop_digest.py", "--test", "ok"),
    ],
)
def test_script_self_check_passes(script, flag, ok):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), flag],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert ok in proc.stdout.splitlines()
