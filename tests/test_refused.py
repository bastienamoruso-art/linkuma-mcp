"""Tests for the refused-orders analyser."""

from __future__ import annotations

import httpx
import pytest
import respx

from linkuma_mcp.client import LinkumaClient
from linkuma_mcp.tools import refused


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
    refused.register(fake, lambda: client)
    return fake.tools["linkuma_orders_refused_analyze"]


@pytest.mark.asyncio
async def test_detects_anchor_pattern(tool, base_url):
    payload = {
        "data": [
            {"order_id": f"o{i}", "tier": "premium",
             "target_url": "https://example.fr/page",
             "anchor": "exact",
             "refusal_reason": "anchor over-optimisation"}
            for i in range(5)
        ]
    }
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/orders").mock(return_value=httpx.Response(200, json=payload))
        res = await tool()
    assert res["total_refused"] == 5
    assert any("anchor" in p for p in res["common_patterns"])
    assert any("Diversify anchors" in r for r in res["recommendations"])


@pytest.mark.asyncio
async def test_detects_url_hot_spot(tool, base_url):
    payload = {
        "data": [
            {"order_id": f"o{i}", "tier": "standard",
             "target_url": "https://example.fr/badpage",
             "anchor": "x", "refusal_reason": "url not indexable"}
            for i in range(4)
        ]
    }
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/orders").mock(return_value=httpx.Response(200, json=payload))
        res = await tool()
    assert any("Abandon" in r for r in res["recommendations"])
    assert any("Validate target URLs" in r for r in res["recommendations"])


@pytest.mark.asyncio
async def test_no_refusals(tool, base_url):
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/orders").mock(return_value=httpx.Response(200, json={"data": []}))
        res = await tool()
    assert res["total_refused"] == 0
    assert "No refused orders" in res["common_patterns"][0]


@pytest.mark.asyncio
async def test_passes_project_filter(tool, base_url):
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        route = rsx.get("/orders").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        await tool(project_id="p1")
        req = route.calls.last.request
        assert "project_id=p1" in str(req.url)
        assert "status=refused" in str(req.url)
