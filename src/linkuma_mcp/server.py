"""FastMCP entrypoint.

Registers 10 tools and 3 read-only resources. The client is built lazily
(first use) so importing the module without a key — for tests, for example —
does not crash.
"""

from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv
from fastmcp import FastMCP

from . import accounts
from .client import LinkumaClient
from .thematics import fetch_thematics
from .tools import (
    bulk,
    cart,
    dashboard,
    doctor,
    editorial_campaign,
    export,
    local_campaign,
    orders,
    projects,
    refused,
    suggest_tier,
    thematics,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LINKUMA_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)

mcp = FastMCP("linkuma-mcp")


def get_client() -> LinkumaClient:
    """Return the default-account client.

    v0.2.0: routes through the multi-account resolver. If
    `LINKUMA_API_KEYS_JSON` is unset, falls back to `LINKUMA_API_KEY` under
    the alias `default` — fully backwards-compatible with v0.1.0.
    """
    return accounts.get_client()


# ---------------------------------------------------------------------------
# Register tools
# ---------------------------------------------------------------------------

doctor.register(mcp, get_client)
projects.register(mcp, get_client)
thematics.register(mcp, get_client)
cart.register(mcp, get_client)
orders.register(mcp, get_client)
local_campaign.register(mcp, get_client)
# v0.2.0 — full-Linkuma tools (zero external dependency)
editorial_campaign.register(mcp, get_client)
suggest_tier.register(mcp, get_client)
export.register(mcp, get_client)
dashboard.register(mcp, get_client)
refused.register(mcp, get_client)
bulk.register(mcp, get_client)


# ---------------------------------------------------------------------------
# Resources (read-only views)
# ---------------------------------------------------------------------------


@mcp.resource("linkuma://settings/credit")
async def resource_credit() -> str:
    """Current credit balance (EUR) as JSON."""
    client = get_client()
    payload = await client.get_settings()
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.resource("linkuma://projects")
async def resource_projects() -> str:
    """All projects on the account, as JSON."""
    client = get_client()
    payload = await client.list_projects()
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.resource("linkuma://thematics")
async def resource_thematics() -> str:
    """Premium thematics for `lang=fr`, as JSON (suitable for local citations)."""
    client = get_client()
    items = await fetch_thematics(client, "premium", "fr")
    return json.dumps([t.model_dump() for t in items], ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Launch the MCP server over stdio. Used by the `linkuma-mcp` script."""
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
