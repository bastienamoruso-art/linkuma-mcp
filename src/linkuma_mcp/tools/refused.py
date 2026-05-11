"""linkuma_orders_refused_analyze — pattern detection on refused orders.

Pure local logic over the Linkuma API. Surfaces:
- the most common refusal reasons
- the URLs / tiers concentrated in refusals
- concrete recommendations to reduce the refusal rate
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..accounts import get_client as get_account_client
from ..client import LinkumaClient

# Tokens that hint at specific root causes. Match-by-substring (lowercased).
_ANCHOR_HINTS = {"anchor", "ancre", "over-optim", "sur-optim", "exact"}
_URL_HINTS = {"url", "indexab", "404", "redirect", "robots", "noindex", "dns"}
_CONTENT_HINTS = {"content", "contenu", "thin", "thematique", "thematic"}


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_orders_refused_analyze(
        project_id: str | None = None,
        since: str = "90d_ago",
        account: str | None = None,
    ) -> dict:
        """Analyse refused orders to surface patterns and recommendations.

        Returns counts by reason / URL / tier plus a set of human-readable
        recommendations ranked by impact. Pure local heuristics — no LLM, no
        external API.
        """
        since_iso = _resolve_since(since)
        client = get_account_client(account) if account is not None else get_client()

        params: dict[str, Any] = {
            "limit": 500,
            "status": "refused",
            "since": since_iso,
        }
        # Enrich orders with project_id via /projects (Linkuma /carts orders
        # don't carry project_id directly). We pre-load the lookup before
        # filtering so the project_id filter actually applies.
        order_to_project: dict[str, str] = {}
        if project_id:
            try:
                projects = await client.list_projects()
                for p in projects:
                    pid = p.get("id")
                    for o in p.get("orders") or []:
                        oid = o.get("id") or o.get("order_id")
                        if oid and pid:
                            order_to_project[str(oid)] = str(pid)
            except Exception:  # pragma: no cover - defensive
                pass

        orders = await client.list_orders(params={"limit": 500, "status": "refused", "since": since_iso})
        if project_id:
            # Back-fill project_id then filter.
            for o in orders:
                if not o.get("project_id"):
                    oid = o.get("order_id") or o.get("id")
                    if oid and str(oid) in order_to_project:
                        o["project_id"] = order_to_project[str(oid)]
            orders = [o for o in orders if str(o.get("project_id") or "") == str(project_id)]

        by_reason: Counter = Counter()
        by_url: Counter = Counter()
        by_tier: Counter = Counter()
        reason_buckets: Counter = Counter()  # anchor / url / content / other

        for o in orders:
            reason = (o.get("refusal_reason") or "").strip()
            url = (o.get("target_url") or "").strip()
            tier = (o.get("tier") or "").strip()
            if reason:
                by_reason[reason] += 1
                reason_buckets[_categorise_reason(reason)] += 1
            if url:
                by_url[url] += 1
            if tier:
                by_tier[tier] += 1

        total = len(orders)
        common_patterns = _format_patterns(by_reason, by_url, by_tier, total)
        recommendations = _recommendations(reason_buckets, by_url, total)

        return {
            "total_refused": total,
            "since": since_iso,
            "by_reason": dict(by_reason.most_common(10)),
            "by_url": dict(by_url.most_common(10)),
            "by_tier": dict(by_tier),
            "common_patterns": common_patterns,
            "recommendations": recommendations,
        }


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


def _categorise_reason(reason: str) -> str:
    low = reason.lower()
    if any(h in low for h in _ANCHOR_HINTS):
        return "anchor"
    if any(h in low for h in _URL_HINTS):
        return "url"
    if any(h in low for h in _CONTENT_HINTS):
        return "content"
    return "other"


def _format_patterns(
    by_reason: Counter, by_url: Counter, by_tier: Counter, total: int
) -> list[str]:
    patterns: list[str] = []
    if not total:
        return ["No refused orders in the analysed window."]

    top_reason = by_reason.most_common(1)
    if top_reason:
        reason, count = top_reason[0]
        share = count / total
        patterns.append(
            f"`{reason}` accounts for {share * 100:.0f}% of refusals ({count}/{total})"
        )

    top_url = by_url.most_common(1)
    if top_url:
        url, count = top_url[0]
        share = count / total
        if share >= 0.3:
            patterns.append(
                f"URL `{url}` concentrates {share * 100:.0f}% of refusals "
                f"({count}/{total}) — consider abandoning it"
            )

    top_tier = by_tier.most_common(1)
    if top_tier:
        tier, count = top_tier[0]
        share = count / total
        if share >= 0.6:
            patterns.append(
                f"Tier `{tier}` over-represented in refusals ({share * 100:.0f}%)"
            )

    return patterns


def _recommendations(
    reason_buckets: Counter, by_url: Counter, total: int
) -> list[str]:
    recs: list[str] = []
    if not total:
        return recs

    if reason_buckets["anchor"] >= max(1, total * 0.25):
        recs.append(
            "Diversify anchors — over-optimised anchors are driving "
            f"{reason_buckets['anchor']}/{total} refusals. Use the `mixed` "
            "anchor strategy in `linkuma_editorial_campaign_plan`."
        )

    if reason_buckets["url"] >= max(1, total * 0.25):
        recs.append(
            "Validate target URLs before ordering — "
            f"{reason_buckets['url']}/{total} refusals look URL-related. "
            "Confirm 200 status, noindex absent, and canonical alignment."
        )

    if reason_buckets["content"] >= max(1, total * 0.25):
        recs.append(
            "Review thematic + briefing — "
            f"{reason_buckets['content']}/{total} refusals look content-related. "
            "Make sure the thematic_id matches the target URL's topic."
        )

    # URL hot-spot — same URL refused more than 3 times.
    for url, count in by_url.most_common(3):
        if count >= 3 and count / total >= 0.3:
            recs.append(
                f"Abandon `{url}` as a target — it has accumulated {count} "
                "refusals. Switch to a fresh URL with cleaner on-page signals."
            )

    if not recs:
        recs.append(
            "No dominant pattern detected — keep monitoring refusals via "
            "`linkuma_dashboard` and re-run this analysis monthly."
        )
    return recs


def _resolve_since(since: str) -> str:
    raw = (since or "").strip().lower()
    if not raw:
        return _ago_days(90)
    if raw.endswith("d_ago"):
        try:
            n = int(raw.split("d_ago")[0])
            return _ago_days(n)
        except ValueError:
            return _ago_days(90)
    if raw.endswith("w_ago"):
        try:
            n = int(raw.split("w_ago")[0])
            return _ago_days(n * 7)
        except ValueError:
            return _ago_days(90)
    return since


def _ago_days(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
