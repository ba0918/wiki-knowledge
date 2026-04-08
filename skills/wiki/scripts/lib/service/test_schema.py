"""Unit tests for lib/service/schema.py (v1 YAML I/O + v0/v1 detection).

``schema.py`` is the single entry point for converting v1 articles between
in-memory :class:`Article` instances and their on-disk YAML-frontmatter
representation. It also provides the ``detect_schema_version`` helper so
that ``migrate.py`` can tell v0 articles from v1 articles before deciding
what to do.

Key invariants verified here:

* **Round-trip equivalence**: ``load(dump(article))`` returns an article
  equal to the original, including all tuple-typed nested structures
  (``sources`` / ``claims`` / ``relations.related_to`` / ``tags`` /
  ``extensions["review"]["audit"]``). YAML dumps tuples as lists, so the
  loader is responsible for the inverse conversion.
* **Field validation**: missing required fields, illegal ``status`` values,
  illegal ``article_type`` values, and wrong primitive types return
  ``Err(SchemaError.*)`` rather than raising.
* **v0 detection**: legacy ``.wiki/concepts/*.md`` files (no
  ``schema_version``) are detected as v0 without crashing, so migrations
  can route them appropriately.
* **Forward compatibility**: unknown top-level fields on a v1 article are
  preserved in ``Article.extensions`` rather than rejected, so that future
  minor extensions do not break older readers.
"""

from __future__ import annotations

import pytest

from lib.domain.types import (
    Article,
    Claim,
    Err,
    GeneratedBy,
    KnowledgeTime,
    Ok,
    Relations,
    ReviewAuditEntry,
    Source,
)
from lib.service.schema import (
    SchemaError,
    detect_schema_version,
    dump_article,
    load_article,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_article() -> Article:
    return Article(
        schema_version=1,
        article_id="20260408163658-customer-a-ops",
        article_type="runbook",
        title="顧客A 運用フロー",
        captured_at="2026-04-08",
        knowledge_time=KnowledgeTime(valid_from="2023-04-01", valid_to=None),
        status="current",
        sources=(
            Source(
                id="src-001",
                type="file",
                ref=".wiki/raw/articles/customer-a.md",
                source_version=1,
                content_hash="sha256:" + "a" * 64,
                fetched_at="2026-04-08T09:12:00Z",
                permalink=None,
            ),
            Source(
                id="src-002",
                type="slack_thread",
                ref=".wiki/raw/slack/20230401-ops.md",
                source_version=2,
                content_hash="sha256:" + "b" * 64,
                fetched_at="2026-04-08T09:15:00Z",
                permalink="https://example.slack.com/archives/C1/p1700000000000100",
            ),
        ),
        relations=Relations(
            supersedes=("20230101-old-ops",),
            related_to=("20260101-customer-b-ops", "20250315-escalation-matrix"),
        ),
        claims=(
            Claim(
                claim_id="20260408163658-customer-a-ops#c-12345678",
                subject="Customer A",
                attribute="runbook_exists",
                period=KnowledgeTime(valid_from="2023-04-01", valid_to=None),
                predicate="Customer A has an approved runbook for payment ops",
                source_refs=("src-001", "src-002"),
            ),
        ),
        claim_refs=("20250101-other-article#c-aabbccdd",),
        generated_by=GeneratedBy(
            tool="wiki-compile",
            version=1,
            generated_at="2026-04-08T09:12:00Z",
        ),
        extensions={
            "review": {
                "audit": (
                    ReviewAuditEntry(
                        resolver="mizumi",
                        resolved_at="2026-04-08T12:00:00Z",
                        status_before="disputed",
                        status_after="current",
                        reason="Reviewed evidence with ops lead",
                        superseded_by_id=None,
                    ),
                )
            }
        },
        tags=("runbook", "customer-a", "payments"),
        body=(
            "## Summary\n"
            "Customer A の運用フローのサマリ。\n\n"
            "## Current Understanding\n"
            "See [[20250315-escalation-matrix]] for escalation.\n"
        ),
    )


# ---------------------------------------------------------------------------
# Round-trip equivalence
# ---------------------------------------------------------------------------


def test_dump_then_load_round_trips_equal() -> None:
    original = _sample_article()
    text = dump_article(original)
    result = load_article(text)
    assert isinstance(result, Ok), (
        f"expected Ok, got {result!r}"
        f"\n--- dumped text ---\n{text}"
    )
    assert result.value == original


def test_dump_then_load_preserves_empty_collections() -> None:
    empty = Article(
        schema_version=1,
        article_id="20260408000000-empty",
        article_type="concept",
        title="empty",
        captured_at="2026-04-08",
        knowledge_time=KnowledgeTime(valid_from=None, valid_to=None),
        status="unverified",
        sources=(),
        relations=Relations(),
        claims=(),
        claim_refs=(),
        generated_by=GeneratedBy(
            tool="wiki-compile", version=1, generated_at="2026-04-08T09:12:00Z"
        ),
        extensions={},
        tags=(),
        body="",
    )
    text = dump_article(empty)
    result = load_article(text)
    assert isinstance(result, Ok)
    assert result.value == empty


def test_dump_then_load_preserves_review_audit_tuple() -> None:
    article = _sample_article()
    text = dump_article(article)
    result = load_article(text)
    assert isinstance(result, Ok)
    audit = result.value.extensions["review"]["audit"]  # type: ignore[index]
    assert isinstance(audit, tuple)
    assert len(audit) == 1
    assert audit[0].resolver == "mizumi"


def test_dump_includes_body_after_frontmatter() -> None:
    article = _sample_article()
    text = dump_article(article)
    # The YAML frontmatter is delimited by ``---`` on its own line; the body
    # must come after the second ``---``.
    assert text.startswith("---\n")
    parts = text.split("---\n", 2)
    assert len(parts) == 3
    body_section = parts[2]
    assert "Customer A の運用フローのサマリ" in body_section


# ---------------------------------------------------------------------------
# Field validation (returns Err, never raises)
# ---------------------------------------------------------------------------


def test_load_rejects_missing_article_id() -> None:
    bad = """---
schema_version: 1
article_type: concept
title: t
captured_at: 2026-04-08
knowledge_time: {valid_from: null, valid_to: null}
status: current
sources: []
relations: {}
claims: []
claim_refs: []
generated_by: {tool: x, version: 1, generated_at: '2026-04-08T09:12:00Z'}
extensions: {}
tags: []
---
body
"""
    result = load_article(bad)
    assert isinstance(result, Err)
    assert result.error == SchemaError.MISSING_FIELD
    assert "article_id" in result.detail


def test_load_rejects_unknown_status() -> None:
    article = _sample_article()
    text = dump_article(article)
    text = text.replace("status: current", "status: conflicted")
    result = load_article(text)
    assert isinstance(result, Err)
    assert result.error == SchemaError.INVALID_STATUS


def test_load_rejects_unknown_article_type() -> None:
    article = _sample_article()
    text = dump_article(article)
    text = text.replace("article_type: runbook", "article_type: howto")
    result = load_article(text)
    assert isinstance(result, Err)
    assert result.error == SchemaError.INVALID_ARTICLE_TYPE


def test_load_rejects_unknown_schema_version() -> None:
    article = _sample_article()
    text = dump_article(article)
    text = text.replace("schema_version: 1", "schema_version: 99")
    result = load_article(text)
    assert isinstance(result, Err)
    assert result.error == SchemaError.UNSUPPORTED_SCHEMA_VERSION


def test_load_rejects_v0_article() -> None:
    """schema.py only loads v1; v0 articles must route through
    migrations/v0_to_v1.py, so load_article must Err distinctly."""
    v0_text = """---
title: legacy
type: wiki
source_refs: ["raw/articles/foo.md"]
created: 2024-01-01
updated: 2024-01-02
category: concepts
tags: [legacy]
---
legacy body
"""
    result = load_article(v0_text)
    assert isinstance(result, Err)
    assert result.error == SchemaError.LEGACY_SCHEMA


# ---------------------------------------------------------------------------
# Schema version detection (works on raw text for pre-flight)
# ---------------------------------------------------------------------------


def test_detect_schema_version_v1() -> None:
    text = dump_article(_sample_article())
    assert detect_schema_version(text) == 1


def test_detect_schema_version_v0() -> None:
    v0_text = """---
title: legacy
type: wiki
source_refs: [a]
created: 2024-01-01
updated: 2024-01-02
category: c
tags: []
---
body
"""
    assert detect_schema_version(v0_text) == 0


def test_detect_schema_version_returns_none_for_unknown() -> None:
    garbage = "no frontmatter at all\njust body"
    assert detect_schema_version(garbage) is None


# ---------------------------------------------------------------------------
# Forward compatibility — unknown top-level fields preserved
# ---------------------------------------------------------------------------


def test_unknown_top_level_field_preserved_in_extensions() -> None:
    article = _sample_article()
    text = dump_article(article)
    # Inject an unknown field at the top of the frontmatter.
    text = text.replace(
        "schema_version: 1\n",
        "schema_version: 1\nexperimental_field: {foo: bar}\n",
        1,
    )
    result = load_article(text)
    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    # Unknown fields are stashed under extensions["_unknown"] so that the
    # next writer can round-trip them if they also preserve extensions.
    unknown = result.value.extensions.get("_unknown")
    assert unknown is not None
    assert unknown == {"experimental_field": {"foo": "bar"}}  # type: ignore[comparison-overlap]


# ---------------------------------------------------------------------------
# Tuple round-trip sanity (YAML dumps tuples as lists)
# ---------------------------------------------------------------------------


def test_tags_survive_list_to_tuple_conversion() -> None:
    article = _sample_article()
    text = dump_article(article)
    result = load_article(text)
    assert isinstance(result, Ok)
    assert isinstance(result.value.tags, tuple)
    assert result.value.tags == ("runbook", "customer-a", "payments")


def test_relations_related_to_is_tuple_after_load() -> None:
    article = _sample_article()
    text = dump_article(article)
    result = load_article(text)
    assert isinstance(result, Ok)
    assert isinstance(result.value.relations.related_to, tuple)


def test_claims_source_refs_is_tuple_after_load() -> None:
    article = _sample_article()
    text = dump_article(article)
    result = load_article(text)
    assert isinstance(result, Ok)
    assert isinstance(result.value.claims[0].source_refs, tuple)
