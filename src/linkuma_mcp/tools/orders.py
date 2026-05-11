"""linkuma_orders_list / linkuma_orders_get."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..client import LinkumaClient
from ..errors import LinkumaValidationError

# Status values accepted on input.
#
# Linkuma's API actually returns short-form statuses on each order
# (`pending`, `published`, `refused`, ...). The legacy "spec" labels
# (`pending_validation`, `in_writing`, `awaiting_publication`) are kept here
# for backwards compatibility but will be matched against the normalised
# short form in the client filter.
_VALID_STATUS = {
    # short-form (current Linkuma API)
    "pending",
    "in_progress",
    "published",
    "refused",
    "cancelled",
    # legacy / spec labels (still accepted)
    "pending_validation",
    "in_writing",
    "awaiting_publication",
}


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_orders_list(
        project_id: str | None = None,
        status: str | None = None,
        since: str | None = None,
        external_ref: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List orders, optionally filtered by project / status / since / external_ref.

        `status` accepts: pending_validation, in_writing, awaiting_publication,
        published, refused. `since` is ISO8601 (e.g. `2026-05-01`). Pass
        `external_ref` to look up a single order by its idempotency key — this
        is the recovery path after a `LinkumaOrderUncertain` error.
        """
        if status and status not in _VALID_STATUS:
            raise LinkumaValidationError(
                f"status must be one of {sorted(_VALID_STATUS)}; got `{status}`"
            )
        params: dict[str, Any] = {"limit": limit}
        if project_id:
            params["project_id"] = project_id
        if status:
            params["status"] = status
        if since:
            params["since"] = since
        if external_ref:
            params["external_ref"] = external_ref

        client = get_client()
        return await client.list_orders(params=params)

    @mcp.tool()
    async def linkuma_orders_get(order_id: str) -> dict:
        """Return the full detail of a single order, including state history."""
        if not order_id:
            raise LinkumaValidationError("order_id is required")
        client = get_client()
        return await client.get_order(order_id)
