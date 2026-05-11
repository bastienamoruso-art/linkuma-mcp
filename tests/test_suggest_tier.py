"""Tests for the heuristic suggest_tier tool."""

from __future__ import annotations

import pytest

from linkuma_mcp.client import LinkumaClient
from linkuma_mcp.errors import LinkumaValidationError
from linkuma_mcp.tools import suggest_tier


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
    suggest_tier.register(fake, lambda: LinkumaClient())
    return fake.tools["linkuma_suggest_tier"]


@pytest.mark.asyncio
async def test_local_low_budget_recommends_citation_linkuma(tool):
    res = await tool(
        context="local",
        target_url="https://www.example.fr/",
        budget_eur=15,
        goal="ranking",
    )
    assert res["recommended_tier"] == "citation_linkuma"


@pytest.mark.asyncio
async def test_local_high_budget_recommends_citation_boost(tool):
    res = await tool(
        context="local",
        target_url="https://www.example.fr/",
        budget_eur=200,
        goal="ranking",
    )
    assert res["recommended_tier"] == "citation_boost"


@pytest.mark.asyncio
async def test_editorial_high_competition_recommends_premium(tool):
    res = await tool(
        context="editorial",
        target_url="https://www.example.fr/",
        budget_eur=300,
        competition_level="high",
    )
    assert res["recommended_tier"] == "premium"
    assert res["expected_cost_eur"] == 30.0


@pytest.mark.asyncio
async def test_editorial_low_competition_low_budget_basic(tool):
    res = await tool(
        context="editorial",
        target_url="https://www.example.fr/",
        budget_eur=20,
        competition_level="low",
    )
    assert res["recommended_tier"] == "basic"


@pytest.mark.asyncio
async def test_brand_low_budget_basic(tool):
    res = await tool(
        context="brand",
        target_url="https://www.example.fr/",
        budget_eur=50,
    )
    assert res["recommended_tier"] == "basic"


@pytest.mark.asyncio
async def test_brand_high_budget_premium(tool):
    res = await tool(
        context="brand",
        target_url="https://www.example.fr/",
        budget_eur=500,
    )
    assert res["recommended_tier"] == "premium"


@pytest.mark.asyncio
async def test_diversity_returns_mix_alternatives(tool):
    res = await tool(
        context="editorial",
        target_url="https://www.example.fr/",
        budget_eur=200,
        goal="diversity",
    )
    assert res["recommended_tier"] == "standard"
    tiers_in_alternatives = {a["tier"] for a in res["alternatives"]}
    assert {"premium", "basic"}.issubset(tiers_in_alternatives)


@pytest.mark.asyncio
async def test_invalid_context_raises(tool):
    with pytest.raises(LinkumaValidationError):
        await tool(
            context="weird",
            target_url="https://www.example.fr/",
            budget_eur=100,
        )


@pytest.mark.asyncio
async def test_invalid_url_raises(tool):
    with pytest.raises(LinkumaValidationError):
        await tool(
            context="editorial",
            target_url="ftp://nope",
            budget_eur=100,
        )


@pytest.mark.asyncio
async def test_invalid_budget_raises(tool):
    with pytest.raises(LinkumaValidationError):
        await tool(
            context="editorial",
            target_url="https://x.fr/",
            budget_eur=0,
        )
