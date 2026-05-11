"""Multi-account resolver for Linkuma API keys.

v0.2.0 introduces optional multi-account support without breaking v1 users.

Selection priority:
1. `LINKUMA_API_KEYS_JSON` — JSON map of `{alias: api_key}`. Selected via the
   `account` parameter on every tool. Defaults to the first alias when omitted.
2. `LINKUMA_API_KEY` — single key, exposed under the alias `default`.

A v1 user with only `LINKUMA_API_KEY` set continues to work unchanged: every
tool resolves to the single key when `account` is omitted.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from .errors import LinkumaConfigError

if TYPE_CHECKING:  # pragma: no cover
    from .client import LinkumaClient

logger = logging.getLogger("linkuma_mcp")

# Cache of LinkumaClient instances, keyed by resolved alias. Built lazily so
# importing the module without any key (in tests) does not crash.
_clients: dict[str, LinkumaClient] = {}


def _load_accounts_map() -> dict[str, str]:
    """Return `{alias: api_key}` from env, never raising.

    Falls back to `{"default": LINKUMA_API_KEY}` when `LINKUMA_API_KEYS_JSON`
    is unset. Returns `{}` if neither is defined.
    """
    raw = os.getenv("LINKUMA_API_KEYS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LinkumaConfigError(
                f"LINKUMA_API_KEYS_JSON is not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise LinkumaConfigError(
                "LINKUMA_API_KEYS_JSON must be a non-empty JSON object mapping "
                "aliases to API keys"
            )
        # Stringify all values defensively.
        return {str(k).strip(): str(v).strip() for k, v in parsed.items() if v}

    single = os.getenv("LINKUMA_API_KEY", "").strip()
    if single:
        return {"default": single}
    return {}


def list_accounts() -> list[str]:
    """Return all configured aliases (order-preserving)."""
    return list(_load_accounts_map().keys())


def resolve_account(account: str | None = None) -> tuple[str, str]:
    """Return `(alias, api_key)` for the requested account.

    - `account=None` -> first alias.
    - `account="default"` works for both v1 single-key and an explicit alias.
    - Unknown alias -> `LinkumaConfigError` listing the valid ones.
    """
    accounts = _load_accounts_map()
    if not accounts:
        raise LinkumaConfigError(
            "No Linkuma API key configured. Set LINKUMA_API_KEY (single-account) "
            "or LINKUMA_API_KEYS_JSON='{\"alias\":\"key\",...}' (multi-account)."
        )

    if account is None or account == "":
        alias = next(iter(accounts))
        return alias, accounts[alias]

    if account not in accounts:
        raise LinkumaConfigError(
            f"Unknown account alias `{account}`. Available: {sorted(accounts)}"
        )
    return account, accounts[account]


def get_client(account: str | None = None) -> LinkumaClient:
    """Return a cached `LinkumaClient` bound to the resolved account.

    Clients are created lazily and cached per alias for the lifetime of the
    process. Resetting the env (e.g. in tests) should clear the cache via
    `reset_clients()`.
    """
    from .client import LinkumaClient

    alias, api_key = resolve_account(account)
    cached = _clients.get(alias)
    if cached is not None:
        return cached
    client = LinkumaClient(api_key=api_key)
    _clients[alias] = client
    return client


def reset_clients() -> None:
    """Drop cached clients (used by tests when the env mutates)."""
    _clients.clear()
