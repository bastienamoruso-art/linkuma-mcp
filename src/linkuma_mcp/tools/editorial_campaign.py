"""linkuma_editorial_campaign_plan / linkuma_editorial_campaign_execute.

Editorial-link counterpart of `local_campaign`. Produces a plan of N editorial
backlinks (tiers basic/standard/premium) with built-in anchor strategies and
per-item idempotency on `{plan_id}-{idx:02d}`.

v0.3.0 — items follow the real Linkuma cart schema:
    type ∈ {basic, standard, premium}, url, project_id, thematic_id, qty,
    anchor (custom by default), anchor_value, distribution, started_at,
    fast_publication, brief, pagekw.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import idempotency
from ..accounts import get_client as get_account_client
from ..anchors import generate_anchors
from ..checks import (
    is_valid_http_url,
    pagekw_anchor_coherence,
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
)
from ..thematics import fetch_thematics
from .cart import (
    _detect_overopt,
    build_order_body,
    build_price_body,
)

logger = logging.getLogger("linkuma_mcp")

_VALID_TIERS = {"basic", "standard", "premium", "auto"}
_VALID_STRATEGIES = {"branded", "exact", "semantic", "mixed"}

# Indicative per-link price floors when /carts/price is unavailable for the
# local estimate. Authoritative total always comes from the API.
_TYPE_PRICE_FLOOR = {"basic": 7.0, "standard": 10.0, "premium": 30.0}


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_editorial_campaign_plan(
        project_id: str,
        target_url: str,
        count: int,
        budget_cap_eur: float,
        keywords: list[str],
        tier: str = "auto",
        anchor_strategy: str = "mixed",
        thematic_id: str | None = None,
        language: str = "fr",
        spread_days: int = 30,
        pagekw_per_target: str | None = None,
        account: str | None = None,
    ) -> dict:
        """Dry-run plan for an editorial backlink campaign.

        Returns the proposed items (with anchors generated via the requested
        strategy), a thematic recommendation, the projected total, warnings,
        and a single-use `confirm_token` to feed to
        `linkuma_editorial_campaign_execute`. Nothing is committed at this
        stage — only `/thematics` (read) and `/carts/price` (read) are called.

        `tier="auto"` distributes 70% premium / 30% standard if budget permits,
        else falls back to a feasible mix.

        `anchor_strategy="mixed"` (default) = 30% branded / 20% exact /
        50% semantic — the safest distribution against algorithmic penalties.
        """
        _validate_plan_inputs(
            project_id=project_id,
            target_url=target_url,
            count=count,
            budget_cap_eur=budget_cap_eur,
            tier=tier,
            anchor_strategy=anchor_strategy,
            keywords=keywords,
        )

        client = get_account_client(account) if account is not None else get_client()

        # ---- tier sequence
        tiers_sequence = _resolve_tier_sequence(count, budget_cap_eur, tier)

        # ---- anchors
        anchors_payload = generate_anchors(
            count=count,
            strategy=anchor_strategy,
            keywords=keywords,
            target_url=target_url,
            language=language,
        )
        anchors = anchors_payload["anchors"]

        # ---- thematic resolution (caller may override)
        thematic_proposed: dict
        thematic_alternatives: list[dict] = []
        thematic_warnings: list[str] = []
        if thematic_id:
            thematic_proposed = {"id": thematic_id, "name": None, "source": "user"}
        else:
            (
                thematic_proposed,
                thematic_alternatives,
                thematic_warnings,
            ) = await _propose_thematic(client, tiers_sequence, keywords, language)

        # ---- build items
        plan_id = idempotency.generate_external_ref(project_slug="edt")
        # Linkuma requires `started_at` to be at least J+4 working days. Use
        # J+5 calendar days as a safe buffer (covers a single weekend).
        start = _add_working_days(datetime.now(UTC).date(), 5)
        step = max(1, spread_days // max(count, 1))

        items: list[dict] = []
        for idx, (item_type, anchor_value) in enumerate(
            zip(tiers_sequence, anchors, strict=False)
        ):
            started_at = (start + timedelta(days=idx * step)).isoformat()
            items.append(
                {
                    "type": item_type,
                    "url": target_url,
                    "project_id": project_id,
                    "thematic_id": thematic_proposed["id"],
                    "qty": 1,
                    "anchor": "custom",
                    "anchor_value": anchor_value,
                    "distribution": "direct",
                    "started_at": started_at,
                    "fast_publication": False,
                    "pagekw": pagekw_per_target,
                    # local-only
                    "external_ref": f"{plan_id}-{idx:02d}",
                }
            )

        # ---- warnings
        warnings: list[str] = list(thematic_warnings)
        warnings.extend(anchors_payload.get("warnings", []))
        warnings.extend(_detect_overopt(items))
        if pagekw_per_target:
            for idx, it in enumerate(items):
                if not pagekw_anchor_coherence(pagekw_per_target, it.get("anchor_value") or ""):
                    warnings.append(
                        f"items[{idx}]: pagekw `{pagekw_per_target}` shares no tokens "
                        f"with anchor `{it.get('anchor_value')}` — risk of editorial refusal"
                    )

        # ---- price
        try:
            price_resp = await client.cart_price(build_price_body(items))
            total_eur = _extract_total(price_resp)
        except Exception as exc:
            logger.warning("cart_price failed during editorial plan: %s", exc)
            total_eur = _estimate_total(items)
            warnings.append(
                f"could not call /carts/price ({exc}); used local estimate"
            )

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
                "account": account,
            }
        )

        return {
            "plan_id": plan_id,
            "items": items,
            "anchor_distribution": anchors_payload.get("distribution", {}),
            "thematic_proposed": thematic_proposed,
            "thematic_alternatives": thematic_alternatives,
            "total_eur": total_eur,
            "warnings": warnings,
            "confirm_token": confirm_token,
            "budget_cap_eur_global": _global_budget_cap(),
        }

    @mcp.tool()
    async def linkuma_editorial_campaign_execute(
        confirm_token: str,
        dry_run: bool = False,
        account: str | None = None,
    ) -> dict:
        """Commit a previously-planned editorial campaign.

        Each item gets its own `external_ref` (`{plan_id}-{idx:02d}`), so a
        partial failure (credit shortage, network error) is replayable: rerun
        the same plan and the local idempotency cache will skip already-shipped
        items.

        Credit is re-checked BEFORE every order. On the first insufficient-
        credit error the batch stops and the remaining items are returned in
        `orders_failed`.
        """
        payload = consume_confirm_token(confirm_token)
        plan_id: str = payload["plan_id"]
        items: list[dict] = payload["items"]
        total_eur: float = float(payload["total_eur"])
        plan_account: str | None = payload.get("account") if isinstance(payload, dict) else None

        if dry_run:
            return {
                "batch_id": plan_id,
                "dry_run": True,
                "items": items,
                "total_eur": total_eur,
            }

        enforce_budget(total_eur)

        resolved_account = account if account is not None else plan_account
        client = (
            get_account_client(resolved_account)
            if resolved_account is not None
            else get_client()
        )

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

            try:
                settings = await client.get_settings()
                current_credit = _extract_credit(settings)
                credit_after = current_credit
                est_item_price = _TYPE_PRICE_FLOOR.get(item.get("type", ""), 10.0)
                if current_credit < est_item_price:
                    orders_failed.append(
                        {
                            "external_ref": ext_ref,
                            "reason": "insufficient_credit",
                            "credit_eur": current_credit,
                        }
                    )
                    break
            except Exception:  # pragma: no cover - defensive
                pass

            body = build_order_body(
                [item],
                external_ref=ext_ref,
                nice_name=item.get("nice_name") or f"edt-{ext_ref}",
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
    target_url: str,
    count: int,
    budget_cap_eur: float,
    tier: str,
    anchor_strategy: str,
    keywords: list[str],
) -> None:
    if not project_id:
        raise LinkumaValidationError("project_id is required")
    if not is_valid_http_url(target_url):
        raise LinkumaValidationError(
            f"target_url `{target_url}` is not a valid http(s) URL"
        )
    if count <= 0 or count > 100:
        raise LinkumaValidationError("count must be between 1 and 100")
    if budget_cap_eur <= 0:
        raise LinkumaValidationError("budget_cap_eur must be > 0")
    if tier not in _VALID_TIERS:
        raise LinkumaValidationError(
            f"tier must be one of {sorted(_VALID_TIERS)}; got `{tier}`"
        )
    if anchor_strategy not in _VALID_STRATEGIES:
        raise LinkumaValidationError(
            f"anchor_strategy must be one of {sorted(_VALID_STRATEGIES)}; "
            f"got `{anchor_strategy}`"
        )
    if not keywords:
        raise LinkumaValidationError(
            "keywords must not be empty — they seed anchor generation"
        )


def _add_working_days(start_date, working_days: int):
    """Add `working_days` business days (Mon-Fri) to `start_date`."""
    from datetime import timedelta as _td

    d = start_date
    added = 0
    while added < working_days:
        d = d + _td(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def _resolve_tier_sequence(
    count: int, budget_cap_eur_value: float, tier: str
) -> list[str]:
    """Translate the `tier` parameter to a per-item list of tiers."""
    if tier in {"basic", "standard", "premium"}:
        return [tier] * count

    # auto: 70/30 premium/standard, shrink premium share if it overshoots budget
    premium = round(count * 0.7)
    standard = count - premium
    est_total = (
        premium * _TYPE_PRICE_FLOOR["premium"]
        + standard * _TYPE_PRICE_FLOOR["standard"]
    )
    while est_total > budget_cap_eur_value and premium > 0:
        premium -= 1
        standard = count - premium
        est_total = (
            premium * _TYPE_PRICE_FLOOR["premium"]
            + standard * _TYPE_PRICE_FLOOR["standard"]
        )
    return (["premium"] * premium) + (["standard"] * standard)


async def _propose_thematic(
    client: LinkumaClient,
    tiers_sequence: list[str],
    keywords: list[str],
    language: str,
) -> tuple[dict, list[dict], list[str]]:
    """Pick a thematic based on the keywords. No external NLP; substring match."""
    warnings: list[str] = []
    primary_tier = (
        "premium" if "premium" in tiers_sequence
        else (tiers_sequence[0] if tiers_sequence else "standard")
    )
    try:
        thematics = await fetch_thematics(client, primary_tier, language)
    except Exception as exc:  # pragma: no cover - network paths
        raise LinkumaValidationError(
            f"could not fetch thematics for tier={primary_tier}: {exc}"
        ) from exc
    if not thematics:
        raise LinkumaValidationError(
            f"no thematics returned for tier={primary_tier}, lang={language}"
        )

    hint = " ".join(keywords).lower() if keywords else ""
    if hint:
        scored: list[tuple[int, dict]] = []
        for th in thematics:
            score = 0
            name = (th.name or "").lower()
            parent = (th.parent_name or "").lower()
            for token in hint.split():
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
            f"keywords {keywords!r} did not match any thematic name; falling back "
            "to the first available"
        )

    chosen = thematics[0].model_dump()
    alternatives = [t.model_dump() for t in thematics[1:4]]
    return chosen, alternatives, warnings


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


def _estimate_total(items: list[dict]) -> float:
    return sum(_TYPE_PRICE_FLOOR.get(it.get("type", ""), 10.0) for it in items)


def _global_budget_cap() -> float:
    return budget_cap_eur()
