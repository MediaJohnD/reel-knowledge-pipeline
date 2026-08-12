from __future__ import annotations

import pytest

from reel_pipeline import llm_client


@pytest.fixture(autouse=True)
def _reset_llm_rate_limiter():
    """llm_client._last_call_at is process-global (deliberately, so it throttles
    across the whole worker process) - reset it between tests so one test's
    calls never throttle a later, unrelated test.
    """
    llm_client._last_call_at.clear()
    yield
    llm_client._last_call_at.clear()
