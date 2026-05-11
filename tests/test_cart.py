"""Tests for linkuma_cart_price + linkuma_cart_order.

Covers the v0.3.0 body schema: items use `type`, `url`, `project_id`,
`thematic_id`, `anchor`, `anchor_value`, `started_at`, etc. Top-level
keys `payment_method` / `external_ref` / `nice_name` exist ONLY for
/carts/order, never for /carts/price.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from linkuma_mcp.client import LinkumaClient
from linkuma_mcp.tools import cart
from linkuma_mcp.tools.cart import (
    build_order_body,
    build_price_body,
    strip_local_fields,
)


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self, *args, **kwargs):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn

        return wrap


@pytest.fixture
def registered(base_url):
    fake = _FakeMCP()
    client = LinkumaClient()
    cart.register(fake, lambda: client)
    return fake.tools, client


# ---------------------------------------------------------------------------
# Pure helpers — body builders
# ---------------------------------------------------------------------------


def test_strip_local_fields_drops_pagekw_and_helpers() -> None:
    item = {
        "type": "citation_boost",
        "url": "https://x.fr/",
        "project_id": "p1",
        "thematic_id": "t1",
        "anchor": "custom",
        "anchor_value": "a",
        "started_at": "2026-05-25",
        "pagekw": "internal-only",
        "nice_name": "internal",
        "external_ref": "ext-1",
    }
    out = strip_local_fields(item)
    assert "pagekw" not in out
    assert "nice_name" not in out
    assert "external_ref" not in out
    assert out["type"] == "citation_boost"


def test_build_price_body_only_carries_items() -> None:
    body = build_price_body(
        [
            {
                "type": "citation_boost",
                "url": "https://x.fr/",
                "project_id": "p1",
                "thematic_id": "t1",
                "anchor": "custom",
                "anchor_value": "a",
                "started_at": "2026-05-25",
            }
        ]
    )
    assert set(body.keys()) == {"items"}
    assert body["items"][0]["type"] == "citation_boost"


def test_build_order_body_adds_payment_metadata() -> None:
    body = build_order_body(
        [
            {
                "type": "premium",
                "url": "https://x.fr/",
                "project_id": "p1",
                "thematic_id": "t1",
                "anchor": "custom",
                "anchor_value": "a",
                "started_at": "2026-05-25",
            }
        ],
        external_ref="REF-001",
        nice_name="Cart name",
    )
    assert body["payment_method"] == "direct_credits"
    assert body["external_ref"] == "REF-001"
    assert body["nice_name"] == "Cart name"
    assert body["items"][0]["type"] == "premium"


# ---------------------------------------------------------------------------
# cart_price tool — happy path posts the right body shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cart_price_posts_v030_schema(registered, base_url) -> None:
    tools, _ = registered
    price = tools["linkuma_cart_price"]

    posted_body: dict = {}

    def _capture_price(req: httpx.Request) -> httpx.Response:
        nonlocal posted_body
        posted_body = json.loads(req.content)
        return httpx.Response(200, json={"total_price": 35})

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.post("/carts/price").mock(side_effect=_capture_price)
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )

        res = await price(
            items=[
                {
                    "type": "citation_boost",
                    "url": "https://example.fr/",
                    "map": "https://www.google.com/maps/place/Test/@1,2,17z/data=",
                    "project_id": "p1",
                    "thematic_id": "t1",
                    "category_id": "c1",
                    "qty": 1,
                    "anchor": "custom",
                    "anchor_value": "ancre",
                    "distribution": "direct",
                    "started_at": "2026-05-25",
                    "fast_publication": False,
                    "brief": "Test brief",
                }
            ],
            strict_checks=False,
        )

    # Top-level keys: only `items` (no payment_method on /carts/price).
    assert set(posted_body.keys()) == {"items"}
    sent = posted_body["items"][0]
    assert sent["type"] == "citation_boost"
    assert sent["url"] == "https://example.fr/"
    assert sent["anchor"] == "custom"
    assert sent["anchor_value"] == "ancre"
    assert sent["started_at"] == "2026-05-25"
    # Response is extracted from "total_price" (the real Linkuma key).
    assert res["total_eur"] == 35
    assert res["confirm_token"]
    assert not res["blockers"]


@pytest.mark.asyncio
async def test_cart_price_rejects_missing_required_fields(registered, base_url) -> None:
    tools, _ = registered
    price = tools["linkuma_cart_price"]

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        # Even if the network endpoint exists, local validation runs first.
        rsx.post("/carts/price").mock(
            return_value=httpx.Response(200, json={"total_price": 0})
        )
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )

        res = await price(
            items=[
                {
                    # missing type, project_id, thematic_id, started_at
                    "url": "https://example.fr/",
                    "anchor": "custom",
                    "anchor_value": "a",
                }
            ],
            strict_checks=False,
        )

    # Blockers populated; no confirm_token.
    assert res["blockers"]
    assert res["confirm_token"] is None
    blockers = "\n".join(res["blockers"])
    assert "type" in blockers
    assert "project_id" in blockers
    assert "thematic_id" in blockers
    assert "started_at" in blockers


@pytest.mark.asyncio
async def test_cart_price_citation_requires_map(registered, base_url) -> None:
    tools, _ = registered
    price = tools["linkuma_cart_price"]

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.post("/carts/price").mock(
            return_value=httpx.Response(200, json={"total_price": 0})
        )
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )

        res = await price(
            items=[
                {
                    "type": "citation_boost",
                    "url": "https://example.fr/",
                    "project_id": "p1",
                    "thematic_id": "t1",
                    "anchor": "custom",
                    "anchor_value": "a",
                    "started_at": "2026-05-25",
                    # missing map
                }
            ],
            strict_checks=False,
        )

    assert any("map is required" in b for b in res["blockers"])


@pytest.mark.asyncio
async def test_cart_price_custom_anchor_requires_value(registered, base_url) -> None:
    tools, _ = registered
    price = tools["linkuma_cart_price"]

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.post("/carts/price").mock(
            return_value=httpx.Response(200, json={"total_price": 0})
        )
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )

        res = await price(
            items=[
                {
                    "type": "premium",
                    "url": "https://example.fr/",
                    "project_id": "p1",
                    "thematic_id": "t1",
                    "anchor": "custom",
                    # missing anchor_value
                    "started_at": "2026-05-25",
                }
            ],
            strict_checks=False,
        )

    assert any("anchor_value is required" in b for b in res["blockers"])


# ---------------------------------------------------------------------------
# cart_order tool — posts payment_method + external_ref + nice_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cart_order_posts_v030_top_level_keys(registered, base_url) -> None:
    tools, _ = registered
    price = tools["linkuma_cart_price"]
    order = tools["linkuma_cart_order"]

    posted_order_body: dict = {}

    def _capture_order(req: httpx.Request) -> httpx.Response:
        nonlocal posted_order_body
        posted_order_body = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "data": {
                    "id": "cart-1",
                    "orders": [{"id": "ord-1", "price": 35}],
                }
            },
        )

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.post("/carts/price").mock(
            return_value=httpx.Response(200, json={"total_price": 35})
        )
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )
        rsx.get("/carts").mock(return_value=httpx.Response(200, json={"data": []}))
        rsx.post("/carts/order").mock(side_effect=_capture_order)

        priced = await price(
            items=[
                {
                    "type": "citation_boost",
                    "url": "https://example.fr/",
                    "map": "https://www.google.com/maps/place/Test/@1,2,17z/data=",
                    "project_id": "p1",
                    "thematic_id": "t1",
                    "category_id": "c1",
                    "anchor": "custom",
                    "anchor_value": "ancre",
                    "started_at": "2026-05-25",
                }
            ],
            strict_checks=False,
        )

        result = await order(
            confirm_token=priced["confirm_token"],
            external_ref="TEST-001",
            nice_name="Cart Test",
        )

    assert posted_order_body["payment_method"] == "direct_credits"
    assert posted_order_body["external_ref"] == "TEST-001"
    assert posted_order_body["nice_name"] == "Cart Test"
    assert result["order_id"] == "ord-1"
    assert result["external_ref"] == "TEST-001"


# ---------------------------------------------------------------------------
# Local campaign emits citation_boost / citation_linkuma items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_plan_emits_citation_type(base_url) -> None:
    from linkuma_mcp.tools import local_campaign

    fake = _FakeMCP()
    client = LinkumaClient()
    local_campaign.register(fake, lambda: client)

    thematics_payload = {
        "data": [
            {
                "id": "th-maison",
                "name": "Maison",
                "categories": [{"id": "th-travaux", "name": "Travaux"}],
            }
        ]
    }

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/thematics/premium").mock(
            return_value=httpx.Response(200, json=thematics_payload)
        )
        rsx.post("/carts/price").mock(
            return_value=httpx.Response(200, json={"total_price": 35})
        )

        res = await fake.tools["linkuma_local_campaign_plan"](
            project_id="p1",
            business_name="Test Biz",
            target_url="https://test-biz.fr/",
            gmaps_url="https://www.google.com/maps/place/Test/@1,2,17z/data=",
            count=2,
            budget_cap_eur=200,
            thematic_hint="travaux",
        )

    assert all(it["type"] in {"citation_boost", "citation_linkuma"} for it in res["items"])
    assert all(it["map"] for it in res["items"])
    assert all(it["anchor"] == "custom" for it in res["items"])
    assert all(it["anchor_value"] for it in res["items"])
    await client.aclose()
