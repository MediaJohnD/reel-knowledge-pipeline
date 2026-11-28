"""index_sync: ask a local search index to update after notes are written."""

from __future__ import annotations

import httpx

from reel_pipeline import index_sync
from reel_pipeline.config import SearchIndexSyncConfig, Settings

URL = "http://localhost:42110/api/update?t=markdown&force=false"


def _settings(tmp_path, **kw) -> Settings:
    cfg = SearchIndexSyncConfig(enabled=True, update_url=URL, **kw)
    return Settings(project_root=tmp_path, search_index_sync=cfg)


def _fake_get(monkeypatch, calls, fail=False):
    def get(self, url, *a, **k):
        calls.append(url)
        if fail:
            raise httpx.ConnectError("down")
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)


def test_calls_index_when_notes_written(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_get(monkeypatch, calls)
    s = _settings(tmp_path)
    s.ensure_directories()
    assert index_sync.notify_index(s, 3, now=100000.0) is True
    assert calls == [URL]


def test_disabled_or_no_notes_does_nothing(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_get(monkeypatch, calls)
    off = Settings(project_root=tmp_path)
    assert index_sync.notify_index(off, 3) is False
    on = _settings(tmp_path)
    assert index_sync.notify_index(on, 0) is False
    assert calls == []


def test_non_loopback_url_is_refused(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_get(monkeypatch, calls)
    cfg = SearchIndexSyncConfig(enabled=True, update_url="http://example.com/api/update")
    s = Settings(project_root=tmp_path, search_index_sync=cfg)
    assert index_sync.notify_index(s, 2) is False
    assert calls == []


def test_throttled_within_interval_then_allowed_after(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_get(monkeypatch, calls)
    s = _settings(tmp_path, min_interval_minutes=30)
    s.ensure_directories()
    assert index_sync.notify_index(s, 1, now=100000.0) is True
    assert index_sync.notify_index(s, 1, now=100000.0 + 600) is False  # 10 min later
    assert index_sync.notify_index(s, 1, now=100000.0 + 31 * 60) is True
    assert len(calls) == 2


def test_failure_never_raises_and_is_not_throttled(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_get(monkeypatch, calls, fail=True)
    s = _settings(tmp_path)
    s.ensure_directories()
    assert index_sync.notify_index(s, 1, now=100000.0) is False
    assert index_sync.notify_index(s, 1, now=100001.0) is False  # failed call set no stamp
    assert len(calls) == 2
