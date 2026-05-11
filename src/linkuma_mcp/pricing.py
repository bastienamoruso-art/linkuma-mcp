"""Pricing helpers — tier_mix logic, budget cap enforcement, confirm tokens.

The confirm-token store lives in-process. v1 deliberately keeps it ephemeral:
losing tokens on restart is by design (forces a fresh price+plan).
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

from .errors import LinkumaBudgetExceeded, LinkumaConfirmTokenError

# Spec §5.6 — confirm tokens expire after 10 minutes.
_TOKEN_TTL_S = 10 * 60

# token -> (created_at, payload)
_tokens: dict[str, tuple[float, dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Budget cap
# ---------------------------------------------------------------------------


def budget_cap_eur() -> float:
    raw = os.getenv("LINKUMA_BUDGET_CAP_EUR", "500").strip()
    try:
        return float(raw)
    except ValueError:
        return 500.0


def enforce_budget(total_eur: float) -> None:
    cap = budget_cap_eur()
    if total_eur > cap:
        raise LinkumaBudgetExceeded(
            f"cart total {total_eur:.2f} EUR exceeds LINKUMA_BUDGET_CAP_EUR={cap:.2f}",
            details={"total_eur": total_eur, "cap_eur": cap},
        )


# ---------------------------------------------------------------------------
# tier_mix for local citation campaigns
# ---------------------------------------------------------------------------

# Indicative per-link price floors (EUR). Used only when the price endpoint is
# unavailable in dry-run paths. The real total always comes from /carts/price.
_TIER_PRICE_FLOOR = {
    "basic": 7.0,      # Starter
    "standard": 10.0,  # Linkuma
    "premium": 30.0,   # Boost
}


def resolve_tier_mix(
    count: int, budget_cap_eur_value: float, mode: str = "auto"
) -> dict[str, int]:
    """Distribute `count` items across `boost` (premium) and `linkuma` (standard).

    - `boost` only      -> all premium
    - `linkuma` only    -> all standard
    - `auto`            -> 70% premium / 30% standard if budget allows, else falls
                            back to a feasible mix.
    """
    if count <= 0:
        return {"premium": 0, "standard": 0}

    if mode == "boost":
        return {"premium": count, "standard": 0}
    if mode == "linkuma":
        return {"premium": 0, "standard": count}

    # auto
    premium = round(count * 0.7)
    standard = count - premium
    est_total = premium * _TIER_PRICE_FLOOR["premium"] + standard * _TIER_PRICE_FLOOR["standard"]
    while est_total > budget_cap_eur_value and premium > 0:
        premium -= 1
        standard = count - premium
        est_total = premium * _TIER_PRICE_FLOOR["premium"] + standard * _TIER_PRICE_FLOOR["standard"]
    return {"premium": premium, "standard": standard}


# ---------------------------------------------------------------------------
# Confirm token registry
# ---------------------------------------------------------------------------


def issue_confirm_token(payload: dict[str, Any]) -> str:
    """Return a fresh token bound to a payload (typically the price response)."""
    _gc_tokens()
    token = "lkm_cnf_" + secrets.token_urlsafe(16)
    _tokens[token] = (time.time(), payload)
    return token


def consume_confirm_token(token: str) -> dict[str, Any]:
    """Verify, return payload, then invalidate the token (single-use)."""
    _gc_tokens()
    entry = _tokens.pop(token, None)
    if entry is None:
        raise LinkumaConfirmTokenError(
            "confirm_token missing, unknown or already consumed — call cart_price "
            "(or local_campaign_plan) again to get a fresh one"
        )
    created_at, payload = entry
    if (time.time() - created_at) > _TOKEN_TTL_S:
        raise LinkumaConfirmTokenError(
            "confirm_token expired (>10 minutes) — call cart_price again to get a "
            "fresh one"
        )
    return payload


def _gc_tokens() -> None:
    cutoff = time.time() - _TOKEN_TTL_S
    expired = [t for t, (ts, _) in _tokens.items() if ts < cutoff]
    for t in expired:
        _tokens.pop(t, None)
