"""linkuma_cart_price + linkuma_cart_order with idempotency and confirm tokens.

v0.3.0 — body schema aligned with the real Linkuma API. The MCP now passes
real production orders (validated 2026-05-11 on Maisons Elytis: 10 orders at
35 EUR each).

Per-item required fields:
    type, url, project_id, thematic_id, qty, anchor, started_at

Per-item optional fields:
    map (required for citation_*), category_id, anchor_value (required when
    anchor=="custom"), distribution, distribution_value, fast_publication,
    brief, pagekw, improved_text, url2, anchor2_type, custom_anchor2,
    is_no_link, additional_words_count

Top-level cart body:
    /carts/price   -> { "items": [...] }
    /carts/order   -> { "items": [...], "payment_method": "direct_credits",
                        "external_ref": "...", "nice_name": "..." }
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .. import idempotency
from ..checks import (
    detect_anchor_over_optimisation,
    is_valid_http_url,
    pagekw_anchor_coherence,
    target_url_status,
)
from ..client import LinkumaClient
from ..errors import (
    LinkumaBudgetExceeded,
    LinkumaDuplicateOrder,
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

logger = logging.getLogger("linkuma_mcp")

_VALID_TYPES = {"basic", "standard", "premium", "citation_linkuma", "citation_boost"}
_VALID_ANCHORS = {"url", "generic", "custom"}
_VALID_DISTRIBUTION = {"direct", "schedule"}
_CITATION_TYPES = {"citation_linkuma", "citation_boost"}


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_cart_price(
        items: list[dict],
        strict_checks: bool = True,
    ) -> dict:
        """Simulate a cart price + run all business checks.

        Each item must contain at minimum: `type`, `url`, `project_id`,
        `thematic_id`, `anchor`, `started_at`. Citation items
        (`citation_boost`, `citation_linkuma`) additionally require `map`.
        Items with `anchor="custom"` require `anchor_value`.

        Returns the API total plus our own `warnings` / `blockers`:
        - url reachable (HEAD or GET, follow redirects)
        - anchor over-optimisation (batch + history)
        - pagekw vs anchor coherence
        - credit sufficiency
        - budget cap (LINKUMA_BUDGET_CAP_EUR)

        On success, a single-use `confirm_token` (valid 10 minutes) is
        returned. Pass it to `linkuma_cart_order` to commit.
        """
        if not items:
            raise LinkumaValidationError("items must contain at least one entry")

        client = get_client()
        warnings: list[str] = []
        blockers: list[str] = []

        # ---- local checks (cheap)
        for idx, it in enumerate(items):
            blockers.extend(_validate_item_shape(idx, it))
            anchor = it.get("anchor", "custom")
            pagekw = it.get("pagekw")
            anchor_value = it.get("anchor_value")
            if pagekw and anchor == "custom" and anchor_value and not pagekw_anchor_coherence(
                pagekw, anchor_value
            ):
                warnings.append(
                    f"items[{idx}]: pagekw `{pagekw}` shares no tokens with "
                    f"anchor_value `{anchor_value}` — risk of editorial refusal"
                )

        warnings.extend(_detect_overopt(items))

        # ---- live url check (optional) — only if no blockers so far
        if strict_checks and not blockers:
            for idx, it in enumerate(items):
                url = it.get("url")
                if not url:
                    continue
                ok, status, err = await target_url_status(url)
                if not ok:
                    warnings.append(
                        f"items[{idx}].url returned status {status or 'n/a'} "
                        f"({err or 'unreachable'}) — fix it before ordering"
                    )

        # ---- call /carts/price for the authoritative total
        body = build_price_body(items)
        try:
            price_resp = await client.cart_price(body)
        except Exception as exc:
            # Surface 422 body content for debug — never swallow.
            details = getattr(exc, "details", None)
            logger.warning("cart_price failed: %s details=%s", exc, details)
            raise
        total_eur = _extract_total(price_resp)

        # ---- budget cap
        try:
            enforce_budget(total_eur)
        except LinkumaBudgetExceeded as exc:
            blockers.append(exc.message)

        # ---- credit check
        credit_after: float | None = None
        try:
            settings = await client.get_settings()
            current_credit = _extract_credit(settings)
            credit_after = current_credit - total_eur
            if credit_after < 0:
                blockers.append(
                    f"insufficient credit: total {total_eur:.2f} EUR > credit "
                    f"{current_credit:.2f} EUR"
                )
        except Exception as exc:  # pragma: no cover - network paths
            warnings.append(f"could not fetch credit: {exc}")

        # ---- issue confirm token only when there are no blockers
        confirm_token: str | None = None
        if not blockers:
            confirm_token = issue_confirm_token(
                {
                    "items": items,
                    "total_eur": total_eur,
                }
            )

        return {
            "total_eur": total_eur,
            "items_priced": _extract_priced_items(price_resp),
            "warnings": warnings,
            "blockers": blockers,
            "credit_after_eur": credit_after,
            "confirm_token": confirm_token,
            "budget_cap_eur": budget_cap_eur(),
        }

    @mcp.tool()
    async def linkuma_cart_order(
        confirm_token: str,
        external_ref: str | None = None,
        nice_name: str | None = None,
        payment_method: str = "direct_credits",
    ) -> dict:
        """Commit a cart previously priced via `linkuma_cart_price`.

        Idempotency: the local store at `~/.linkuma-mcp/idempotency.json` is
        checked BEFORE every POST. A repeat call with the same `external_ref`
        returns the cached result and does not hit the network.

        Zero auto-retry on 5xx or timeout — you get a `LinkumaOrderUncertain`
        error. Recover via `linkuma_orders_list(external_ref=...)`.
        """
        payload = consume_confirm_token(confirm_token)
        items = payload["items"]
        priced_total = float(payload["total_eur"])

        # Refuse if budget cap changed since price.
        enforce_budget(priced_total)

        # Use first item's project_id as the slug seed for the auto-generated ref.
        first_project = ""
        if items and isinstance(items[0], dict):
            first_project = str(items[0].get("project_id") or "")[:8]
        ext_ref = external_ref or idempotency.generate_external_ref(
            project_slug=first_project or "cart"
        )

        # ---- Layer 1: local idempotency cache
        cached = idempotency.get(ext_ref)
        if cached is not None:
            logger.info("idempotency hit for external_ref=%s", ext_ref)
            raise LinkumaDuplicateOrder(
                f"order with external_ref={ext_ref} already exists locally",
                details={"cached": cached},
            )

        client = get_client()

        # ---- Layer 2 + 3: server-side dedup
        try:
            existing = await client.list_orders(params={"external_ref": ext_ref})
        except Exception:  # pragma: no cover - defensive
            existing = []
        if existing:
            idempotency.remember(
                ext_ref,
                {
                    "order_id": existing[0].get("order_id") or existing[0].get("id"),
                    "source": "server-dedup",
                },
            )
            raise LinkumaDuplicateOrder(
                f"order with external_ref={ext_ref} already exists on Linkuma",
                details={"server_record": existing[0]},
            )

        body = build_order_body(
            items,
            external_ref=ext_ref,
            nice_name=nice_name or f"cart-{ext_ref}",
            payment_method=payment_method,
        )

        # ---- credit check immediately before order
        try:
            settings = await client.get_settings()
            current_credit = _extract_credit(settings)
            if current_credit < priced_total:
                raise LinkumaInsufficientCredit(
                    f"credit dropped below cart total: {current_credit:.2f} EUR < "
                    f"{priced_total:.2f} EUR"
                )
        except LinkumaInsufficientCredit:
            raise
        except Exception:  # pragma: no cover - non-blocking
            pass

        try:
            resp = await client.cart_order(body)
        except LinkumaOrderUncertain:
            raise

        order_id = _extract_order_id(resp)
        record = {
            "order_id": order_id,
            "external_ref": ext_ref,
            "total_eur": priced_total,
            "response": resp,
        }
        idempotency.remember(ext_ref, record)

        return {
            "order_id": order_id,
            "external_ref": ext_ref,
            "total_eur": priced_total,
            "credit_after_eur": _extract_credit(resp) or None,
            "items_ordered": _extract_items_ordered(resp),
        }


# ---------------------------------------------------------------------------
# Body builders (shared with campaign tools)
# ---------------------------------------------------------------------------

# Fields kept locally for plan/coherence work but stripped before POST.
_LOCAL_ONLY_FIELDS = {"pagekw", "nice_name", "external_ref"}


def strip_local_fields(item: dict) -> dict:
    """Return a copy of `item` without MCP-local helper fields and None values."""
    return {
        k: v
        for k, v in item.items()
        if k not in _LOCAL_ONLY_FIELDS and v is not None
    }


def build_price_body(items: list[dict]) -> dict:
    """Build the body for POST /carts/price.

    Only `items` is allowed at the top level. `payment_method`, `external_ref`
    and `nice_name` are valid only for `/carts/order`.
    """
    return {"items": [strip_local_fields(i) for i in items]}


def build_order_body(
    items: list[dict],
    *,
    external_ref: str,
    nice_name: str,
    payment_method: str = "direct_credits",
) -> dict:
    """Build the body for POST /carts/order."""
    return {
        "items": [strip_local_fields(i) for i in items],
        "payment_method": payment_method,
        "external_ref": external_ref,
        "nice_name": nice_name,
    }


# ---------------------------------------------------------------------------
# Item validation
# ---------------------------------------------------------------------------


def _validate_item_shape(idx: int, item: dict) -> list[str]:
    """Return a list of blocker messages for `item`. Empty if all-good."""
    out: list[str] = []
    itype = item.get("type")
    if itype not in _VALID_TYPES:
        out.append(
            f"items[{idx}].type must be one of {sorted(_VALID_TYPES)}; "
            f"got `{itype}`"
        )
    if not item.get("project_id"):
        out.append(f"items[{idx}].project_id is required")
    if not item.get("thematic_id"):
        out.append(f"items[{idx}].thematic_id is required")
    url = item.get("url")
    if not item.get("is_no_link") and not is_valid_http_url(url or ""):
        out.append(
            f"items[{idx}].url is not a valid http(s) URL (got `{url}`); "
            "pass `is_no_link=true` if you really want a no-link mention"
        )
    if itype in _CITATION_TYPES and not item.get("map"):
        out.append(
            f"items[{idx}].map is required for citation items (canonical GMaps URL)"
        )
    anchor = item.get("anchor", "custom")
    if anchor not in _VALID_ANCHORS:
        out.append(
            f"items[{idx}].anchor must be one of {sorted(_VALID_ANCHORS)}; "
            f"got `{anchor}`"
        )
    if anchor == "custom" and not item.get("anchor_value"):
        out.append(
            f"items[{idx}].anchor_value is required when anchor=='custom'"
        )
    distribution = item.get("distribution", "direct")
    if distribution not in _VALID_DISTRIBUTION:
        out.append(
            f"items[{idx}].distribution must be one of {sorted(_VALID_DISTRIBUTION)}; "
            f"got `{distribution}`"
        )
    if not item.get("started_at"):
        out.append(f"items[{idx}].started_at is required (ISO date YYYY-MM-DD)")
    return out


def _detect_overopt(items: list[dict]) -> list[str]:
    """Adapt v0.3 item shape to the over-optimisation detector (which expects
    `target_url` and `anchor` strings)."""
    flat = [
        {
            "target_url": it.get("url") or "",
            "anchor": (it.get("anchor_value") or it.get("anchor") or ""),
        }
        for it in items
    ]
    return detect_anchor_over_optimisation(flat)


# ---------------------------------------------------------------------------
# Response extractors
# ---------------------------------------------------------------------------


def _extract_total(payload: Any) -> float:
    if not isinstance(payload, dict):
        return 0.0
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("total_eur", "total_price", "total", "amount_eur", "amount", "price"):
        v = data.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return 0.0


def _extract_priced_items(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        for key in ("items", "items_priced", "lines", "orders"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


def _extract_order_id(payload: Any) -> str:
    """Pull a single order id out of /carts/order responses.

    The live API returns `{"data": {"id": "<cart_id>", "orders": [{"id": "<order_id>", ...}]}}`.
    """
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    orders = data.get("orders")
    if isinstance(orders, list) and orders and isinstance(orders[0], dict):
        oid = orders[0].get("id") or orders[0].get("order_id")
        if oid:
            return str(oid)
    return str(data.get("order_id") or data.get("id") or "")


def _extract_items_ordered(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    orders = data.get("orders")
    if isinstance(orders, list):
        return orders
    items = data.get("items")
    if isinstance(items, list):
        return items
    return []


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
