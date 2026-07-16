#!/usr/bin/env python3
"""Query retrieval pre-pass: rank candidate articles for a question.

Consumes the derived layers — ``outputs/graph.json`` (link structure, both
directions) and Trust Score v2 — so the LLM query workflow starts from a
deterministic, trust-annotated candidate list instead of scanning index.md
and guessing. Notably, backlink expansion surfaces articles that *reference*
a matched article (e.g. cross-repo flow articles), which are invisible when
reading article bodies alone.

Usage:
    python query_retrieve.py --wiki-root .wiki --keywords trust 信頼度 \
        [--limit 12] [--format table|json]

Exit codes:
    0 = success
    2 = outputs/graph.json missing — run graph_gen.py first (same contract
        as lint-wiki.py; this script never regenerates the graph itself)

Design: pure core (score_seeds / expand_via_graph / rank_candidates) + thin
CLI, following the graph_gen.py / lint-wiki.py precedent. Retrieval design
rationale: query consumes graph.json + Trust Score v2 (absolute-scale, 2026-07-07)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from lib.inventory import parse_articles

import trust_score as _trust_score


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SeedHit:
    """Keyword-match result for one article."""

    score: float
    matched: dict[str, list[str]]  # field name -> matched keywords


@dataclass(frozen=True)
class Expansion:
    """Graph-expansion result for one article."""

    score: float
    via: list[str]  # e.g. "backlink of trust-score", "outbound of querylog"


@dataclass(frozen=True)
class Candidate:
    """One ranked retrieval candidate."""

    slug: str
    score: float
    trust: float | None
    matched: dict[str, list[str]]
    via: list[str]
    title: str = ""


class GraphNotFoundError(FileNotFoundError):
    """outputs/graph.json is missing. The CLI translates this into exit 2
    with an actionable message (mirrors lint-wiki.py's contract)."""


# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

FIELD_WEIGHTS = {
    "slug": 3.0,
    "title": 3.0,
    "tags": 2.0,
    "body": 1.0,
}

EXPANSION_FACTOR = 0.5  # neighbor score = seed score * factor (per edge)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def score_seeds(
    articles: list[dict], keywords: list[str]
) -> dict[str, SeedHit]:
    """Score articles by case-insensitive keyword match.

    *articles* items are mappings with ``slug`` / ``title`` / ``tags`` /
    ``body``. Each keyword counts at most once per field; weights are
    FIELD_WEIGHTS. Articles with no match are excluded.
    """
    lowered = [k.lower() for k in keywords if k.strip()]
    seeds: dict[str, SeedHit] = {}

    for article in articles:
        fields = {
            "slug": article["slug"].lower(),
            "title": str(article.get("title", "")).lower(),
            "tags": " ".join(str(t) for t in article.get("tags", [])).lower(),
            "body": str(article.get("body", "")).lower(),
        }
        score = 0.0
        matched: dict[str, list[str]] = {}
        for field_name, weight in FIELD_WEIGHTS.items():
            hits = [k for k in lowered if k in fields[field_name]]
            if hits:
                score += weight * len(hits)
                matched[field_name] = hits
        if score > 0:
            seeds[article["slug"]] = SeedHit(score=score, matched=matched)

    return seeds


def expand_via_graph(
    seed_scores: dict[str, float],
    edges: list[dict],
    *,
    factor: float = EXPANSION_FACTOR,
) -> dict[str, Expansion]:
    """Expand seeds one hop along graph edges, in **both** directions.

    Outbound: a seed links to a neighbor. Inbound (backlink): a neighbor
    links to a seed — the direction that is invisible from article bodies
    and the main reason the graph layer feeds retrieval.

    A seed's influence is **split across its connections** (PageRank-style
    degree normalization): each neighbor gains ``seed * factor / degree``.
    Without this, a densely linked wiki lets expansion sums swamp the seed
    scores and the ranking flattens (observed on the dogfood wiki: 52 edges
    over 12 nodes put six articles in an exact tie).
    """
    # Total degree (both directions) per seed, counted over all edges.
    degrees: dict[str, int] = {slug: 0 for slug in seed_scores}
    for edge in edges:
        for endpoint in (edge.get("source"), edge.get("target")):
            if endpoint in degrees:
                degrees[endpoint] += 1

    scores: dict[str, float] = {}
    vias: dict[str, list[str]] = {}

    def _add(slug: str, seed: str, via: str) -> None:
        gain = seed_scores[seed] * factor / max(degrees[seed], 1)
        scores[slug] = scores.get(slug, 0.0) + gain
        vias.setdefault(slug, []).append(via)

    for edge in edges:
        src = edge.get("source")
        dst = edge.get("target")
        if src in seed_scores and dst is not None:
            _add(dst, src, f"outbound of {src}")
        if dst in seed_scores and src is not None:
            _add(src, dst, f"backlink of {dst}")

    return {
        slug: Expansion(score=scores[slug], via=vias[slug]) for slug in scores
    }


def rank_candidates(
    seeds: dict[str, SeedHit],
    expansions: dict[str, Expansion],
    *,
    trust_by_slug: dict[str, float],
    limit: int,
    titles: dict[str, str] | None = None,
) -> list[Candidate]:
    """Merge seed and expansion scores into a ranked candidate list."""
    titles = titles or {}
    slugs = set(seeds) | set(expansions)
    candidates: list[Candidate] = []
    for slug in slugs:
        seed = seeds.get(slug)
        exp = expansions.get(slug)
        candidates.append(
            Candidate(
                slug=slug,
                score=round(
                    (seed.score if seed else 0.0)
                    + (exp.score if exp else 0.0),
                    3,
                ),
                trust=trust_by_slug.get(slug),
                matched=seed.matched if seed else {},
                via=exp.via if exp else [],
                title=titles.get(slug, ""),
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.slug))
    return candidates[:limit]


# ---------------------------------------------------------------------------
# I/O composition
# ---------------------------------------------------------------------------

def _load_graph_or_raise(wiki_root: Path) -> dict:
    graph_path = wiki_root / "outputs" / "graph.json"
    if not graph_path.exists():
        raise GraphNotFoundError(str(graph_path))
    return json.loads(graph_path.read_text(encoding="utf-8"))


def _trust_scores(wiki_root: Path) -> dict[str, float]:
    """Compute Trust Score v2 per slug (in-process)."""
    articles = _trust_score.parse_article_metadata(wiki_root / "concepts")
    entries = _trust_score.load_querylog(
        wiki_root / "outputs" / "querylog.jsonl")
    scores = _trust_score.compute_trust_scores(articles, entries)
    return {s.slug: s.score for s in scores}


def retrieve(
    wiki_root: Path, keywords: list[str], *, limit: int = 12
) -> list[Candidate]:
    """End-to-end retrieval: seeds -> graph expansion -> trust annotation."""
    graph = _load_graph_or_raise(wiki_root)
    inventory = parse_articles(wiki_root)

    seed_inputs = [
        {"slug": a.slug, "title": a.title, "tags": list(a.tags), "body": a.body}
        for a in inventory
    ]
    seeds = score_seeds(seed_inputs, keywords)
    expansions = expand_via_graph(
        {slug: hit.score for slug, hit in seeds.items()},
        graph.get("edges", []),
    )
    return rank_candidates(
        seeds,
        expansions,
        trust_by_slug=_trust_scores(wiki_root),
        limit=limit,
        titles={a.slug: a.title for a in inventory},
    )


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_table(candidates: list[Candidate]) -> str:
    if not candidates:
        return "No candidates."
    lines = [
        f"{'Article':<40} {'Score':>6} {'Trust':>6}  Why",
        "-" * 90,
    ]
    for c in candidates:
        why_parts = [
            f"matched {field}: {', '.join(kw)}"
            for field, kw in c.matched.items()
        ] + c.via
        # Keep the table scannable; full provenance is in --format json.
        if len(why_parts) > 4:
            why_parts = why_parts[:4] + [f"(+{len(why_parts) - 4} more)"]
        trust_str = f"{c.trust:.2f}" if c.trust is not None else "n/a"
        lines.append(
            f"{c.slug:<40} {c.score:>6.2f} {trust_str:>6}  "
            f"{'; '.join(why_parts)}"
        )
    return "\n".join(lines)


def format_json(candidates: list[Candidate]) -> str:
    return json.dumps(
        [
            {
                "slug": c.slug,
                "title": c.title,
                "score": c.score,
                "trust": c.trust,
                "matched": c.matched,
                "via": c.via,
            }
            for c in candidates
        ],
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rank candidate wiki articles for a query "
                    "(graph + trust aware).")
    parser.add_argument("--wiki-root", required=True, type=Path)
    parser.add_argument(
        "--keywords", required=True, nargs="+",
        help="Query keywords (Japanese or English; case-insensitive)")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument(
        "--format", choices=["table", "json"], default="table", dest="fmt")
    args = parser.parse_args(argv)

    try:
        candidates = retrieve(args.wiki_root, args.keywords, limit=args.limit)
    except GraphNotFoundError as exc:
        print(
            f"Error: graph file not found: {exc}\n"
            f"Run first: python3 skills/wiki/scripts/graph_gen.py "
            f"--wiki-root {args.wiki_root}",
            file=sys.stderr,
        )
        return 2

    if args.fmt == "json":
        print(format_json(candidates))
    else:
        print(format_table(candidates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
