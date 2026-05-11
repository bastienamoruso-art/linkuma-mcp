"""linkuma_bulk_reorder — replay refused orders with adjustments.

Builds a fresh plan from a list of refused order_ids, applying optional
adjustments (most usefully `anchor: "auto_rewrite"` which regenerates anchors
via the `mixed` strategy). Same plan -> confirm -> execute pattern as the rest
of the v0.2.0 toolkit.
"""

from __future__ import annotations

from collections.abc import Callable
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

_TIER_PRICE_FLOOR = {"basic": 7.0, "standard": 10.0, "premium": 30.0}


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
        - `{"tier": "<basic|standard|premium>"}` -> downgrade/upgrade tier
        - `{"target_url": "<new>"}` -> override the target URL across the batch
        - `{"thematic_id": "<new>"}` -> override thematic across the batch

        Returns a plan with `confirm_token` to feed to
        `linkuma_bulk_reorder_execute`.
        """
        if not refused_order_ids:
            raise LinkumaValidationError("refused_order_ids must not be empty")

        adjustments = adjustments or {}
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

        # Generate new anchors if requested.
        new_anchors: list[str] = []
        if adjustments.get("anchor") == "auto_rewrite":
            # Collect keywords from sources (pagekw + existing anchor as seed).
            keywords: list[str] = []
            for s in sources:
                if s.get("pagekw"):
                    keywords.append(str(s["pagekw"]))
                elif s.get("anchor"):
                    keywords.append(str(s["anchor"]))
            target_url = adjustments.get("target_url") or (sources[0].get("target_url") or "")
            new_anchors = generate_anchors(
                count=len(sources),
                strategy="mixed",
                keywords=keywords or [(sources[0].get("anchor") or "")],
                target_url=target_url,
            )["anchors"]

        plan_id = idempotency.generate_external_ref(project_slug="bulk")
        items: list[dict] = []
        warnings: list[str] = []
        if not_found:
            warnings.append(
                f"could not fetch {len(not_found)} order(s): {not_found}"
            )

        for idx, src in enumerate(sources):
            tier = adjustments.get("tier") or src.get("tier") or "standard"
            target_url = adjustments.get("target_url") or src.get("target_url") or ""
            thematic_id = adjustments.get("thematic_id") or src.get("thematic_id") or ""
            anchor = (
                new_anchors[idx]
                if (adjustments.get("anchor") == "auto_rewrite" and idx < len(new_anchors))
                else src.get("anchor")
            )
            pagekw = src.get("pagekw")
            items.append(
                {
                    "tier": tier,
                    "thematic_id": thematic_id,
                    "target_url": target_url,
                    "anchor": anchor,
                    "pagekw": pagekw,
                    "external_ref": f"{plan_id}-{idx:02d}",
                    "source_order_id": src.get("order_id") or src.get("id"),
                    "project_id": src.get("project_id"),
                }
            )

        # Local price estimate (we don't hit /carts/price to keep this fast
        # and idempotent across projects). Authoritative price returns from
        # /carts/order at execute time.
        total_eur = sum(_TIER_PRICE_FLOOR.get(it["tier"], 10.0) for it in items)

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

            body = {
                "project_id": item.get("project_id"),
                "items": [_strip_local_fields(item)],
                "external_ref": ext_ref,
                "payment_method": "direct_credits",
            }
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

            order_id = resp.get("order_id") or resp.get("id") or ""
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
                    "tier": item["tier"],
                    "anchor": item["anchor"],
                }
            )

        return {
            "batch_id": plan_id,
            "orders_created": orders_created,
            "orders_failed": orders_failed,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LOCAL_ONLY_FIELDS = {"external_ref", "source_order_id", "project_id", "pagekw"}
_VALID_ADJUSTMENT_KEYS = {"anchor", "tier", "target_url", "thematic_id"}


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
    tier = adjustments.get("tier")
    if tier is not None and tier not in {"basic", "standard", "premium"}:
        raise LinkumaValidationError(
            f"`tier` adjustment must be basic/standard/premium; got `{tier}`"
        )


def _strip_local_fields(item: dict) -> dict:
    return {
        k: v
        for k, v in item.items()
        if k not in _LOCAL_ONLY_FIELDS and v is not None
    }
