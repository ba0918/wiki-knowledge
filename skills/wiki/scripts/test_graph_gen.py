"""Unit tests for graph_gen.py (pure builder + CLI wrapper)."""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

import pytest

from graph_gen import Graph, build_graph, generate, graph_to_json, load_graph
from lib.graph_schema import GRAPH_SCHEMA_VERSION
from lib.inventory import parse_articles


FM = textwrap.dedent("""\
    ---
    title: {title}
    type: wiki
    source_refs:
      - "raw/articles/{slug}.md"
    created: 2026-01-01
    updated: 2026-01-02
    category: concepts
    tags: [test]
    ---

    # {title}

    {body}
    """)


def _write(concepts: Path, slug: str, title: str, body: str) -> None:
    (concepts / f"{slug}.md").write_text(
        FM.format(title=title, slug=slug, body=body), encoding="utf-8"
    )


def _setup_wiki(tmp_path: Path) -> Path:
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    _write(concepts, "foo", "Foo", "Links to [[bar]] and [[missing]].")
    _write(concepts, "bar", "Bar", "Points back to [[foo]].")
    _write(concepts, "baz", "Baz", "Orphan-ish references [[foo]].")
    return tmp_path


def test_build_graph_nodes_and_edges(tmp_path: Path) -> None:
    wiki_root = _setup_wiki(tmp_path)
    articles = parse_articles(wiki_root)

    g = build_graph(articles)

    assert [n.id for n in g.nodes] == ["bar", "baz", "foo"]
    # Edges sorted by (source, target, relation_type); dead edge excluded.
    edge_keys = [(e.source, e.target) for e in g.edges]
    assert edge_keys == [("bar", "foo"), ("baz", "foo"), ("foo", "bar")]
    for e in g.edges:
        assert e.relation_type == "wikilink"
        assert e.weight == 1.0
        assert e.co_citation_count == 0
        assert e.claim_id is None


def test_dangling_links_captured(tmp_path: Path) -> None:
    wiki_root = _setup_wiki(tmp_path)
    articles = parse_articles(wiki_root)
    g = build_graph(articles)
    assert {"source": "foo", "target": "missing"} in g.dangling_links


def test_build_graph_without_querylog(tmp_path: Path) -> None:
    wiki_root = _setup_wiki(tmp_path)
    articles = parse_articles(wiki_root)
    g = build_graph(articles, querylog=None)
    assert isinstance(g, Graph)


def test_graph_json_is_deterministic(tmp_path: Path) -> None:
    wiki_root = _setup_wiki(tmp_path)
    articles = parse_articles(wiki_root)
    g = build_graph(articles, source_inventory_sha256="abc")
    a = graph_to_json(g, generated_at="2026-04-07T00:00:00Z")
    b = graph_to_json(g, generated_at="2026-04-07T00:00:00Z")
    assert a == b
    assert hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(
        b.encode()
    ).hexdigest()


def test_schema_version_and_reserved_fields(tmp_path: Path) -> None:
    wiki_root = _setup_wiki(tmp_path)
    articles = parse_articles(wiki_root)
    g = build_graph(articles)
    as_dict = g.to_dict(generated_at="2026-04-07T00:00:00Z")
    assert as_dict["version"] == GRAPH_SCHEMA_VERSION == "1.0"
    assert "dangling_links" in as_dict["metadata"]
    assert "source_inventory_sha256" in as_dict["metadata"]
    # Reserved node/edge fields present.
    for n in as_dict["nodes"]:
        assert "_custom" in n
    for e in as_dict["edges"]:
        assert "claim_id" in e
        assert "_custom" in e
        assert "co_citation_count" in e
        assert "co_citation_frequency" in e


def test_cli_generate_writes_graph_and_inventory(tmp_path: Path) -> None:
    wiki_root = _setup_wiki(tmp_path)
    graph_path = generate(wiki_root, generated_at="2026-04-07T00:00:00Z")
    assert graph_path.exists()
    assert (wiki_root / "outputs" / "inventory.json").exists()

    loaded = load_graph(graph_path)
    assert loaded["version"] == "1.0"
    assert loaded["metadata"]["node_count"] == 3

    # Determinism: regenerating with same timestamp produces identical bytes.
    first = graph_path.read_bytes()
    generate(wiki_root, generated_at="2026-04-07T00:00:00Z")
    second = graph_path.read_bytes()
    assert first == second


def test_self_link_is_ignored(tmp_path: Path) -> None:
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    _write(concepts, "solo", "Solo", "Self ref [[solo]] should be ignored.")
    articles = parse_articles(tmp_path)
    g = build_graph(articles)
    assert g.edges == ()
