"""linkuma_dashboard — cross-project aggregated view.

Computes everything client-side from a `list_orders` + `list_projects` call
plus a `get_settings` for the credit. No external dependency.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..accounts import get_client as get_account_client
from ..client import LinkumaClient
from ..errors import LinkumaValidationError

_VALID_SCOPES = {"all", "project"}
_REFUSAL_RATE_ALERT = 0.20
_PENDING_DAYS_ALERT = 14
_LOW_CREDIT_THRESHOLD_RATIO = 0.1  # alert when remaining < 10% of period spend


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_dashboard(
        scope: str = "all",
        project_id: str | None = None,
        since: str = "30d_ago",
        account: str | None = None,
    ) -> dict:
        """Aggregate cross-project KPIs from Linkuma.

        - `scope="all"` (default) covers every project on the account.
        - `scope="project"` requires `project_id`.
        - `since` ISO8601 OR shorthand `30d_ago` / `90d_ago` (default `30d_ago`).

        Returns credit, spend, breakdown by project / tier / status, and a
        list of automatic alerts (low credit, refusal hot-spot, stale orders).
        """
        if scope not in _VALID_SCOPES:
            raise LinkumaValidationError(
                f"scope must be one of {sorted(_VALID_SCOPES)}; got `{scope}`"
            )
        if scope == "project" and not project_id:
            raise LinkumaValidationError("scope='project' requires project_id")

        since_iso = _resolve_since(since)
        client = get_account_client(account) if account is not None else get_client()

        # Credit snapshot
        credit_remaining = 0.0
        try:
            settings = await client.get_settings()
            credit_remaining = _extract_credit(settings)
        except Exception:  # pragma: no cover - non-blocking
            pass

        # Orders for the period
        params: dict[str, Any] = {"limit": 500, "since": since_iso}
        if scope == "project":
            params["project_id"] = project_id
        orders = await client.list_orders(params=params)

        # Project name map
        project_names: dict[str, str] = {}
        try:
            projects = await client.list_projects()
            for p in projects:
                pid = p.get("id")
                if pid:
                    project_names[str(pid)] = p.get("name") or ""
        except Exception:  # pragma: no cover - non-blocking
            pass

        # ---- aggregate
        by_project: dict[str, dict[str, Any]] = {}
        by_tier: Counter = Counter()
        by_status: Counter = Counter()
        total_period_spend = 0.0

        for o in orders:
            pid = str(o.get("project_id") or "")
            tier = (o.get("tier") or "").lower()
            status = (o.get("status") or "").lower()
            price = _coerce_price(o)

            slot = by_project.setdefault(
                pid,
                {
                    "project_id": pid,
                    "name": project_names.get(pid, ""),
                    "orders_count": 0,
                    "total_spent_eur": 0.0,
                    "by_status": Counter(),
                    "refused": 0,
                },
            )
            slot["orders_count"] += 1
            slot["total_spent_eur"] += price
            slot["by_status"][status] += 1
            if status == "refused":
                slot["refused"] += 1

            by_tier[tier] += 1
            by_status[status] += 1
            total_period_spend += price

        # Materialise by_project + compute refusal_rate per project
        project_rows: list[dict[str, Any]] = []
        alerts: list[dict[str, str]] = []
        for pid, slot in by_project.items():
            refused = slot["refused"]
            orders_count = slot["orders_count"]
            refusal_rate = (refused / orders_count) if orders_count else 0.0
            project_rows.append(
                {
                    "project_id": pid,
                    "name": slot["name"],
                    "orders_count": orders_count,
                    "total_spent_eur": round(slot["total_spent_eur"], 2),
                    "by_status": dict(slot["by_status"]),
                    "refusal_rate": round(refusal_rate, 3),
                }
            )
            if refusal_rate > _REFUSAL_RATE_ALERT and orders_count >= 5:
                alerts.append(
                    {
                        "type": "refusal_rate_high",
                        "message": f"Project `{slot['name'] or pid}` has a "
                        f"{refusal_rate * 100:.0f}% refusal rate over {orders_count} "
                        "orders — investigate target URLs and anchor strategy",
                    }
                )

        # Stale-pending alert: anything in pending_validation for > 14 days
        cutoff = datetime.now(UTC) - timedelta(days=_PENDING_DAYS_ALERT)
        stale_pending = 0
        for o in orders:
            if (o.get("status") or "").lower() == "pending_validation":
                created = _parse_iso(o.get("created_at"))
                if created and created < cutoff:
                    stale_pending += 1
        if stale_pending:
            alerts.append(
                {
                    "type": "stale_pending",
                    "message": f"{stale_pending} order(s) stuck in pending_validation "
                    f"for >{_PENDING_DAYS_ALERT} days — contact Linkuma support",
                }
            )

        # Low credit alert
        if total_period_spend > 0 and credit_remaining < (
            total_period_spend * _LOW_CREDIT_THRESHOLD_RATIO
        ):
            alerts.append(
                {
                    "type": "low_credit",
                    "message": f"Credit ({credit_remaining:.2f} EUR) is under "
                    f"10% of last-period spend ({total_period_spend:.2f} EUR) — top up soon",
                }
            )

        return {
            "scope": scope,
            "since": since_iso,
            "credit_remaining_eur": round(credit_remaining, 2),
            "credit_consumed_period_eur": round(total_period_spend, 2),
            "orders_total": len(orders),
            "by_project": sorted(
                project_rows, key=lambda r: r["total_spent_eur"], reverse=True
            ),
            "by_tier": dict(by_tier),
            "by_status": dict(by_status),
            "alerts": alerts,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_since(since: str) -> str:
    """Accept ISO8601 or shorthand `Nd_ago`/`Nw_ago`."""
    raw = (since or "").strip().lower()
    if not raw:
        return _ago_days(30)
    if raw.endswith("d_ago"):
        try:
            n = int(raw.split("d_ago")[0])
            return _ago_days(n)
        except ValueError:
            return _ago_days(30)
    if raw.endswith("w_ago"):
        try:
            n = int(raw.split("w_ago")[0])
            return _ago_days(n * 7)
        except ValueError:
            return _ago_days(30)
    return since


def _ago_days(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


def _coerce_price(order: dict) -> float:
    for key in ("price_eur", "price", "amount_eur", "amount", "total_eur"):
        v = order.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return 0.0


def _extract_credit(payload: Any) -> float:
    if not isinstance(payload, dict):
        return 0.0
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    for key in ("credit_eur", "credit", "balance", "balance_eur"):
        v = payload.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # Accept both `YYYY-MM-DD` and full ISO with timezone.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None
