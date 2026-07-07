"""Unit tests for lib/domain/types.py (frozen dataclass contracts + Result type).

These tests are pure: no filesystem, no network, no mocks required.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from typing import get_args

import pytest

from lib.domain.types import (
    Article,
    ArticleType,
    Claim,
    Err,
    GeneratedBy,
    KnowledgeTime,
    Ok,
    Relations,
    ReviewAuditEntry,
    Segment,
    Source,
    Status,
    is_err,
    is_ok,
)


# ---------------------------------------------------------------------------
# Literal type declarations
# ---------------------------------------------------------------------------


def test_article_type_literal_values() -> None:
    allowed = set(get_args(ArticleType))
    assert allowed == {"decision", "runbook", "reference", "concept"}


def test_status_literal_values() -> None:
    """Status is the single source of truth: 4 values only (no 'conflicted', no 'draft')."""
    allowed = set(get_args(Status))
    assert allowed == {"current", "historical", "disputed", "unverified"}


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def _make_source(**overrides: object) -> Source:
    base: dict[str, object] = dict(
        id="src-001",
        type="file",
        ref=".wiki/raw/articles/foo.md",
        source_version=1,
        content_hash="sha256:" + "a" * 64,
        fetched_at="2026-04-08T09:12:00Z",
    )
    base.update(overrides)
    return Source(**base)  # type: ignore[arg-type]


def test_source_frozen() -> None:
    s = _make_source()
    with pytest.raises(FrozenInstanceError):
        s.id = "src-002"  # type: ignore[misc]


def test_source_permalink_default_none() -> None:
    s = _make_source()
    assert s.permalink is None


def test_source_equality_by_value() -> None:
    a = _make_source()
    b = _make_source()
    assert a == b  # frozen dataclass eq=True by default


def test_source_revision_default_none() -> None:
    s = _make_source()
    assert s.revision is None


def test_source_accepts_revision() -> None:
    s = _make_source(revision="48b0c795f4feb37343b2832d991c5c6a3900c08a")
    assert s.revision == "48b0c795f4feb37343b2832d991c5c6a3900c08a"


# ---------------------------------------------------------------------------
# GeneratedBy (strictly 3 fields — no audit, no extra)
# ---------------------------------------------------------------------------


def test_generated_by_has_exactly_three_fields() -> None:
    names = {f.name for f in fields(GeneratedBy)}
    assert names == {"tool", "version", "generated_at"}


def test_generated_by_frozen() -> None:
    g = GeneratedBy(tool="wiki-compile", version=1, generated_at="2026-04-08T09:12:00Z")
    with pytest.raises(FrozenInstanceError):
        g.version = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------


def test_segment_defaults() -> None:
    seg = Segment(speaker="alice", speaker_type="user", ts="1700000000.000100", content="hi")
    assert seg.edited_at is None
    assert seg.deleted is False
    assert seg.orphan is False
    assert seg.reply_to is None


def test_segment_frozen() -> None:
    seg = Segment(speaker="alice", speaker_type="user", ts="1", content="hi")
    with pytest.raises(FrozenInstanceError):
        seg.content = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# KnowledgeTime ("valid_to: null" = still current, no known end)
# ---------------------------------------------------------------------------


def test_knowledge_time_valid_to_can_be_none() -> None:
    kt = KnowledgeTime(valid_from="2023-04-01", valid_to=None)
    assert kt.valid_to is None


def test_knowledge_time_frozen() -> None:
    kt = KnowledgeTime(valid_from="2023-04-01", valid_to=None)
    with pytest.raises(FrozenInstanceError):
        kt.valid_from = "2024-01-01"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Relations (tuple defaults, immutable)
# ---------------------------------------------------------------------------


def test_relations_default_empty_tuples() -> None:
    r = Relations()
    assert r.supersedes == ()
    assert r.superseded_by is None
    assert r.caused_by == ()
    assert r.derived_from == ()
    assert r.implements == ()
    assert r.depends_on == ()
    assert r.related_to == ()


def test_relations_frozen() -> None:
    r = Relations()
    with pytest.raises(FrozenInstanceError):
        r.superseded_by = "art-002"  # type: ignore[misc]


def test_relations_tuple_members_not_list() -> None:
    r = Relations(supersedes=("art-001",), related_to=("art-002", "art-003"))
    # tuple type guarantees immutability (no append possible)
    assert isinstance(r.supersedes, tuple)
    assert isinstance(r.related_to, tuple)


# ---------------------------------------------------------------------------
# Claim (source_refs is tuple, deterministic claim_id is caller's concern)
# ---------------------------------------------------------------------------


def test_claim_frozen_and_tuple_source_refs() -> None:
    kt = KnowledgeTime(valid_from="2023-04-01", valid_to=None)
    c = Claim(
        claim_id="art-001#c-abcdef12",
        subject="Customer A",
        attribute="runbook_exists",
        period=kt,
        predicate="Customer A has a runbook",
        source_refs=("src-001",),
    )
    assert isinstance(c.source_refs, tuple)
    with pytest.raises(FrozenInstanceError):
        c.predicate = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ReviewAuditEntry
# ---------------------------------------------------------------------------


def test_review_audit_entry_frozen_with_defaults() -> None:
    e = ReviewAuditEntry(
        resolver="mizumi",
        resolved_at="2026-04-08T12:00:00Z",
        status_before="disputed",
        status_after="current",
    )
    assert e.reason == ""
    assert e.superseded_by_id is None
    with pytest.raises(FrozenInstanceError):
        e.reason = "updated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Article (happy-path construction)
# ---------------------------------------------------------------------------


def _make_article(**overrides: object) -> Article:
    base: dict[str, object] = dict(
        schema_version=1,
        article_id="20260408163658-customer-a-ops",
        article_type="runbook",
        title="顧客A 運用フロー",
        captured_at="2026-04-08",
        knowledge_time=KnowledgeTime(valid_from="2023-04-01", valid_to=None),
        status="current",
        sources=(_make_source(),),
        relations=Relations(),
        claims=(),
        claim_refs=(),
        generated_by=GeneratedBy(
            tool="wiki-compile", version=1, generated_at="2026-04-08T09:12:00Z"
        ),
        extensions={},
        tags=(),
        body="## Summary\nbody text\n",
    )
    base.update(overrides)
    return Article(**base)  # type: ignore[arg-type]


def test_article_constructs_with_all_required_fields() -> None:
    art = _make_article()
    assert art.schema_version == 1
    assert art.article_id == "20260408163658-customer-a-ops"
    assert art.article_type == "runbook"
    assert art.status == "current"
    assert art.sources[0].id == "src-001"


def test_article_frozen() -> None:
    art = _make_article()
    with pytest.raises(FrozenInstanceError):
        art.title = "changed"  # type: ignore[misc]


def test_article_sources_and_claims_are_tuples() -> None:
    art = _make_article()
    assert isinstance(art.sources, tuple)
    assert isinstance(art.claims, tuple)
    assert isinstance(art.claim_refs, tuple)
    assert isinstance(art.tags, tuple)


def test_article_extensions_is_mutable_dict_but_review_namespace_convention() -> None:
    """extensions is typed as dict[str, object] by convention (read-only runtime rule)."""
    art = _make_article()
    assert isinstance(art.extensions, dict)
    # schema convention: extensions["review"]["audit"] is the audit canonical location.
    # The type is dict (not frozen) since frozen dataclass cannot hold a truly immutable mapping
    # in Python stdlib. This is enforced by runtime convention, not type system.


# ---------------------------------------------------------------------------
# Result type (Ok / Err)
# ---------------------------------------------------------------------------


def test_ok_holds_value_and_is_frozen() -> None:
    r = Ok(value=42)
    assert r.value == 42
    with pytest.raises(FrozenInstanceError):
        r.value = 7  # type: ignore[misc]


def test_err_holds_error_with_optional_detail() -> None:
    e = Err(error="not_found")
    assert e.error == "not_found"
    assert e.detail == ""
    e2 = Err(error="timeout", detail="after 30s")
    assert e2.detail == "after 30s"


def test_err_is_frozen() -> None:
    e = Err(error="boom")
    with pytest.raises(FrozenInstanceError):
        e.error = "changed"  # type: ignore[misc]


def test_is_ok_and_is_err_type_guards() -> None:
    """Narrow Ok/Err via helper predicates (isinstance-based)."""
    ok: Ok[int] | Err[str] = Ok(value=1)
    err: Ok[int] | Err[str] = Err(error="bad")
    assert is_ok(ok) is True
    assert is_err(ok) is False
    assert is_ok(err) is False
    assert is_err(err) is True


def test_ok_err_round_trip_with_match_statement() -> None:
    """Result can be pattern-matched by type (exhaustive style)."""

    def describe(result: Ok[int] | Err[str]) -> str:
        match result:
            case Ok(value=v):
                return f"ok:{v}"
            case Err(error=e):
                return f"err:{e}"
            case _:
                return "unknown"

    assert describe(Ok(value=10)) == "ok:10"
    assert describe(Err(error="boom")) == "err:boom"
