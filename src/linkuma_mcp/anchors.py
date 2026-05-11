"""Anchor generation strategies for editorial campaigns.

Built-in anti-over-optimisation: a `mixed` strategy distributes 30% branded /
20% exact / 50% semantic anchors, which keeps the link profile defensible
against algorithmic penalties (Penguin et al.).

All generators are deterministic given the same seed (so a plan -> execute
cycle can replay the exact same anchor sequence).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import tldextract

# Distribution targets for the `mixed` strategy (kept conservative).
MIXED_DISTRIBUTION = {"branded": 0.30, "exact": 0.20, "semantic": 0.50}

# Common French stopwords to strip when synthesising semantic variants.
_FR_STOPWORDS = {
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "pour",
    "avec", "sans", "sur", "par", "en", "au", "aux", "ce", "ces", "cet",
    "cette", "votre", "mon", "ma", "mes", "ton", "ta", "tes",
    "son", "sa", "ses", "notre", "nos", "vos", "leur", "leurs",
}

_WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)


# ---------------------------------------------------------------------------
# Branded anchors — derived from the target URL's registered domain
# ---------------------------------------------------------------------------


def _registered_brand(target_url: str) -> str:
    """Return the registered brand name from a URL (no TLD, no www).

    `https://www.example.fr/page` -> `example`. Falls back to host if
    tldextract cannot parse it.
    """
    parsed = urlparse(target_url.strip())
    host = parsed.netloc or target_url
    extracted = tldextract.extract(host)
    if extracted.domain:
        return extracted.domain
    return host.replace("www.", "")


def branded_variants(target_url: str) -> list[str]:
    """Return a small set of branded anchor variants.

    Includes the bare brand, the brand titled, "Brand.tld", and the full
    canonical host without scheme. Deduplicated, order-preserving.
    """
    parsed = urlparse(target_url.strip())
    brand = _registered_brand(target_url)
    extracted = tldextract.extract(parsed.netloc or target_url)
    tld = extracted.suffix or ""
    host = parsed.netloc or target_url

    candidates = [
        brand,
        brand.title(),
        brand.capitalize(),
        f"{brand}.{tld}" if tld else brand,
        host.replace("www.", ""),
    ]
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        c = c.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Exact anchors — straight from the keyword list
# ---------------------------------------------------------------------------


def exact_variants(keywords: list[str]) -> list[str]:
    """Return a deduped, trimmed copy of `keywords`."""
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords or []:
        cleaned = " ".join((kw or "").split())
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            out.append(cleaned)
    return out


# ---------------------------------------------------------------------------
# Semantic anchors — LSI-style variations without any external API
# ---------------------------------------------------------------------------

# Small, hand-curated heuristics. We deliberately keep this lightweight to
# avoid shipping a third-party NLP dependency.
_SEMANTIC_PREFIXES = ["en savoir plus sur", "découvrez", "consultez", "voir"]
_SEMANTIC_SUFFIXES = ["en ligne", "sur le site", "officiel"]
_CALL_TO_READ = ["cliquez ici", "lire la suite", "plus d'infos", "en savoir plus"]


def _tokenise(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall(text or "") if len(t) > 1]


def semantic_variants(
    keywords: list[str], target_url: str, language: str = "fr"
) -> list[str]:
    """Generate semantic / LSI-style anchor variants.

    Strategy (deterministic):
    - For each keyword, drop trailing stopwords and emit the bare noun phrase.
    - Combine with `_SEMANTIC_PREFIXES` and `_SEMANTIC_SUFFIXES`.
    - Add generic CTAs as last-resort entries (kept low priority via order).
    - Add the brand combined with the first keyword ("brand keyword").

    `language` is currently fr-only; falls back to fr behavior otherwise.
    """
    _ = language  # reserved for future i18n
    brand = _registered_brand(target_url)
    variants: list[str] = []
    seen: set[str] = set()

    def _push(s: str) -> None:
        cleaned = " ".join(s.split())
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            variants.append(cleaned)

    for kw in keywords or []:
        tokens = [t for t in _tokenise(kw) if t.lower() not in _FR_STOPWORDS]
        if not tokens:
            continue
        bare = " ".join(tokens)
        _push(bare)
        for prefix in _SEMANTIC_PREFIXES:
            _push(f"{prefix} {bare}")
        for suffix in _SEMANTIC_SUFFIXES:
            _push(f"{bare} {suffix}")
        _push(f"{brand} {bare}")

    # Generic CTAs — appended last, so a caller that asks for N variants gets
    # keyword-derived ones first.
    for cta in _CALL_TO_READ:
        _push(cta)

    return variants


# ---------------------------------------------------------------------------
# Top-level: build a sequence of N anchors with the requested strategy
# ---------------------------------------------------------------------------


_VALID_STRATEGIES = {"branded", "exact", "semantic", "mixed"}


def _round_distribution(count: int, ratios: dict[str, float]) -> dict[str, int]:
    """Convert ratios to integer counts that sum to `count`.

    Uses largest-remainder rounding for fairness.
    """
    raw = {k: ratios[k] * count for k in ratios}
    floors = {k: int(v) for k, v in raw.items()}
    remainder = count - sum(floors.values())
    if remainder > 0:
        # Distribute the leftover to the buckets with the largest fractional part.
        fracs = sorted(
            ((k, raw[k] - floors[k]) for k in ratios),
            key=lambda x: x[1],
            reverse=True,
        )
        for k, _ in fracs[:remainder]:
            floors[k] += 1
    return floors


def generate_anchors(
    count: int,
    *,
    strategy: str = "mixed",
    keywords: list[str] | None = None,
    target_url: str = "",
    language: str = "fr",
) -> dict:
    """Generate `count` anchors with the requested strategy.

    Returns:
        {
          "anchors": [str, ...] of length `count`,
          "distribution": {"branded": N, "exact": N, "semantic": N},
          "warnings": [str, ...],
        }

    Anti-duplication: anchors cycle through each bucket independently. If a
    bucket is exhausted we reuse it (with a warning) rather than crash.
    """
    if strategy not in _VALID_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {sorted(_VALID_STRATEGIES)}; got `{strategy}`"
        )
    if count <= 0:
        return {"anchors": [], "distribution": {}, "warnings": []}

    keywords = keywords or []
    warnings: list[str] = []

    branded = branded_variants(target_url) if target_url else []
    exact = exact_variants(keywords)
    semantic = semantic_variants(keywords, target_url, language=language)

    if strategy == "branded":
        plan = {"branded": count, "exact": 0, "semantic": 0}
    elif strategy == "exact":
        plan = {"branded": 0, "exact": count, "semantic": 0}
    elif strategy == "semantic":
        plan = {"branded": 0, "exact": 0, "semantic": count}
    else:
        plan = _round_distribution(count, MIXED_DISTRIBUTION)

    pools = {"branded": branded, "exact": exact, "semantic": semantic}
    anchors: list[str] = []
    actual = {"branded": 0, "exact": 0, "semantic": 0}

    for bucket in ("branded", "exact", "semantic"):
        wanted = plan[bucket]
        pool = pools[bucket]
        if wanted == 0:
            continue
        if not pool:
            warnings.append(
                f"strategy `{strategy}` requested {wanted} `{bucket}` anchors but "
                f"no source data was available — falling back to other buckets"
            )
            # Reallocate to the largest non-empty bucket.
            for fallback in ("semantic", "exact", "branded"):
                if pools[fallback]:
                    plan[fallback] += wanted
                    break
            else:
                # No pool has anything — emit synthetic placeholders.
                for _ in range(wanted):
                    anchors.append("en savoir plus")
                    actual[bucket] += 1
            continue
        for i in range(wanted):
            anchor = pool[i % len(pool)]
            anchors.append(anchor)
            actual[bucket] += 1
            if i >= len(pool):
                # Repeated from the same pool — flag it ONCE per bucket.
                if not any(
                    f"bucket `{bucket}` was exhausted" in w for w in warnings
                ):
                    warnings.append(
                        f"bucket `{bucket}` was exhausted; repeating its entries "
                        "to reach the requested count"
                    )

    return {
        "anchors": anchors,
        "distribution": actual,
        "warnings": warnings,
    }
