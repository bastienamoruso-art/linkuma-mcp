"""Tests for the CSV export tool."""

from __future__ import annotations

import httpx
import pytest
import respx

from linkuma_mcp.client import LinkumaClient
from linkuma_mcp.errors import LinkumaValidationError
from linkuma_mcp.tools import export


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
    export.register(fake, lambda: client)
    return fake.tools["linkuma_export_orders"], client


@pytest.mark.asyncio
async def test_export_inline_csv(tool, base_url):
    t, _ = tool
    orders_payload = {
        "data": [
            {
                "order_id": "ord-1",
                "external_ref": "ref-1",
                "project_id": "p1",
                "status": "published",
                "tier": "premium",
                "target_url": "https://example.fr/page",
                "anchor": "courtier orleans",
                "pagekw": "courtier immobilier orleans",
                "thematic_id": "th-x",
                "published_url": "https://blog.fr/post",
                "price_eur": 30,
                "created_at": "2026-05-01",
            },
            {
                "order_id": "ord-2",
                "external_ref": "ref-2",
                "project_id": "p1",
                "status": "refused",
                "tier": "standard",
                "target_url": "https://example.fr/page",
                "anchor": "exact match",
                "refusal_reason": "anchor over-optimisation",
                "price_eur": "10",
            },
        ]
    }
    projects_payload = [{"id": "p1", "name": "Acme Site"}]

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/orders").mock(return_value=httpx.Response(200, json=orders_payload))
        rsx.get("/projects").mock(return_value=httpx.Response(200, json=projects_payload))

        res = await t(project_id="p1", since="2026-01-01")

    assert res["rows_count"] == 2
    assert "csv_content" in res
    lines = res["csv_content"].strip().splitlines()
    assert lines[0].startswith("order_id,external_ref,project_id,project_name,")
    assert "Acme Site" in lines[1]
    assert "30.00" in lines[1]
    assert "anchor over-optimisation" in lines[2]


@pytest.mark.asyncio
async def test_export_to_file(tool, base_url, tmp_path):
    t, _ = tool
    out = tmp_path / "orders.csv"

    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/orders").mock(return_value=httpx.Response(200, json={"data": []}))
        rsx.get("/projects").mock(return_value=httpx.Response(200, json=[]))

        res = await t(output_path=str(out))

    assert res["output_path"] == str(out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert content.startswith("order_id,external_ref")
    assert res["rows_count"] == 0


@pytest.mark.asyncio
async def test_export_rejects_non_csv_format(tool):
    t, _ = tool
    with pytest.raises(LinkumaValidationError):
        await t(format="sheet")


@pytest.mark.asyncio
async def test_export_handles_missing_project_name(tool, base_url):
    t, _ = tool
    orders_payload = {
        "data": [
            {
                "order_id": "o1",
                "project_id": "ghost",
                "tier": "basic",
                "status": "published",
            }
        ]
    }
    with respx.mock(base_url=base_url, assert_all_called=False) as rsx:
        rsx.get("/orders").mock(return_value=httpx.Response(200, json=orders_payload))
        rsx.get("/projects").mock(return_value=httpx.Response(200, json=[]))

        res = await t()
    assert res["rows_count"] == 1
    # project_name column is present but empty
    assert "ghost" in res["csv_content"]
