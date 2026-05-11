"""Pydantic models for Linkuma payloads.

All models allow extra fields (`extra="allow"`) so a schema change on Linkuma's
side does not break the MCP. Unknown fields are logged as warnings by the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Tier = Literal["basic", "standard", "premium"]
OrderStatus = Literal[
    "pending_validation",
    "in_writing",
    "awaiting_publication",
    "published",
    "refused",
]


class _LinkumaBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class Project(_LinkumaBase):
    id: str
    name: str
    target_domain: str | None = None
    created_at: datetime | None = None


class Thematic(_LinkumaBase):
    id: str
    name: str
    parent_id: str | None = None
    parent_name: str | None = None


class CartItem(_LinkumaBase):
    """Single line in a price/order request.

    Fields aligned with /carts/price and /carts/order. `pagekw` is local-only
    metadata used by anchor/pagekw coherence checks; it is stripped before
    POST if not part of the upstream schema.
    """

    tier: Tier
    thematic_id: str
    target_url: str
    anchor: str
    pagekw: str | None = None
    publish_date: str | None = None  # ISO date (YYYY-MM-DD)
    # Optional local citation fields (only used by local_campaign tools).
    gmaps_url: str | None = None
    brief: str | None = None
    nice_name: str | None = None


class PricedItem(_LinkumaBase):
    tier: Tier
    target_url: str
    anchor: str
    price_eur: float
    thematic_id: str | None = None


class CartPriceResponse(_LinkumaBase):
    total_eur: float
    items_priced: list[PricedItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    credit_after_eur: float | None = None
    confirm_token: str | None = None  # populated by the MCP layer


class Order(_LinkumaBase):
    order_id: str
    external_ref: str | None = None
    project_id: str | None = None
    status: OrderStatus | str | None = None
    tier: Tier | None = None
    target_url: str | None = None
    anchor: str | None = None
    published_url: str | None = None
    created_at: datetime | None = None
    refusal_reason: str | None = None


class CartOrderResponse(_LinkumaBase):
    order_id: str
    external_ref: str
    total_eur: float
    credit_after_eur: float | None = None
    items_ordered: list[dict[str, Any]] = Field(default_factory=list)


class Settings(_LinkumaBase):
    credit_eur: float = 0.0
    user_email: str | None = None


class LocalCampaignItem(_LinkumaBase):
    tier: Tier
    thematic_id: str
    target_url: str
    gmaps_url: str
    anchor: str
    publish_date: str
    nice_name: str
    external_ref: str
    brief: str | None = None


class LocalCampaignPlan(_LinkumaBase):
    plan_id: str
    items: list[LocalCampaignItem]
    thematic_proposed: dict[str, Any]
    thematic_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    total_eur: float
    warnings: list[str] = Field(default_factory=list)
    confirm_token: str
    created_at: datetime
