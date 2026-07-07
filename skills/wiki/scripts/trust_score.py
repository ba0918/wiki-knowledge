#!/usr/bin/env python3
"""Trust Score engine: compute per-article trust scores from wiki metadata.

Usage:
    python trust_score.py --wiki-root .wiki [--format table|json|report]

Trust Score v2 is a weighted sum of 4 factors, each an **absolute** score in
0.0-1.0 (v1's min-max normalization made scores relative to the rest of the
wiki, which broke the absolute 0.30 warning threshold — in a uniform wiki
someone always sank to 0; see docs/plans/20260707200608):

  - Source count   (0.30)  saturating n/(n+1): 1=0.50, 2=0.67, 3=0.75
  - Freshness      (0.20)  half-life decay 0.5**(days/365): 1y=0.50, 2y=0.25
                           (snapshot semantics — staleness is divergence
                           *risk*, never invalidity, so it never reaches 0)
  - Citation freq  (0.30)  saturating c/(c+2), from QueryLog
  - Backlink count (0.20)  saturating b/(b+2)

When QueryLog is empty, citation is excluded and weights redistribute:
  Source 0.40, Freshness 0.30, Backlink 0.30

``ArticleScore.*_norm`` fields hold the absolute factor scores (the name is
kept for output-format compatibility with v1).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# Re-use helpers from sibling modules.
# lint-wiki.py has a hyphen, so we use importlib to load it.
import importlib.util as _ilu

def _import_from_file(module_name: str, file_name: str):
    """Import a module from a sibling file by filename."""
    _here = Path(__file__).resolve().parent
    spec = _ilu.spec_from_file_location(module_name, _here / file_name)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[module_name] = mod  # register before exec for Python 3.10 compat
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

_lint_wiki = _import_from_file("lint_wiki", "lint-wiki.py")
find_wikilinks = _lint_wiki.find_wikilinks
parse_frontmatter = _lint_wiki.parse_frontmatter

_querylog_stats = _import_from_file("querylog_stats", "querylog_stats.py")
load_querylog = _querylog_stats.load_querylog


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleMeta:
    """Metadata extracted from a single wiki article."""

    slug: str
    source_refs: list[str]
    updated: date | None
    related: list[str]
    wikilinks: list[str]


@dataclass(frozen=True)
class ArticleScore:
    """Computed trust score for a single article."""

    slug: str
    score: float
    source_raw: float
    freshness_raw: float
    citation_raw: float
    backlink_raw: float
    source_norm: float
    freshness_norm: float
    citation_norm: float
    backlink_norm: float


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

WEIGHTS_FULL = {
    "source": 0.30,
    "freshness": 0.20,
    "citation": 0.30,
    "backlink": 0.20,
}

WEIGHTS_NO_QUERYLOG = {
    "source": 0.40,
    "freshness": 0.30,
    "citation": 0.00,
    "backlink": 0.30,
}


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def parse_article_metadata(concept_dir: Path) -> list[ArticleMeta]:
    """Read all .md files in *concept_dir* and extract metadata.

    Returns a list of ArticleMeta sorted by slug.
    """
    articles: list[ArticleMeta] = []
    if not concept_dir.exists():
        return articles

    for md_file in sorted(concept_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        wikilinks = find_wikilinks(text)

        source_refs = fm.get("source_refs", [])
        if isinstance(source_refs, str):
            source_refs = [source_refs]

        updated_str = fm.get("updated", "")
        updated: date | None = None
        if updated_str:
            try:
                updated = datetime.strptime(str(updated_str), "%Y-%m-%d").date()
            except ValueError:
                pass

        related = fm.get("related", [])
        if isinstance(related, str):
            related = [related]

        articles.append(
            ArticleMeta(
                slug=md_file.stem,
                source_refs=source_refs,
                updated=updated,
                related=related,
                wikilinks=wikilinks,
            )
        )
    return articles


def _normalize_slug(ref: str) -> str:
    """Normalize a reference to a bare slug.

    ``concepts/foo.md`` -> ``foo``
    ``foo.md``          -> ``foo``
    ``foo``             -> ``foo``
    """
    name = ref.split("/")[-1]  # strip directory prefix
    if name.endswith(".md"):
        name = name[:-3]
    return name


def count_backlinks(articles: list[ArticleMeta]) -> dict[str, int]:
    """Count how many *distinct* articles reference each slug.

    Both ``related`` frontmatter entries and ``[[wikilink]]`` in the body
    contribute, but references from the same source article are deduplicated.
    """
    # slug -> set of source slugs
    refs: dict[str, set[str]] = {}

    for article in articles:
        # Collect all target slugs from this article (deduplicated per source)
        targets: set[str] = set()
        for r in article.related:
            targets.add(_normalize_slug(r))
        for w in article.wikilinks:
            targets.add(_normalize_slug(w))

        # Don't count self-references
        targets.discard(article.slug)

        for target in targets:
            refs.setdefault(target, set()).add(article.slug)

    return {slug: len(sources) for slug, sources in refs.items()}


def count_citations(
    entries: list[dict], articles: list[ArticleMeta]
) -> dict[str, int]:
    """Count how many times each article slug appears in QueryLog sources_cited."""
    counts: dict[str, int] = {}
    known_slugs = {a.slug for a in articles}

    for entry in entries:
        for cited in entry.get("sources_cited", []):
            slug = _normalize_slug(cited)
            if slug in known_slugs:
                counts[slug] = counts.get(slug, 0) + 1

    return counts


FRESHNESS_HALF_LIFE_DAYS = 365.0
SOURCE_SATURATION_K = 1.0
CITATION_SATURATION_K = 2.0
BACKLINK_SATURATION_K = 2.0


def saturating(count: float, k: float) -> float:
    """Absolute saturating curve ``count / (count + k)``.

    Maps a non-negative count to 0.0-1.0 with diminishing returns: the k-th
    unit reaches 0.5, and the curve approaches (but never reaches) 1.0.
    Unlike min-max normalization, the result depends only on the article
    itself, so the 0.30 warning threshold has a stable meaning.
    """
    if count <= 0:
        return 0.0
    return count / (count + k)


def freshness_factor(updated: date | None, today: date) -> float:
    """Half-life freshness decay: ``0.5 ** (elapsed_days / 365)``.

    Snapshot semantics: articles pin facts to their capture point
    (``source_revision`` etc.), so age expresses *divergence risk*, not
    invalidity — the factor decays but never reaches 0. Only an article
    with no ``updated`` date at all scores 0.0.
    """
    if updated is None:
        return 0.0
    elapsed = (today - updated).days
    if elapsed <= 0:
        return 1.0
    return 0.5 ** (elapsed / FRESHNESS_HALF_LIFE_DAYS)


def compute_trust_scores(
    articles: list[ArticleMeta],
    querylog_entries: list[dict],
    *,
    today: date | None = None,
) -> list[ArticleScore]:
    """Compute trust scores for all articles.

    Pure function -- no I/O, no side effects.
    *today* defaults to ``date.today()`` but is injectable for testing.
    """
    if not articles:
        return []

    if today is None:
        today = date.today()

    use_querylog = len(querylog_entries) > 0
    weights = WEIGHTS_FULL if use_querylog else WEIGHTS_NO_QUERYLOG

    backlinks = count_backlinks(articles)
    citations = count_citations(querylog_entries, articles)

    # Raw values per article
    source_raws: list[float] = []
    freshness_raws: list[float] = []
    citation_raws: list[float] = []
    backlink_raws: list[float] = []

    for a in articles:
        source_raws.append(float(len(a.source_refs)))
        freshness_raws.append(freshness_factor(a.updated, today))
        citation_raws.append(float(citations.get(a.slug, 0)))
        backlink_raws.append(float(backlinks.get(a.slug, 0)))

    # Absolute factor scores (v2 — no min-max normalization)
    source_norms = [saturating(v, SOURCE_SATURATION_K) for v in source_raws]
    freshness_norms = list(freshness_raws)  # freshness_factor is already 0-1
    citation_norms = [saturating(v, CITATION_SATURATION_K) for v in citation_raws]
    backlink_norms = [saturating(v, BACKLINK_SATURATION_K) for v in backlink_raws]

    results: list[ArticleScore] = []
    for i, a in enumerate(articles):
        score = (
            weights["source"] * source_norms[i]
            + weights["freshness"] * freshness_norms[i]
            + weights["citation"] * citation_norms[i]
            + weights["backlink"] * backlink_norms[i]
        )
        results.append(
            ArticleScore(
                slug=a.slug,
                score=round(score, 2),
                source_raw=source_raws[i],
                freshness_raw=freshness_raws[i],
                citation_raw=citation_raws[i],
                backlink_raw=backlink_raws[i],
                source_norm=round(source_norms[i], 4),
                freshness_norm=round(freshness_norms[i], 4),
                citation_norm=round(citation_norms[i], 4),
                backlink_norm=round(backlink_norms[i], 4),
            )
        )

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_table(scores: list[ArticleScore], use_querylog: bool) -> str:
    """Format scores as a human-readable table."""
    lines: list[str] = []
    header = f"{'Article':<40} {'Score':>6}  {'Src':>5} {'Fresh':>5} {'Cite':>5} {'BL':>5}"
    lines.append(header)
    lines.append("-" * len(header))
    for s in scores:
        ql_mark = f"{s.citation_norm:.2f}" if use_querylog else "  n/a"
        lines.append(
            f"{s.slug:<40} {s.score:>6.2f}  "
            f"{s.source_norm:>5.2f} {s.freshness_norm:>5.2f} "
            f"{ql_mark:>5} {s.backlink_norm:>5.2f}"
        )
    return "\n".join(lines)


def format_json(scores: list[ArticleScore]) -> str:
    """Format scores as JSON."""
    data = []
    for s in scores:
        data.append(
            {
                "slug": s.slug,
                "score": s.score,
                "breakdown": {
                    "source": {"raw": s.source_raw, "norm": s.source_norm},
                    "freshness": {"raw": s.freshness_raw, "norm": s.freshness_norm},
                    "citation": {"raw": s.citation_raw, "norm": s.citation_norm},
                    "backlink": {"raw": s.backlink_raw, "norm": s.backlink_norm},
                },
            }
        )
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_report(
    scores: list[ArticleScore], use_querylog: bool, today: date
) -> str:
    """Format scores as a Markdown report."""
    lines: list[str] = []
    lines.append(f"# Trust Score Report ({today.isoformat()})")
    lines.append("")
    if not use_querylog:
        lines.append(
            "> **Note:** QueryLog is empty. Citation frequency is excluded; "
            "weights are redistributed (Source 0.40, Freshness 0.30, Backlink 0.30)."
        )
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"| {'Article':<40} | {'Score':>6} | {'Source':>6} | {'Fresh':>6} "
        f"| {'Cite':>6} | {'BL':>6} |"
    )
    lines.append(
        f"|{'-' * 42}|{'-' * 8}|{'-' * 8}|{'-' * 8}|{'-' * 8}|{'-' * 8}|"
    )
    for s in scores:
        cite_str = f"{s.citation_norm:.2f}" if use_querylog else "n/a"
        lines.append(
            f"| {s.slug:<40} | {s.score:>6.2f} | {s.source_norm:>6.2f} "
            f"| {s.freshness_norm:>6.2f} | {cite_str:>6} | {s.backlink_norm:>6.2f} |"
        )
    lines.append("")

    # Detail per article
    lines.append("## Detail")
    lines.append("")
    for s in scores:
        lines.append(f"### {s.slug} (score: {s.score})")
        lines.append("")
        lines.append(f"- Source count (raw): {s.source_raw:.0f}")
        lines.append(f"- Freshness (raw): {s.freshness_raw:.4f}")
        lines.append(f"- Citation count (raw): {s.citation_raw:.0f}")
        lines.append(f"- Backlink count (raw): {s.backlink_raw:.0f}")
        lines.append("")

    # Low-score warnings
    low = [s for s in scores if s.score < 0.3]
    if low:
        lines.append("## Warnings")
        lines.append("")
        for s in low:
            lines.append(f"- {s.slug}: score {s.score} < 0.30")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Compute Wiki Trust Scores.")
    parser.add_argument(
        "--wiki-root",
        required=True,
        type=Path,
        help="Wiki root directory",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "report"],
        default="table",
        dest="fmt",
        help="Output format (default: table)",
    )
    args = parser.parse_args()

    wiki_root: Path = args.wiki_root
    concepts_dir = wiki_root / "concepts"
    logfile = wiki_root / "outputs" / "querylog.jsonl"

    articles = parse_article_metadata(concepts_dir)
    entries = load_querylog(logfile)

    today = date.today()
    scores = compute_trust_scores(articles, entries, today=today)

    use_querylog = len(entries) > 0

    if args.fmt == "json":
        print(format_json(scores))
    elif args.fmt == "report":
        report_dir = wiki_root / "outputs" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{today.strftime('%Y%m%d')}-trust-score.md"
        content = format_report(scores, use_querylog, today)
        report_path.write_text(content, encoding="utf-8")
        print(f"Report written to {report_path}")
    else:
        print(format_table(scores, use_querylog))


if __name__ == "__main__":
    main()
