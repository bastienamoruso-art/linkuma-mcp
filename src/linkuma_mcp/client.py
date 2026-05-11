"""Async httpx wrapper around the Linkuma REST API.

Responsibilities:
- carry the bearer token, base URL, timeout
- enforce a soft client-side rate limit (token bucket, 2 req/s default)
- respect server `Retry-After` on 429
- ZERO automatic retry on `POST /carts/order` (spec §5.6) — translates 5xx /
  timeout to `LinkumaOrderUncertain`
- redact api keys from log lines (anything matching ^lkm_ or len > 30)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import httpx

from .errors import (
    LinkumaAuthError,
    LinkumaConfigError,
    LinkumaInsufficientCredit,
    LinkumaOrderUncertain,
    LinkumaRateLimited,
    LinkumaServerError,
    LinkumaUnreachable,
    LinkumaValidationError,
)

DEFAULT_BASE_URL = "https://app.linkuma.com/api/v1"


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------

_LONG_TOKEN_RE = re.compile(r"\b(lkm_[A-Za-z0-9_\-]+|[A-Za-z0-9_\-]{31,})\b")


class _SecretFilter(logging.Filter):
    """Mask API keys and other long-looking secrets from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _redact(record.msg)
            if record.args:
                # Only redact string args; leave numbers/bools/etc untouched so
                # that %-formatting in the original message still works.
                redacted: list = []
                for a in record.args if isinstance(record.args, tuple) else (record.args,):
                    if isinstance(a, str):
                        redacted.append(_redact(a))
                    else:
                        redacted.append(a)
                record.args = tuple(redacted)  # type: ignore[assignment]
        except Exception:  # pragma: no cover - defensive
            pass
        return True


def _redact(text: str) -> str:
    return _LONG_TOKEN_RE.sub("[redacted]", text)


def _install_log_filter(logger: logging.Logger) -> None:
    for f in logger.filters:
        if isinstance(f, _SecretFilter):
            return
    logger.addFilter(_SecretFilter())


logger = logging.getLogger("linkuma_mcp")
_install_log_filter(logger)


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class _RateLimiter:
    def __init__(self, rps: float) -> None:
        self.rps = max(rps, 0.1)
        self._min_interval = 1.0 / self.rps
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LinkumaClient:
    """Async client. Use as an async context manager or call `aclose()`."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        rps: float | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        api_key = api_key or os.getenv("LINKUMA_API_KEY", "").strip()
        if not api_key:
            raise LinkumaConfigError(
                "LINKUMA_API_KEY is required (env var or constructor arg)"
            )
        self._api_key = api_key
        self.base_url = (
            base_url
            or os.getenv("LINKUMA_BASE_URL", "").strip()
            or DEFAULT_BASE_URL
        ).rstrip("/")
        rps_raw = os.getenv("LINKUMA_RATE_LIMIT_RPS", "").strip()
        if rps is None:
            try:
                rps = float(rps_raw) if rps_raw else 2.0
            except ValueError:
                rps = 2.0
        self.rps = rps
        self._limiter = _RateLimiter(rps)

        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "linkuma-mcp/0.2.1 (+https://github.com/bastienamoruso-art/linkuma-mcp)",
            },
            transport=transport,
        )

    async def __aenter__(self) -> LinkumaClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ HTTP

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        is_order_post: bool = False,
        retry_on_429: bool = True,
    ) -> Any:
        """Low-level request. Returns parsed JSON or raises a typed error.

        `is_order_post=True` disables ALL retries (spec §5.6).
        """
        await self._limiter.acquire()
        url = path if path.startswith("http") else f"/{path.lstrip('/')}"
        logger.debug("HTTP %s %s params=%s", method, url, params)

        try:
            resp = await self._http.request(method, url, params=params, json=json)
        except httpx.TimeoutException as exc:
            if is_order_post:
                raise LinkumaOrderUncertain(
                    "POST /carts/order timed out — DO NOT retry. State unknown. "
                    "Recovery: list orders with external_ref to check whether the "
                    "order was created.",
                    details={"error": str(exc)},
                ) from exc
            raise LinkumaUnreachable(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            if is_order_post:
                raise LinkumaOrderUncertain(
                    "POST /carts/order network error — DO NOT retry. State unknown. "
                    "Recovery: list orders with external_ref to check.",
                    details={"error": str(exc)},
                ) from exc
            raise LinkumaUnreachable(f"network error: {exc}") from exc

        # ---- rate limit handling
        if resp.status_code == 429:
            if is_order_post or not retry_on_429:
                raise LinkumaRateLimited(
                    "429 from Linkuma",
                    details={"retry_after": resp.headers.get("Retry-After")},
                )
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            logger.warning("rate-limited; sleeping %.2fs before retry", retry_after)
            await asyncio.sleep(retry_after)
            return await self.request(
                method,
                path,
                params=params,
                json=json,
                is_order_post=False,
                retry_on_429=False,
            )

        return _check_response(resp, is_order_post=is_order_post)

    async def get(self, path: str, *, params: dict | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(
        self, path: str, *, json: dict | None = None, is_order_post: bool = False
    ) -> Any:
        return await self.request(
            "POST", path, json=json, is_order_post=is_order_post
        )

    # ----------------------------------------------------------- convenience

    async def get_settings(self) -> dict:
        return await self.get("/settings")

    async def list_projects(self) -> list[dict]:
        payload = await self.get("/projects")
        return _extract_list(payload)

    async def create_project(self, body: dict) -> dict:
        payload = await self.post("/projects", json=body)
        return _extract_first(payload)

    async def cart_price(self, body: dict) -> dict:
        return await self.post("/carts/price", json=body)

    async def cart_order(self, body: dict) -> dict:
        return await self.post("/carts/order", json=body, is_order_post=True)

    async def list_carts(self, *, params: dict | None = None) -> list[dict]:
        payload = await self.get("/carts", params=params)
        return _extract_list(payload)

    async def list_orders(self, *, params: dict | None = None) -> list[dict]:
        """List orders by walking /carts.

        Linkuma has no flat ``GET /orders`` endpoint. We pull ``/carts`` (which
        embeds each cart's orders inline) and flatten + normalise the result.

        Normalisation:
            API field          -> MCP field
            id                 -> order_id (kept as id too)
            url                -> target_url
            type               -> tier (mapped: "Citation Boost" -> "citation_boost", ...)
            price              -> price_eur
            refusal_reason     -> refusal_reason (kept; from posts if present)
            cart.id            -> cart_id
            cart.nice_name     -> cart_nice_name + external_ref fallback

        Client-side filters applied after fetch (since the /carts endpoint does
        not honour ``status``/``project_id``/``since`` consistently):
            - status            (matches normalised order.status)
            - project_id        (matches order.project_id derived from cart)
            - since             (ISO YYYY-MM-DD; keeps orders with created_at >= since)
            - external_ref      (exact match on order.external_ref OR cart_nice_name)
        """
        # Pull all carts. We intentionally don't forward ``params`` to /carts —
        # the upstream filters are unreliable. We filter locally below instead.
        carts = await self.list_carts()
        orders: list[dict] = []
        for cart in carts:
            if not isinstance(cart, dict):
                continue
            cart_id = cart.get("id")
            cart_external_ref = cart.get("external_ref") or cart.get("nice_name")
            nice_name = cart.get("nice_name")
            cart_status = cart.get("status")
            cart_created = cart.get("created_at")
            cart_project_id = cart.get("project_id")
            inner = cart.get("orders") or []
            if not inner:
                continue
            for order in inner:
                if not isinstance(order, dict):
                    continue
                o = dict(order)
                # Normalise identifiers
                o.setdefault("order_id", o.get("id"))
                o.setdefault("cart_id", cart_id)
                o.setdefault("external_ref", cart_external_ref)
                o.setdefault("cart_nice_name", nice_name)
                o.setdefault("cart_status", cart_status)
                # Normalise project_id (carts may carry it, orders rarely do)
                if not o.get("project_id") and cart_project_id:
                    o["project_id"] = cart_project_id
                # Normalise URL
                if not o.get("target_url") and o.get("url"):
                    o["target_url"] = o["url"]
                # Normalise tier (API "type" field, e.g. "Citation Boost")
                if not o.get("tier"):
                    o["tier"] = _normalise_tier(o.get("type"))
                # Normalise price
                if o.get("price_eur") is None and o.get("price") is not None:
                    try:
                        o["price_eur"] = float(o["price"])
                    except (TypeError, ValueError):
                        pass
                # Fallback created_at
                if not o.get("created_at"):
                    o["created_at"] = cart_created
                orders.append(o)

        # ------- client-side filters
        params = params or {}
        status_filter = params.get("status")
        project_filter = params.get("project_id")
        since_filter = params.get("since")
        ext_ref_filter = params.get("external_ref")
        limit = params.get("limit")

        def _keep(o: dict) -> bool:
            if status_filter and (o.get("status") or "").lower() != status_filter.lower():
                return False
            if project_filter and str(o.get("project_id") or "") != str(project_filter):
                return False
            if ext_ref_filter and o.get("external_ref") != ext_ref_filter:
                return False
            if since_filter:
                created = o.get("created_at")
                if created and str(created)[:10] < str(since_filter)[:10]:
                    return False
            return True

        filtered = [o for o in orders if _keep(o)]
        if isinstance(limit, int) and limit > 0:
            filtered = filtered[:limit]
        return filtered

    async def get_cart(self, cart_id: str) -> dict:
        payload = await self.get(f"/carts/{cart_id}")
        return _extract_first(payload)

    async def get_order(self, order_id: str) -> dict:
        payload = await self.get(f"/orders/{order_id}")
        return _extract_first(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(raw: str | None) -> float:
    if not raw:
        return 1.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 1.0


def _check_response(resp: httpx.Response, *, is_order_post: bool) -> Any:
    status = resp.status_code

    if 200 <= status < 300:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    body: dict[str, Any]
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {"raw": resp.text}

    message = _extract_message(body) or resp.reason_phrase or f"HTTP {status}"
    details = {"status": status, "body": body}

    if status in (401, 403):
        raise LinkumaAuthError(f"auth error: {message}", details=details)
    if status == 402:
        raise LinkumaInsufficientCredit(
            f"insufficient credit: {message}", details=details
        )
    if status == 422:
        raise LinkumaValidationError(f"validation error: {message}", details=details)
    if 500 <= status < 600:
        if is_order_post:
            raise LinkumaOrderUncertain(
                "POST /carts/order returned 5xx — DO NOT retry. State unknown. "
                "Recovery: list orders with external_ref to determine state.",
                details=details,
            )
        raise LinkumaServerError(f"server error: {message}", details=details)

    raise LinkumaValidationError(f"unexpected status: {message}", details=details)


def _extract_message(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("message", "error", "detail", "title"):
            v = body.get(key)
            if isinstance(v, str):
                return v
    return None


def _extract_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "results"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


# Map Linkuma API "type" labels onto our tier/citation vocabulary.
_TIER_FROM_TYPE = {
    "citation boost": "citation_boost",
    "citation linkuma": "citation_linkuma",
    "citation": "citation_linkuma",
    "basic": "basic",
    "standard": "standard",
    "premium": "premium",
    "starter": "basic",
    "linkuma": "standard",
    "boost": "premium",
}


def _normalise_tier(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    key = raw.strip().lower()
    return _TIER_FROM_TYPE.get(key, key)


def _extract_first(payload: Any) -> dict:
    if isinstance(payload, dict):
        for key in ("data", "item", "result"):
            v = payload.get(key)
            if isinstance(v, dict):
                return v
        return payload
    if isinstance(payload, list) and payload:
        return payload[0] if isinstance(payload[0], dict) else {}
    return {}
