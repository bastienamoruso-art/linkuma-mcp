"""Local JSON-backed idempotency store.

Used to dedupe `POST /carts/order` calls across processes / sessions / crashes.
Keyed by `external_ref`. Read AND mutate under a file lock so concurrent runs
do not corrupt the store.

The implementation purposefully avoids `portalocker` to keep dependencies
minimal — we use `fcntl.flock` on POSIX, which covers macOS / Linux (the
expected deployment targets). On Windows the lock degrades to a best-effort
in-process threading.Lock; that is documented as a known limitation.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl  # type: ignore[import-not-found]

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows only
    _HAS_FCNTL = False

_DEFAULT_DIR = Path.home() / ".linkuma-mcp"
_FILE_NAME = "idempotency.json"
_INPROC_LOCK = threading.Lock()


def _store_dir() -> Path:
    raw = os.getenv("LINKUMA_IDEMPOTENCY_DIR", "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_DIR


def _store_path() -> Path:
    d = _store_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / _FILE_NAME


@contextlib.contextmanager
def _locked_file(mode: str) -> Iterator[Any]:
    """Open the store with a cross-process exclusive lock."""
    path = _store_path()
    if not path.exists():
        path.write_text("{}", encoding="utf-8")

    with _INPROC_LOCK:
        fh = path.open(mode, encoding="utf-8")
        try:
            if _HAS_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
            yield fh
        finally:
            try:
                if _HAS_FCNTL:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            finally:
                fh.close()


def _read_all() -> dict[str, dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        with _locked_file("r") as fh:
            text = fh.read().strip()
            return json.loads(text) if text else {}
    except json.JSONDecodeError:
        # Corrupt store — return empty and let the next write rebuild it.
        return {}


def get(external_ref: str) -> dict[str, Any] | None:
    """Return the cached record for an external_ref, or None."""
    data = _read_all()
    return data.get(external_ref)


def remember(external_ref: str, payload: dict[str, Any]) -> None:
    """Persist a record. Overwrites any previous entry under the same key."""
    with _locked_file("r+") as fh:
        text = fh.read().strip()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {}
        record = dict(payload)
        record.setdefault("recorded_at", datetime.now(UTC).isoformat())
        data[external_ref] = record
        fh.seek(0)
        fh.truncate()
        json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)


def all_records() -> dict[str, dict[str, Any]]:
    """Snapshot of the whole store (defensive copy)."""
    return dict(_read_all())


def generate_external_ref(project_slug: str | None = None) -> str:
    """`{project_slug}-{YYYYMMDD}-{nanoid8}` per spec section 1 + 5.6."""
    from nanoid import generate

    slug = (project_slug or "lkm").strip().lower() or "lkm"
    date = time.strftime("%Y%m%d", time.gmtime())
    suffix = generate(size=8, alphabet="abcdefghijklmnopqrstuvwxyz0123456789")
    return f"{slug}-{date}-{suffix}"
