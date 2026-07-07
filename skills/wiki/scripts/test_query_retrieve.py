"""Tests for query_retrieve.py — retrieval pre-pass for the query workflow."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from query_retrieve import (
    Candidate,
    GraphNotFoundError,
    expand_via_graph,
    rank_candidates,
    retrieve,
    score_seeds,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _article(slug: str, title: str, tags: list[str], body: str) -> dict:
    """Minimal seed-input mapping used by score_seeds."""
    return {"slug": slug, "title": title, "tags": tags, "body": body}


def _write_article(concepts: Path, slug: str, title: str,
                   tags: str, body: str) -> None:
    (concepts / f"{slug}.md").write_text(textwrap.dedent(f"""\
        ---
        title: {title}
        type: wiki
        source_refs:
          - "raw/articles/src.md"
        created: 2026-07-01
        updated: 2026-07-01
        category: concepts
        tags: [{tags}]
        ---

        {body}
        """), encoding="utf-8")


def _make_wiki(tmp_path: Path, *, with_graph: bool = True) -> Path:
    """3-article wiki: trust-score <- querylog (backlink), orphan-ish extra."""
    wiki_root = tmp_path / ".wiki"
    concepts = wiki_root / "concepts"
    concepts.mkdir(parents=True)
    (wiki_root / "raw" / "articles").mkdir(parents=True)
    (wiki_root / "raw" / "articles" / "src.md").write_text("src", encoding="utf-8")

    _write_article(concepts, "trust-score", "Trust Score 信頼度スコア",
                   "trust-score, quality", "スコアの説明。")
    _write_article(concepts, "querylog", "QueryLog 基盤",
                   "querylog", "ログの説明。[[trust-score]] を参照。")
    _write_article(concepts, "unrelated", "Unrelated Article",
                   "misc", "何の関係もない記事。")

    if with_graph:
        graph = {
            "nodes": [
                {"id": "trust-score", "slug": "trust-score"},
                {"id": "querylog", "slug": "querylog"},
                {"id": "unrelated", "slug": "unrelated"},
            ],
            "edges": [
                {"source": "querylog", "target": "trust-score",
                 "relation_type": "wikilink"},
            ],
            "metadata": {"dangling_links": []},
        }
        outputs = wiki_root / "outputs"
        outputs.mkdir(parents=True)
        (outputs / "graph.json").write_text(
            json.dumps(graph), encoding="utf-8")

    return wiki_root


# ---------------------------------------------------------------------------
# score_seeds
# ---------------------------------------------------------------------------

class TestScoreSeeds:
    def test_title_match_weighs_3(self):
        seeds = score_seeds([_article("a", "Trust Score", [], "")], ["trust"])
        assert seeds["a"].score == 3.0
        assert "title" in seeds["a"].matched

    def test_tag_match_weighs_2(self):
        seeds = score_seeds([_article("a", "X", ["trust-score"], "")], ["trust"])
        assert seeds["a"].score == 2.0
        assert "tags" in seeds["a"].matched

    def test_body_match_weighs_1(self):
        seeds = score_seeds([_article("a", "X", [], "trust の話")], ["trust"])
        assert seeds["a"].score == 1.0
        assert "body" in seeds["a"].matched

    def test_slug_match_weighs_3(self):
        seeds = score_seeds([_article("trust-score", "X", [], "")], ["trust"])
        assert seeds["trust-score"].score == 3.0

    def test_keywords_accumulate(self):
        seeds = score_seeds(
            [_article("a", "Trust Score", [], "信頼度の説明")],
            ["trust", "信頼度"],
        )
        # title hit (3) + body hit (1)
        assert seeds["a"].score == 4.0

    def test_case_insensitive(self):
        seeds = score_seeds([_article("a", "TRUST Score", [], "")], ["trust"])
        assert seeds["a"].score == 3.0

    def test_no_match_excluded(self):
        seeds = score_seeds([_article("a", "X", [], "y")], ["trust"])
        assert seeds == {}

    def test_same_keyword_counts_once_per_field(self):
        seeds = score_seeds(
            [_article("a", "X", [], "trust and trust and trust")], ["trust"])
        assert seeds["a"].score == 1.0


# ---------------------------------------------------------------------------
# expand_via_graph
# ---------------------------------------------------------------------------

EDGES = [
    {"source": "querylog", "target": "trust-score", "relation_type": "wikilink"},
    {"source": "gap", "target": "trust-score", "relation_type": "wikilink"},
]


class TestExpandViaGraph:
    def test_outbound_neighbor(self):
        """Seed 'querylog' links out to 'trust-score'."""
        exp = expand_via_graph({"querylog": 4.0}, EDGES)
        assert exp["trust-score"].score == pytest.approx(2.0)  # 4.0 * 0.5
        assert any("outbound" in v for v in exp["trust-score"].via)

    def test_inbound_backlink_neighbor(self):
        """Seed 'trust-score' is linked FROM 'querylog' and 'gap' — backlink
        expansion must surface them (invisible from article bodies alone).
        trust-score has degree 2, so each neighbor gets 4.0 * 0.5 / 2."""
        exp = expand_via_graph({"trust-score": 4.0}, EDGES)
        assert exp["querylog"].score == pytest.approx(1.0)
        assert exp["gap"].score == pytest.approx(1.0)
        assert any("backlink" in v for v in exp["querylog"].via)

    def test_degree_normalization_dampens_dense_hubs(self):
        """A seed's influence is split across its connections (PageRank-ish):
        in a densely linked wiki, expansion must not swamp seed scores."""
        hub_edges = [
            {"source": "hub", "target": f"n{i}", "relation_type": "wikilink"}
            for i in range(10)
        ]
        exp = expand_via_graph({"hub": 4.0}, hub_edges)
        # degree(hub) = 10 -> each neighbor gets 4.0 * 0.5 / 10 = 0.2
        assert exp["n0"].score == pytest.approx(0.2)

    def test_multiple_connections_accumulate(self):
        exp = expand_via_graph({"querylog": 4.0, "gap": 2.0}, EDGES)
        # querylog/gap each have degree 1: 4.0*0.5/1 + 2.0*0.5/1 = 3.0
        assert exp["trust-score"].score == pytest.approx(3.0)

    def test_no_seeds_no_expansion(self):
        assert expand_via_graph({}, EDGES) == {}


# ---------------------------------------------------------------------------
# rank_candidates
# ---------------------------------------------------------------------------

class TestRankCandidates:
    def test_seed_and_expansion_merge_and_sort(self):
        seeds = score_seeds(
            [_article("a", "Trust Score", [], ""),
             _article("b", "X", [], "trust")],
            ["trust"],
        )
        exp = expand_via_graph(
            {s: h.score for s, h in seeds.items()},
            [{"source": "a", "target": "c", "relation_type": "wikilink"}],
        )
        ranked = rank_candidates(seeds, exp, trust_by_slug={"a": 0.75}, limit=10)
        slugs = [c.slug for c in ranked]
        assert slugs[0] == "a"          # 3.0
        assert set(slugs) == {"a", "b", "c"}
        assert ranked[0].trust == 0.75

    def test_limit(self):
        seeds = score_seeds(
            [_article(f"a{i}", "trust", [], "") for i in range(10)], ["trust"])
        ranked = rank_candidates(seeds, {}, trust_by_slug={}, limit=3)
        assert len(ranked) == 3


# ---------------------------------------------------------------------------
# retrieve (integration)
# ---------------------------------------------------------------------------

class TestRetrieve:
    def test_end_to_end(self, tmp_path):
        wiki_root = _make_wiki(tmp_path)
        ranked = retrieve(wiki_root, ["trust", "信頼度"], limit=10)
        slugs = [c.slug for c in ranked]
        assert slugs[0] == "trust-score"        # slug+title+tag match
        assert "querylog" in slugs              # backlink of trust-score
        assert "unrelated" not in slugs
        # every candidate carries a trust annotation
        assert all(c.trust is not None for c in ranked)

    def test_graph_missing_raises(self, tmp_path):
        wiki_root = _make_wiki(tmp_path, with_graph=False)
        with pytest.raises(GraphNotFoundError):
            retrieve(wiki_root, ["trust"], limit=10)

    def test_candidate_records_why(self, tmp_path):
        wiki_root = _make_wiki(tmp_path)
        ranked = retrieve(wiki_root, ["trust"], limit=10)
        top = ranked[0]
        assert isinstance(top, Candidate)
        assert top.matched            # matched fields recorded
        by_slug = {c.slug: c for c in ranked}
        assert by_slug["querylog"].via  # expansion provenance recorded
