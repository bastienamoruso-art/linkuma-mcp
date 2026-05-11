"""Business checks used by cart_price and the campaign builders.

Everything here is pure / side-effect-free except `target_url_status`, which
performs a HEAD request — kept async so callers can decide to skip it.
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

import httpx
import tldextract

# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

_GMAPS_BAD_HOSTS = {
    "share.google",
    "maps.app.goo.gl",
    "g.co",
    "goo.gl",
}

_GMAPS_OK_RE = re.compile(
    r"^https?://(www\.)?google\.[a-z.]{2,6}/maps/place/", re.IGNORECASE
)


def is_valid_http_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_valid_gmaps_url(url: str) -> tuple[bool, str | None]:
    """Return (ok, reason_if_not).

    Rejects short URLs that have not been expanded (share.google, goo.gl,
    g.co/maps, maps.app.goo.gl). Accepts canonical desktop format
    `www.google.com/maps/place/...`.
    """
    if not url:
        return False, "empty gmaps_url"
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "not an http(s) url"

    host = parsed.netloc.lower()
    # strip leading 'www.' for comparison but keep original for the OK regex
    bare = host[4:] if host.startswith("www.") else host
    if bare in _GMAPS_BAD_HOSTS or host in _GMAPS_BAD_HOSTS:
        return False, (
            f"short gmaps url ({host}) — expand it to the canonical "
            "`www.google.com/maps/place/...` desktop format first"
        )
    # Also reject g.co/maps/* and goo.gl/maps/*
    if bare in {"g.co", "goo.gl"} and parsed.path.startswith("/maps"):
        return False, f"short gmaps url ({host}{parsed.path}) — expand it first"
    if not _GMAPS_OK_RE.match(url):
        return False, (
            "gmaps url must match `https://www.google.com/maps/place/...` "
            "(canonical desktop format)"
        )
    return True, None


def is_valid_target_domain(domain: str) -> bool:
    if not domain:
        return False
    extracted = tldextract.extract(domain)
    return bool(extracted.domain and extracted.suffix)


# ---------------------------------------------------------------------------
# Anchor over-optimisation
# ---------------------------------------------------------------------------


def normalise_anchor(anchor: str) -> str:
    return " ".join(anchor.lower().split())


def detect_anchor_over_optimisation(
    items: list[dict],
    *,
    history_pairs: list[tuple[str, str]] | None = None,
    batch_threshold: int = 2,
    history_threshold: int = 4,
) -> list[str]:
    """Return a list of warning strings.

    `items` is a list of dicts with at least `target_url` and `anchor` keys.
    `history_pairs` is an optional list of `(target_url, anchor)` from the past
    90 days. The thresholds match spec §5.5.
    """
    warnings: list[str] = []
    batch_counter: Counter[tuple[str, str]] = Counter()
    for it in items:
        key = (it["target_url"], normalise_anchor(it["anchor"]))
        batch_counter[key] += 1

    for (url, anchor), count in batch_counter.items():
        if count > batch_threshold:
            warnings.append(
                f"anchor `{anchor}` appears {count}x for `{url}` in this batch "
                f"(threshold: {batch_threshold}) — over-optimisation risk"
            )

    if history_pairs:
        history_counter: Counter[tuple[str, str]] = Counter()
        for url, anchor in history_pairs:
            history_counter[(url, normalise_anchor(anchor))] += 1
        for (url, anchor), count in batch_counter.items():
            historic = history_counter.get((url, anchor), 0)
            total = historic + count
            if total > history_threshold:
                warnings.append(
                    f"anchor `{anchor}` will reach {total} occurrences for `{url}` "
                    f"(history {historic} + batch {count}, threshold "
                    f"{history_threshold})"
                )

    return warnings


# ---------------------------------------------------------------------------
# pagekw vs anchor coherence
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD_RE.findall(text or "") if len(t) > 2}


def pagekw_anchor_coherence(pagekw: str, anchor: str, *, min_overlap: int = 1) -> bool:
    """Lightweight check: at least `min_overlap` shared tokens (len>2)."""
    if not pagekw or not anchor:
        return True  # cannot judge — let it pass
    return len(_tokens(pagekw) & _tokens(anchor)) >= min_overlap


# ---------------------------------------------------------------------------
# Live target_url status
# ---------------------------------------------------------------------------


async def target_url_status(url: str, *, timeout: float = 5.0) -> tuple[bool, int | None, str | None]:
    """Return (ok, status_code, error_str). ok = status 200 reachable."""
    if not is_valid_http_url(url):
        return False, None, "invalid url"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout
        ) as client:
            try:
                resp = await client.head(url)
                if resp.status_code in (405, 403):
                    # Some sites refuse HEAD — retry GET.
                    resp = await client.get(url)
            except httpx.HTTPError:
                resp = await client.get(url)
        return resp.status_code == 200, resp.status_code, None
    except httpx.HTTPError as exc:
        return False, None, str(exc)
