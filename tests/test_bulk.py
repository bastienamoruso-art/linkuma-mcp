"""Tests for the bulk_reorder tool."""

from __future__ import annotations

import httpx
import pytest
import respx

from linkuma_mcp.client import LinkumaClient
from linkuma_mcp.errors import LinkumaValidationError
from linkuma_mcp.tools import bulk


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn
        return wrap


@pytest.fixture
def registered(base_url):
    fake = _FakeMCP()
    client = LinkumaClient()
    bulk.register(fake, lambda: client)
    return fake.tools, client


@pytest.mark.asyncio
async def test_plan_rejects_empty_list(registered):
    tools, _ = registered
    with pytest.raises(LinkumaValidationError):
        await tools["linkuma_bulk_reorder_plan"](refused_order_ids=[])


@pytest.mark.asyncio
async def test_plan_rejects_unknown_adjustment(registered):
    tools, _ = registered
    with pytest.raises(LinkumaValidationError):
        await tools["linkuma_bulk_reorder_plan"](
            refused_order_ids=["o1"],
            adjustments={"foobar": "x"},
        )


@pytest.mark.asyncio
async def test_plan_rejects_bad_anchor_value(registered):
    tools, _ = registered
    with pytest.raises(LinkumaValidationError):
        await tools["linkuma_bulk_reorder_plan"](
            refused_order_ids=["o1"],
            adjustments={"anchor": "manual_rewrite"},
        )


@pytest.mark.asyncio
async def test_plan_rejects_bad_tier(registered):
    tools, _ = registered
    with pytest.raises(LinkumaValidationError):
        await tools["linkuma_bulk_reorder_plan"](
            refused_order_ids=["o1"],
            adjustments={"tier": "ultra"},
        )


@pytest.mark.asyncio
async def test_plan_then_execute_with_auto_rewrite(registered, base_url):
    tools, _ = registered
    plan = tools["linkuma_bulk_reorder_plan"]
    execute = tools["linkuma_bulk_reorder_execute"]

    refused_orders = [
        {
            "order_id": "ord-old-1",
            "project_id": "p1",
            "tier": "standard",
            "target_url": "https://example.fr/page",
            "anchor": "exact match",
            "pagekw": "plombier paris",
            "thematic_id": "th-x",
        },
        {
            "order_id": "ord-old-2",
            "project_id": "p1",
            "tier": "standard",
            "target_url": "https://example.fr/page",
            "anchor": "exact match",
            "pagekw": "plombier paris",
            "thematic_id": "th-x",
        },
    ]

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/orders/ord-old-1").mock(
            return_value=httpx.Response(200, json=refused_orders[0])
        )
        rsx.get("/orders/ord-old-2").mock(
            return_value=httpx.Response(200, json=refused_orders[1])
        )

        order_ids = iter([f"new-{i}" for i in range(10)])
        rsx.post("/carts/order").mock(
            side_effect=lambda req: httpx.Response(
                200, json={"order_id": next(order_ids)}
            )
        )

        plan_res = await plan(
            refused_order_ids=["ord-old-1", "ord-old-2"],
            adjustments={"anchor": "auto_rewrite"},
        )
        assert len(plan_res["items"]) == 2
        # anchors were regenerated -> not equal to original "exact match"
        anchors = [it["anchor"] for it in plan_res["items"]]
        # at least one should differ from the original
        assert not all(a == "exact match" for a in anchors)

        result = await execute(confirm_token=plan_res["confirm_token"])
        assert len(result["orders_created"]) == 2


@pytest.mark.asyncio
async def test_plan_passes_through_tier_override(registered, base_url):
    tools, _ = registered
    plan = tools["linkuma_bulk_reorder_plan"]

    refused = {
        "order_id": "ord-x",
        "project_id": "p1",
        "tier": "standard",
        "target_url": "https://example.fr/",
        "anchor": "x",
    }
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/orders/ord-x").mock(return_value=httpx.Response(200, json=refused))

        plan_res = await plan(
            refused_order_ids=["ord-x"],
            adjustments={"tier": "premium"},
        )
    assert plan_res["items"][0]["type"] == "premium"
