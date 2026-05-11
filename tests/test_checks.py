"""Tests for url validation, anchor over-opt, pagekw coherence, gmaps urls."""

from __future__ import annotations

import httpx
import pytest
import respx

from linkuma_mcp import checks

# ---------------------------------------------------------------------------
# Generic URL validity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://example.com/page", True),
        ("http://example.com", True),
        ("ftp://example.com", False),
        ("", False),
        ("not-a-url", False),
        ("https://", False),
    ],
)
def test_is_valid_http_url(url, ok) -> None:
    assert checks.is_valid_http_url(url) is ok


# ---------------------------------------------------------------------------
# Gmaps URL gating (spec safety bullet)
# ---------------------------------------------------------------------------


def test_gmaps_url_canonical_accepted() -> None:
    ok, reason = checks.is_valid_gmaps_url(
        "https://www.google.com/maps/place/Some+Place/@45.0,4.0,17z/data=!4m6"
    )
    assert ok is True
    assert reason is None


@pytest.mark.parametrize(
    "url",
    [
        "https://share.google/abc123",
        "https://maps.app.goo.gl/abcdef",
        "https://goo.gl/maps/abc",
        "https://g.co/maps/abc",
    ],
)
def test_gmaps_url_short_rejected(url) -> None:
    ok, reason = checks.is_valid_gmaps_url(url)
    assert ok is False
    assert reason is not None


def test_gmaps_url_non_google_rejected() -> None:
    ok, reason = checks.is_valid_gmaps_url("https://maps.example.com/place/x")
    assert ok is False
    assert reason is not None


def test_gmaps_url_empty_rejected() -> None:
    ok, reason = checks.is_valid_gmaps_url("")
    assert ok is False
    assert reason is not None


# ---------------------------------------------------------------------------
# Domain validity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain, ok",
    [
        ("plaques24.fr", True),
        ("sub.example.co.uk", True),
        ("localhost", False),
        ("", False),
        ("not_a_domain", False),
    ],
)
def test_is_valid_target_domain(domain, ok) -> None:
    assert checks.is_valid_target_domain(domain) is ok


# ---------------------------------------------------------------------------
# Anchor over-optimisation
# ---------------------------------------------------------------------------


def test_anchor_over_opt_within_batch() -> None:
    items = [
        {"target_url": "https://a.com/x", "anchor": "courtier orléans"},
        {"target_url": "https://a.com/x", "anchor": "Courtier Orléans"},
        {"target_url": "https://a.com/x", "anchor": "  courtier orléans  "},
    ]
    warnings = checks.detect_anchor_over_optimisation(items, batch_threshold=2)
    assert len(warnings) == 1
    assert "3x" in warnings[0]


def test_anchor_over_opt_with_history() -> None:
    items = [{"target_url": "https://a.com/x", "anchor": "plaques personnalisées"}]
    history = [("https://a.com/x", "plaques personnalisées")] * 5
    warnings = checks.detect_anchor_over_optimisation(
        items, history_pairs=history, history_threshold=4
    )
    assert any("6 occurrences" in w for w in warnings)


def test_anchor_over_opt_no_warning_when_safe() -> None:
    items = [
        {"target_url": "https://a.com/x", "anchor": "anchor a"},
        {"target_url": "https://a.com/x", "anchor": "anchor b"},
        {"target_url": "https://a.com/x", "anchor": "anchor c"},
    ]
    assert checks.detect_anchor_over_optimisation(items, batch_threshold=2) == []


# ---------------------------------------------------------------------------
# pagekw coherence
# ---------------------------------------------------------------------------


def test_pagekw_coherence_match() -> None:
    assert checks.pagekw_anchor_coherence("plaques immatriculation", "plaques perso") is True


def test_pagekw_coherence_mismatch() -> None:
    assert checks.pagekw_anchor_coherence("recettes cuisine", "plaques voiture") is False


def test_pagekw_coherence_empty_inputs_pass() -> None:
    assert checks.pagekw_anchor_coherence("", "anything") is True
    assert checks.pagekw_anchor_coherence("anything", "") is True


# ---------------------------------------------------------------------------
# Live target_url status (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_url_status_200() -> None:
    with respx.mock(assert_all_called=True) as rsx:
        rsx.head("https://ok.example.com/").mock(return_value=httpx.Response(200))
        ok, status, err = await checks.target_url_status("https://ok.example.com/")
        assert ok is True
        assert status == 200
        assert err is None


@pytest.mark.asyncio
async def test_target_url_status_404() -> None:
    with respx.mock() as rsx:
        rsx.head("https://nope.example.com/").mock(return_value=httpx.Response(404))
        ok, status, _ = await checks.target_url_status("https://nope.example.com/")
        assert ok is False
        assert status == 404


@pytest.mark.asyncio
async def test_target_url_status_invalid_url_short_circuits() -> None:
    ok, status, err = await checks.target_url_status("not-a-url")
    assert ok is False
    assert status is None
    assert err == "invalid url"
