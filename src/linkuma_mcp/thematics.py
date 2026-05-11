"""In-memory cache of thematics, keyed by `(tier, lang)`.

Spec §5.4 — TTL: 1 hour. No on-disk cache in v1 (intentionally simple).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .models import Thematic

if TYPE_CHECKING:  # pragma: no cover
    from .client import LinkumaClient

_TTL_SECONDS = 60 * 60
_cache: dict[tuple[str, str], tuple[float, list[Thematic]]] = {}


def _parse_thematics(payload: dict) -> list[Thematic]:
    """Flatten the API response into a list of (id, name, parent_id, parent_name)."""
    items: list[Thematic] = []
    data = payload.get("data", payload)
    if isinstance(data, dict):
        data = data.get("thematics", [])
    if not isinstance(data, list):
        return items

    for parent in data:
        parent_id = parent.get("id")
        parent_name = parent.get("name")
        # Linkuma exposes either `categories` or `children` depending on tier.
        children = parent.get("categories") or parent.get("children") or []
        if not children:
            items.append(
                Thematic(
                    id=parent_id,
                    name=parent_name,
                    parent_id=None,
                    parent_name=None,
                )
            )
            continue
        for child in children:
            items.append(
                Thematic(
                    id=child.get("id"),
                    name=child.get("name"),
                    parent_id=parent_id,
                    parent_name=parent_name,
                )
            )
    return items


async def fetch_thematics(
    client: LinkumaClient, tier: str, lang: str = "fr"
) -> list[Thematic]:
    """Return cached thematics or fetch them from /thematics/{tier}."""
    key = (tier, lang)
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached[0]) < _TTL_SECONDS:
        return cached[1]

    payload = await client.get(f"/thematics/{tier}", params={"lang": lang})
    items = _parse_thematics(payload)
    _cache[key] = (now, items)
    return items


def clear_cache() -> None:
    _cache.clear()
