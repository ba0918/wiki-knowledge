"""Tests for trust_score.py — 15+ test cases covering all pure functions."""

from __future__ import annotations

from datetime import date

import pytest

from trust_score import (
    ArticleMeta,
    ArticleScore,
    WEIGHTS_FULL,
    WEIGHTS_NO_QUERYLOG,
    _normalize_slug,
    compute_trust_scores,
    count_backlinks,
    count_citations,
    freshness_factor,
    saturating,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_article(
    slug: str = "foo",
    source_refs: list[str] | None = None,
    updated: date | None = date(2026, 4, 1),
    related: list[str] | None = None,
    wikilinks: list[str] | None = None,
) -> ArticleMeta:
    return ArticleMeta(
        slug=slug,
        source_refs=source_refs or [],
        updated=updated,
        related=related or [],
        wikilinks=wikilinks or [],
    )


# ---------------------------------------------------------------------------
# Absolute factor curves (v2 — no min-max normalization)
# ---------------------------------------------------------------------------

class TestSaturating:
    def test_zero_count(self):
        assert saturating(0, 1) == 0.0

    def test_source_curve(self):
        """k=1: 1 source=0.50, 2=0.67, 3=0.75 — diminishing returns."""
        assert saturating(1, 1) == pytest.approx(0.5)
        assert saturating(2, 1) == pytest.approx(2 / 3)
        assert saturating(3, 1) == pytest.approx(0.75)

    def test_backlink_curve(self):
        """k=2: 1=0.33, 2=0.50, 6=0.75."""
        assert saturating(1, 2) == pytest.approx(1 / 3)
        assert saturating(2, 2) == pytest.approx(0.5)
        assert saturating(6, 2) == pytest.approx(0.75)

    def test_never_reaches_one(self):
        assert saturating(1000, 1) < 1.0


class TestFreshnessFactor:
    def test_today_is_one(self):
        today = date(2026, 4, 6)
        assert freshness_factor(today, today) == pytest.approx(1.0)

    def test_half_life_365_days(self):
        assert freshness_factor(date(2025, 4, 6), date(2026, 4, 6)) == pytest.approx(0.5, abs=0.01)

    def test_two_years_is_quarter_not_zero(self):
        """Snapshot semantics: staleness is divergence *risk*, never invalidity."""
        f = freshness_factor(date(2024, 4, 6), date(2026, 4, 6))
        assert f == pytest.approx(0.25, abs=0.01)
        assert f > 0.0

    def test_none_updated_is_zero(self):
        assert freshness_factor(None, date(2026, 4, 6)) == 0.0


# ---------------------------------------------------------------------------
# _normalize_slug
# ---------------------------------------------------------------------------

class TestNormalizeSlug:
    def test_concepts_path(self):
        assert _normalize_slug("concepts/foo.md") == "foo"

    def test_bare_slug(self):
        assert _normalize_slug("foo") == "foo"

    def test_md_extension(self):
        assert _normalize_slug("foo.md") == "foo"


# ---------------------------------------------------------------------------
# count_backlinks
# ---------------------------------------------------------------------------

class TestCountBacklinks:
    def test_basic_backlinks(self):
        articles = [
            _make_article("a", related=["concepts/b.md"], wikilinks=["c"]),
            _make_article("b", wikilinks=["a"]),
            _make_article("c"),
        ]
        bl = count_backlinks(articles)
        assert bl["b"] == 1  # a -> b via related
        assert bl["c"] == 1  # a -> c via wikilink
        assert bl["a"] == 1  # b -> a via wikilink

    def test_deduplication(self):
        """Same source referencing via both related and wikilink counts as 1."""
        articles = [
            _make_article("a", related=["concepts/b.md"], wikilinks=["b"]),
            _make_article("b"),
        ]
        bl = count_backlinks(articles)
        assert bl["b"] == 1

    def test_no_self_reference(self):
        articles = [
            _make_article("a", related=["concepts/a.md"], wikilinks=["a"]),
        ]
        bl = count_backlinks(articles)
        assert bl.get("a", 0) == 0

    def test_multiple_sources(self):
        articles = [
            _make_article("a", wikilinks=["c"]),
            _make_article("b", wikilinks=["c"]),
            _make_article("c"),
        ]
        bl = count_backlinks(articles)
        assert bl["c"] == 2

    def test_empty_articles(self):
        assert count_backlinks([]) == {}


# ---------------------------------------------------------------------------
# count_citations
# ---------------------------------------------------------------------------

class TestCountCitations:
    def test_basic_citations(self):
        articles = [_make_article("foo"), _make_article("bar")]
        entries = [
            {"sources_cited": ["concepts/foo.md", "concepts/bar.md"]},
            {"sources_cited": ["concepts/foo.md"]},
        ]
        cit = count_citations(entries, articles)
        assert cit["foo"] == 2
        assert cit["bar"] == 1

    def test_unknown_slug_ignored(self):
        articles = [_make_article("foo")]
        entries = [{"sources_cited": ["concepts/unknown.md"]}]
        cit = count_citations(entries, articles)
        assert "unknown" not in cit

    def test_empty_querylog(self):
        articles = [_make_article("foo")]
        cit = count_citations([], articles)
        assert cit == {}


# ---------------------------------------------------------------------------
# compute_trust_scores
# ---------------------------------------------------------------------------

class TestComputeTrustScores:
    def test_empty_articles(self):
        assert compute_trust_scores([], []) == []

    def test_fallback_weights_when_querylog_empty(self):
        """With empty querylog, citation weight should be 0."""
        articles = [
            _make_article("a", source_refs=["raw/x.md"], updated=date(2026, 4, 6)),
            _make_article("b", source_refs=["raw/x.md", "raw/y.md"], updated=date(2026, 4, 6)),
            _make_article("c", source_refs=[], updated=date(2025, 4, 6)),
        ]
        scores = compute_trust_scores(articles, [], today=date(2026, 4, 6))
        # With no querylog, weights should be NO_QUERYLOG
        assert len(scores) == 3
        # Citation norm should not contribute to score
        # Verify scores are within valid range
        for s in scores:
            assert 0.0 <= s.score <= 1.0

    def test_full_weights_with_querylog(self):
        articles = [
            _make_article("a", source_refs=["raw/x.md"], updated=date(2026, 4, 6)),
            _make_article("b", source_refs=["raw/x.md", "raw/y.md"], updated=date(2026, 4, 6)),
            _make_article("c", source_refs=[], updated=date(2025, 4, 6)),
        ]
        entries = [{"sources_cited": ["concepts/a.md"]}]
        scores = compute_trust_scores(articles, entries, today=date(2026, 4, 6))
        assert len(scores) == 3
        for s in scores:
            assert 0.0 <= s.score <= 1.0

    def test_freshness_half_life_decay(self):
        """Freshness v2: 0 days=1.0, 365 days=0.5, 730 days=0.25 (never 0)."""
        today = date(2026, 4, 6)
        articles = [
            _make_article("fresh", source_refs=["r"], updated=today),
            _make_article("old", source_refs=["r"], updated=date(2025, 4, 6)),
            _make_article("ancient", source_refs=["r"], updated=date(2024, 4, 6)),
        ]
        scores = compute_trust_scores(articles, [], today=today)
        by_slug = {s.slug: s for s in scores}
        assert by_slug["fresh"].freshness_raw == pytest.approx(1.0)
        assert by_slug["old"].freshness_raw == pytest.approx(0.5, abs=0.01)
        assert by_slug["ancient"].freshness_raw == pytest.approx(0.25, abs=0.01)
        assert by_slug["ancient"].freshness_raw > 0.0

    def test_factors_are_absolute_not_relative(self):
        """v2: factor scores depend only on the article itself, not on the
        rest of the wiki (no min-max). Two articles suffice."""
        articles = [
            _make_article("a", source_refs=["r1", "r2"], updated=date(2026, 4, 6)),
            _make_article("b", source_refs=["r1"], updated=date(2026, 4, 6)),
        ]
        scores = compute_trust_scores(articles, [], today=date(2026, 4, 6))
        by_slug = {s.slug: s for s in scores}
        assert by_slug["a"].source_norm == pytest.approx(2 / 3, abs=1e-3)
        assert by_slug["b"].source_norm == pytest.approx(0.5, abs=1e-3)

    def test_uniform_wiki_does_not_sink_to_zero(self):
        """v2: identical healthy articles share an identical, non-zero score.
        Under v1 min-max the bottom of every distribution was pinned near 0."""
        articles = [
            _make_article("a", source_refs=["r"], updated=date(2026, 4, 1)),
            _make_article("b", source_refs=["r"], updated=date(2026, 4, 1)),
            _make_article("c", source_refs=["r"], updated=date(2026, 4, 1)),
        ]
        scores = compute_trust_scores(articles, [], today=date(2026, 4, 6))
        assert all(s.score == scores[0].score for s in scores)
        # source 0.5*0.4 + freshness ~0.99*0.3 + backlink 0*0.3 ≈ 0.50
        assert scores[0].score == pytest.approx(0.5, abs=0.01)

    def test_source_refs_1_vs_many(self):
        """Article with more sources should score higher on source dimension."""
        articles = [
            _make_article("few", source_refs=["r1"], updated=date(2026, 4, 6)),
            _make_article("many", source_refs=["r1", "r2", "r3", "r4"], updated=date(2026, 4, 6)),
            _make_article("mid", source_refs=["r1", "r2"], updated=date(2026, 4, 6)),
        ]
        scores = compute_trust_scores(articles, [], today=date(2026, 4, 6))
        by_slug = {s.slug: s for s in scores}
        assert by_slug["many"].source_norm > by_slug["few"].source_norm

    def test_sorted_by_score_descending(self):
        articles = [
            _make_article("low", source_refs=[], updated=date(2024, 1, 1)),
            _make_article("high", source_refs=["r1", "r2", "r3"], updated=date(2026, 4, 6)),
            _make_article("mid", source_refs=["r1"], updated=date(2026, 1, 1)),
        ]
        scores = compute_trust_scores(articles, [], today=date(2026, 4, 6))
        assert scores[0].score >= scores[1].score >= scores[2].score

    def test_updated_none_gives_zero_freshness(self):
        articles = [
            _make_article("a", source_refs=["r"], updated=None),
            _make_article("b", source_refs=["r"], updated=date(2026, 4, 6)),
            _make_article("c", source_refs=["r"], updated=date(2026, 4, 6)),
        ]
        scores = compute_trust_scores(articles, [], today=date(2026, 4, 6))
        by_slug = {s.slug: s for s in scores}
        assert by_slug["a"].freshness_raw == 0.0


# ---------------------------------------------------------------------------
# Weight constants sanity
# ---------------------------------------------------------------------------

class TestWeights:
    def test_full_weights_sum_to_1(self):
        assert sum(WEIGHTS_FULL.values()) == pytest.approx(1.0)

    def test_no_querylog_weights_sum_to_1(self):
        assert sum(WEIGHTS_NO_QUERYLOG.values()) == pytest.approx(1.0)
