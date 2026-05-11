"""linkuma_cart_price + linkuma_cart_order with idempotency and confirm tokens."""

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


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_cart_price(
        project_id: str,
        items: list[dict],
        strict_checks: bool = True,
    ) -> dict:
        """Simulate a cart price + run all business checks.

        Returns the API total plus our own `warnings` / `blockers`:
        - target_url reachable (HEAD or GET, follow redirects)
        - anchor over-optimisation (batch + history)
        - pagekw vs anchor coherence
        - credit sufficiency
        - budget cap (LINKUMA_BUDGET_CAP_EUR)

        On success, a single-use `confirm_token` (valid 10 minutes) is
        returned. Pass it to `linkuma_cart_order` to commit.
        """
        if not project_id:
            raise LinkumaValidationError("project_id is required")
        if not items:
            raise LinkumaValidationError("items must contain at least one entry")

        client = get_client()
        warnings: list[str] = []
        blockers: list[str] = []

        # ---- local checks (cheap)
        for idx, it in enumerate(items):
            url = it.get("target_url")
            if not is_valid_http_url(url or ""):
                blockers.append(f"items[{idx}].target_url is not a valid http(s) URL")
                continue
            anchor = it.get("anchor")
            if not anchor or not str(anchor).strip():
                blockers.append(f"items[{idx}].anchor is required")
            pagekw = it.get("pagekw")
            if pagekw and not pagekw_anchor_coherence(pagekw, anchor or ""):
                warnings.append(
                    f"items[{idx}]: pagekw `{pagekw}` shares no tokens with anchor `{anchor}` "
                    "— risk of editorial refusal"
                )

        warnings.extend(detect_anchor_over_optimisation(items))

        # ---- live target_url check (optional)
        if strict_checks and not blockers:
            for idx, it in enumerate(items):
                url = it["target_url"]
                ok, status, err = await target_url_status(url)
                if not ok:
                    warnings.append(
                        f"items[{idx}].target_url returned status {status or 'n/a'} "
                        f"({err or 'unreachable'}) — fix it before ordering"
                    )

        # ---- call /carts/price for the authoritative total
        body = {"project_id": project_id, "items": [_strip_local_fields(i) for i in items]}
        price_resp = await client.cart_price(body)
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
                    "project_id": project_id,
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
        project_id = payload["project_id"]
        items = payload["items"]
        priced_total = float(payload["total_eur"])

        # Refuse if budget cap changed since price.
        enforce_budget(priced_total)

        ext_ref = external_ref or idempotency.generate_external_ref(
            project_slug=str(project_id)[:8]
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
                {"order_id": existing[0].get("order_id") or existing[0].get("id"), "source": "server-dedup"},
            )
            raise LinkumaDuplicateOrder(
                f"order with external_ref={ext_ref} already exists on Linkuma",
                details={"server_record": existing[0]},
            )

        body: dict[str, Any] = {
            "project_id": project_id,
            "items": [_strip_local_fields(i) for i in items],
            "external_ref": ext_ref,
            "payment_method": payment_method,
        }
        if nice_name:
            body["nice_name"] = nice_name

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

        order_id = resp.get("order_id") or resp.get("id") or ""
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
            "items_ordered": resp.get("items", []),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_LOCAL_ONLY_FIELDS = {"pagekw"}


def _strip_local_fields(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in _LOCAL_ONLY_FIELDS and v is not None}


def _extract_total(payload: Any) -> float:
    if not isinstance(payload, dict):
        return 0.0
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    for key in ("total_eur", "total", "amount_eur", "amount"):
        v = payload.get(key)
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
        if isinstance(payload.get("data"), dict):
            payload = payload["data"]
        for key in ("items", "items_priced", "lines"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
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
