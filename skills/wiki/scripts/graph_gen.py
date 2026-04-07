#!/usr/bin/env python3
"""Knowledge graph generator: inventory -> graph.json (read-only view).

Responsibility
--------------
- Consume an in-memory inventory (list of :class:`ArticleInventory`) and
  build a deterministic graph (nodes + edges).
- CLI wrapper loads the wiki, invokes the pure ``build_graph()``, and writes
  ``.wiki/outputs/graph.json``.
- Dead wikilinks (targets not present in the inventory) are NOT emitted as
  edges; they are recorded under ``metadata.dangling_links`` so downstream
  consumers (lint) can surface them without crossing layer boundaries.

Usage::

    python graph_gen.py --wiki-root .wiki
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Support both "python graph_gen.py" and "python -m graph_gen".
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lib.graph_schema import GRAPH_SCHEMA_VERSION, GraphEdge, GraphNode
from lib.inventory import (
    ArticleInventory,
    parse_articles,
    to_json as inventory_to_json,
)


# ---------------------------------------------------------------------------
# Graph container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Graph:
    """An immutable graph snapshot (nodes + edges + metadata)."""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    dangling_links: tuple[dict, ...]
    source_inventory_sha256: str

    def to_dict(self, *, generated_at: str) -> dict:
        return {
            "version": GRAPH_SCHEMA_VERSION,
            "generated_at": generated_at,
            "metadata": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "source_inventory_sha256": self.source_inventory_sha256,
                "dangling_links": [dict(d) for d in self.dangling_links],
            },
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }


# ---------------------------------------------------------------------------
# Pure graph builder
# ---------------------------------------------------------------------------

def build_graph(
    inventory: list[ArticleInventory],
    *,
    querylog: object | None = None,  # reserved for Layer 3
    source_inventory_sha256: str = "",
) -> Graph:
    """Build a deterministic graph from an inventory.

    Parameters
    ----------
    inventory:
        Output of :func:`lib.inventory.parse_articles`.
    querylog:
        Reserved for Layer 3 co-citation enrichment. Currently unused.
    source_inventory_sha256:
        SHA-256 of the serialized inventory JSON; embedded into the graph's
        metadata so consumers can verify the derivation is in sync.
    """
    del querylog  # reserved; intentionally unused in v1.0

    # Nodes: one per article, sorted by slug.
    nodes = tuple(
        sorted(
            (
                GraphNode(
                    id=a.slug,
                    slug=a.slug,
                    type="article",
                    category=a.category,
                )
                for a in inventory
            ),
            key=lambda n: n.id,
        )
    )

    known_slugs = {a.slug for a in inventory}

    edge_list: list[GraphEdge] = []
    dangling: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()

    for article in inventory:
        for linked in article.wikilinks:
            if linked == article.slug:
                continue
            if linked not in known_slugs:
                dangling.append({"source": article.slug, "target": linked})
                continue
            key = (article.slug, linked, "wikilink")
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edge_list.append(
                GraphEdge(
                    source=article.slug,
                    target=linked,
                    relation_type="wikilink",
                )
            )

    edges = tuple(
        sorted(edge_list, key=lambda e: (e.source, e.target, e.relation_type))
    )
    dangling_tuple = tuple(
        sorted(dangling, key=lambda d: (d["source"], d["target"]))
    )

    return Graph(
        nodes=nodes,
        edges=edges,
        dangling_links=dangling_tuple,
        source_inventory_sha256=source_inventory_sha256,
    )


def graph_to_json(graph: Graph, *, generated_at: str) -> str:
    """Serialize a Graph to canonical JSON (deterministic ordering)."""
    return json.dumps(
        graph.to_dict(generated_at=generated_at),
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(wiki_root: Path, *, generated_at: str | None = None) -> Path:
    """Generate ``.wiki/outputs/graph.json`` for ``wiki_root``.

    Also writes ``.wiki/outputs/inventory.json`` so downstream tools
    (and determinism tests) have a single on-disk derivation.
    Returns the path to the written graph.json.
    """
    if generated_at is None:
        generated_at = _now_iso()

    articles = parse_articles(wiki_root)
    inv_json = inventory_to_json(
        articles, wiki_root=wiki_root, generated_at=generated_at
    )
    inv_sha = hashlib.sha256(inv_json.encode("utf-8")).hexdigest()

    graph = build_graph(articles, source_inventory_sha256=inv_sha)
    graph_json = graph_to_json(graph, generated_at=generated_at)

    out_dir = wiki_root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "inventory.json").write_text(inv_json, encoding="utf-8")
    graph_path = out_dir / "graph.json"
    graph_path.write_text(graph_json, encoding="utf-8")
    return graph_path


def load_graph(graph_path: Path) -> dict:
    """Load a previously generated graph.json as a plain dict."""
    return json.loads(graph_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate .wiki/outputs/graph.json from concepts/*.md",
    )
    parser.add_argument("--wiki-root", type=Path, required=True)
    args = parser.parse_args(argv)

    if not args.wiki_root.is_dir():
        print(f"Error: {args.wiki_root} is not a directory", file=sys.stderr)
        return 1

    path = generate(args.wiki_root)
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
