"""linkuma_local_campaign_plan / linkuma_local_campaign_execute.

Local citation workflow with dry-run, tier_mix, gmaps URL validation,
and per-item idempotency (`{plan_id}-{idx}`).

v0.3.0 — items are emitted in the real Linkuma cart schema:
    type ∈ {citation_boost, citation_linkuma}
    url, map, project_id, thematic_id, category_id, qty, anchor, anchor_value,
    distribution, started_at, fast_publication, brief.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import idempotency
from ..checks import (
    is_valid_gmaps_url,
    is_valid_http_url,
)
from ..client import LinkumaClient
from ..errors import (
    LinkumaBudgetExceeded,
    LinkumaInsufficientCredit,
    LinkumaOrderUncertain,
    LinkumaValidationError,
)
from ..pricing import (
    budget_cap_eur,
    consume_confirm_token,
    enforce_budget,
    issue_confirm_token,
    resolve_tier_mix,
)
from ..thematics import fetch_thematics
from .cart import (
    _detect_overopt,
    build_order_body,
    build_price_body,
)

logger = logging.getLogger("linkuma_mcp")

_VALID_TIER_MIX = {"auto", "boost", "linkuma"}

# Map our internal "premium"/"standard" tier knobs onto Linkuma citation `type`.
_TIER_TO_TYPE = {
    "premium": "citation_boost",
    "standard": "citation_linkuma",
}


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_local_campaign_plan(
        project_id: str,
        business_name: str,
        target_url: str,
        gmaps_url: str,
        count: int,
        budget_cap_eur: float,
        anchors: list[str] | None = None,
        tier_mix: str = "auto",
        thematic_hint: str | None = None,
        category_id: str | None = None,
        language: str = "fr",
        spread_days: int = 30,
    ) -> dict:
        """Build a dry-run citation campaign plan.

        Returns the proposed items, a thematic recommendation with up to 3
        alternatives, the projected total, warnings, and a single-use
        `confirm_token` to feed to `linkuma_local_campaign_execute`.

        Nothing is sent to Linkuma at this stage beyond a /thematics read and
        a /carts/price call. The plan is held in memory and expires with
        its confirm_token (10 minutes).
        """
        _validate_plan_inputs(
            project_id=project_id,
            business_name=business_name,
            target_url=target_url,
            gmaps_url=gmaps_url,
            count=count,
            budget_cap_eur=budget_cap_eur,
            tier_mix=tier_mix,
        )

        client = get_client()
        anchors = anchors or [business_name]
        anchors = [a.strip() for a in anchors if a and a.strip()]
        if not anchors:
            anchors = [business_name.strip()]

        # ---- resolve tier mix
        mix = resolve_tier_mix(count, budget_cap_eur, tier_mix)
        tiers_sequence: list[str] = (["premium"] * mix["premium"]) + (
            ["standard"] * mix["standard"]
        )

        # ---- pick thematics
        thematic_proposed, alternatives, thematic_warnings = await _propose_thematic(
            client, tiers_sequence, thematic_hint, language
        )

        # If caller didn't pass category_id but the proposed thematic exposes
        # children via parent_id, use the proposed leaf as category and its
        # parent as thematic. The thematic shape returned by fetch_thematics
        # is already flattened (leaf nodes), so the "thematic_id" we receive
        # is actually the leaf id. Linkuma accepts that as `category_id` and
        # the parent as `thematic_id` for citation items.
        resolved_thematic_id = (
            thematic_proposed.get("parent_id") or thematic_proposed["id"]
        )
        resolved_category_id = category_id or (
            thematic_proposed["id"] if thematic_proposed.get("parent_id") else None
        )

        # ---- build items
        plan_id = idempotency.generate_external_ref(project_slug="lcl")
        # Linkuma requires `started_at` to be at least J+4 working days. Use
        # J+5 calendar days as a safe buffer (covers a single weekend).
        start = _add_working_days(datetime.now(UTC).date(), 5)
        step = max(1, spread_days // max(count, 1))

        items: list[dict] = []
        for idx, tier in enumerate(tiers_sequence):
            started_at = (start + timedelta(days=idx * step)).isoformat()
            anchor_value = anchors[idx % len(anchors)]
            items.append(
                {
                    "type": _TIER_TO_TYPE[tier],
                    "url": target_url,
                    "map": gmaps_url,
                    "project_id": project_id,
                    "thematic_id": resolved_thematic_id,
                    "category_id": resolved_category_id,
                    "qty": 1,
                    "anchor": "custom",
                    "anchor_value": anchor_value,
                    "distribution": "direct",
                    "started_at": started_at,
                    "fast_publication": False,
                    # local-only helpers (stripped at POST):
                    "nice_name": f"{business_name} #{idx + 1:02d}",
                    "external_ref": f"{plan_id}-{idx:02d}",
                }
            )

        # ---- local warnings
        warnings: list[str] = list(thematic_warnings)
        warnings.extend(_detect_overopt(items))

        # ---- price the bundle
        try:
            price_resp = await client.cart_price(build_price_body(items))
            total_eur = _extract_total(price_resp)
        except Exception as exc:
            logger.warning("cart_price failed during plan: %s", exc)
            total_eur = _estimate_total(items)
            warnings.append(f"could not call /carts/price ({exc}); used local estimate")

        if total_eur > budget_cap_eur:
            warnings.append(
                f"projected total {total_eur:.2f} EUR exceeds your budget_cap_eur "
                f"{budget_cap_eur:.2f}"
            )
        try:
            enforce_budget(total_eur)
        except LinkumaBudgetExceeded as exc:
            warnings.append(exc.message)

        confirm_token = issue_confirm_token(
            {
                "plan_id": plan_id,
                "project_id": project_id,
                "items": items,
                "total_eur": total_eur,
            }
        )

        return {
            "plan_id": plan_id,
            "items": items,
            "thematic_proposed": thematic_proposed,
            "thematic_alternatives": alternatives,
            "total_eur": total_eur,
            "warnings": warnings,
            "confirm_token": confirm_token,
            "budget_cap_eur_global": _global_budget_cap(),
        }

    @mcp.tool()
    async def linkuma_local_campaign_execute(
        confirm_token: str,
        dry_run: bool = False,
    ) -> dict:
        """Execute a previously-planned campaign.

        Each item gets its own `external_ref` (`{plan_id}-{idx:02d}`), so a
        partial failure (e.g. credit runs out mid-batch) is fully resumable:
        re-run the plan and only the missing items will be re-attempted.

        Credit is re-checked BEFORE every single order. On the first
        insufficient-credit error, the batch stops and the remaining items
        are returned in `orders_failed`.
        """
        payload = consume_confirm_token(confirm_token)
        plan_id: str = payload["plan_id"]
        items: list[dict] = payload["items"]
        total_eur: float = float(payload["total_eur"])

        if dry_run:
            return {
                "batch_id": plan_id,
                "dry_run": True,
                "items": items,
                "total_eur": total_eur,
            }

        enforce_budget(total_eur)

        client = get_client()
        orders_created: list[dict] = []
        orders_failed: list[dict] = []
        credit_after: float | None = None

        for item in items:
            ext_ref = item["external_ref"]
            cached = idempotency.get(ext_ref)
            if cached is not None:
                orders_created.append(
                    {
                        "external_ref": ext_ref,
                        "order_id": cached.get("order_id"),
                        "deduped": True,
                    }
                )
                continue

            # credit check immediately before each item
            try:
                settings = await client.get_settings()
                current_credit = _extract_credit(settings)
                credit_after = current_credit
                est_item_price = _type_floor(item.get("type", ""))
                if current_credit < est_item_price:
                    orders_failed.append(
                        {
                            "external_ref": ext_ref,
                            "reason": "insufficient_credit",
                            "credit_eur": current_credit,
                        }
                    )
                    break
            except Exception:  # pragma: no cover
                pass

            body = build_order_body(
                [item],
                external_ref=ext_ref,
                nice_name=item.get("nice_name") or f"local-{ext_ref}",
            )
            try:
                resp = await client.cart_order(body)
            except LinkumaOrderUncertain as exc:
                orders_failed.append(
                    {
                        "external_ref": ext_ref,
                        "reason": "uncertain",
                        "message": exc.message,
                        "recovery": "call linkuma_orders_list(external_ref=...) to "
                        "confirm whether the order was created",
                    }
                )
                break
            except LinkumaInsufficientCredit as exc:
                orders_failed.append(
                    {
                        "external_ref": ext_ref,
                        "reason": "insufficient_credit",
                        "message": exc.message,
                    }
                )
                break
            except Exception as exc:
                orders_failed.append(
                    {
                        "external_ref": ext_ref,
                        "reason": "error",
                        "message": str(exc),
                    }
                )
                continue

            order_id = _extract_order_id(resp)
            idempotency.remember(
                ext_ref,
                {
                    "order_id": order_id,
                    "external_ref": ext_ref,
                    "plan_id": plan_id,
                    "response": resp,
                },
            )
            orders_created.append(
                {
                    "external_ref": ext_ref,
                    "order_id": order_id,
                    "type": item["type"],
                    "anchor_value": item.get("anchor_value"),
                    "started_at": item.get("started_at"),
                }
            )
            credit_after = _extract_credit(resp) or credit_after

        return {
            "batch_id": plan_id,
            "orders_created": orders_created,
            "orders_failed": orders_failed,
            "credit_after_eur": credit_after,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_plan_inputs(
    *,
    project_id: str,
    business_name: str,
    target_url: str,
    gmaps_url: str,
    count: int,
    budget_cap_eur: float,
    tier_mix: str,
) -> None:
    if not project_id:
        raise LinkumaValidationError("project_id is required")
    if not business_name or not business_name.strip():
        raise LinkumaValidationError("business_name is required")
    if not is_valid_http_url(target_url):
        raise LinkumaValidationError(f"target_url `{target_url}` is not a valid http(s) URL")
    ok, reason = is_valid_gmaps_url(gmaps_url)
    if not ok:
        raise LinkumaValidationError(f"gmaps_url rejected: {reason}")
    if count <= 0 or count > 100:
        raise LinkumaValidationError("count must be between 1 and 100")
    if budget_cap_eur <= 0:
        raise LinkumaValidationError("budget_cap_eur must be > 0")
    if tier_mix not in _VALID_TIER_MIX:
        raise LinkumaValidationError(
            f"tier_mix must be one of {sorted(_VALID_TIER_MIX)}; got `{tier_mix}`"
        )


async def _propose_thematic(
    client: LinkumaClient,
    tiers_sequence: list[str],
    hint: str | None,
    language: str,
) -> tuple[dict, list[dict], list[str]]:
    warnings: list[str] = []
    # Use premium thematics when any premium item is in the mix; else use the
    # majority tier. (Linkuma thematics IDs aren't always shared across tiers.)
    primary_tier = "premium" if "premium" in tiers_sequence else (
        tiers_sequence[0] if tiers_sequence else "premium"
    )
    try:
        thematics = await fetch_thematics(client, primary_tier, language)
    except Exception as exc:  # pragma: no cover
        raise LinkumaValidationError(
            f"could not fetch thematics for tier={primary_tier}: {exc}"
        ) from exc

    if not thematics:
        raise LinkumaValidationError(
            f"no thematics returned for tier={primary_tier}, lang={language}"
        )

    if hint:
        hint_l = hint.lower()
        scored: list[tuple[int, dict]] = []
        for th in thematics:
            score = 0
            name = (th.name or "").lower()
            parent = (th.parent_name or "").lower()
            for token in hint_l.split():
                if token in name:
                    score += 2
                if token in parent:
                    score += 1
            if score:
                scored.append((score, th.model_dump()))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            top = [s[1] for s in scored[:4]]
            return top[0], top[1:], warnings
        warnings.append(
            f"thematic_hint `{hint}` did not match any thematic; falling back "
            "to the first available"
        )

    chosen = thematics[0].model_dump()
    alternatives = [t.model_dump() for t in thematics[1:4]]
    return chosen, alternatives, warnings


def _add_working_days(start_date, working_days: int):
    """Add `working_days` business days (Mon-Fri) to `start_date`."""
    from datetime import timedelta as _td

    d = start_date
    added = 0
    while added < working_days:
        d = d + _td(days=1)
        if d.weekday() < 5:  # Mon=0..Fri=4
            added += 1
    return d


def _type_floor(item_type: str) -> float:
    return {
        "citation_boost": 35.0,
        "citation_linkuma": 10.0,
        "premium": 30.0,
        "standard": 10.0,
        "basic": 7.0,
    }.get(item_type, 10.0)


def _estimate_total(items: list[dict]) -> float:
    return sum(_type_floor(it.get("type", "")) for it in items)


def _extract_total(payload: Any) -> float:
    if not isinstance(payload, dict):
        return 0.0
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("total_eur", "total_price", "total", "amount_eur", "amount", "price"):
        v = data.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _extract_order_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    orders = data.get("orders")
    if isinstance(orders, list) and orders and isinstance(orders[0], dict):
        oid = orders[0].get("id") or orders[0].get("order_id")
        if oid:
            return str(oid)
    return str(data.get("order_id") or data.get("id") or "")


def _extract_credit(payload: Any) -> float:
    # Linkuma /settings returns a list-of-one in practice: [{"user_id":..,"credit":..}]
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return 0.0
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    for key in ("credit_eur", "credit", "balance", "balance_eur", "credit_after_eur"):
        v = payload.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return 0.0


def _global_budget_cap() -> float:
    return budget_cap_eur()
