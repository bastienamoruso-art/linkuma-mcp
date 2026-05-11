"""linkuma_bulk_reorder — replay refused orders with adjustments.

Builds a fresh plan from a list of refused order_ids, applying optional
adjustments (most usefully `anchor: "auto_rewrite"` which regenerates anchors
via the `mixed` strategy). Same plan -> confirm -> execute pattern as the rest
of the v0.2.0 toolkit.

v0.3.0 — items follow the real cart schema (`type`, `url`, `anchor_value`,
`distribution`, `started_at`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .. import idempotency
from ..accounts import get_client as get_account_client
from ..anchors import generate_anchors
from ..client import LinkumaClient
from ..errors import (
    LinkumaBudgetExceeded,
    LinkumaInsufficientCredit,
    LinkumaOrderUncertain,
    LinkumaValidationError,
)
from ..pricing import (
    consume_confirm_token,
    enforce_budget,
    issue_confirm_token,
)
from .cart import build_order_body

_TYPE_PRICE_FLOOR = {
    "basic": 7.0,
    "standard": 10.0,
    "premium": 30.0,
    "citation_linkuma": 10.0,
    "citation_boost": 35.0,
}
_VALID_TYPES = set(_TYPE_PRICE_FLOOR)

# Legacy "Citation Boost" / "Citation Linkuma" string mapper from `get_order` API.
_TYPE_FROM_LABEL = {
    "citation boost": "citation_boost",
    "citation linkuma": "citation_linkuma",
    "basic": "basic",
    "standard": "standard",
    "premium": "premium",
}


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_bulk_reorder_plan(
        refused_order_ids: list[str],
        adjustments: dict | None = None,
        account: str | None = None,
    ) -> dict:
        """Plan a bulk re-order of previously refused orders.

        Adjustments currently supported:
        - `{"anchor": "auto_rewrite"}` -> regenerate anchors via `mixed`
        - `{"type": "<basic|standard|premium|citation_*>"}` -> change item type
        - `{"url": "<new>"}` -> override the target URL across the batch
        - `{"thematic_id": "<new>"}` -> override thematic across the batch

        Backwards-compat aliases: `tier` -> `type`, `target_url` -> `url`.

        Returns a plan with `confirm_token` to feed to
        `linkuma_bulk_reorder_execute`.
        """
        if not refused_order_ids:
            raise LinkumaValidationError("refused_order_ids must not be empty")

        adjustments = _normalise_adjustments(adjustments or {})
        _validate_adjustments(adjustments)

        client = get_account_client(account) if account is not None else get_client()

        # Fetch the source refused orders to copy their fields.
        sources: list[dict] = []
        not_found: list[str] = []
        for oid in refused_order_ids:
            try:
                order = await client.get_order(oid)
            except Exception:
                not_found.append(oid)
                continue
            if not order:
                not_found.append(oid)
                continue
            sources.append(order)

        if not sources:
            raise LinkumaValidationError(
                f"none of the provided order_ids could be fetched: {refused_order_ids}"
            )

        # Generate new anchor values if requested.
        new_anchor_values: list[str] = []
        if adjustments.get("anchor") == "auto_rewrite":
            keywords: list[str] = []
            for s in sources:
                if s.get("pagekw"):
                    keywords.append(str(s["pagekw"]))
                elif _source_anchor(s):
                    keywords.append(str(_source_anchor(s)))
            target_url = adjustments.get("url") or _source_url(sources[0]) or ""
            new_anchor_values = generate_anchors(
                count=len(sources),
                strategy="mixed",
                keywords=keywords or [_source_anchor(sources[0]) or ""],
                target_url=target_url,
            )["anchors"]

        plan_id = idempotency.generate_external_ref(project_slug="bulk")
        items: list[dict] = []
        warnings: list[str] = []
        if not_found:
            warnings.append(
                f"could not fetch {len(not_found)} order(s): {not_found}"
            )

        today_iso = datetime.now(UTC).date().isoformat()

        for idx, src in enumerate(sources):
            item_type = adjustments.get("type") or _source_type(src) or "standard"
            url = adjustments.get("url") or _source_url(src) or ""
            thematic_id = (
                adjustments.get("thematic_id") or src.get("thematic_id") or ""
            )
            anchor_value = (
                new_anchor_values[idx]
                if (
                    adjustments.get("anchor") == "auto_rewrite"
                    and idx < len(new_anchor_values)
                )
                else _source_anchor(src)
            )
            items.append(
                {
                    "type": item_type,
                    "url": url,
                    "project_id": src.get("project_id"),
                    "thematic_id": thematic_id,
                    "qty": 1,
                    "anchor": "custom",
                    "anchor_value": anchor_value,
                    "distribution": "direct",
                    "started_at": today_iso,
                    "fast_publication": False,
                    "pagekw": src.get("pagekw"),
                    # local-only metadata
                    "external_ref": f"{plan_id}-{idx:02d}",
                    "source_order_id": src.get("order_id") or src.get("id"),
                }
            )

        # Local price estimate (we don't hit /carts/price to keep this fast
        # and idempotent across projects). Authoritative price returns from
        # /carts/order at execute time.
        total_eur = sum(
            _TYPE_PRICE_FLOOR.get(it.get("type", ""), 10.0) for it in items
        )

        try:
            enforce_budget(total_eur)
        except LinkumaBudgetExceeded as exc:
            warnings.append(exc.message)

        confirm_token = issue_confirm_token(
            {
                "plan_id": plan_id,
                "items": items,
                "total_eur": total_eur,
                "account": account,
            }
        )

        return {
            "plan_id": plan_id,
            "items": items,
            "total_eur": total_eur,
            "warnings": warnings,
            "not_found": not_found,
            "confirm_token": confirm_token,
        }

    @mcp.tool()
    async def linkuma_bulk_reorder_execute(
        confirm_token: str,
        dry_run: bool = False,
        account: str | None = None,
    ) -> dict:
        """Commit a bulk-reorder plan."""
        payload = consume_confirm_token(confirm_token)
        plan_id: str = payload["plan_id"]
        items: list[dict] = payload["items"]
        total_eur: float = float(payload["total_eur"])
        plan_account: str | None = payload.get("account")

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

            body = build_order_body(
                [item],
                external_ref=ext_ref,
                nice_name=f"bulk-{ext_ref}",
            )
            try:
                resp = await client.cart_order(body)
            except LinkumaOrderUncertain as exc:
                orders_failed.append(
                    {
                        "external_ref": ext_ref,
                        "reason": "uncertain",
                        "message": exc.message,
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
                    "bulk_reorder": True,
                },
            )
            orders_created.append(
                {
                    "external_ref": ext_ref,
                    "order_id": order_id,
                    "type": item["type"],
                    "anchor_value": item.get("anchor_value"),
                }
            )

        return {
            "batch_id": plan_id,
            "orders_created": orders_created,
            "orders_failed": orders_failed,
        }


# ---------------------------------------------------------------------------
# Source order helpers (normalise legacy /orders/{id} shape -> v0.3 cart fields)
# ---------------------------------------------------------------------------


def _source_type(order: dict) -> str | None:
    raw = order.get("type") or order.get("tier")
    if not raw:
        return None
    key = str(raw).strip().lower()
    return _TYPE_FROM_LABEL.get(key, key if key in _VALID_TYPES else None)


def _source_url(order: dict) -> str | None:
    return order.get("url") or order.get("target_url")


def _source_anchor(order: dict) -> str | None:
    # /orders returns either `anchor` (single str) or `anchor_values` (csv).
    av = order.get("anchor_value") or order.get("anchor_values") or order.get("anchor")
    return av


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------


_VALID_ADJUSTMENT_KEYS = {"anchor", "type", "url", "thematic_id"}


def _normalise_adjustments(adjustments: dict) -> dict:
    """Accept legacy keys (`tier`, `target_url`) for backwards compat."""
    out = dict(adjustments)
    if "tier" in out and "type" not in out:
        out["type"] = out.pop("tier")
    if "target_url" in out and "url" not in out:
        out["url"] = out.pop("target_url")
    return out


def _validate_adjustments(adjustments: dict) -> None:
    extra = set(adjustments) - _VALID_ADJUSTMENT_KEYS
    if extra:
        raise LinkumaValidationError(
            f"unsupported adjustment keys: {sorted(extra)}; "
            f"valid keys: {sorted(_VALID_ADJUSTMENT_KEYS)}"
        )
    anchor = adjustments.get("anchor")
    if anchor is not None and anchor != "auto_rewrite":
        raise LinkumaValidationError(
            f"`anchor` adjustment must be `auto_rewrite` or omitted; got `{anchor}`"
        )
    item_type = adjustments.get("type")
    if item_type is not None and item_type not in _VALID_TYPES:
        raise LinkumaValidationError(
            f"`type` adjustment must be one of {sorted(_VALID_TYPES)}; "
            f"got `{item_type}`"
        )


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
