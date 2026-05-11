"""Shared fixtures.

Each test gets a temporary idempotency dir, a stable rate limit, a known API
key, and a respx-mocked Linkuma base URL.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("LINKUMA_API_KEY", "lkm_test_" + "x" * 32)
    monkeypatch.setenv("LINKUMA_BUDGET_CAP_EUR", "500")
    monkeypatch.setenv("LINKUMA_RATE_LIMIT_RPS", "1000")  # disable for tests
    monkeypatch.setenv("LINKUMA_LOW_CREDIT_THRESHOLD_EUR", "50")
    monkeypatch.setenv("LINKUMA_IDEMPOTENCY_DIR", str(tmp_path / "idem"))
    monkeypatch.setenv("LINKUMA_BASE_URL", "https://test.linkuma.local/api/v1")
    # Reset the thematics in-memory cache between tests.
    from linkuma_mcp import thematics

    thematics.clear_cache()
    yield
    # Cleanup confirm tokens
    from linkuma_mcp import pricing

    pricing._tokens.clear()  # type: ignore[attr-defined]


@pytest.fixture
def base_url() -> str:
    return os.environ["LINKUMA_BASE_URL"]
