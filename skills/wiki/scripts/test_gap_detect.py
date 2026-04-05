#!/usr/bin/env python3
"""Tests for gap_detect.py — Gap Detection + Auto Ingest proposal engine."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

# We'll import after the module exists; tests are written first (Red phase).
from gap_detect import (
    ArticleInfo,
    ConfirmedGap,
    IngestProposal,
    compute_coverage,
    detect_gaps,
    extract_tokens,
    format_json,
    format_report,
    generate_proposals,
    load_articles,
)


# ---------------------------------------------------------------------------
# TestExtractTokens
# ---------------------------------------------------------------------------

class TestExtractTokens:
    """extract_tokens: テキストをトークン集合に変換する純粋関数."""

    def test_basic_english(self):
        result = extract_tokens("Hello World")
        assert "hello" in result
        assert "world" in result

    def test_hyphenated(self):
        result = extract_tokens("gap-detection auto_ingest")
        assert "gap" in result
        assert "detection" in result
        assert "auto" in result
        assert "ingest" in result

    def test_empty_string(self):
        result = extract_tokens("")
        assert result == frozenset()

    def test_japanese_bigram(self):
        result = extract_tokens("知識ベース")
        # bigrams: 知識, 識ベ, ベー, ース
        assert "知識" in result
        assert "ベー" in result

    def test_mixed_text(self):
        result = extract_tokens("LLM知識ベース")
        # ASCII part: "llm"
        assert "llm" in result
        # Japanese bigrams
        assert "知識" in result


# ---------------------------------------------------------------------------
# TestComputeCoverage
# ---------------------------------------------------------------------------

class TestComputeCoverage:
    """compute_coverage: トークン重複率でカバレッジを計算する純粋関数."""

    def _make_article(self, slug: str, tokens: frozenset[str]) -> ArticleInfo:
        return ArticleInfo(
            slug=slug,
            title=slug,
            tags=(),
            body="",
            tokens=tokens,
        )

    def test_full_match(self):
        topic_tokens = frozenset({"gap", "detection"})
        articles = [self._make_article("a", frozenset({"gap", "detection", "extra"}))]
        coverage, related = compute_coverage(topic_tokens, articles)
        assert coverage == 1.0

    def test_partial_match(self):
        topic_tokens = frozenset({"gap", "detection"})
        articles = [self._make_article("a", frozenset({"gap", "other"}))]
        coverage, related = compute_coverage(topic_tokens, articles)
        assert coverage == 0.5

    def test_no_match(self):
        topic_tokens = frozenset({"gap", "detection"})
        articles = [self._make_article("a", frozenset({"unrelated"}))]
        coverage, related = compute_coverage(topic_tokens, articles)
        assert coverage == 0.0

    def test_single_token(self):
        topic_tokens = frozenset({"llm"})
        articles = [self._make_article("a", frozenset({"llm", "wiki"}))]
        coverage, related = compute_coverage(topic_tokens, articles)
        assert coverage == 1.0


# ---------------------------------------------------------------------------
# TestDetectGaps
# ---------------------------------------------------------------------------

class TestDetectGaps:
    """detect_gaps: QueryLog エントリからギャップを検出する純粋関数."""

    def _make_article(self, slug: str, tokens: frozenset[str]) -> ArticleInfo:
        return ArticleInfo(
            slug=slug,
            title=slug,
            tags=(),
            body="",
            tokens=tokens,
        )

    def test_uncovered_topic(self):
        entries = [
            {"gap_noted": True, "gap_topics": ["quantum computing"]},
            {"gap_noted": True, "gap_topics": ["quantum computing"]},
        ]
        articles = [self._make_article("llm", frozenset({"llm", "wiki"}))]
        gaps = detect_gaps(entries, articles, threshold=0.8)
        assert len(gaps) == 1
        assert gaps[0].topic == "quantum computing"
        assert gaps[0].frequency == 2

    def test_all_covered(self):
        entries = [
            {"gap_noted": True, "gap_topics": ["llm wiki"]},
        ]
        articles = [self._make_article("llm-wiki", frozenset({"llm", "wiki"}))]
        gaps = detect_gaps(entries, articles, threshold=0.8)
        assert len(gaps) == 0

    def test_empty_querylog(self):
        articles = [self._make_article("a", frozenset({"x"}))]
        gaps = detect_gaps([], articles, threshold=0.8)
        assert gaps == []

    def test_threshold_control(self):
        entries = [
            {"gap_noted": True, "gap_topics": ["gap detection"]},
        ]
        # Article covers "gap" but not "detection" -> coverage = 0.5
        articles = [self._make_article("a", frozenset({"gap", "other"}))]
        # With threshold 0.4, coverage 0.5 >= 0.4 -> not a gap
        gaps_high = detect_gaps(entries, articles, threshold=0.4)
        assert len(gaps_high) == 0
        # With threshold 0.8, coverage 0.5 < 0.8 -> gap
        gaps_low = detect_gaps(entries, articles, threshold=0.8)
        assert len(gaps_low) == 1

    def test_related_articles(self):
        entries = [
            {"gap_noted": True, "gap_topics": ["gap detection"]},
        ]
        # Article covers "gap" -> partial match (0.5), above half-threshold for related
        articles = [self._make_article("partial", frozenset({"gap", "other"}))]
        gaps = detect_gaps(entries, articles, threshold=0.8)
        assert len(gaps) == 1
        assert "partial" in gaps[0].related_articles


# ---------------------------------------------------------------------------
# TestGenerateProposals
# ---------------------------------------------------------------------------

class TestGenerateProposals:
    """generate_proposals: ギャップから優先度付き提案を生成する純粋関数."""

    def test_priority_calculation(self):
        gaps = [
            ConfirmedGap(topic="topic-a", frequency=10, coverage=0.2, related_articles=()),
            ConfirmedGap(topic="topic-b", frequency=5, coverage=0.5, related_articles=()),
        ]
        proposals = generate_proposals(gaps)
        assert len(proposals) == 2
        # priority = freq * (1-cov), normalized to 0-1
        # topic-a: 10 * 0.8 = 8.0  (max)
        # topic-b: 5 * 0.5 = 2.5
        # Normalized: a=1.0, b=2.5/8.0=0.3125
        assert proposals[0].topic == "topic-a"
        assert proposals[0].priority == 1.0

    def test_suggested_queries(self):
        gaps = [
            ConfirmedGap(topic="quantum computing", frequency=3, coverage=0.1, related_articles=()),
        ]
        proposals = generate_proposals(gaps)
        assert len(proposals) == 1
        assert any("quantum computing" in q for q in proposals[0].suggested_queries)

    def test_empty_list(self):
        proposals = generate_proposals([])
        assert proposals == []


# ---------------------------------------------------------------------------
# TestFormatters
# ---------------------------------------------------------------------------

class TestFormatters:
    """format_json / format_report: 出力フォーマッタのテスト."""

    def _sample_data(self):
        gaps = [
            ConfirmedGap(topic="quantum", frequency=5, coverage=0.1, related_articles=("llm",)),
        ]
        proposals = [
            IngestProposal(
                topic="quantum",
                priority=0.9,
                suggested_queries=("quantum computing wiki",),
                related_articles=("llm",),
            ),
        ]
        return gaps, proposals

    def test_format_json_valid(self):
        gaps, proposals = self._sample_data()
        output = format_json(gaps, proposals)
        data = json.loads(output)
        assert "gaps" in data
        assert "proposals" in data
        assert len(data["gaps"]) == 1
        assert len(data["proposals"]) == 1

    def test_format_report_header(self):
        gaps, proposals = self._sample_data()
        from datetime import date

        output = format_report(gaps, proposals, date(2026, 4, 6))
        assert "# Gap Detection Report" in output
        assert "2026-04-06" in output


# ---------------------------------------------------------------------------
# TestIntegration
# ---------------------------------------------------------------------------

class TestIntegration:
    """tmp_path を使ったフルパイプライン統合テスト."""

    def _setup_wiki(self, tmp_path: Path, querylog_lines: list[str] | None = None):
        """Helper: Create a minimal wiki structure in tmp_path."""
        concepts = tmp_path / "concepts"
        concepts.mkdir(parents=True)

        # Create a sample article
        article = concepts / "llm-wiki.md"
        article.write_text(
            textwrap.dedent("""\
            ---
            title: LLM Wiki
            type: concepts
            tags:
              - llm
              - wiki
            source_refs:
              - raw/articles/src.md
            created: "2026-01-01"
            updated: "2026-04-01"
            category: concepts
            ---

            # LLM Wiki

            This is about [[llm-wiki-knowledge-base]].
            """),
            encoding="utf-8",
        )

        # Create querylog
        outputs = tmp_path / "outputs"
        outputs.mkdir(parents=True)
        logfile = outputs / "querylog.jsonl"
        if querylog_lines:
            logfile.write_text("\n".join(querylog_lines) + "\n", encoding="utf-8")
        else:
            logfile.write_text("", encoding="utf-8")

        return tmp_path

    def test_full_pipeline(self, tmp_path: Path):
        querylog_lines = [
            json.dumps({
                "query": "quantum computing basics",
                "gap_noted": True,
                "gap_topics": ["quantum computing"],
                "sources_consulted": [],
                "sources_cited": [],
            }),
            json.dumps({
                "query": "quantum entanglement",
                "gap_noted": True,
                "gap_topics": ["quantum computing"],
                "sources_consulted": [],
                "sources_cited": [],
            }),
        ]
        wiki_root = self._setup_wiki(tmp_path, querylog_lines)

        articles = load_articles(wiki_root / "concepts")
        assert len(articles) == 1

        # load_querylog from sibling module
        from gap_detect import _querylog_stats
        entries = _querylog_stats.load_querylog(wiki_root / "outputs" / "querylog.jsonl")
        assert len(entries) == 2

        gaps = detect_gaps(entries, articles, threshold=0.8)
        assert len(gaps) >= 1

        proposals = generate_proposals(gaps)
        assert len(proposals) >= 1
        assert proposals[0].priority > 0.0

    def test_empty_querylog(self, tmp_path: Path):
        wiki_root = self._setup_wiki(tmp_path)

        articles = load_articles(wiki_root / "concepts")

        from gap_detect import _querylog_stats
        entries = _querylog_stats.load_querylog(wiki_root / "outputs" / "querylog.jsonl")

        gaps = detect_gaps(entries, articles, threshold=0.8)
        assert gaps == []

        proposals = generate_proposals(gaps)
        assert proposals == []

        # JSON format should still be valid
        output = format_json(gaps, proposals)
        data = json.loads(output)
        assert data["gaps"] == []
        assert data["proposals"] == []
