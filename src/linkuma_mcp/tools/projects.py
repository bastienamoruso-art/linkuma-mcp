"""linkuma_projects_list / linkuma_projects_create."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..checks import is_valid_target_domain
from ..client import LinkumaClient
from ..errors import LinkumaValidationError


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_projects_list(query: str | None = None) -> list[dict]:
        """List all projects on the account.

        Optional `query` filters on a case-insensitive substring of the project
        name. Read-only.
        """
        client = get_client()
        projects = await client.list_projects()
        if query:
            q = query.lower().strip()
            projects = [p for p in projects if q in (p.get("name") or "").lower()]
        return projects

    @mcp.tool()
    async def linkuma_projects_create(
        name: str, target_domain: str, notes: str | None = None
    ) -> dict:
        """Create a project, or return the existing one with the same exact name.

        Dedup strategy: GET /projects first; if any project has exactly
        `name`, return it. Otherwise POST /projects. This makes the tool
        safe to call repeatedly with the same input.
        """
        if not name or not name.strip():
            raise LinkumaValidationError("name is required")
        if not is_valid_target_domain(target_domain):
            raise LinkumaValidationError(
                f"target_domain `{target_domain}` is not a valid public domain"
            )

        client = get_client()
        existing = await client.list_projects()
        for p in existing:
            if (p.get("name") or "").strip() == name.strip():
                return {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "target_domain": p.get("target_domain") or target_domain,
                    "deduped": True,
                }

        body = {"name": name.strip(), "target_domain": target_domain.strip()}
        if notes:
            body["notes"] = notes
        created = await client.create_project(body)
        return {
            "id": created.get("id"),
            "name": created.get("name", name),
            "target_domain": created.get("target_domain", target_domain),
            "deduped": False,
        }
