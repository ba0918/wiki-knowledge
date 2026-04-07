#!/usr/bin/env python3
"""Wiki lint checker: dead links, orphans, missing sources, link quality,
article quality, format violations.

Usage:
    python lint-wiki.py --wiki-root .wiki [--format table|json|report]
    python lint-wiki.py .wiki                # positional fallback

Output format:
    table  (default) — human-readable table to stdout
    json   — structured JSON to stdout
    report — Markdown report to .wiki/outputs/reports/{YYYYMMDD}-lint.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path


# ---------------------------------------------------------------------------
# Data types (immutable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """A single lint finding."""

    severity: str          # "error" | "warning" | "info"
    check: str             # e.g. "dead_link", "orphan", ...
    slug: str              # article slug (or target slug for coverage_gap)
    message: str           # human-readable description
    details: dict | None = None  # check-specific extra info


@dataclass(frozen=True)
class ArticleInventory:
    """Metadata for a single wiki article."""

    slug: str
    path: str
    frontmatter: dict
    wikilinks: list[str]
    text: str              # full file content
    body: str              # content after frontmatter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def find_wikilinks(text: str) -> list[str]:
    """Extract [[slug]] references from text.

    Wikilinks inside fenced code blocks (```...```) and inline code spans
    (`...`) are excluded so that documentation/example mentions do not
    produce dead-link findings.
    """
    stripped = _FENCE_RE.sub("", text)
    stripped = _INLINE_CODE_RE.sub("", stripped)
    return re.findall(r"\[\[([a-z0-9-]+)\]\]", stripped)


def parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter as a simple key-value dict."""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm: dict = {}
    last_key: str | None = None
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


def _extract_body(text: str) -> str:
    """Return text content after frontmatter (if any)."""
    if not text.startswith("---"):
        return text
    end = text.find("---", 3)
    if end == -1:
        return text
    return text[end + 3:].strip()


def _normalize_slug(ref: str) -> str:
    """Normalize a reference path to a bare slug.

    ``concepts/foo.md`` -> ``foo``
    ``foo.md``          -> ``foo``
    ``foo``             -> ``foo``
    """
    name = ref.split("/")[-1]
    if name.endswith(".md"):
        name = name[:-3]
    return name


# ---------------------------------------------------------------------------
# Inventory builder
# ---------------------------------------------------------------------------

def _build_inventory(wiki_root: Path) -> dict[str, ArticleInventory]:
    """Build an inventory of all articles in concepts/."""
    concepts_dir = wiki_root / "concepts"
    if not concepts_dir.exists():
        return {}

    inventory: dict[str, ArticleInventory] = {}
    for md_file in sorted(concepts_dir.glob("*.md")):
        slug = md_file.stem
        text = md_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        body = _extract_body(text)
        wikilinks = find_wikilinks(text)

        inventory[slug] = ArticleInventory(
            slug=slug,
            path=str(md_file),
            frontmatter=fm,
            wikilinks=wikilinks,
            text=text,
            body=body,
        )
    return inventory


# ---------------------------------------------------------------------------
# Check functions (pure: inventory in → findings out)
# ---------------------------------------------------------------------------

def _check_dead_links(inventory: dict[str, ArticleInventory]) -> list[Finding]:
    """Check 1: Detect wikilinks that point to non-existent articles."""
    findings: list[Finding] = []
    all_slugs = set(inventory.keys())
    for slug, article in inventory.items():
        for linked in article.wikilinks:
            if linked not in all_slugs:
                findings.append(Finding(
                    severity="error",
                    check="dead_link",
                    slug=slug,
                    message=f"[[{linked}]] in {slug} -> concepts/{linked}.md does not exist",
                    details={"target": linked},
                ))
    return findings


def _check_orphans(inventory: dict[str, ArticleInventory]) -> list[Finding]:
    """Check 2: Detect articles with no inbound links."""
    inbound: dict[str, set[str]] = {}
    for slug, article in inventory.items():
        for linked in article.wikilinks:
            if linked in inventory:
                inbound.setdefault(linked, set()).add(slug)

    findings: list[Finding] = []
    for slug in inventory:
        if slug not in inbound or len(inbound[slug]) == 0:
            findings.append(Finding(
                severity="warning",
                check="orphan",
                slug=slug,
                message=f"{slug} has no inbound [[wikilink]] from other articles",
            ))
    return findings


def _check_missing_sources(
    inventory: dict[str, ArticleInventory], wiki_root: Path,
) -> list[Finding]:
    """Check 3: Detect source_refs pointing to non-existent files."""
    findings: list[Finding] = []
    for slug, article in inventory.items():
        source_refs = article.frontmatter.get("source_refs", [])
        if isinstance(source_refs, str):
            source_refs = [source_refs]
        for ref in source_refs:
            ref_path = wiki_root / ref
            if not ref_path.exists():
                findings.append(Finding(
                    severity="error",
                    check="missing_source",
                    slug=slug,
                    message=f"{slug} references {ref} but file does not exist",
                    details={"source_ref": ref},
                ))
    return findings


def _check_missing_fm(inventory: dict[str, ArticleInventory]) -> list[Finding]:
    """Check 4: Detect missing required frontmatter fields."""
    required_fields = [
        "title", "type", "source_refs", "created", "updated", "category", "tags",
    ]
    findings: list[Finding] = []
    for slug, article in inventory.items():
        fm = article.frontmatter
        missing = [f for f in required_fields if f not in fm]
        if missing:
            findings.append(Finding(
                severity="warning",
                check="missing_frontmatter",
                slug=slug,
                message=f"{slug} is missing frontmatter fields: {', '.join(missing)}",
                details={"missing_fields": missing},
            ))
    return findings


def _check_coverage_gaps(inventory: dict[str, ArticleInventory]) -> list[Finding]:
    """Check 5: Detect slugs referenced 2+ times but having no article."""
    all_slugs = set(inventory.keys())
    reference_counts: dict[str, int] = {}
    for article in inventory.values():
        for linked in set(article.wikilinks):
            reference_counts[linked] = reference_counts.get(linked, 0) + 1

    findings: list[Finding] = []
    for linked, count in reference_counts.items():
        if linked not in all_slugs and count >= 2:
            findings.append(Finding(
                severity="info",
                check="coverage_gap",
                slug=linked,
                message=f"[[{linked}]] referenced {count} times but no page exists",
                details={"reference_count": count},
            ))
    return findings


def _check_link_quality(inventory: dict[str, ArticleInventory]) -> list[Finding]:
    """Check 6: Detect one-way links and related/wikilink mismatches."""
    findings: list[Finding] = []
    all_slugs = set(inventory.keys())

    # Build sets of wikilinks and related slugs per article
    wikilink_sets: dict[str, set[str]] = {}
    related_sets: dict[str, set[str]] = {}

    for slug, article in inventory.items():
        wikilink_sets[slug] = set(article.wikilinks) & all_slugs
        related_raw = article.frontmatter.get("related", [])
        if isinstance(related_raw, str):
            related_raw = [related_raw]
        if isinstance(related_raw, list):
            related_sets[slug] = {_normalize_slug(r) for r in related_raw} & all_slugs
        else:
            related_sets[slug] = set()

    # One-way link detection: A links to B but B has no link/related back to A
    for slug, targets in wikilink_sets.items():
        for target in targets:
            if target == slug:
                continue
            reverse_links = wikilink_sets.get(target, set())
            reverse_related = related_sets.get(target, set())
            if slug not in reverse_links and slug not in reverse_related:
                findings.append(Finding(
                    severity="warning",
                    check="one_way_link",
                    slug=slug,
                    message=f"{slug} -> {target} is one-way (no backlink or related)",
                    details={"target": target},
                ))

    # Related/wikilink mismatch: in related FM but not in body wikilinks, or vice versa
    for slug in inventory:
        related = related_sets.get(slug, set())
        body_links = wikilink_sets.get(slug, set())
        # In related but not in body wikilinks
        for r_slug in related:
            if r_slug != slug and r_slug not in body_links:
                findings.append(Finding(
                    severity="warning",
                    check="related_mismatch",
                    slug=slug,
                    message=f"{slug}: '{r_slug}' is in related FM but not in body [[wikilinks]]",
                    details={"related_slug": r_slug, "direction": "related_only"},
                ))
        # In body wikilinks but not in related
        for w_slug in body_links:
            if w_slug != slug and w_slug not in related:
                findings.append(Finding(
                    severity="warning",
                    check="related_mismatch",
                    slug=slug,
                    message=f"{slug}: '{w_slug}' is in body [[wikilinks]] but not in related FM",
                    details={"related_slug": w_slug, "direction": "wikilink_only"},
                ))

    return findings


def _check_article_quality(inventory: dict[str, ArticleInventory]) -> list[Finding]:
    """Check 7: Detect short articles and speculation overload."""
    findings: list[Finding] = []

    for slug, article in inventory.items():
        body = article.body

        # Short article: body < 50 words
        word_count = len(body.split())
        if word_count < 50:
            findings.append(Finding(
                severity="warning",
                check="short_article",
                slug=slug,
                message=f"{slug} has only {word_count} words (< 50)",
                details={"word_count": word_count},
            ))

        # Speculation overload: > [推測] blocks > 30% of body lines
        body_lines = [line for line in body.splitlines() if line.strip()]
        if body_lines:
            spec_lines = [
                line for line in body_lines if line.strip().startswith("> [推測]")
            ]
            ratio = len(spec_lines) / len(body_lines)
            if ratio > 0.30:
                findings.append(Finding(
                    severity="warning",
                    check="speculation_overload",
                    slug=slug,
                    message=f"{slug} has {ratio:.0%} speculation blocks (> 30%)",
                    details={
                        "speculation_lines": len(spec_lines),
                        "total_lines": len(body_lines),
                        "ratio": round(ratio, 2),
                    },
                ))

    return findings


_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAG_PATTERN = re.compile(r"^[a-z0-9-]+$")


def _check_format(
    inventory: dict[str, ArticleInventory],
    wiki_root: Path,
    schema: dict | None,
    categories: list | None,
) -> list[Finding]:
    """Check 8: Detect format violations (slug, schema, category, etc.)."""
    findings: list[Finding] = []
    valid_categories = set()
    if categories:
        valid_categories = {c["slug"] for c in categories}

    for slug, article in inventory.items():
        fm = article.frontmatter

        # Slug naming violation
        if not _SLUG_PATTERN.match(slug):
            findings.append(Finding(
                severity="error",
                check="slug_violation",
                slug=slug,
                message=f"{slug} does not match slug pattern ^[a-z0-9]+(-[a-z0-9]+)*$",
            ))

        if schema is None:
            continue

        # type violation (const: "wiki")
        type_prop = schema.get("properties", {}).get("type", {})
        if "const" in type_prop:
            expected_type = type_prop["const"]
            actual_type = fm.get("type")
            if actual_type is not None and actual_type != expected_type:
                findings.append(Finding(
                    severity="error",
                    check="type_violation",
                    slug=slug,
                    message=f"{slug}: type is '{actual_type}', expected '{expected_type}'",
                    details={"actual": actual_type, "expected": expected_type},
                ))

        # category violation
        if valid_categories:
            cat = fm.get("category")
            if cat is not None and cat not in valid_categories:
                findings.append(Finding(
                    severity="warning",
                    check="category_violation",
                    slug=slug,
                    message=f"{slug}: category '{cat}' not in categories.json",
                    details={"actual": cat, "valid": sorted(valid_categories)},
                ))

        # date format violation
        for date_field in ("created", "updated"):
            val = fm.get(date_field)
            if val is not None and not _DATE_PATTERN.match(str(val)):
                findings.append(Finding(
                    severity="warning",
                    check="date_format_violation",
                    slug=slug,
                    message=f"{slug}: {date_field} '{val}' is not YYYY-MM-DD",
                    details={"field": date_field, "value": str(val)},
                ))

        # tags format violation
        tags = fm.get("tags", [])
        if isinstance(tags, list):
            for tag in tags:
                if not _TAG_PATTERN.match(str(tag)):
                    findings.append(Finding(
                        severity="warning",
                        check="tag_format_violation",
                        slug=slug,
                        message=f"{slug}: tag '{tag}' does not match ^[a-z0-9-]+$",
                        details={"tag": str(tag)},
                    ))

        # source_refs empty
        source_refs = fm.get("source_refs")
        if isinstance(source_refs, list) and len(source_refs) == 0:
            findings.append(Finding(
                severity="error",
                check="source_refs_empty",
                slug=slug,
                message=f"{slug}: source_refs is empty (minItems: 1)",
            ))

        # related type violation
        related = fm.get("related")
        if related is not None and not isinstance(related, list):
            findings.append(Finding(
                severity="warning",
                check="related_type_violation",
                slug=slug,
                message=f"{slug}: related is not an array",
                details={"actual_type": type(related).__name__},
            ))

    return findings


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_table(findings: list[Finding]) -> str:
    """Format findings as a human-readable table."""
    if not findings:
        return "No findings."

    lines: list[str] = []
    header = f"{'Severity':<10} {'Check':<25} {'Slug':<30} {'Message'}"
    lines.append(header)
    lines.append("-" * len(header))

    for f in sorted(findings, key=lambda x: (
        {"error": 0, "warning": 1, "info": 2}.get(x.severity, 3),
        x.check, x.slug,
    )):
        lines.append(f"{f.severity:<10} {f.check:<25} {f.slug:<30} {f.message}")

    # Summary
    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info")
    lines.append("")
    lines.append(f"Total: {error_count} errors, {warning_count} warnings, {info_count} info")
    return "\n".join(lines)


def format_json(findings: list[Finding]) -> str:
    """Format findings as JSON (compatible with previous output)."""
    grouped: dict[str, list[dict]] = {"error": [], "warning": [], "info": []}
    for f in findings:
        entry: dict = {
            "check": f.check,
            "slug": f.slug,
            "message": f.message,
        }
        if f.details:
            entry.update(f.details)
        grouped.setdefault(f.severity, []).append(entry)

    summary = {sev: len(items) for sev, items in grouped.items()}
    output = {"summary": summary, "findings": grouped}
    return json.dumps(output, ensure_ascii=False, indent=2)


def format_report(findings: list[Finding], *, today: date | None = None) -> str:
    """Format findings as a Markdown report."""
    if today is None:
        today = date.today()

    lines: list[str] = []
    lines.append(f"# Lint Report ({today.isoformat()})")
    lines.append("")

    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Errors: {error_count}")
    lines.append(f"- Warnings: {warning_count}")
    lines.append(f"- Info: {info_count}")
    lines.append("")

    lines.append("## Findings")
    lines.append("")

    if not findings:
        lines.append("No findings.")
    else:
        for severity in ("error", "warning", "info"):
            group = [f for f in findings if f.severity == severity]
            if not group:
                continue
            lines.append(f"### {severity.capitalize()} ({len(group)})")
            lines.append("")
            for f in group:
                lines.append(f"- **[{f.check}]** `{f.slug}`: {f.message}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def lint(wiki_root: Path) -> list[Finding]:
    """Run all lint checks and return a flat list of findings."""
    concepts_dir = wiki_root / "concepts"
    if not concepts_dir.exists():
        return [Finding(
            severity="error",
            check="structure",
            slug="",
            message=f"{concepts_dir} does not exist",
        )]

    inventory = _build_inventory(wiki_root)
    if not inventory:
        return []

    # Load schema and categories (once)
    schema: dict | None = None
    categories: list | None = None

    schema_path = wiki_root / "schema" / "page-template.json"
    if schema_path.exists():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

    categories_path = wiki_root / "schema" / "categories.json"
    if categories_path.exists():
        categories = json.loads(categories_path.read_text(encoding="utf-8"))

    findings: list[Finding] = []
    findings.extend(_check_dead_links(inventory))
    findings.extend(_check_orphans(inventory))
    findings.extend(_check_missing_sources(inventory, wiki_root))
    findings.extend(_check_missing_fm(inventory))
    findings.extend(_check_coverage_gaps(inventory))
    findings.extend(_check_link_quality(inventory))
    findings.extend(_check_article_quality(inventory))
    findings.extend(_check_format(inventory, wiki_root, schema, categories))

    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Wiki lint checker: dead links, orphans, quality, format.",
    )
    parser.add_argument(
        "positional_root",
        nargs="?",
        type=Path,
        default=None,
        help="Wiki root directory (positional fallback)",
    )
    parser.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki root directory",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "report"],
        default="table",
        dest="fmt",
        help="Output format (default: table)",
    )
    return parser


def main() -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    # Resolve wiki_root: --wiki-root takes priority, then positional
    wiki_root: Path | None = args.wiki_root or args.positional_root
    if wiki_root is None:
        parser.error("wiki root is required: use --wiki-root or pass as positional argument")

    if not wiki_root.is_dir():
        print(f"Error: {wiki_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    findings = lint(wiki_root)

    if args.fmt == "json":
        print(format_json(findings))
    elif args.fmt == "report":
        report_dir = wiki_root / "outputs" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()
        report_path = report_dir / f"{today.strftime('%Y%m%d')}-lint.md"
        content = format_report(findings, today=today)
        report_path.write_text(content, encoding="utf-8")
        print(f"Report written to {report_path}")
    else:
        print(format_table(findings))

    # Exit code: 1 if errors, 0 otherwise
    error_count = sum(1 for f in findings if f.severity == "error")
    sys.exit(1 if error_count > 0 else 0)


if __name__ == "__main__":
    main()
