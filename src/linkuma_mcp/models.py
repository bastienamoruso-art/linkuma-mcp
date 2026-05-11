"""Pydantic models for Linkuma payloads.

All models allow extra fields (`extra="allow"`) so a schema change on Linkuma's
side does not break the MCP. Unknown fields are logged as warnings by the client.

v0.3.0 — cart item schema aligned with the live `/carts/price` and `/carts/order`
endpoints (reverse-engineered from production orders). See:
- `type` (not `tier`), `url` (not `target_url`), `map`, `project_id`, `thematic_id`,
  `category_id`, `qty`, `anchor`, `anchor_value`, `distribution`, `started_at`,
  `fast_publication`, `brief`, etc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Catalogue tiers exposed by the Linkuma `/carts/{price,order}` `type` field.
ItemType = Literal[
    "basic",
    "standard",
    "premium",
    "citation_linkuma",
    "citation_boost",
]
# Legacy alias used by suggest_tier and historical normalisation paths.
Tier = ItemType

OrderStatus = Literal[
    "pending_validation",
    "in_writing",
    "awaiting_publication",
    "published",
    "refused",
]

Anchor = Literal["url", "generic", "custom"]
Distribution = Literal["direct", "schedule"]


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
    """Single line in a `/carts/price` or `/carts/order` request.

    Aligned with the body shape that Linkuma actually accepts in production
    (reverse-engineered from successful orders, 2026-05-11). Local-only
    helper fields (`pagekw`, `nice_name`, `external_ref`) are stripped before
    POST by the cart helpers.
    """

    # ------------------------------------------------------------------ core
    type: ItemType
    url: str
    project_id: str
    thematic_id: str
    qty: int = 1

    # --------------------------------------------------------------- citation
    # `map` is the canonical desktop GMaps URL. Required for citation_* items.
    map: str | None = None
    # `category_id` complements `thematic_id` for citation_* items (e.g. Travaux
    # under Maison thematic).
    category_id: str | None = None

    # ----------------------------------------------------------------- anchor
    anchor: Anchor = "custom"
    anchor_value: str | None = None  # required when anchor == "custom"

    # --------------------------------------------------------- scheduling/dist
    distribution: Distribution = "direct"
    distribution_value: int | None = None  # only used when distribution=="schedule"
    started_at: str | None = None  # ISO date YYYY-MM-DD
    fast_publication: bool = False

    # ---------------------------------------------------------- editorial only
    brief: str | None = None
    pagekw: str | None = None  # local-only metadata for coherence checks
    improved_text: bool = False
    url2: str | None = None
    anchor2_type: str | None = None
    custom_anchor2: str | None = None
    is_no_link: bool = False
    additional_words_count: int | None = None  # 100 / 200 / 300 / 400 / 500

    # --------------------------------------------------------------- local-only
    # Helper metadata, NEVER sent upstream. Stripped before POST.
    nice_name: str | None = None
    external_ref: str | None = None


class PricedItem(_LinkumaBase):
    type: ItemType | None = None
    url: str | None = None
    anchor: str | None = None
    price_eur: float | None = None
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
    """Citation campaign line. Aligned with v0.3.0 CartItem schema."""

    type: ItemType  # citation_boost or citation_linkuma
    url: str
    map: str
    project_id: str
    thematic_id: str
    category_id: str | None = None
    qty: int = 1
    anchor: Anchor = "custom"
    anchor_value: str | None = None
    distribution: Distribution = "direct"
    started_at: str | None = None
    fast_publication: bool = False
    brief: str | None = None
    # Local helpers
    nice_name: str | None = None
    external_ref: str | None = None


class LocalCampaignPlan(_LinkumaBase):
    plan_id: str
    items: list[LocalCampaignItem]
    thematic_proposed: dict[str, Any]
    thematic_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    total_eur: float
    warnings: list[str] = Field(default_factory=list)
    confirm_token: str
    created_at: datetime
