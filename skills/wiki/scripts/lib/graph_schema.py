"""Graph schema definitions (version 1.0).

Immutable dataclasses describing the nodes and edges of ``graph.json``.
Reserved fields (``_custom``, ``claim_id``, ``co_citation_count``,
``co_citation_frequency``) are pre-allocated so Layer 3 features can be
added without breaking consumers of v1.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GRAPH_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class GraphNode:
    """A node in the knowledge graph (currently: one article)."""

    id: str
    slug: str
    type: str           # "article"
    category: str
    _custom: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "type": self.type,
            "category": self.category,
            "_custom": dict(self._custom),
        }


@dataclass(frozen=True)
class GraphEdge:
    """A directed edge between two nodes."""

    source: str
    target: str
    relation_type: str          # "wikilink" (v1.0)
    weight: float = 1.0
    co_citation_count: int = 0
    co_citation_frequency: float = 0.0
    confidence: float = 1.0
    sources: tuple[str, ...] = ("wikilink",)
    claim_id: str | None = None
    _custom: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "relation_type": self.relation_type,
            "weight": self.weight,
            "co_citation_count": self.co_citation_count,
            "co_citation_frequency": self.co_citation_frequency,
            "confidence": self.confidence,
            "sources": list(self.sources),
            "claim_id": self.claim_id,
            "_custom": dict(self._custom),
        }
