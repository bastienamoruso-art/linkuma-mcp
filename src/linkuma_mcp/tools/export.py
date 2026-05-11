"""linkuma_export_orders — CSV export from Linkuma orders.

Zero external dependency: uses Python's stdlib `csv` module. Writes UTF-8
with a BOM-less header. If `output_path` is provided, writes to disk;
otherwise returns the CSV content inline.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..accounts import get_client as get_account_client
from ..client import LinkumaClient
from ..errors import LinkumaValidationError

# Stable column order — matches the spec.
_FIELDS = [
    "order_id",
    "external_ref",
    "project_id",
    "project_name",
    "status",
    "tier",
    "target_url",
    "anchor",
    "pagekw",
    "thematic_id",
    "published_url",
    "price_eur",
    "created_at",
    "started_at",
    "published_at",
    "refusal_reason",
]


def register(mcp: Any, get_client: Callable[[], LinkumaClient]) -> None:
    @mcp.tool()
    async def linkuma_export_orders(
        project_id: str | None = None,
        since: str | None = None,
        status: str | None = None,
        format: str = "csv",
        output_path: str | None = None,
        limit: int = 500,
        account: str | None = None,
    ) -> dict:
        """Export orders to CSV.

        - `output_path` (optional): if set, write the file and return its path.
        - Otherwise return `csv_content` inline (small jobs, <500 rows).
        - `since` ISO8601 (e.g. `2026-01-01`).
        - `status` one of pending_validation/in_writing/awaiting_publication/published/refused.
        """
        if format != "csv":
            raise LinkumaValidationError(
                f"format must be `csv` (only supported format in v0.2.0); got `{format}`"
            )

        client = get_account_client(account) if account is not None else get_client()

        params: dict[str, Any] = {"limit": limit}
        if project_id:
            params["project_id"] = project_id
        if since:
            params["since"] = since
        if status:
            params["status"] = status

        orders = await client.list_orders(params=params)

        # Build a project_id -> name map to populate `project_name` rows.
        project_names: dict[str, str] = {}
        try:
            projects = await client.list_projects()
            for p in projects:
                pid = p.get("id")
                if pid:
                    project_names[str(pid)] = p.get("name") or ""
        except Exception:  # pragma: no cover - non-blocking
            pass

        rows = [_flatten_order(o, project_names) for o in orders]

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        csv_content = buf.getvalue()

        result: dict[str, Any] = {
            "rows_count": len(rows),
            "fields": _FIELDS,
        }

        if output_path:
            path = Path(output_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(csv_content, encoding="utf-8")
            result["output_path"] = str(path)
        else:
            result["csv_content"] = csv_content

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_order(order: dict, project_names: dict[str, str]) -> dict[str, Any]:
    """Project a Linkuma order dict onto our flat CSV schema."""
    project_id = order.get("project_id") or ""
    project_name = project_names.get(str(project_id), "")
    row: dict[str, Any] = {f: "" for f in _FIELDS}
    row.update(
        {
            "order_id": order.get("order_id") or order.get("id") or "",
            "external_ref": order.get("external_ref") or "",
            "project_id": project_id,
            "project_name": project_name,
            "status": order.get("status") or "",
            "tier": order.get("tier") or "",
            "target_url": order.get("target_url") or "",
            "anchor": order.get("anchor") or "",
            "pagekw": order.get("pagekw") or "",
            "thematic_id": order.get("thematic_id") or "",
            "published_url": order.get("published_url") or "",
            "price_eur": _coerce_float(order.get("price_eur") or order.get("price")),
            "created_at": order.get("created_at") or "",
            "started_at": order.get("started_at") or "",
            "published_at": order.get("published_at") or "",
            "refusal_reason": order.get("refusal_reason") or "",
        }
    )
    return row


def _coerce_float(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""
