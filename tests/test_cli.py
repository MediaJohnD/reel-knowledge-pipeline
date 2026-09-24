from __future__ import annotations

import errno
import socket
import time
from datetime import UTC, datetime
from unittest import mock

import pytest
from typer.testing import CliRunner

from reel_pipeline.cli import _wait_for_bindable, app
from reel_pipeline.config import DownloadConfig, Settings
from reel_pipeline.models import ItemStatus, QueueSource, StateRecord
from reel_pipeline.queue_manager import QueueManager

runner = CliRunner()


def make_settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["youtube.com"], blocked_domains=[]),
    )


def _seed_failed_permanent(settings: Settings, content_id: str) -> None:
    qm = QueueManager(settings)
    now = datetime.now(UTC)
    state = qm.load_state()
    state[content_id] = StateRecord(
        content_id=content_id,
        url=f"https://youtube.com/{content_id}",
        normalized_url=f"https://youtube.com/{content_id}",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED_PERMANENT,
        added_at=now,
        updated_at=now,
        attempt_count=5,
        error="boom",
    )
    qm.save_state(state)


def test_retry_by_content_id_resets_matching_record(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr("reel_pipeline.cli.get_settings", lambda: settings)
    _seed_failed_permanent(settings, "abc123")

    result = runner.invoke(app, ["retry", "abc123"])

    assert result.exit_code == 0
    assert "reset: abc123" in result.stdout
    record = QueueManager(settings).load_state()["abc123"]
    assert record.status == ItemStatus.PENDING


def test_retry_all_failed_permanent_resets_every_matching_record(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr("reel_pipeline.cli.get_settings", lambda: settings)
    _seed_failed_permanent(settings, "abc123")
    _seed_failed_permanent(settings, "def456")

    result = runner.invoke(app, ["retry", "--all-failed-permanent"])

    assert result.exit_code == 0
    state = QueueManager(settings).load_state()
    assert state["abc123"].status == ItemStatus.PENDING
    assert state["def456"].status == ItemStatus.PENDING


def test_retry_without_content_id_or_flag_errors(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr("reel_pipeline.cli.get_settings", lambda: settings)

    result = runner.invoke(app, ["retry"])

    assert result.exit_code != 0


def test_retry_rejects_content_id_combined_with_all_failed_permanent(tmp_path, monkeypatch):
    """Regression test: passing both used to silently ignore content_id and
    reset every FAILED_PERMANENT record instead - a footgun, since the CLI
    gave no indication content_id was ignored.
    """
    settings = make_settings(tmp_path)
    monkeypatch.setattr("reel_pipeline.cli.get_settings", lambda: settings)
    _seed_failed_permanent(settings, "abc123")
    _seed_failed_permanent(settings, "def456")

    result = runner.invoke(app, ["retry", "abc123", "--all-failed-permanent"])

    assert result.exit_code != 0
    state = QueueManager(settings).load_state()
    assert state["abc123"].status == ItemStatus.FAILED_PERMANENT
    assert state["def456"].status == ItemStatus.FAILED_PERMANENT


def test_retry_reports_when_nothing_matches(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr("reel_pipeline.cli.get_settings", lambda: settings)

    result = runner.invoke(app, ["retry", "nope"])

    assert result.exit_code != 0
    assert "No matching" in result.stdout


# --- _wait_for_bindable ------------------------------------------------------
# Regression cover for the 2026-09-20 outage: uvicorn treats a not-yet-assigned
# bind address as fatal and exits in ~2s, so a Tailscale-not-up-yet logon killed
# the webhook server outright. See the docstring on _wait_for_bindable.

# Unassigned on this host, and in the CGNAT range Tailscale uses - binding it
# raises EADDRNOTAVAIL, the exact error the real outage produced.
UNASSIGNED_HOST = "100.64.101.99"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_wait_for_bindable_returns_immediately_when_address_is_free():
    slept: list[float] = []

    _wait_for_bindable(
        "127.0.0.1",
        _free_port(),
        timeout=30.0,
        poll_interval=1.0,
        sleep=slept.append,
    )

    assert slept == []  # never waited on an address that was ready


def test_wait_for_bindable_returns_once_the_address_appears():
    """The real recovery path: absent at first, assigned a moment later."""
    host = {"value": UNASSIGNED_HOST}
    port = _free_port()
    slept: list[float] = []

    def fake_bind(address: tuple[str, int]) -> None:
        if host["value"] == UNASSIGNED_HOST:
            raise OSError(errno.EADDRNOTAVAIL, "address not available")

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            host["value"] = "127.0.0.1"  # Tailscale finishes coming up

    with mock.patch.object(socket.socket, "bind", side_effect=fake_bind, autospec=False):
        _wait_for_bindable(host["value"], port, timeout=60.0, poll_interval=2.0, sleep=sleep)

    assert slept == [2.0, 2.0, 2.0]  # waited, then proceeded rather than dying


def test_wait_for_bindable_raises_after_timeout_when_address_never_appears():
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        # Must really advance the clock: the bound is enforced against
        # time.monotonic(), so a no-op sleep would spin instead of expiring.
        slept.append(seconds)
        time.sleep(seconds)

    started = time.monotonic()
    with pytest.raises(OSError) as excinfo:
        _wait_for_bindable(
            UNASSIGNED_HOST, _free_port(), timeout=0.3, poll_interval=0.1, sleep=sleep
        )
    elapsed = time.monotonic() - started

    assert excinfo.value.errno == errno.EADDRNOTAVAIL
    assert elapsed < 2.0  # gives up near the deadline instead of spinning forever
    assert max(slept) <= 0.1  # never waits longer than one poll interval


def test_wait_for_bindable_does_not_wait_when_port_is_already_in_use():
    """EADDRINUSE is a different bug; waiting on it would mask a real error."""
    slept: list[float] = []
    port = _free_port()

    with socket.socket() as holder:
        holder.bind(("127.0.0.1", port))
        holder.listen(1)

        _wait_for_bindable("127.0.0.1", port, timeout=30.0, poll_interval=1.0, sleep=slept.append)

    assert slept == []  # returned straight away so uvicorn reports it as before


def test_wait_for_bindable_disabled_by_zero_timeout():
    slept: list[float] = []

    with pytest.raises(OSError):
        _wait_for_bindable(
            UNASSIGNED_HOST, _free_port(), timeout=0.0, poll_interval=2.0, sleep=slept.append
        )

    assert slept == []
