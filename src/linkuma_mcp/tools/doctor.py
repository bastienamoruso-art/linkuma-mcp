"""linkuma_doctor — health check + onboarding quickstart."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from ..client import LinkumaClient
from ..errors import LinkumaAuthError, LinkumaUnreachable

QUICKSTART_MD = """\
## Linkuma MCP — quickstart

1. **List projects** — call `linkuma_projects_list`.
2. **Pick or create one** — `linkuma_projects_create(name, target_domain)`
   is idempotent on the exact `name`.
3. **Price a single link** — `linkuma_cart_price(project_id, items=[...])`.
   The response carries a `confirm_token` valid for 10 minutes.
4. **Place the order** — `linkuma_cart_order(..., confirm_token=...,
   external_ref="my-ref")`. Replays with the same `external_ref` are no-ops.
5. **Local citation campaign** — `linkuma_local_campaign_plan(...)` returns
   a dry-run plan; `linkuma_local_campaign_execute(plan_id, confirm_token)`
   commits it.

Safety defaults you can rely on:
- Hard budget cap via `LINKUMA_BUDGET_CAP_EUR` (default 500 EUR)
- Client-side rate limit `LINKUMA_RATE_LIMIT_RPS` (default 2)
- Zero auto-retry on `POST /carts/order` (you get `LinkumaOrderUncertain`)
- Local idempotency cache at `~/.linkuma-mcp/idempotency.json`
"""


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_doctor() -> dict:
        """Verify API key, return credit and a Markdown quickstart.

        Use this as the FIRST call in any session. It tells you whether the
        key works, how much credit you have, and how to drive the rest of
        the toolkit.
        """
        threshold = float(os.getenv("LINKUMA_LOW_CREDIT_THRESHOLD_EUR", "50") or 50)
        try:
            client = get_client()
            settings = await client.get_settings()
        except LinkumaAuthError as exc:
            return {
                "api_key_valid": False,
                "error": exc.message,
                "quickstart_md": QUICKSTART_MD,
            }
        except LinkumaUnreachable as exc:
            return {
                "api_key_valid": False,
                "error": f"unreachable: {exc.message}",
                "quickstart_md": QUICKSTART_MD,
            }

        credit = _extract_credit(settings)
        projects = await client.list_projects()
        recent_orders = await _safe_count_recent_orders(client)

        return {
            "api_key_valid": True,
            "credit_eur": credit,
            "low_credit_warning": credit < threshold,
            "low_credit_threshold_eur": threshold,
            "projects_count": len(projects),
            "recent_orders_30d": recent_orders,
            "quickstart_md": QUICKSTART_MD,
        }


def _extract_credit(settings: Any) -> float:
    if not isinstance(settings, dict):
        return 0.0
    if isinstance(settings.get("data"), dict):
        settings = settings["data"]
    for key in ("credit_eur", "credit", "balance", "balance_eur"):
        v = settings.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return 0.0


async def _safe_count_recent_orders(client: LinkumaClient) -> int:
    try:
        orders = await client.list_orders(params={"limit": 100})
    except Exception:
        return 0
    return len(orders)
