#!/usr/bin/env python3
"""Gap Detection + Auto Ingest proposal engine.

Usage:
    python gap_detect.py --wiki-root .wiki [--format table|json|report] [--threshold 0.8]

Analyzes QueryLog gap_topics, computes coverage against existing articles,
and generates prioritized ingest proposals for uncovered topics.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Re-use helpers from sibling modules (trust_score.py pattern).
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
parse_frontmatter = _lint_wiki.parse_frontmatter

_querylog_stats = _import_from_file("querylog_stats", "querylog_stats.py")
load_querylog = _querylog_stats.load_querylog


# ---------------------------------------------------------------------------
# Data types (immutable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleInfo:
    """Article with pre-computed tokens for coverage matching."""

    slug: str
    title: str
    tags: tuple[str, ...]
    body: str
    tokens: frozenset[str]


@dataclass(frozen=True)
class ConfirmedGap:
    """A topic confirmed as a knowledge gap."""

    topic: str
    frequency: int
    coverage: float
    related_articles: tuple[str, ...]


@dataclass(frozen=True)
class IngestProposal:
    """A prioritized proposal to ingest a new topic."""

    topic: str
    priority: float
    suggested_queries: tuple[str, ...]
    related_articles: tuple[str, ...]


# ---------------------------------------------------------------------------
# Tokenization (pure function)
# ---------------------------------------------------------------------------

_RE_ASCII_SEGMENT = re.compile(r"[a-zA-Z0-9]+")
_RE_MULTIBYTE_SEGMENT = re.compile(r"[^\x00-\x7F]+")


def _normalize_fullwidth(text: str) -> str:
    """Convert fullwidth alphanumerics to halfwidth."""
    return unicodedata.normalize("NFKC", text)


def extract_tokens(text: str) -> frozenset[str]:
    """Extract a set of tokens from text for coverage matching.

    - English: lowercase, split on space/hyphen/underscore
    - Japanese: character bigrams
    - Mixed: separate ASCII and multibyte parts, apply respective strategies
    """
    if not text:
        return frozenset()

    text = _normalize_fullwidth(text)
    # Remove punctuation/symbols but keep letters, digits, spaces, and multibyte
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)

    tokens: set[str] = set()

    # ASCII tokens (English words)
    for match in _RE_ASCII_SEGMENT.finditer(text):
        word = match.group().lower()
        # Split on underscore boundaries within the word
        for part in word.split("_"):
            if part:
                tokens.add(part)

    # Multibyte tokens (Japanese bigrams)
    for match in _RE_MULTIBYTE_SEGMENT.finditer(text):
        segment = match.group()
        # Filter out whitespace-only segments
        segment = segment.strip()
        if len(segment) >= 2:
            for i in range(len(segment) - 1):
                tokens.add(segment[i : i + 2])
        elif len(segment) == 1:
            tokens.add(segment)

    return frozenset(tokens)


# ---------------------------------------------------------------------------
# Article loading (I/O layer)
# ---------------------------------------------------------------------------

def load_articles(concepts_dir: Path) -> list[ArticleInfo]:
    """Load all articles from concepts directory with pre-computed tokens."""
    articles: list[ArticleInfo] = []
    if not concepts_dir.exists():
        return articles

    for md_file in sorted(concepts_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)

        title = fm.get("title", md_file.stem)
        tags_raw = fm.get("tags", [])
        if isinstance(tags_raw, str):
            tags_raw = [tags_raw]
        tags = tuple(tags_raw)

        # Extract body (after frontmatter)
        body = text
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                body = text[end + 3 :]

        # Pre-compute tokens from title + tags + slug + body
        token_source = " ".join([title, md_file.stem, " ".join(tags), body])
        tokens = extract_tokens(token_source)

        articles.append(
            ArticleInfo(
                slug=md_file.stem,
                title=title,
                tags=tags,
                body=body,
                tokens=tokens,
            )
        )

    return articles


# ---------------------------------------------------------------------------
# Coverage computation (pure function)
# ---------------------------------------------------------------------------

def compute_coverage(
    topic_tokens: frozenset[str],
    articles: list[ArticleInfo],
    threshold: float = 0.8,
) -> tuple[float, tuple[str, ...]]:
    """Compute how well existing articles cover a topic.

    Returns (max_coverage, related_slugs) where:
    - max_coverage: highest token overlap ratio across all articles
    - related_slugs: articles with overlap >= threshold/2
    """
    if not topic_tokens or not articles:
        return 0.0, ()

    max_coverage = 0.0
    related: list[str] = []
    half_threshold = threshold / 2

    for article in articles:
        overlap = len(topic_tokens & article.tokens)
        ratio = overlap / len(topic_tokens)
        if ratio > max_coverage:
            max_coverage = ratio
        if ratio >= half_threshold:
            related.append(article.slug)

    return max_coverage, tuple(sorted(related))


# ---------------------------------------------------------------------------
# Gap detection (pure function)
# ---------------------------------------------------------------------------

def detect_gaps(
    entries: list[dict],
    articles: list[ArticleInfo],
    threshold: float = 0.8,
) -> list[ConfirmedGap]:
    """Detect knowledge gaps from QueryLog entries.

    Only processes entries where gap_noted=True and gap_topics is non-empty.
    """
    from collections import Counter

    topic_counter: Counter[str] = Counter()
    for entry in entries:
        if not entry.get("gap_noted", False):
            continue
        gap_topics = entry.get("gap_topics", [])
        if not gap_topics:
            continue
        for topic in gap_topics:
            topic_counter[topic] += 1

    if not topic_counter:
        return []

    gaps: list[ConfirmedGap] = []
    for topic, frequency in topic_counter.most_common():
        topic_tokens = extract_tokens(topic)
        coverage, related = compute_coverage(topic_tokens, articles, threshold)
        if coverage < threshold:
            gaps.append(
                ConfirmedGap(
                    topic=topic,
                    frequency=frequency,
                    coverage=round(coverage, 4),
                    related_articles=related,
                )
            )

    return gaps


# ---------------------------------------------------------------------------
# Proposal generation (pure function)
# ---------------------------------------------------------------------------

def generate_proposals(gaps: list[ConfirmedGap]) -> list[IngestProposal]:
    """Generate prioritized ingest proposals from confirmed gaps.

    Priority = frequency * (1 - coverage), normalized to 0.0-1.0.
    """
    if not gaps:
        return []

    # Compute raw priorities
    raw_priorities: list[float] = []
    for gap in gaps:
        raw_priorities.append(gap.frequency * (1.0 - gap.coverage))

    max_priority = max(raw_priorities) if raw_priorities else 1.0
    if max_priority == 0:
        max_priority = 1.0

    proposals: list[IngestProposal] = []
    for gap, raw_p in zip(gaps, raw_priorities):
        normalized = round(raw_p / max_priority, 4)
        queries = (
            f"{gap.topic} wiki",
            f"{gap.topic} overview",
            f"{gap.topic} tutorial",
        )
        proposals.append(
            IngestProposal(
                topic=gap.topic,
                priority=normalized,
                suggested_queries=queries,
                related_articles=gap.related_articles,
            )
        )

    # Sort by priority descending
    proposals.sort(key=lambda p: p.priority, reverse=True)
    return proposals


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_table(gaps: list[ConfirmedGap], proposals: list[IngestProposal]) -> str:
    """Format gaps and proposals as a human-readable ASCII table."""
    lines: list[str] = []

    if not gaps:
        lines.append("No knowledge gaps detected.")
        return "\n".join(lines)

    header = f"{'Topic':<40} {'Freq':>5} {'Cov':>5} {'Priority':>8}  {'Related'}"
    lines.append(header)
    lines.append("-" * len(header))

    proposal_map = {p.topic: p for p in proposals}
    for gap in gaps:
        p = proposal_map.get(gap.topic)
        priority_str = f"{p.priority:.2f}" if p else "n/a"
        related_str = ", ".join(gap.related_articles) if gap.related_articles else "-"
        lines.append(
            f"{gap.topic:<40} {gap.frequency:>5} {gap.coverage:>5.2f} "
            f"{priority_str:>8}  {related_str}"
        )

    return "\n".join(lines)


def format_json(gaps: list[ConfirmedGap], proposals: list[IngestProposal]) -> str:
    """Format gaps and proposals as JSON."""
    data = {
        "gaps": [
            {
                "topic": g.topic,
                "frequency": g.frequency,
                "coverage": g.coverage,
                "related_articles": list(g.related_articles),
            }
            for g in gaps
        ],
        "proposals": [
            {
                "topic": p.topic,
                "priority": p.priority,
                "suggested_queries": list(p.suggested_queries),
                "related_articles": list(p.related_articles),
            }
            for p in proposals
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_report(
    gaps: list[ConfirmedGap],
    proposals: list[IngestProposal],
    today: date,
) -> str:
    """Format gaps and proposals as a Markdown report."""
    lines: list[str] = []
    lines.append(f"# Gap Detection Report ({today.isoformat()})")
    lines.append("")

    if not gaps:
        lines.append("No knowledge gaps detected.")
        return "\n".join(lines)

    lines.append("## Confirmed Gaps")
    lines.append("")
    lines.append(
        f"| {'Topic':<35} | {'Freq':>5} | {'Coverage':>8} | {'Related':<30} |"
    )
    lines.append(
        f"|{'-' * 37}|{'-' * 7}|{'-' * 10}|{'-' * 32}|"
    )
    for g in gaps:
        related = ", ".join(g.related_articles) if g.related_articles else "-"
        lines.append(
            f"| {g.topic:<35} | {g.frequency:>5} | {g.coverage:>8.2f} | {related:<30} |"
        )
    lines.append("")

    lines.append("## Ingest Proposals")
    lines.append("")
    for p in proposals:
        lines.append(f"### {p.topic} (priority: {p.priority:.2f})")
        lines.append("")
        if p.related_articles:
            lines.append(f"- Related: {', '.join(p.related_articles)}")
        lines.append("- Suggested queries:")
        for q in p.suggested_queries:
            lines.append(f"  - `{q}`")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Detect wiki knowledge gaps and generate ingest proposals."
    )
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
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="Coverage threshold for gap detection (default: 0.8)",
    )
    args = parser.parse_args()

    wiki_root: Path = args.wiki_root
    concepts_dir = wiki_root / "concepts"
    logfile = wiki_root / "outputs" / "querylog.jsonl"

    articles = load_articles(concepts_dir)
    entries = load_querylog(logfile)
    gaps = detect_gaps(entries, articles, threshold=args.threshold)
    proposals = generate_proposals(gaps)

    today = date.today()

    if args.fmt == "json":
        print(format_json(gaps, proposals))
    elif args.fmt == "report":
        report_dir = wiki_root / "outputs" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{today.strftime('%Y%m%d')}-gap-detect.md"
        content = format_report(gaps, proposals, today)
        report_path.write_text(content, encoding="utf-8")
        print(f"Report written to {report_path}")
    else:
        print(format_table(gaps, proposals))


if __name__ == "__main__":
    main()
