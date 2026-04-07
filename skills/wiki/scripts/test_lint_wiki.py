#!/usr/bin/env python3
"""Unit tests for lint-wiki.py.

Tests cover:
  - Finding / ArticleInventory dataclasses
  - _build_inventory
  - Existing checks: dead_link, orphan, missing_source, missing_frontmatter, coverage_gap
  - New checks: link_quality, article_quality, format
  - Output formatters: format_table, format_json, format_report
  - CLI argument parsing
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import lint-wiki.py (hyphenated filename)
# ---------------------------------------------------------------------------

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("lint_wiki", _here / "lint-wiki.py")
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["lint_wiki"] = _mod  # register before exec for Python 3.10 compat
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

Finding = _mod.Finding
ArticleInventory = _mod.ArticleInventory
find_wikilinks = _mod.find_wikilinks
parse_frontmatter = _mod.parse_frontmatter
_build_inventory = _mod._build_inventory
_check_dead_links = _mod._check_dead_links
_check_orphans = _mod._check_orphans
_check_missing_sources = _mod._check_missing_sources
_check_missing_fm = _mod._check_missing_fm
_check_coverage_gaps = _mod._check_coverage_gaps
_check_link_quality = _mod._check_link_quality
_check_article_quality = _mod._check_article_quality
_check_format = _mod._check_format
format_table = _mod.format_table
format_json = _mod.format_json
format_report = _mod.format_report
lint = _mod.lint
_normalize_slug = _mod._normalize_slug


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_FM = textwrap.dedent("""\
    ---
    title: Test Article
    type: wiki
    source_refs:
      - "raw/articles/test.md"
    created: 2026-01-01
    updated: 2026-01-01
    category: concepts
    tags: [test]
    related:
      - "concepts/other.md"
    ---

    # Test Article

    Some body text here with a [[other]] wikilink.
    More text to make it long enough.
    This article has plenty of words to pass the short-article check.
    We need at least fifty words in the body for it to be considered a real article.
    So here are some more words to pad this out and reach that threshold comfortably.
""")


def test_find_wikilinks_still_extracts_plain():
    assert find_wikilinks("see [[foo]] and [[bar-baz]]") == ["foo", "bar-baz"]


def test_find_wikilinks_ignores_inline_code_span():
    text = "Use `[[wikilink]]` syntax to link, e.g. [[real-slug]]."
    assert find_wikilinks(text) == ["real-slug"]


def test_find_wikilinks_ignores_fenced_code_block():
    text = "Intro [[real]] then:\n```\n[[bar]]\n[[baz]]\n```\nAfter [[tail]]."
    assert find_wikilinks(text) == ["real", "tail"]


def test_dead_link_check_skips_example_wikilinks(tmp_path):
    body = textwrap.dedent("""\
        ---
        title: Example
        type: wiki
        category: concepts
        created: 2026-01-01
        updated: 2026-01-01
        tags: [test]
        ---

        # Example

        Use the `[[wikilink]]` notation. Inside fences:
        ```
        [[foo]]
        ```
        End.
        """)
    wiki_root = _make_wiki_basic(tmp_path, {"example": body})
    inv = _build_inventory(wiki_root)
    findings = _check_dead_links(inv)
    assert [f for f in findings if f.check == "dead_link"] == []


def _make_wiki_basic(tmp_path: Path, articles: dict[str, str]) -> Path:
    wiki_root = tmp_path / ".wiki"
    concepts = wiki_root / "concepts"
    concepts.mkdir(parents=True)
    for slug, content in articles.items():
        (concepts / f"{slug}.md").write_text(content, encoding="utf-8")
    (wiki_root / "schema").mkdir(parents=True, exist_ok=True)
    return wiki_root


def _make_wiki(tmp_path: Path, articles: dict[str, str], *,
               schema: dict | None = None,
               categories: list | None = None,
               raw_files: list[str] | None = None) -> Path:
    """Create a minimal wiki structure in tmp_path and return wiki_root."""
    wiki_root = tmp_path / ".wiki"
    concepts = wiki_root / "concepts"
    concepts.mkdir(parents=True)

    for slug, content in articles.items():
        (concepts / f"{slug}.md").write_text(content, encoding="utf-8")

    schema_dir = wiki_root / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)

    if schema is not None:
        (schema_dir / "page-template.json").write_text(
            json.dumps(schema), encoding="utf-8"
        )

    if categories is not None:
        (schema_dir / "categories.json").write_text(
            json.dumps(categories), encoding="utf-8"
        )

    if raw_files:
        for rf in raw_files:
            rp = wiki_root / rf
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text("source content", encoding="utf-8")

    return wiki_root


DEFAULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["title", "type", "source_refs", "created", "updated", "category", "tags"],
    "properties": {
        "title": {"type": "string"},
        "type": {"type": "string", "const": "wiki"},
        "source_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "created": {"type": "string", "format": "date"},
        "updated": {"type": "string", "format": "date"},
        "category": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "related": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

DEFAULT_CATEGORIES = [
    {"slug": "concepts", "name": "Concepts"},
    {"slug": "tools", "name": "Tools"},
]


# ===========================================================================
# _normalize_slug
# ===========================================================================

class TestNormalizeSlug:
    def test_bare_slug(self):
        assert _normalize_slug("foo") == "foo"

    def test_with_md_extension(self):
        assert _normalize_slug("foo.md") == "foo"

    def test_with_directory_prefix(self):
        assert _normalize_slug("concepts/foo.md") == "foo"

    def test_nested_path(self):
        assert _normalize_slug("raw/articles/foo.md") == "foo"


# ===========================================================================
# ArticleInventory / Finding dataclasses
# ===========================================================================

class TestDataclasses:
    def test_finding_is_frozen(self):
        f = Finding(severity="error", check="test", slug="a", message="m")
        with pytest.raises(AttributeError):
            f.severity = "warning"  # type: ignore[misc]

    def test_finding_default_details(self):
        f = Finding(severity="error", check="test", slug="a", message="m")
        assert f.details is None

    def test_finding_with_details(self):
        f = Finding(severity="error", check="test", slug="a", message="m",
                    details={"target": "b"})
        assert f.details == {"target": "b"}

    def test_article_inventory_is_frozen(self):
        a = ArticleInventory(slug="a", path="/a.md", frontmatter={},
                             wikilinks=[], text="", body="")
        with pytest.raises(AttributeError):
            a.slug = "b"  # type: ignore[misc]


# ===========================================================================
# _build_inventory
# ===========================================================================

class TestBuildInventory:
    def test_extracts_metadata(self, tmp_path):
        wiki_root = _make_wiki(tmp_path, {"alpha": VALID_FM})
        inv = _build_inventory(wiki_root)
        assert "alpha" in inv
        assert inv["alpha"].slug == "alpha"
        assert "other" in inv["alpha"].wikilinks
        assert inv["alpha"].frontmatter["title"] == "Test Article"

    def test_body_excludes_frontmatter(self, tmp_path):
        wiki_root = _make_wiki(tmp_path, {"alpha": VALID_FM})
        inv = _build_inventory(wiki_root)
        assert "---" not in inv["alpha"].body
        assert "Test Article" in inv["alpha"].body

    def test_concepts_not_exist_returns_empty(self, tmp_path):
        wiki_root = tmp_path / ".wiki"
        wiki_root.mkdir()
        # No concepts/ directory
        inv = _build_inventory(wiki_root)
        assert inv == {}


# ===========================================================================
# Check 1: Dead Links
# ===========================================================================

class TestCheckDeadLinks:
    def test_detects_dead_link(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            ---
            Body [[nonexistent]]
        """)
        wiki_root = _make_wiki(tmp_path, {"alpha": content})
        inv = _build_inventory(wiki_root)
        findings = _check_dead_links(inv)
        assert len(findings) == 1
        assert findings[0].check == "dead_link"
        assert findings[0].severity == "error"
        assert findings[0].details["target"] == "nonexistent"

    def test_no_dead_link_when_target_exists(self, tmp_path):
        a = "---\ntitle: A\ntype: wiki\n---\nBody [[beta]]"
        b = "---\ntitle: B\ntype: wiki\n---\nBody text"
        wiki_root = _make_wiki(tmp_path, {"alpha": a, "beta": b})
        inv = _build_inventory(wiki_root)
        findings = _check_dead_links(inv)
        assert len(findings) == 0


# ===========================================================================
# Check 2: Orphans
# ===========================================================================

class TestCheckOrphans:
    def test_detects_orphan(self, tmp_path):
        a = "---\ntitle: A\ntype: wiki\n---\nBody"
        b = "---\ntitle: B\ntype: wiki\n---\nBody"
        wiki_root = _make_wiki(tmp_path, {"alpha": a, "beta": b})
        inv = _build_inventory(wiki_root)
        findings = _check_orphans(inv)
        # Both are orphans (neither links to the other)
        slugs = {f.slug for f in findings}
        assert "alpha" in slugs
        assert "beta" in slugs

    def test_no_orphan_when_linked(self, tmp_path):
        a = "---\ntitle: A\ntype: wiki\n---\nBody [[beta]]"
        b = "---\ntitle: B\ntype: wiki\n---\nBody [[alpha]]"
        wiki_root = _make_wiki(tmp_path, {"alpha": a, "beta": b})
        inv = _build_inventory(wiki_root)
        findings = _check_orphans(inv)
        assert len(findings) == 0


# ===========================================================================
# Check 3: Missing Sources
# ===========================================================================

class TestCheckMissingSources:
    def test_detects_missing_source(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            source_refs:
              - "raw/articles/missing.md"
            ---
            Body
        """)
        wiki_root = _make_wiki(tmp_path, {"alpha": content})
        inv = _build_inventory(wiki_root)
        findings = _check_missing_sources(inv, wiki_root)
        assert len(findings) == 1
        assert findings[0].check == "missing_source"

    def test_no_missing_when_source_exists(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            source_refs:
              - "raw/articles/exists.md"
            ---
            Body
        """)
        wiki_root = _make_wiki(tmp_path, {"alpha": content},
                               raw_files=["raw/articles/exists.md"])
        inv = _build_inventory(wiki_root)
        findings = _check_missing_sources(inv, wiki_root)
        assert len(findings) == 0


# ===========================================================================
# Check 4: Missing Frontmatter
# ===========================================================================

class TestCheckMissingFrontmatter:
    def test_detects_missing_fields(self, tmp_path):
        content = "---\ntitle: A\n---\nBody"
        wiki_root = _make_wiki(tmp_path, {"alpha": content})
        inv = _build_inventory(wiki_root)
        findings = _check_missing_fm(inv)
        assert len(findings) == 1
        assert findings[0].check == "missing_frontmatter"
        assert "type" in findings[0].details["missing_fields"]

    def test_no_missing_when_complete(self, tmp_path):
        wiki_root = _make_wiki(tmp_path, {"alpha": VALID_FM})
        inv = _build_inventory(wiki_root)
        findings = _check_missing_fm(inv)
        assert len(findings) == 0


# ===========================================================================
# Check 5: Coverage Gaps
# ===========================================================================

class TestCheckCoverageGaps:
    def test_detects_coverage_gap(self, tmp_path):
        a = "---\ntitle: A\ntype: wiki\n---\nBody [[missing-topic]]"
        b = "---\ntitle: B\ntype: wiki\n---\nBody [[missing-topic]]"
        wiki_root = _make_wiki(tmp_path, {"alpha": a, "beta": b})
        inv = _build_inventory(wiki_root)
        findings = _check_coverage_gaps(inv)
        assert len(findings) == 1
        assert findings[0].check == "coverage_gap"
        assert findings[0].slug == "missing-topic"

    def test_no_gap_when_single_reference(self, tmp_path):
        a = "---\ntitle: A\ntype: wiki\n---\nBody [[missing-topic]]"
        b = "---\ntitle: B\ntype: wiki\n---\nBody"
        wiki_root = _make_wiki(tmp_path, {"alpha": a, "beta": b})
        inv = _build_inventory(wiki_root)
        findings = _check_coverage_gaps(inv)
        assert len(findings) == 0


# ===========================================================================
# Check 6: Link Quality
# ===========================================================================

class TestCheckLinkQuality:
    def test_detects_one_way_link(self, tmp_path):
        a = "---\ntitle: A\ntype: wiki\n---\nBody [[beta]]"
        b = "---\ntitle: B\ntype: wiki\n---\nBody text only"
        wiki_root = _make_wiki(tmp_path, {"alpha": a, "beta": b})
        inv = _build_inventory(wiki_root)
        findings = _check_link_quality(inv)
        one_way = [f for f in findings if f.check == "one_way_link"]
        assert len(one_way) == 1
        assert one_way[0].slug == "alpha"
        assert one_way[0].details["target"] == "beta"

    def test_no_one_way_when_bidirectional(self, tmp_path):
        a = "---\ntitle: A\ntype: wiki\n---\nBody [[beta]]"
        b = "---\ntitle: B\ntype: wiki\n---\nBody [[alpha]]"
        wiki_root = _make_wiki(tmp_path, {"alpha": a, "beta": b})
        inv = _build_inventory(wiki_root)
        findings = _check_link_quality(inv)
        one_way = [f for f in findings if f.check == "one_way_link"]
        assert len(one_way) == 0

    def test_detects_related_mismatch(self, tmp_path):
        # related has "beta" but body doesn't mention [[beta]]
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            related:
              - "concepts/beta.md"
            ---
            Body without any wikilinks
        """)
        b = "---\ntitle: B\ntype: wiki\n---\nBody"
        wiki_root = _make_wiki(tmp_path, {"alpha": content, "beta": b})
        inv = _build_inventory(wiki_root)
        findings = _check_link_quality(inv)
        mismatch = [f for f in findings if f.check == "related_mismatch"]
        assert len(mismatch) >= 1


# ===========================================================================
# Check 7: Article Quality
# ===========================================================================

class TestCheckArticleQuality:
    def test_detects_short_article(self, tmp_path):
        content = "---\ntitle: A\ntype: wiki\n---\nShort."
        wiki_root = _make_wiki(tmp_path, {"alpha": content})
        inv = _build_inventory(wiki_root)
        findings = _check_article_quality(inv)
        short = [f for f in findings if f.check == "short_article"]
        assert len(short) == 1

    def test_no_short_article_when_long_enough(self, tmp_path):
        wiki_root = _make_wiki(tmp_path, {"alpha": VALID_FM})
        inv = _build_inventory(wiki_root)
        findings = _check_article_quality(inv)
        short = [f for f in findings if f.check == "short_article"]
        assert len(short) == 0

    def test_detects_speculation_overload(self, tmp_path):
        # 10 lines total, 4 speculation = 40% > 30%
        body_lines = ["> [推測] line"] * 4 + ["normal line"] * 6
        content = "---\ntitle: A\ntype: wiki\n---\n" + "\n".join(body_lines)
        wiki_root = _make_wiki(tmp_path, {"alpha": content})
        inv = _build_inventory(wiki_root)
        findings = _check_article_quality(inv)
        spec = [f for f in findings if f.check == "speculation_overload"]
        assert len(spec) == 1


# ===========================================================================
# Check 8: Format Violations
# ===========================================================================

class TestCheckFormat:
    def test_detects_slug_violation(self, tmp_path):
        content = "---\ntitle: A\ntype: wiki\n---\nBody"
        wiki_root = _make_wiki(tmp_path, {"Bad_Name": content},
                               schema=DEFAULT_SCHEMA,
                               categories=DEFAULT_CATEGORIES)
        inv = _build_inventory(wiki_root)
        findings = _check_format(inv, wiki_root, DEFAULT_SCHEMA, DEFAULT_CATEGORIES)
        slug_v = [f for f in findings if f.check == "slug_violation"]
        assert len(slug_v) == 1

    def test_detects_invalid_type(self, tmp_path):
        content = "---\ntitle: A\ntype: blog\n---\nBody"
        wiki_root = _make_wiki(tmp_path, {"alpha": content},
                               schema=DEFAULT_SCHEMA,
                               categories=DEFAULT_CATEGORIES)
        inv = _build_inventory(wiki_root)
        findings = _check_format(inv, wiki_root, DEFAULT_SCHEMA, DEFAULT_CATEGORIES)
        type_v = [f for f in findings if f.check == "type_violation"]
        assert len(type_v) == 1
        assert type_v[0].severity == "error"

    def test_detects_invalid_category(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            category: nonexistent
            ---
            Body
        """)
        wiki_root = _make_wiki(tmp_path, {"alpha": content},
                               schema=DEFAULT_SCHEMA,
                               categories=DEFAULT_CATEGORIES)
        inv = _build_inventory(wiki_root)
        findings = _check_format(inv, wiki_root, DEFAULT_SCHEMA, DEFAULT_CATEGORIES)
        cat_v = [f for f in findings if f.check == "category_violation"]
        assert len(cat_v) == 1

    def test_detects_invalid_date_format(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            created: 01/01/2026
            updated: 2026-01-01
            ---
            Body
        """)
        wiki_root = _make_wiki(tmp_path, {"alpha": content},
                               schema=DEFAULT_SCHEMA,
                               categories=DEFAULT_CATEGORIES)
        inv = _build_inventory(wiki_root)
        findings = _check_format(inv, wiki_root, DEFAULT_SCHEMA, DEFAULT_CATEGORIES)
        date_v = [f for f in findings if f.check == "date_format_violation"]
        assert len(date_v) == 1

    def test_detects_invalid_tags_format(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            tags: [Good-Tag, BAD TAG]
            ---
            Body
        """)
        wiki_root = _make_wiki(tmp_path, {"alpha": content},
                               schema=DEFAULT_SCHEMA,
                               categories=DEFAULT_CATEGORIES)
        inv = _build_inventory(wiki_root)
        findings = _check_format(inv, wiki_root, DEFAULT_SCHEMA, DEFAULT_CATEGORIES)
        tag_v = [f for f in findings if f.check == "tag_format_violation"]
        assert len(tag_v) >= 1

    def test_detects_empty_source_refs(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            source_refs: []
            ---
            Body
        """)
        wiki_root = _make_wiki(tmp_path, {"alpha": content},
                               schema=DEFAULT_SCHEMA,
                               categories=DEFAULT_CATEGORIES)
        inv = _build_inventory(wiki_root)
        findings = _check_format(inv, wiki_root, DEFAULT_SCHEMA, DEFAULT_CATEGORIES)
        sr_v = [f for f in findings if f.check == "source_refs_empty"]
        assert len(sr_v) == 1
        assert sr_v[0].severity == "error"

    def test_detects_related_type_violation(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            title: A
            type: wiki
            related: not-a-list
            ---
            Body
        """)
        wiki_root = _make_wiki(tmp_path, {"alpha": content},
                               schema=DEFAULT_SCHEMA,
                               categories=DEFAULT_CATEGORIES)
        inv = _build_inventory(wiki_root)
        findings = _check_format(inv, wiki_root, DEFAULT_SCHEMA, DEFAULT_CATEGORIES)
        rel_v = [f for f in findings if f.check == "related_type_violation"]
        assert len(rel_v) == 1

    def test_skips_when_schema_none(self, tmp_path):
        content = "---\ntitle: A\ntype: blog\n---\nBody"
        wiki_root = _make_wiki(tmp_path, {"alpha": content})
        inv = _build_inventory(wiki_root)
        # No schema/categories → should still detect slug issues but skip schema-based checks
        findings = _check_format(inv, wiki_root, None, None)
        # slug_violation check should still work (doesn't need schema)
        type_v = [f for f in findings if f.check == "type_violation"]
        assert len(type_v) == 0  # type check requires schema


# ===========================================================================
# Output Formatters
# ===========================================================================

class TestFormatTable:
    def test_returns_table_string(self):
        findings = [
            Finding(severity="error", check="dead_link", slug="a",
                    message="dead link", details={"target": "b"}),
            Finding(severity="warning", check="orphan", slug="c",
                    message="orphan article"),
        ]
        result = format_table(findings)
        assert "error" in result.lower() or "Error" in result or "ERROR" in result
        assert "dead_link" in result
        assert isinstance(result, str)


class TestFormatJson:
    def test_returns_valid_json(self):
        findings = [
            Finding(severity="error", check="dead_link", slug="a",
                    message="dead link"),
        ]
        result = format_json(findings)
        data = json.loads(result)
        assert "summary" in data
        assert "findings" in data

    def test_json_structure(self):
        findings = [
            Finding(severity="error", check="dead_link", slug="a",
                    message="dead link"),
            Finding(severity="warning", check="orphan", slug="b",
                    message="orphan"),
        ]
        result = format_json(findings)
        data = json.loads(result)
        assert data["summary"]["error"] == 1
        assert data["summary"]["warning"] == 1


class TestFormatReport:
    def test_returns_markdown_report(self):
        findings = [
            Finding(severity="error", check="dead_link", slug="a",
                    message="dead link"),
        ]
        result = format_report(findings)
        assert result.startswith("# Lint Report")
        assert "## Summary" in result
        assert "## Findings" in result


# ===========================================================================
# CLI argument parsing
# ===========================================================================

class TestCLI:
    def test_wiki_root_argument(self):
        """Verify --wiki-root and --format are parsed correctly."""
        parser = _mod._build_parser()
        args = parser.parse_args(["--wiki-root", ".wiki", "--format", "json"])
        assert args.wiki_root == Path(".wiki")
        assert args.fmt == "json"

    def test_positional_fallback(self):
        """Verify positional argument works as fallback for --wiki-root."""
        parser = _mod._build_parser()
        args = parser.parse_args([".wiki"])
        # Resolve same way as main(): --wiki-root takes priority, then positional
        resolved = args.wiki_root or args.positional_root
        assert resolved == Path(".wiki")

    def test_default_format_is_table(self):
        parser = _mod._build_parser()
        args = parser.parse_args(["--wiki-root", ".wiki"])
        assert args.fmt == "table"


# ===========================================================================
# Integration: lint() orchestrator
# ===========================================================================

class TestLintOrchestrator:
    def test_returns_findings_list(self, tmp_path):
        wiki_root = _make_wiki(tmp_path, {"alpha": VALID_FM},
                               schema=DEFAULT_SCHEMA,
                               categories=DEFAULT_CATEGORIES,
                               raw_files=["raw/articles/test.md"])
        findings = lint(wiki_root)
        assert isinstance(findings, list)
        assert all(isinstance(f, Finding) for f in findings)

    def test_concepts_not_exist_returns_error(self, tmp_path):
        wiki_root = tmp_path / ".wiki"
        wiki_root.mkdir()
        findings = lint(wiki_root)
        assert len(findings) == 1
        assert findings[0].check == "structure"
        assert findings[0].severity == "error"
