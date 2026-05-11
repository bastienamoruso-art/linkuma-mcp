"""Tests for the dashboard aggregator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from linkuma_mcp.client import LinkumaClient
from linkuma_mcp.tools import dashboard


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn
        return wrap


@pytest.fixture
def tool(base_url):
    fake = _FakeMCP()
    client = LinkumaClient()
    dashboard.register(fake, lambda: client)
    return fake.tools["linkuma_dashboard"]


@pytest.mark.asyncio
async def test_dashboard_aggregates_by_project_and_tier(tool, base_url):
    orders_payload = {
        "data": [
            {"order_id": "o1", "project_id": "p1", "tier": "premium",
             "status": "published", "price_eur": 30},
            {"order_id": "o2", "project_id": "p1", "tier": "premium",
             "status": "published", "price_eur": 30},
            {"order_id": "o3", "project_id": "p2", "tier": "standard",
             "status": "refused", "price_eur": 10},
        ]
    }
    projects_payload = [
        {"id": "p1", "name": "Site A"},
        {"id": "p2", "name": "Site B"},
    ]
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 500})
        )
        rsx.get("/orders").mock(return_value=httpx.Response(200, json=orders_payload))
        rsx.get("/projects").mock(
            return_value=httpx.Response(200, json=projects_payload)
        )

        res = await tool()

    assert res["orders_total"] == 3
    assert res["credit_remaining_eur"] == 500
    assert res["credit_consumed_period_eur"] == 70.0
    by_project = {row["project_id"]: row for row in res["by_project"]}
    assert by_project["p1"]["orders_count"] == 2
    assert by_project["p1"]["total_spent_eur"] == 60.0
    assert res["by_tier"]["premium"] == 2
    assert res["by_status"]["published"] == 2


@pytest.mark.asyncio
async def test_dashboard_refusal_rate_alert(tool, base_url):
    # 5 orders, 2 refused -> 40% rate -> alert
    orders_payload = {
        "data": [
            {"order_id": f"o{i}", "project_id": "p1", "tier": "premium",
             "status": "refused" if i < 2 else "published", "price_eur": 30}
            for i in range(5)
        ]
    }
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )
        rsx.get("/orders").mock(return_value=httpx.Response(200, json=orders_payload))
        rsx.get("/projects").mock(
            return_value=httpx.Response(200, json=[{"id": "p1", "name": "Site A"}])
        )
        res = await tool()
    refusal_alerts = [a for a in res["alerts"] if a["type"] == "refusal_rate_high"]
    assert refusal_alerts


@pytest.mark.asyncio
async def test_dashboard_stale_pending_alert(tool, base_url):
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    orders_payload = {
        "data": [
            {"order_id": "o1", "project_id": "p1", "tier": "premium",
             "status": "pending_validation", "created_at": old, "price_eur": 30}
        ]
    }
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/settings").mock(
            return_value=httpx.Response(200, json={"credit_eur": 1000})
        )
        rsx.get("/orders").mock(return_value=httpx.Response(200, json=orders_payload))
        rsx.get("/projects").mock(return_value=httpx.Response(200, json=[]))
        res = await tool()
    assert any(a["type"] == "stale_pending" for a in res["alerts"])


@pytest.mark.asyncio
async def test_dashboard_scope_project_requires_id(tool):
    from linkuma_mcp.errors import LinkumaValidationError
    with pytest.raises(LinkumaValidationError):
        await tool(scope="project")


@pytest.mark.asyncio
async def test_dashboard_since_shorthand_resolves(tool, base_url):
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/settings").mock(return_value=httpx.Response(200, json={"credit_eur": 0}))
        rsx.get("/orders").mock(return_value=httpx.Response(200, json={"data": []}))
        rsx.get("/projects").mock(return_value=httpx.Response(200, json=[]))
        res = await tool(since="90d_ago")
    # YYYY-MM-DD shape
    assert len(res["since"]) == 10
    assert res["since"][4] == "-"
