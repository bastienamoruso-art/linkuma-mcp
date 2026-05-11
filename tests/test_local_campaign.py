"""End-to-end test of plan -> execute with respx mocks.

Covers:
- a happy path with a 3-item plan
- gmaps URL rejection
- partial failure when credit runs out mid-batch
- idempotency replay (calling execute twice returns deduped results)
"""

from __future__ import annotations

import httpx
import pytest
import respx

from linkuma_mcp import idempotency
from linkuma_mcp.errors import LinkumaValidationError
from linkuma_mcp.tools import local_campaign

# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


class _FakeMCP:
    """Mimics fastmcp's @mcp.tool() decorator. Records the registered functions."""

    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self, *args, **kwargs):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn

        return wrap


@pytest.fixture
def registered_tools(base_url):
    from linkuma_mcp.client import LinkumaClient

    fake = _FakeMCP()
    client = LinkumaClient()

    local_campaign.register(fake, lambda: client)
    yield fake.tools, client
    # cleanup
    import asyncio

    asyncio.get_event_loop().run_until_complete(client.aclose())


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_rejects_short_gmaps(registered_tools) -> None:
    tools, _ = registered_tools
    plan = tools["linkuma_local_campaign_plan"]
    with pytest.raises(LinkumaValidationError):
        await plan(
            project_id="p1",
            business_name="Test",
            target_url="https://example.com/",
            gmaps_url="https://maps.app.goo.gl/abcdef",
            count=3,
            budget_cap_eur=200,
        )


@pytest.mark.asyncio
async def test_plan_rejects_invalid_target_url(registered_tools) -> None:
    tools, _ = registered_tools
    plan = tools["linkuma_local_campaign_plan"]
    with pytest.raises(LinkumaValidationError):
        await plan(
            project_id="p1",
            business_name="Test",
            target_url="not-a-url",
            gmaps_url="https://www.google.com/maps/place/Test/@1,2,17z/data=",
            count=3,
            budget_cap_eur=200,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_then_execute_happy_path(registered_tools, base_url) -> None:
    tools, _ = registered_tools
    plan = tools["linkuma_local_campaign_plan"]
    execute = tools["linkuma_local_campaign_execute"]

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
            return_value=httpx.Response(200, json={"total_eur": 90})
        )
        # plenty of credit
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )
        rsx.get("/orders").mock(return_value=httpx.Response(200, json={"data": []}))

        # Order endpoint returns distinct ids
        order_ids = iter([f"order-{i}" for i in range(10)])
        rsx.post("/carts/order").mock(
            side_effect=lambda req: httpx.Response(
                200, json={"order_id": next(order_ids), "credit_eur": 950}
            )
        )

        plan_res = await plan(
            project_id="p1",
            business_name="Maisons Test",
            target_url="https://maisons-test.fr/",
            gmaps_url="https://www.google.com/maps/place/Maisons+Test/@45,4,17z/data=",
            count=3,
            budget_cap_eur=200,
            thematic_hint="travaux",
        )

        assert plan_res["plan_id"]
        assert len(plan_res["items"]) == 3
        # thematic_hint matched "Travaux"
        assert plan_res["thematic_proposed"]["name"] == "Travaux"
        assert plan_res["confirm_token"].startswith("lkm_cnf_")
        assert plan_res["total_eur"] == 90

        result = await execute(confirm_token=plan_res["confirm_token"])
        assert len(result["orders_created"]) == 3
        assert result["orders_failed"] == []
        assert all(o["order_id"] for o in result["orders_created"])


# ---------------------------------------------------------------------------
# Partial failure: credit drops to zero after the first order.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_stops_when_credit_drops(registered_tools, base_url) -> None:
    tools, _ = registered_tools
    plan = tools["linkuma_local_campaign_plan"]
    execute = tools["linkuma_local_campaign_execute"]

    thematics_payload = {
        "data": [{"id": "th-x", "name": "X", "categories": []}]
    }

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/thematics/premium").mock(
            return_value=httpx.Response(200, json=thematics_payload)
        )
        rsx.post("/carts/price").mock(
            return_value=httpx.Response(200, json={"total_eur": 90})
        )
        rsx.get("/orders").mock(return_value=httpx.Response(200, json={"data": []}))

        # /settings is only called once per item by execute() (no pre-flight
        # call during plan). Sequence: item1 -> 1000 (ok), item2 -> 0 (stop).
        credit_responses = iter([1000, 0, 0, 0, 0, 0, 0])
        rsx.get("/settings").mock(
            side_effect=lambda req: httpx.Response(
                200, json={"credit_eur": next(credit_responses)}
            )
        )
        rsx.post("/carts/order").mock(
            return_value=httpx.Response(200, json={"order_id": "ord-1"})
        )

        plan_res = await plan(
            project_id="p1",
            business_name="Test",
            target_url="https://example.com/",
            gmaps_url="https://www.google.com/maps/place/Test/@1,2,17z/data=",
            count=3,
            budget_cap_eur=200,
        )

        result = await execute(confirm_token=plan_res["confirm_token"])
        assert len(result["orders_created"]) == 1
        assert len(result["orders_failed"]) >= 1
        assert any(
            f["reason"] == "insufficient_credit" for f in result["orders_failed"]
        )


# ---------------------------------------------------------------------------
# Idempotency replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_replays_deduped_items(registered_tools, base_url) -> None:
    tools, _ = registered_tools
    plan = tools["linkuma_local_campaign_plan"]
    execute = tools["linkuma_local_campaign_execute"]

    thematics_payload = {
        "data": [{"id": "th-x", "name": "X", "categories": []}]
    }

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/thematics/premium").mock(
            return_value=httpx.Response(200, json=thematics_payload)
        )
        rsx.post("/carts/price").mock(
            return_value=httpx.Response(200, json={"total_eur": 60})
        )
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )
        rsx.get("/orders").mock(return_value=httpx.Response(200, json={"data": []}))
        rsx.post("/carts/order").mock(
            return_value=httpx.Response(200, json={"order_id": "ord-dedup"})
        )

        plan_res = await plan(
            project_id="p1",
            business_name="Test",
            target_url="https://example.com/",
            gmaps_url="https://www.google.com/maps/place/Test/@1,2,17z/data=",
            count=2,
            budget_cap_eur=200,
        )
        token = plan_res["confirm_token"]
        items = plan_res["items"]

        # Pre-fill the local store as if the first item already shipped.
        idempotency.remember(items[0]["external_ref"], {"order_id": "previous-run"})

        result = await execute(confirm_token=token)
        # First item is deduped from local store, second is freshly ordered.
        deduped = [o for o in result["orders_created"] if o.get("deduped")]
        fresh = [o for o in result["orders_created"] if not o.get("deduped")]
        assert len(deduped) == 1
        assert len(fresh) == 1
        assert deduped[0]["order_id"] == "previous-run"


# ---------------------------------------------------------------------------
# Sanity: tier_mix resolution
# ---------------------------------------------------------------------------


def test_tier_mix_auto_respects_budget() -> None:
    from linkuma_mcp.pricing import resolve_tier_mix

    mix = resolve_tier_mix(10, budget_cap_eur_value=120, mode="auto")
    # at 30 EUR per premium + 10 EUR per standard, total stays <= 120
    assert mix["premium"] * 30 + mix["standard"] * 10 <= 120
    assert mix["premium"] + mix["standard"] == 10


def test_tier_mix_forced_modes() -> None:
    from linkuma_mcp.pricing import resolve_tier_mix

    assert resolve_tier_mix(5, 1000, "boost") == {"premium": 5, "standard": 0}
    assert resolve_tier_mix(5, 1000, "linkuma") == {"premium": 0, "standard": 5}


# ---------------------------------------------------------------------------
# Confirm token expiry
# ---------------------------------------------------------------------------


def test_confirm_token_single_use() -> None:
    from linkuma_mcp.errors import LinkumaConfirmTokenError
    from linkuma_mcp.pricing import consume_confirm_token, issue_confirm_token

    token = issue_confirm_token({"foo": "bar"})
    assert consume_confirm_token(token) == {"foo": "bar"}
    with pytest.raises(LinkumaConfirmTokenError):
        consume_confirm_token(token)
