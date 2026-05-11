"""Tests for the httpx-backed LinkumaClient."""

from __future__ import annotations

import logging
import re

import httpx
import pytest
import respx

from linkuma_mcp.client import LinkumaClient, _redact, logger
from linkuma_mcp.errors import (
    LinkumaAuthError,
    LinkumaInsufficientCredit,
    LinkumaOrderUncertain,
    LinkumaRateLimited,
    LinkumaServerError,
    LinkumaUnreachable,
    LinkumaValidationError,
)

# ---------------------------------------------------------------------------
# Auth header & base URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_header_is_sent(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url, assert_all_called=True) as rsx:
            route = rsx.get("/settings").mock(
                return_value=httpx.Response(200, json={"credit_eur": 200})
            )
            await client.get_settings()
            assert route.called
            req = route.calls.last.request
            assert req.headers["Authorization"].startswith("Bearer lkm_test_")


@pytest.mark.asyncio
async def test_unauthorized_raises_auth_error(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            rsx.get("/settings").mock(
                return_value=httpx.Response(401, json={"message": "Invalid token"})
            )
            with pytest.raises(LinkumaAuthError):
                await client.get_settings()


@pytest.mark.asyncio
async def test_validation_error_on_422(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            rsx.post("/projects").mock(
                return_value=httpx.Response(422, json={"message": "name taken"})
            )
            with pytest.raises(LinkumaValidationError):
                await client.create_project({"name": "x"})


@pytest.mark.asyncio
async def test_insufficient_credit_on_402(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            rsx.post("/carts/price").mock(
                return_value=httpx.Response(402, json={"message": "no credit"})
            )
            with pytest.raises(LinkumaInsufficientCredit):
                await client.cart_price({"items": []})


# ---------------------------------------------------------------------------
# Server errors and order semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_on_non_order_is_server_error(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            rsx.get("/projects").mock(return_value=httpx.Response(500))
            with pytest.raises(LinkumaServerError):
                await client.list_projects()


@pytest.mark.asyncio
async def test_5xx_on_order_post_is_uncertain(base_url) -> None:
    """Spec §5.6: zero retry, raise LinkumaOrderUncertain on 5xx."""
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            rsx.post("/carts/order").mock(return_value=httpx.Response(503))
            with pytest.raises(LinkumaOrderUncertain):
                await client.cart_order({"items": []})


@pytest.mark.asyncio
async def test_timeout_on_order_post_is_uncertain(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            rsx.post("/carts/order").mock(side_effect=httpx.TimeoutException("boom"))
            with pytest.raises(LinkumaOrderUncertain):
                await client.cart_order({"items": []})


@pytest.mark.asyncio
async def test_timeout_on_get_is_unreachable(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            rsx.get("/settings").mock(side_effect=httpx.TimeoutException("boom"))
            with pytest.raises(LinkumaUnreachable):
                await client.get_settings()


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_is_retried_with_retry_after(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            route = rsx.get("/settings")
            route.side_effect = [
                httpx.Response(429, headers={"Retry-After": "0"}, json={"m": "slow"}),
                httpx.Response(200, json={"credit_eur": 100}),
            ]
            data = await client.get_settings()
            assert data["credit_eur"] == 100
            assert route.call_count == 2


@pytest.mark.asyncio
async def test_429_on_order_does_not_retry(base_url) -> None:
    async with LinkumaClient() as client:
        with respx.mock(base_url=base_url) as rsx:
            rsx.post("/carts/order").mock(
                return_value=httpx.Response(429, headers={"Retry-After": "1"})
            )
            with pytest.raises(LinkumaRateLimited):
                await client.cart_order({"items": []})


# ---------------------------------------------------------------------------
# Log filter / redaction
# ---------------------------------------------------------------------------


def test_redact_masks_lkm_prefixed_tokens() -> None:
    assert _redact("hello lkm_abcdefghijklmnop world") == "hello [redacted] world"


def test_redact_masks_long_tokens() -> None:
    long = "A" * 40
    assert _redact(f"key={long}") == "key=[redacted]"


def test_log_filter_is_attached_and_works(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="linkuma_mcp"):
        logger.warning("leaking lkm_supersecretkey1234567890")
    rendered = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "lkm_supersecretkey" not in rendered
    assert re.search(r"\[redacted\]", rendered)


# ---------------------------------------------------------------------------
# Budget cap (enforced at the pricing helper level)
# ---------------------------------------------------------------------------


def test_budget_cap_helper_raises(monkeypatch) -> None:
    from linkuma_mcp.errors import LinkumaBudgetExceeded
    from linkuma_mcp.pricing import enforce_budget

    monkeypatch.setenv("LINKUMA_BUDGET_CAP_EUR", "100")
    with pytest.raises(LinkumaBudgetExceeded):
        enforce_budget(200.0)


def test_budget_cap_helper_passes(monkeypatch) -> None:
    from linkuma_mcp.pricing import enforce_budget

    monkeypatch.setenv("LINKUMA_BUDGET_CAP_EUR", "100")
    enforce_budget(50.0)  # no raise
