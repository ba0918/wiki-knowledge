"""Unit tests for lib/inventory.py (pure function contract)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from lib.inventory import (
    ArticleInventory,
    article_to_json_dict,
    build_inventory,
    compute_sha256,
    extract_body,
    find_wikilinks,
    normalize_newlines,
    parse_article,
    parse_articles,
    parse_frontmatter,
    to_json,
)


def _write(concepts: Path, slug: str, content: str) -> Path:
    p = concepts / f"{slug}.md"
    p.write_text(content, encoding="utf-8")
    return p


FM_TEMPLATE = textwrap.dedent("""\
    ---
    title: {title}
    type: wiki
    source_refs:
      - "raw/articles/{slug}.md"
    created: 2026-01-01
    updated: 2026-01-02
    category: concepts
    tags: [alpha, beta]
    ---

    # {title}

    Body referencing [[other]] and [[other|Aliased]] plus a plain word.
    """)


def test_parse_article_extracts_fields(tmp_path: Path) -> None:
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    path = _write(concepts, "foo", FM_TEMPLATE.format(title="Foo", slug="foo"))

    art = parse_article(path, wiki_root=tmp_path)

    assert art.slug == "foo"
    assert art.path == "concepts/foo.md"
    assert art.title == "Foo"
    assert art.category == "concepts"
    assert art.type == "wiki"
    assert art.updated == "2026-01-02"
    assert art.tags == ("alpha", "beta")
    assert "other" in art.wikilinks
    assert art.source_refs == ("raw/articles/foo.md",)
    assert len(art.sha256) == 64
    assert art.body.startswith("# Foo")


def test_parse_articles_sorts_by_slug(tmp_path: Path) -> None:
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    _write(concepts, "zulu", FM_TEMPLATE.format(title="Z", slug="zulu"))
    _write(concepts, "alpha", FM_TEMPLATE.format(title="A", slug="alpha"))
    _write(concepts, "mike", FM_TEMPLATE.format(title="M", slug="mike"))

    articles = parse_articles(tmp_path)

    assert [a.slug for a in articles] == ["alpha", "mike", "zulu"]


def test_to_json_is_deterministic(tmp_path: Path) -> None:
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    _write(concepts, "foo", FM_TEMPLATE.format(title="Foo", slug="foo"))
    _write(concepts, "bar", FM_TEMPLATE.format(title="Bar", slug="bar"))

    arts = parse_articles(tmp_path)
    a = to_json(arts, wiki_root=tmp_path, generated_at="2026-04-07T00:00:00Z")
    b = to_json(arts, wiki_root=tmp_path, generated_at="2026-04-07T00:00:00Z")

    assert compute_sha256(a) == compute_sha256(b)
    assert a == b


def test_parse_article_without_frontmatter(tmp_path: Path) -> None:
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    path = _write(concepts, "bare", "# Bare\n\nJust [[foo]] text, no FM.\n")

    art = parse_article(path, wiki_root=tmp_path)

    assert art.slug == "bare"
    assert art.frontmatter == {}
    assert art.title == "bare"  # falls back to slug
    assert "foo" in art.wikilinks


def test_find_wikilinks_handles_alias_and_code() -> None:
    text = textwrap.dedent("""\
        Hello [[foo]] and [[bar|Alias Label]].
        Inline `[[skip]]` should be excluded.
        ```
        [[also-skip]]
        ```
        End [[baz]].
        """)
    links = find_wikilinks(text)
    assert links == ["foo", "bar", "baz"]


def test_sha256_normalizes_line_endings() -> None:
    lf = "---\ntitle: X\n---\nbody\n"
    crlf = lf.replace("\n", "\r\n")
    assert compute_sha256(lf) == compute_sha256(crlf)


def test_path_traversal_guard(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    bad = outside / "evil.md"
    bad.write_text("# evil\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside wiki_root"):
        parse_article(bad, wiki_root=wiki_root)


def test_build_inventory_returns_mapping(tmp_path: Path) -> None:
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    _write(concepts, "foo", FM_TEMPLATE.format(title="Foo", slug="foo"))

    inv = build_inventory(tmp_path)
    assert set(inv.keys()) == {"foo"}
    assert isinstance(inv["foo"], ArticleInventory)


def test_article_to_json_dict_excludes_text_and_body(tmp_path: Path) -> None:
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    path = _write(concepts, "foo", FM_TEMPLATE.format(title="Foo", slug="foo"))
    art = parse_article(path, wiki_root=tmp_path)

    d = article_to_json_dict(art)
    assert "text" not in d
    assert "body" not in d
    assert d["slug"] == "foo"
