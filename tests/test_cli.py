from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

from reel_pipeline.cli import app
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
