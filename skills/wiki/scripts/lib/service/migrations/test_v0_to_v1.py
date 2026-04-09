"""Tests for :mod:`lib.service.migrations.v0_to_v1`.

These tests are the authoritative verification of the field mapping
specification at ``.wiki/schema/migrations/v0-to-v1.md``. Any behaviour
that contradicts the spec is a bug. The test names mirror the spec
section headers where possible so traceability is bidirectional.

Test philosophy: most tests exercise the ``up()`` path because that is
the hot path in production (the only one ``migrate.py --apply`` runs).
``down()`` tests cover the lossless round-trip invariant.  ``validate()``
tests cover the idempotence guarantee.
"""

from __future__ import annotations

from typing import Callable, Optional

import pytest

from lib.domain.types import (
    Article,
    Err,
    KnowledgeTime,
    Ok,
    Relations,
    Source,
    is_err,
    is_ok,
)
from lib.service.clock import FixedClock
from lib.service.migrations.base import MigrationError
from lib.service.migrations.v0_to_v1 import V0ToV1Migration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


FileReader = Callable[[str], Optional[bytes]]

_CLOCK = FixedClock(now="2026-04-09T00:00:00Z")
_SAMPLE_CONTENT = b"---\ntitle: Sample\n---\nsome body\n"


def _reader(
    mapping: dict[str, bytes] | None = None,
) -> FileReader:
    """Build a deterministic file reader that resolves known refs."""
    m = mapping or {}
    return lambda ref: m.get(ref)


def _all_found_reader() -> FileReader:
    """Reader that returns fixed bytes for any ref (nothing is missing)."""
    return lambda ref: _SAMPLE_CONTENT


def _all_missing_reader() -> FileReader:
    """Reader that always returns None (every source file is gone)."""
    return lambda _: None


def _make_v0(
    *,
    article_id: str = "querylog",
    title: str = "QueryLog",
    type_: str = "wiki",
    source_refs: list[str] | None = None,
    created: str = "2026-04-06",
    updated: str = "2026-04-06",
    category: str = "concepts",
    tags: list[str] | None = None,
    related: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Construct a v0-shaped mapping for test purposes."""
    m: dict[str, object] = {
        "_article_id": article_id,
        "title": title,
        "type": type_,
        "source_refs": source_refs if source_refs is not None else ["raw/articles/20260406-querylog-feature.md"],
        "created": created,
        "updated": updated,
        "category": category,
        "tags": tags if tags is not None else ["querylog", "jsonl"],
        "related": related if related is not None else [],
    }
    if extra:
        m.update(extra)
    return m


def _migrate(
    mapping: dict[str, object] | None = None,
    body: str = "# QueryLog\n\nbody text\n",
    file_reader: FileReader | None = None,
    clock: FixedClock | None = None,
) -> Ok[Article] | Err[MigrationError]:
    """Shortcut for the most common invocation."""
    clock = clock or _CLOCK
    reader = file_reader or _all_found_reader()
    m = V0ToV1Migration(file_reader=reader, clock=clock)
    return m.up(mapping or _make_v0(), body)


# ---------------------------------------------------------------------------
# Spec §article_type derivation
# ---------------------------------------------------------------------------


def test_concepts_category_maps_to_concept() -> None:
    r = _migrate(mapping=_make_v0(category="concepts"))
    assert is_ok(r)
    assert r.value.article_type == "concept"


def test_tools_category_maps_to_reference() -> None:
    r = _migrate(mapping=_make_v0(category="tools"))
    assert is_ok(r)
    assert r.value.article_type == "reference"


def test_practices_category_maps_to_runbook() -> None:
    r = _migrate(mapping=_make_v0(category="practices"))
    assert is_ok(r)
    assert r.value.article_type == "runbook"


def test_references_category_maps_to_reference() -> None:
    r = _migrate(mapping=_make_v0(category="references"))
    assert is_ok(r)
    assert r.value.article_type == "reference"


def test_unknown_category_falls_back_to_concept_with_tag() -> None:
    r = _migrate(mapping=_make_v0(category="exotic"))
    assert is_ok(r)
    assert r.value.article_type == "concept"
    assert "legacy-unmapped-category" in r.value.tags


# ---------------------------------------------------------------------------
# Spec §Field mapping table — core fields
# ---------------------------------------------------------------------------


def test_article_id_from_mapping() -> None:
    r = _migrate(mapping=_make_v0(article_id="querylog"))
    assert is_ok(r)
    assert r.value.article_id == "querylog"


def test_title_preserved_verbatim() -> None:
    r = _migrate(mapping=_make_v0(title="QueryLog — メタデータ"))
    assert is_ok(r)
    assert r.value.title == "QueryLog — メタデータ"


def test_schema_version_is_1() -> None:
    r = _migrate()
    assert is_ok(r)
    assert r.value.schema_version == 1


def test_captured_at_from_created() -> None:
    r = _migrate(mapping=_make_v0(created="2026-04-06"))
    assert is_ok(r)
    assert r.value.captured_at == "2026-04-06"


def test_knowledge_time_from_created() -> None:
    r = _migrate(mapping=_make_v0(created="2026-04-06"))
    assert is_ok(r)
    kt = r.value.knowledge_time
    assert kt.valid_from == "2026-04-06"
    assert kt.valid_to is None


def test_claims_and_claim_refs_are_empty() -> None:
    r = _migrate()
    assert is_ok(r)
    assert r.value.claims == ()
    assert r.value.claim_refs == ()


def test_body_preserved_verbatim() -> None:
    body = "# Title\n\nParagraph with [[wikilink]].\n"
    r = _migrate(body=body)
    assert is_ok(r)
    assert r.value.body == body


# ---------------------------------------------------------------------------
# Spec §status initial value
# ---------------------------------------------------------------------------


def test_status_is_current_when_sources_found() -> None:
    r = _migrate(file_reader=_all_found_reader())
    assert is_ok(r)
    assert r.value.status == "current"


def test_status_is_unverified_when_all_sources_missing() -> None:
    r = _migrate(file_reader=_all_missing_reader())
    assert is_ok(r)
    assert r.value.status == "unverified"


def test_status_is_current_when_some_sources_found() -> None:
    """Partial source availability does NOT downgrade to unverified."""
    refs = [
        "raw/articles/20260405-a.md",
        "raw/articles/20260405-b.md",
    ]
    reader = _reader({"raw/articles/20260405-a.md": b"found"})
    r = _migrate(
        mapping=_make_v0(source_refs=refs),
        file_reader=reader,
    )
    assert is_ok(r)
    assert r.value.status == "current"


# ---------------------------------------------------------------------------
# Spec §sources[] construction
# ---------------------------------------------------------------------------


def test_sources_basic_structure() -> None:
    refs = ["raw/articles/20260406-querylog-feature.md"]
    content = b"file content here"
    reader = _reader({"raw/articles/20260406-querylog-feature.md": content})
    r = _migrate(mapping=_make_v0(source_refs=refs), file_reader=reader)
    assert is_ok(r)
    s = r.value.sources[0]
    assert s.id == "legacy-1"
    assert s.type == "file"
    assert s.ref == "raw/articles/20260406-querylog-feature.md"
    assert s.source_version == 1
    assert s.content_hash.startswith("sha256:")
    assert len(s.content_hash) == 71  # "sha256:" + 64 hex chars
    assert s.fetched_at == "2026-04-09T00:00:00Z"
    assert s.permalink is None


def test_sources_missing_file_gets_zero_hash() -> None:
    r = _migrate(file_reader=_all_missing_reader())
    assert is_ok(r)
    s = r.value.sources[0]
    assert s.content_hash == "sha256:" + "0" * 64


def test_sources_multiple_refs_get_sequential_ids() -> None:
    refs = ["raw/articles/a.md", "raw/articles/b.md"]
    r = _migrate(
        mapping=_make_v0(source_refs=refs),
        file_reader=_all_found_reader(),
    )
    assert is_ok(r)
    assert r.value.sources[0].id == "legacy-1"
    assert r.value.sources[1].id == "legacy-2"


def test_sources_ref_preserved_verbatim() -> None:
    """ref is NOT normalized — the v0 convention is wiki_root-relative and
    v1 keeps the same convention."""
    ref = "raw/articles/20260406-querylog-feature.md"
    r = _migrate(mapping=_make_v0(source_refs=[ref]))
    assert is_ok(r)
    assert r.value.sources[0].ref == ref


# ---------------------------------------------------------------------------
# Spec §tags
# ---------------------------------------------------------------------------


def test_tags_appends_legacy_v0_marker() -> None:
    r = _migrate(mapping=_make_v0(tags=["querylog", "jsonl"]))
    assert is_ok(r)
    assert r.value.tags[-1] == "legacy-v0"
    assert "querylog" in r.value.tags
    assert "jsonl" in r.value.tags


def test_tags_dedup_preserves_order() -> None:
    r = _migrate(mapping=_make_v0(tags=["a", "b", "a", "c"]))
    assert is_ok(r)
    # a, b, c (deduped, first-seen wins), then legacy-v0
    assert list(r.value.tags) == ["a", "b", "c", "legacy-v0"]


def test_tags_does_not_duplicate_legacy_v0() -> None:
    """If v0 already has 'legacy-v0' for some reason, don't double it."""
    r = _migrate(mapping=_make_v0(tags=["a", "legacy-v0"]))
    assert is_ok(r)
    assert list(r.value.tags).count("legacy-v0") == 1


# ---------------------------------------------------------------------------
# Spec §relations conversion
# ---------------------------------------------------------------------------


def test_related_paths_converted_to_article_ids() -> None:
    related = [
        "concepts/wiki-knowledge-architecture.md",
        "concepts/trust-score.md",
    ]
    r = _migrate(mapping=_make_v0(related=related))
    assert is_ok(r)
    assert r.value.relations.related_to == (
        "wiki-knowledge-architecture",
        "trust-score",
    )


def test_related_empty_yields_empty_relations() -> None:
    r = _migrate(mapping=_make_v0(related=[]))
    assert is_ok(r)
    assert r.value.relations.related_to == ()


def test_related_key_missing_yields_empty_relations() -> None:
    m = _make_v0()
    del m["related"]
    r = _migrate(mapping=m)
    assert is_ok(r)
    assert r.value.relations.related_to == ()


def test_other_relations_are_empty_by_default() -> None:
    r = _migrate()
    assert is_ok(r)
    rel = r.value.relations
    assert rel.supersedes == ()
    assert rel.superseded_by is None
    assert rel.caused_by == ()
    assert rel.derived_from == ()
    assert rel.implements == ()
    assert rel.depends_on == ()


# ---------------------------------------------------------------------------
# Spec §extensions.legacy stash
# ---------------------------------------------------------------------------


def test_extensions_legacy_type_stashed() -> None:
    r = _migrate()
    assert is_ok(r)
    assert r.value.extensions["legacy"]["type"] == "wiki"


def test_extensions_legacy_category_stashed() -> None:
    r = _migrate(mapping=_make_v0(category="tools"))
    assert is_ok(r)
    assert r.value.extensions["legacy"]["category"] == "tools"


def test_extensions_legacy_updated_stashed() -> None:
    r = _migrate(mapping=_make_v0(updated="2026-04-06"))
    assert is_ok(r)
    assert r.value.extensions["legacy"]["updated"] == "2026-04-06"


def test_extensions_legacy_unknown_fields_stashed() -> None:
    r = _migrate(mapping=_make_v0(extra={"custom_field": "hello"}))
    assert is_ok(r)
    assert r.value.extensions["legacy"]["_unknown"] == {"custom_field": "hello"}


def test_extensions_no_unknown_when_all_fields_known() -> None:
    r = _migrate()
    assert is_ok(r)
    assert "_unknown" not in r.value.extensions.get("legacy", {})


# ---------------------------------------------------------------------------
# Spec §generated_by
# ---------------------------------------------------------------------------


def test_generated_by_fields() -> None:
    r = _migrate()
    assert is_ok(r)
    gb = r.value.generated_by
    assert gb.tool == "wiki-migrate"
    assert gb.version == 1
    assert gb.generated_at == "2026-04-09T00:00:00Z"


# ---------------------------------------------------------------------------
# Spec §MigrationError enum
# ---------------------------------------------------------------------------


def test_err_missing_title() -> None:
    m = _make_v0()
    del m["title"]
    r = _migrate(mapping=m)
    assert is_err(r)
    assert r.error == MigrationError.MISSING_REQUIRED_FIELD


def test_err_missing_source_refs() -> None:
    m = _make_v0()
    del m["source_refs"]
    r = _migrate(mapping=m)
    assert is_err(r)
    assert r.error == MigrationError.MISSING_REQUIRED_FIELD


def test_err_empty_source_refs() -> None:
    r = _migrate(mapping=_make_v0(source_refs=[]))
    assert is_err(r)
    assert r.error == MigrationError.EMPTY_SOURCE_REFS


def test_err_missing_created() -> None:
    m = _make_v0()
    del m["created"]
    r = _migrate(mapping=m)
    assert is_err(r)
    assert r.error == MigrationError.MISSING_REQUIRED_FIELD


def test_err_missing_category() -> None:
    m = _make_v0()
    del m["category"]
    r = _migrate(mapping=m)
    assert is_err(r)
    assert r.error == MigrationError.MISSING_REQUIRED_FIELD


def test_err_invalid_type_const() -> None:
    r = _migrate(mapping=_make_v0(type_="blog"))
    assert is_err(r)
    assert r.error == MigrationError.INVALID_TYPE_CONST


def test_err_invalid_date_format() -> None:
    r = _migrate(mapping=_make_v0(created="04-06-2026"))
    assert is_err(r)
    assert r.error == MigrationError.INVALID_DATE


def test_err_invalid_updated_date_format() -> None:
    r = _migrate(mapping=_make_v0(updated="not-a-date"))
    assert is_err(r)
    assert r.error == MigrationError.INVALID_DATE


def test_err_invalid_article_id() -> None:
    r = _migrate(mapping=_make_v0(article_id="UPPER_CASE"))
    assert is_err(r)
    assert r.error == MigrationError.INVALID_ID


def test_err_invalid_related_slug() -> None:
    """A related path whose stem is not a valid article_id returns Err."""
    r = _migrate(mapping=_make_v0(related=["concepts/UPPER.md"]))
    assert is_err(r)
    assert r.error == MigrationError.INVALID_RELATED


def test_err_missing_article_id() -> None:
    m = _make_v0()
    del m["_article_id"]
    r = _migrate(mapping=m)
    assert is_err(r)
    assert r.error == MigrationError.MISSING_REQUIRED_FIELD


# ---------------------------------------------------------------------------
# Spec §Determinism and re-runnability
# ---------------------------------------------------------------------------


def test_migration_is_deterministic() -> None:
    mapping = _make_v0()
    body = "# body\n"
    reader = _all_found_reader()
    clock = FixedClock(now="2026-04-09T00:00:00Z")
    m = V0ToV1Migration(file_reader=reader, clock=clock)
    a = m.up(mapping, body)
    b = m.up(mapping, body)
    assert a == b


# ---------------------------------------------------------------------------
# Spec §Down migration (v1 → v0)
# ---------------------------------------------------------------------------


def test_down_round_trip() -> None:
    """up → down recovers the original v0 mapping (excluding _article_id)."""
    original = _make_v0(
        related=[
            "concepts/wiki-knowledge-architecture.md",
            "concepts/trust-score.md",
        ],
        tags=["querylog", "jsonl"],
    )
    body = "# body text\n"
    m = V0ToV1Migration(file_reader=_all_found_reader(), clock=_CLOCK)
    up_result = m.up(original, body)
    assert is_ok(up_result)
    down_result = m.down(up_result.value)
    assert is_ok(down_result)
    recovered = down_result.value
    assert recovered["title"] == original["title"]
    assert recovered["type"] == "wiki"
    assert recovered["source_refs"] == original["source_refs"]
    assert recovered["created"] == original["created"]
    assert recovered["updated"] == original["updated"]
    assert recovered["category"] == original["category"]
    # down removes legacy-v0 from tags
    assert recovered["tags"] == list(original["tags"])
    # down reconstructs paths from article_ids
    assert recovered["related"] == original["related"]


def test_down_rejects_non_migrated_article() -> None:
    """An article with no extensions.legacy cannot be down-migrated."""
    m = V0ToV1Migration(file_reader=_all_found_reader(), clock=_CLOCK)
    up_result = m.up(_make_v0(), "body")
    assert is_ok(up_result)
    # Strip legacy stash
    art = up_result.value
    no_legacy = Article(
        schema_version=art.schema_version,
        article_id=art.article_id,
        article_type=art.article_type,
        title=art.title,
        captured_at=art.captured_at,
        knowledge_time=art.knowledge_time,
        status=art.status,
        sources=art.sources,
        relations=art.relations,
        claims=art.claims,
        claim_refs=art.claim_refs,
        generated_by=art.generated_by,
        extensions={},
        tags=art.tags,
        body=art.body,
    )
    down_result = m.down(no_legacy)
    assert is_err(down_result)
    assert down_result.error == MigrationError.MISSING_REQUIRED_FIELD


# ---------------------------------------------------------------------------
# Spec §validate
# ---------------------------------------------------------------------------


def test_validate_ok_for_migrated_article() -> None:
    m = V0ToV1Migration(file_reader=_all_found_reader(), clock=_CLOCK)
    up_result = m.up(_make_v0(), "body")
    assert is_ok(up_result)
    val = m.validate(up_result.value)
    assert is_ok(val)
    assert val.value is up_result.value


def test_validate_idempotent() -> None:
    m = V0ToV1Migration(file_reader=_all_found_reader(), clock=_CLOCK)
    up_result = m.up(_make_v0(), "body")
    assert is_ok(up_result)
    val1 = m.validate(up_result.value)
    val2 = m.validate(up_result.value)
    assert val1 == val2
