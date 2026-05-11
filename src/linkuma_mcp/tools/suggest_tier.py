"""linkuma_suggest_tier — pricing intelligence (heuristic-only).

Pure local logic, zero external API. The heuristics encode the rules of thumb
you'd otherwise scribble on a sticky note: which tier matches which context,
how competition shapes the budget, etc.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..checks import is_valid_http_url
from ..client import LinkumaClient
from ..errors import LinkumaValidationError

_VALID_CONTEXTS = {"local", "editorial", "brand"}
_VALID_COMPETITION = {"low", "medium", "high"}
_VALID_GOALS = {"ranking", "traffic", "authority", "diversity"}

# Indicative pricing floors. These match the Linkuma catalogue at v0.2.0.
_TIER_PRICES = {
    "basic": 7.0,        # Starter
    "standard": 10.0,    # Linkuma editorial
    "premium": 30.0,     # Boost
    "citation_linkuma": 10.0,
    "citation_boost": 30.0,
}


def register(mcp: Any, _get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_suggest_tier(
        context: str,
        target_url: str,
        budget_eur: float,
        goal: str = "ranking",
        competition_level: str = "medium",
        account: str | None = None,
    ) -> dict:
        """Recommend a Linkuma tier from context + budget + goal.

        Deterministic, heuristic-only. Does NOT call the Linkuma API.

        Returns:
            {
              "recommended_tier": "basic"|"standard"|"premium"|"citation_linkuma"|"citation_boost",
              "reasoning": str,
              "alternatives": [{"tier": str, "reason": str}],
              "expected_cost_eur": float
            }
        """
        _ = account  # accepted for API symmetry, unused here
        if context not in _VALID_CONTEXTS:
            raise LinkumaValidationError(
                f"context must be one of {sorted(_VALID_CONTEXTS)}; got `{context}`"
            )
        if competition_level not in _VALID_COMPETITION:
            raise LinkumaValidationError(
                f"competition_level must be one of {sorted(_VALID_COMPETITION)}; "
                f"got `{competition_level}`"
            )
        if goal not in _VALID_GOALS:
            raise LinkumaValidationError(
                f"goal must be one of {sorted(_VALID_GOALS)}; got `{goal}`"
            )
        if budget_eur <= 0:
            raise LinkumaValidationError("budget_eur must be > 0")
        if not is_valid_http_url(target_url):
            raise LinkumaValidationError(
                f"target_url `{target_url}` is not a valid http(s) URL"
            )

        recommended, reasoning, alternatives = _recommend(
            context=context,
            budget_eur=budget_eur,
            goal=goal,
            competition_level=competition_level,
        )

        return {
            "recommended_tier": recommended,
            "reasoning": reasoning,
            "alternatives": alternatives,
            "expected_cost_eur": _TIER_PRICES.get(recommended, 0.0),
        }


# ---------------------------------------------------------------------------
# Core heuristic
# ---------------------------------------------------------------------------


def _recommend(
    *,
    context: str,
    budget_eur: float,
    goal: str,
    competition_level: str,
) -> tuple[str, str, list[dict]]:
    """Return (recommended_tier, reasoning, alternatives)."""
    if context == "local":
        if budget_eur >= 30:
            return (
                "citation_boost",
                "Local context with a budget that covers a premium citation "
                "(>=30 EUR). Boost citations include secondary trust signals "
                "and rank better in the local pack.",
                [
                    {
                        "tier": "citation_linkuma",
                        "reason": "if you want to spread the budget over more "
                        "citations (10 EUR each) at the cost of weaker signals",
                    }
                ],
            )
        return (
            "citation_linkuma",
            "Local context with a tight budget (<30 EUR). Linkuma citations at "
            "10 EUR each let you build volume before stepping up to Boost.",
            [
                {
                    "tier": "citation_boost",
                    "reason": "switch when budget reaches >=30 EUR/link for "
                    "stronger trust signals",
                }
            ],
        )

    if context == "brand":
        if budget_eur >= 200:
            return (
                "premium",
                "Brand-building with a large budget (>=200 EUR). Premium "
                "editorials carry the strongest signal-per-link for brand "
                "authority and lasting indexation.",
                [
                    {"tier": "basic", "reason": "for additional volume at low cost"},
                    {"tier": "standard", "reason": "balanced quality / volume mix"},
                ],
            )
        return (
            "basic",
            "Brand-building with a moderate budget. Basic links maximise volume "
            "(7 EUR each) — the goal here is breadth of mentions, not "
            "competition-grade authority.",
            [
                {
                    "tier": "standard",
                    "reason": "for slightly better referring-domain quality",
                }
            ],
        )

    # ---- editorial
    if goal == "diversity":
        return (
            "standard",
            "Editorial with a diversity goal: standard is the baseline, but you "
            "should mix several tiers. See alternatives.",
            [
                {"tier": "premium", "reason": "30% of the mix for strongest links"},
                {"tier": "basic", "reason": "30% of the mix for volume"},
            ],
        )

    if competition_level == "high":
        return (
            "premium",
            "Editorial in a high-competition niche: premium links are the only "
            "tier that consistently moves the needle on saturated SERPs.",
            [
                {
                    "tier": "standard",
                    "reason": "add 1 standard for every 2 premium to keep the "
                    "anchor profile diverse",
                }
            ],
        )

    if competition_level == "low":
        if budget_eur >= 30:
            return (
                "standard",
                "Editorial in a low-competition niche with sufficient budget. "
                "Standard offers a strong cost-to-signal ratio.",
                [
                    {"tier": "basic", "reason": "for additional volume"},
                ],
            )
        return (
            "basic",
            "Editorial in a low-competition niche with a tight budget. Basic "
            "links suffice to move the needle.",
            [
                {"tier": "standard", "reason": "step up when budget allows"},
            ],
        )

    # medium competition
    if budget_eur >= 30:
        return (
            "standard",
            "Editorial in a medium-competition niche: standard hits the sweet "
            "spot between authority and volume.",
            [
                {"tier": "premium", "reason": "for keywords that won't move"},
                {"tier": "basic", "reason": "for additional volume"},
            ],
        )
    return (
        "basic",
        "Editorial in a medium-competition niche with a tight budget. Basic "
        "links to start; revisit once you measure traction.",
        [
            {"tier": "standard", "reason": "step up when budget allows"},
        ],
    )
