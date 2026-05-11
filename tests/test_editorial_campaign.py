"""Tests for the editorial campaign tools."""

from __future__ import annotations

import httpx
import pytest
import respx

from linkuma_mcp import idempotency
from linkuma_mcp.client import LinkumaClient
from linkuma_mcp.errors import LinkumaValidationError
from linkuma_mcp.tools import editorial_campaign


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
    editorial_campaign.register(fake, lambda: client)
    return fake.tools, client


@pytest.mark.asyncio
async def test_plan_rejects_invalid_target_url(registered):
    tools, _ = registered
    with pytest.raises(LinkumaValidationError):
        await tools["linkuma_editorial_campaign_plan"](
            project_id="p1",
            target_url="not-a-url",
            count=3,
            budget_cap_eur=100,
            keywords=["x"],
        )


@pytest.mark.asyncio
async def test_plan_rejects_empty_keywords(registered):
    tools, _ = registered
    with pytest.raises(LinkumaValidationError):
        await tools["linkuma_editorial_campaign_plan"](
            project_id="p1",
            target_url="https://example.fr/",
            count=3,
            budget_cap_eur=100,
            keywords=[],
        )


@pytest.mark.asyncio
async def test_plan_then_execute_happy_path(registered, base_url):
    tools, _ = registered
    plan = tools["linkuma_editorial_campaign_plan"]
    execute = tools["linkuma_editorial_campaign_execute"]

    thematics_payload = {
        "data": [
            {
                "id": "th-finance",
                "name": "Finance",
                "categories": [{"id": "th-pret", "name": "Pret immobilier"}],
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
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )

        order_ids = iter([f"ord-{i}" for i in range(10)])
        rsx.post("/carts/order").mock(
            side_effect=lambda req: httpx.Response(
                200, json={"order_id": next(order_ids), "credit_eur": 950}
            )
        )

        plan_res = await plan(
            project_id="p1",
            target_url="https://example.fr/services/courtier",
            count=3,
            budget_cap_eur=200,
            keywords=["pret immobilier orleans", "courtier"],
            tier="auto",
            anchor_strategy="mixed",
        )
        assert len(plan_res["items"]) == 3
        assert plan_res["anchor_distribution"]["branded"] + \
               plan_res["anchor_distribution"]["exact"] + \
               plan_res["anchor_distribution"]["semantic"] == 3
        assert plan_res["confirm_token"].startswith("lkm_cnf_")
        # Thematic match on the keyword "pret"
        assert plan_res["thematic_proposed"]["name"] in {"Pret immobilier", "Finance"}

        result = await execute(confirm_token=plan_res["confirm_token"])
        assert len(result["orders_created"]) == 3
        assert result["orders_failed"] == []


@pytest.mark.asyncio
async def test_plan_thematic_override(registered, base_url):
    tools, _ = registered
    plan = tools["linkuma_editorial_campaign_plan"]

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.post("/carts/price").mock(
            return_value=httpx.Response(200, json={"total_eur": 30})
        )
        # No /thematics call expected because thematic_id is overridden.

        plan_res = await plan(
            project_id="p1",
            target_url="https://example.fr/",
            count=1,
            budget_cap_eur=50,
            keywords=["x"],
            thematic_id="th-custom",
            anchor_strategy="branded",
        )
    assert plan_res["thematic_proposed"]["id"] == "th-custom"
    assert plan_res["thematic_proposed"]["source"] == "user"


@pytest.mark.asyncio
async def test_execute_replays_deduped(registered, base_url):
    tools, _ = registered
    plan = tools["linkuma_editorial_campaign_plan"]
    execute = tools["linkuma_editorial_campaign_execute"]

    thematics_payload = {"data": [{"id": "th-x", "name": "X", "categories": []}]}

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
        rsx.post("/carts/order").mock(
            return_value=httpx.Response(200, json={"order_id": "fresh-ord"})
        )

        plan_res = await plan(
            project_id="p1",
            target_url="https://example.fr/",
            count=2,
            budget_cap_eur=200,
            keywords=["x"],
            anchor_strategy="branded",
        )
        # Pre-fill the local store for the first item
        idempotency.remember(plan_res["items"][0]["external_ref"], {"order_id": "prev-run"})

        result = await execute(confirm_token=plan_res["confirm_token"])
        deduped = [o for o in result["orders_created"] if o.get("deduped")]
        fresh = [o for o in result["orders_created"] if not o.get("deduped")]
        assert len(deduped) == 1
        assert len(fresh) == 1
