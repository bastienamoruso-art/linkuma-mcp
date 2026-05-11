"""linkuma_thematics_list."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..client import LinkumaClient
from ..errors import LinkumaValidationError
from ..thematics import fetch_thematics

_TIERS = {"basic", "standard", "premium"}


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_thematics_list(tier: str, lang: str = "fr") -> list[dict]:
        """Flat list of thematics for a tier.

        `tier` is one of `basic`, `standard`, `premium`. The MCP keeps a
        per-tier cache (1 hour TTL) so calling this repeatedly within a
        session is cheap.
        """
        if tier not in _TIERS:
            raise LinkumaValidationError(
                f"tier must be one of {sorted(_TIERS)}; got `{tier}`"
            )
        client = get_client()
        thematics = await fetch_thematics(client, tier, lang)
        return [t.model_dump() for t in thematics]
