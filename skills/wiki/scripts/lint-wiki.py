#!/usr/bin/env python3
"""Wiki lint checker: dead links, orphans, missing sources.

Usage:
    python lint-wiki.py <wiki_root>
    python lint-wiki.py .wiki

Output: JSON to stdout with findings grouped by severity.
"""

import json
import re
import sys
from pathlib import Path


def find_wikilinks(text: str) -> list[str]:
    """Extract [[slug]] references from text."""
    return re.findall(r"\[\[([a-z0-9-]+)\]\]", text)


def parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter as a simple key-value dict."""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm = {}
    last_key = None
    for line in text[3:end].strip().splitlines():
        stripped = line.strip()
        # List item (indented "- value")
        if stripped.startswith("- ") and last_key is not None:
            if not isinstance(fm.get(last_key), list):
                fm[last_key] = [] if not fm.get(last_key) else [fm[last_key]]
            fm[last_key].append(
                stripped.removeprefix("- ").strip('"').strip("'")
            )
        elif ":" in line and not stripped.startswith("-"):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            last_key = key
            # Inline array: [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                fm[key] = [
                    v.strip().strip('"').strip("'")
                    for v in value[1:-1].split(",")
                    if v.strip()
                ]
            elif value == "":
                # Next lines may be list items
                fm[key] = []
            else:
                fm[key] = value.strip('"').strip("'")
    return fm


def lint(wiki_root: Path) -> dict:
    concepts_dir = wiki_root / "concepts"
    raw_dir = wiki_root / "raw"

    findings = {"error": [], "warning": [], "info": []}

    if not concepts_dir.exists():
        findings["error"].append(
            {"check": "structure", "message": f"{concepts_dir} does not exist"}
        )
        return findings

    # Build inventory
    articles: dict[str, dict] = {}  # slug -> {path, frontmatter, wikilinks, text}
    all_slugs: set[str] = set()
    all_referenced_slugs: set[str] = set()
    inbound_links: dict[str, set[str]] = {}  # slug -> set of slugs that link to it

    for md_file in sorted(concepts_dir.glob("*.md")):
        slug = md_file.stem
        text = md_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        wikilinks = find_wikilinks(text)

        all_slugs.add(slug)
        articles[slug] = {
            "path": str(md_file),
            "frontmatter": fm,
            "wikilinks": wikilinks,
            "text": text,
        }
        for linked_slug in wikilinks:
            all_referenced_slugs.add(linked_slug)
            inbound_links.setdefault(linked_slug, set()).add(slug)

    # Check 1: Dead links
    for slug, article in articles.items():
        for linked_slug in article["wikilinks"]:
            if linked_slug not in all_slugs:
                findings["error"].append(
                    {
                        "check": "dead_link",
                        "source": slug,
                        "target": linked_slug,
                        "message": f"[[{linked_slug}]] in {slug} -> concepts/{linked_slug}.md does not exist",
                    }
                )

    # Check 2: Orphan articles (no inbound links from other articles)
    for slug in all_slugs:
        if slug not in inbound_links or len(inbound_links[slug]) == 0:
            findings["warning"].append(
                {
                    "check": "orphan",
                    "slug": slug,
                    "message": f"{slug} has no inbound [[wikilink]] from other articles",
                }
            )

    # Check 3: Missing source refs
    for slug, article in articles.items():
        source_refs = article["frontmatter"].get("source_refs", [])
        if isinstance(source_refs, str):
            source_refs = [source_refs]
        for ref in source_refs:
            ref_path = wiki_root / ref
            if not ref_path.exists():
                findings["error"].append(
                    {
                        "check": "missing_source",
                        "slug": slug,
                        "source_ref": ref,
                        "message": f"{slug} references {ref} but file does not exist",
                    }
                )

    # Check 4: Missing frontmatter fields
    required_fields = ["title", "type", "source_refs", "created", "updated", "category", "tags"]
    for slug, article in articles.items():
        fm = article["frontmatter"]
        missing = [f for f in required_fields if f not in fm]
        if missing:
            findings["warning"].append(
                {
                    "check": "missing_frontmatter",
                    "slug": slug,
                    "missing_fields": missing,
                    "message": f"{slug} is missing frontmatter fields: {', '.join(missing)}",
                }
            )

    # Check 5: Coverage gaps (referenced 2+ times but no page)
    reference_counts: dict[str, int] = {}
    for article in articles.values():
        for linked_slug in set(article["wikilinks"]):
            reference_counts[linked_slug] = reference_counts.get(linked_slug, 0) + 1

    for linked_slug, count in reference_counts.items():
        if linked_slug not in all_slugs and count >= 2:
            findings["info"].append(
                {
                    "check": "coverage_gap",
                    "slug": linked_slug,
                    "reference_count": count,
                    "message": f"[[{linked_slug}]] referenced {count} times but no page exists",
                }
            )

    return findings


def main():
    if len(sys.argv) < 2:
        print("Usage: python lint-wiki.py <wiki_root>", file=sys.stderr)
        sys.exit(1)

    wiki_root = Path(sys.argv[1])
    if not wiki_root.is_dir():
        print(f"Error: {wiki_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    results = lint(wiki_root)

    summary = {
        severity: len(items) for severity, items in results.items()
    }
    output = {"summary": summary, "findings": results}

    print(json.dumps(output, ensure_ascii=False, indent=2))

    # Exit code: 1 if errors, 0 otherwise
    sys.exit(1 if summary["error"] > 0 else 0)


if __name__ == "__main__":
    main()
