#!/usr/bin/env python3
"""QueryLog statistics: aggregate query log data and report usage metrics.

Usage:
    python querylog_stats.py --wiki-root .wiki

Output: JSON to stdout with query statistics.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def load_querylog(logfile: Path) -> list[dict]:
    """Load querylog entries from a JSONL file.

    Returns an empty list if the file does not exist or is empty.
    Skips invalid JSON lines with a warning to stderr.
    """
    if not logfile.exists():
        return []

    entries: list[dict] = []
    with logfile.open(encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError:
                print(
                    f"WARNING: skipping invalid JSON at line {line_num}: {stripped[:80]}",
                    file=sys.stderr,
                )
    return entries


def resolve_concepts(concepts_dir: Path) -> list[str]:
    """Return a sorted list of .md filenames in the concepts directory.

    Returns an empty list if the directory does not exist.
    """
    if not concepts_dir.exists():
        return []
    return sorted(p.name for p in concepts_dir.glob("*.md"))


def compute_stats(entries: list[dict], concept_files: list[str]) -> dict:
    """Compute statistics from querylog entries and concept file list.

    Pure function: no I/O, no side effects.
    """
    total_queries = len(entries)

    # --- sources ---
    total_concepts = len(concept_files)
    consulted_set: set[str] = set()
    for entry in entries:
        for src in entry.get("sources_consulted", []):
            # sources_consulted contains paths like "concepts/a.md"
            # Extract filename for comparison with concept_files
            filename = Path(src).name
            consulted_set.add(filename)

    consulted_unique = len(consulted_set & set(concept_files))
    never_consulted = sorted(
        f for f in concept_files if f not in consulted_set
    )
    consultation_rate = (
        consulted_unique / total_concepts if total_concepts > 0 else 0.0
    )

    # --- gaps ---
    queries_with_gaps = sum(1 for e in entries if e.get("gap_noted", False))
    gap_rate = queries_with_gaps / total_queries if total_queries > 0 else 0.0

    topic_counter: Counter[str] = Counter()
    for entry in entries:
        for topic in entry.get("gap_topics", []):
            topic_counter[topic] += 1

    top_topics = [
        {"topic": topic, "count": count}
        for topic, count in topic_counter.most_common()
    ]

    # --- promotions ---
    promoted_count = sum(1 for e in entries if e.get("promoted", False))
    promotion_rate = promoted_count / total_queries if total_queries > 0 else 0.0

    return {
        "total_queries": total_queries,
        "sources": {
            "total_concepts": total_concepts,
            "consulted_unique": consulted_unique,
            "never_consulted": never_consulted,
            "consultation_rate": round(consultation_rate, 3),
        },
        "gaps": {
            "queries_with_gaps": queries_with_gaps,
            "gap_rate": round(gap_rate, 3),
            "top_topics": top_topics,
        },
        "promotions": {
            "promoted_count": promoted_count,
            "promotion_rate": round(promotion_rate, 3),
        },
    }


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Aggregate QueryLog statistics."
    )
    parser.add_argument(
        "--wiki-root",
        required=True,
        type=Path,
        help="Wiki root directory (resolves outputs/querylog.jsonl and concepts/)",
    )
    args = parser.parse_args()

    wiki_root: Path = args.wiki_root
    logfile = wiki_root / "outputs" / "querylog.jsonl"
    concepts_dir = wiki_root / "concepts"

    entries = load_querylog(logfile)
    concept_files = resolve_concepts(concepts_dir)
    stats = compute_stats(entries, concept_files)

    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
