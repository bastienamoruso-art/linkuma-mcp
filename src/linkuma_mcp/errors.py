"""Typed exception hierarchy for Linkuma MCP."""

from __future__ import annotations


class LinkumaError(Exception):
    """Base class for all Linkuma-related errors."""

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details:
            return f"{self.message} | details={self.details}"
        return self.message


class LinkumaConfigError(LinkumaError):
    """Missing or invalid configuration (env vars, paths)."""


class LinkumaAuthError(LinkumaError):
    """401 / 403 from the API, or local key validation failure."""


class LinkumaUnreachable(LinkumaError):
    """Network failure, DNS, connection refused, etc."""


class LinkumaRateLimited(LinkumaError):
    """429 Too Many Requests. Retry-After is surfaced in details."""


class LinkumaValidationError(LinkumaError):
    """422 from the API or local schema/business validation failure."""


class LinkumaInsufficientCredit(LinkumaError):
    """Local check or 402-equivalent: not enough credit for the requested cart."""


class LinkumaBudgetExceeded(LinkumaError):
    """Cart total exceeds LINKUMA_BUDGET_CAP_EUR."""


class LinkumaDuplicateOrder(LinkumaError):
    """external_ref already seen in the local idempotency cache."""


class LinkumaOrderUncertain(LinkumaError):
    """POST /carts/order returned 5xx or timed out. State unknown.

    The caller MUST NOT retry blindly. Recovery path: call linkuma_orders_list
    filtered by external_ref to determine whether the order was created.
    """


class LinkumaConfirmTokenError(LinkumaError):
    """Missing, unknown or expired confirm_token for cart_order or campaign_execute."""


class LinkumaServerError(LinkumaError):
    """Generic 5xx from Linkuma."""
