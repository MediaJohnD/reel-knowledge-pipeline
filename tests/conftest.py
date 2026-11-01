from __future__ import annotations

import socket

import pytest

from reel_pipeline import llm_client


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """validators.is_private_or_local_host resolves hostnames. Tests make no
    real network calls, so every lookup fails (treated as "not private") unless
    a test installs its own fake addresses with fake_dns below.
    """

    def _refuse(*args, **kwargs):
        raise socket.gaierror("real DNS is disabled in tests")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)


@pytest.fixture
def fake_dns(monkeypatch):
    """Installs {hostname: [addresses]}; any other hostname fails to resolve."""

    def _install(table):
        def _lookup(host, *args, **kwargs):
            if host not in table:
                raise socket.gaierror("unknown host")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in table[host]]

        monkeypatch.setattr(socket, "getaddrinfo", _lookup)

    return _install


@pytest.fixture(autouse=True)
def _reset_llm_rate_limiter():
    """llm_client._last_call_at is process-global (deliberately, so it throttles
    across the whole worker process) - reset it between tests so one test's
    calls never throttle a later, unrelated test.
    """
    llm_client._last_call_at.clear()
    yield
    llm_client._last_call_at.clear()
