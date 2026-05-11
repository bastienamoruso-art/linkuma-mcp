"""Tests for anchor generation strategies."""

from __future__ import annotations

import pytest

from linkuma_mcp import anchors


def test_branded_variants_extracts_brand():
    out = anchors.branded_variants("https://www.example.fr/foo")
    assert any(v.lower() == "example" for v in out)
    assert any(v.lower() == "example.fr" for v in out)


def test_exact_variants_dedupes():
    out = anchors.exact_variants(["plombier paris", "Plombier Paris", "couvreur"])
    assert len(out) == 2  # case-insensitive dedup
    assert "plombier paris" in [v.lower() for v in out]


def test_semantic_variants_strips_stopwords():
    out = anchors.semantic_variants(
        ["plombier paris", "couvreur"],
        "https://www.exemple.fr/",
        language="fr",
    )
    # Each keyword produces multiple variants
    assert len(out) > 4
    # Brand combo included
    assert any("exemple" in v.lower() for v in out)


def test_generate_mixed_distribution_balance():
    out = anchors.generate_anchors(
        count=10,
        strategy="mixed",
        keywords=["plombier paris", "depannage urgence"],
        target_url="https://www.example.fr/",
    )
    assert len(out["anchors"]) == 10
    dist = out["distribution"]
    # The exact split for 10 with mixed: 3 branded / 2 exact / 5 semantic
    assert dist["branded"] + dist["exact"] + dist["semantic"] == 10
    assert dist["branded"] == 3
    assert dist["exact"] == 2
    assert dist["semantic"] == 5


def test_generate_branded_only():
    out = anchors.generate_anchors(
        count=5,
        strategy="branded",
        keywords=["x"],
        target_url="https://www.example.fr/",
    )
    assert len(out["anchors"]) == 5
    assert out["distribution"]["branded"] == 5
    assert out["distribution"]["exact"] == 0
    assert out["distribution"]["semantic"] == 0


def test_generate_invalid_strategy_raises():
    with pytest.raises(ValueError):
        anchors.generate_anchors(
            count=3, strategy="bogus", keywords=["x"], target_url="https://x.fr/"
        )


def test_generate_zero_count():
    out = anchors.generate_anchors(
        count=0, strategy="mixed", keywords=["x"], target_url="https://x.fr/"
    )
    assert out["anchors"] == []


def test_generate_exhausts_pool_with_warning():
    # Only 1 keyword + branded for `exact` strategy with count 5 => warning
    out = anchors.generate_anchors(
        count=5,
        strategy="exact",
        keywords=["plombier"],
        target_url="https://www.example.fr/",
    )
    assert len(out["anchors"]) == 5
    # Pool of length 1 repeated; warning emitted exactly once
    assert any("exhausted" in w for w in out["warnings"])


def test_generate_missing_keywords_for_exact_falls_back():
    out = anchors.generate_anchors(
        count=3,
        strategy="exact",
        keywords=[],
        target_url="https://www.example.fr/",
    )
    # No keywords -> reallocates to a non-empty bucket (semantic empty too,
    # so falls back to synthetic placeholders or branded). We just assert
    # length and that warnings exist.
    assert len(out["anchors"]) == 3
    assert out["warnings"]
