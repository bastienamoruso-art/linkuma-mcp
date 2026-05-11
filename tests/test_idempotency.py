"""Tests for the JSON-backed idempotency store."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from linkuma_mcp import idempotency


def _store_file() -> Path:
    return Path(os.environ["LINKUMA_IDEMPOTENCY_DIR"]) / "idempotency.json"


def test_remember_then_get_round_trip() -> None:
    idempotency.remember("ref-1", {"order_id": "abc", "total_eur": 30})
    record = idempotency.get("ref-1")
    assert record is not None
    assert record["order_id"] == "abc"
    assert record["total_eur"] == 30
    assert "recorded_at" in record


def test_get_returns_none_for_unknown_key() -> None:
    assert idempotency.get("missing") is None


def test_remember_writes_pretty_json() -> None:
    idempotency.remember("ref-2", {"order_id": "xyz"})
    path = _store_file()
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "ref-2" in data


def test_remember_overwrites_same_key() -> None:
    idempotency.remember("ref-3", {"order_id": "v1"})
    idempotency.remember("ref-3", {"order_id": "v2"})
    record = idempotency.get("ref-3")
    assert record is not None and record["order_id"] == "v2"


def test_all_records_returns_snapshot() -> None:
    idempotency.remember("ref-a", {"order_id": "1"})
    idempotency.remember("ref-b", {"order_id": "2"})
    snap = idempotency.all_records()
    assert {"ref-a", "ref-b"}.issubset(set(snap))
    # Mutating the snapshot does not affect the store.
    snap["ref-c"] = {"order_id": "3"}
    assert idempotency.get("ref-c") is None


def test_corrupt_file_is_recovered() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert idempotency.get("anything") is None
    # A subsequent write should rewrite the file cleanly.
    idempotency.remember("ref-after-corrupt", {"order_id": "ok"})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "ref-after-corrupt" in data


def test_generate_external_ref_pattern() -> None:
    ref = idempotency.generate_external_ref("acme-corp")
    parts = ref.split("-")
    assert parts[0] == "acme-corp"
    assert len(parts[1]) == 8 and parts[1].isdigit()
    assert len(parts[2]) == 8


def test_generate_external_ref_default_slug() -> None:
    ref = idempotency.generate_external_ref(None)
    assert ref.startswith("lkm-")


@pytest.mark.asyncio
async def test_concurrent_remember_does_not_lose_writes() -> None:
    async def write(i: int) -> None:
        await asyncio.to_thread(idempotency.remember, f"ref-cc-{i}", {"order_id": f"o{i}"})

    await asyncio.gather(*(write(i) for i in range(20)))
    snap = idempotency.all_records()
    assert all(f"ref-cc-{i}" in snap for i in range(20))
