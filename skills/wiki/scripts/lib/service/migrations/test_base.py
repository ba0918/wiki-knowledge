"""Tests for :mod:`lib.service.migrations.base`.

These tests lock down the Migration Protocol surface and the MigrationError
discriminator enum. They do not exercise any concrete migration — that is
the job of ``test_v0_to_v1.py``.

Scope of coverage:

* MigrationError is a ``(str, Enum)`` discriminator and covers every failure
  case called out in ``.wiki/schema/migrations/v0-to-v1.md``.
* MigrationError values are stable snake_case strings (so CLI output and log
  aggregation stay grep-able across refactors).
* The Migration Protocol is ``runtime_checkable`` and accepts a duck-typed
  fake implementation in ``isinstance`` checks.
* The Protocol exposes ``up`` / ``down`` / ``validate`` that all return
  ``Ok`` or ``Err`` — never raise for expected input failures.
* ``from_version`` / ``to_version`` are integer-valued class attributes,
  matching the integer-based schema_version in ``page-template-v1.json``.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from lib.domain.types import (
    Article,
    Err,
    GeneratedBy,
    KnowledgeTime,
    Ok,
    Relations,
)
from lib.service.migrations.base import Migration, MigrationError


# ---------------------------------------------------------------------------
# MigrationError discriminator
# ---------------------------------------------------------------------------


def test_migration_error_is_str_enum() -> None:
    """(str, Enum) inheritance matches the repo-wide discriminator pattern."""
    assert issubclass(MigrationError, Enum)
    assert issubclass(MigrationError, str)


def test_migration_error_members_cover_spec() -> None:
    """Every failure case enumerated in v0-to-v1.md must exist."""
    expected = {
        "INVALID_ID",
        "MISSING_REQUIRED_FIELD",
        "INVALID_DATE",
        "INVALID_TYPE_CONST",
        "INVALID_RELATED",
        "SOURCE_READ_FAILURE",
        "EMPTY_SOURCE_REFS",
        "UNSUPPORTED_VERSION",
    }
    actual = {m.name for m in MigrationError}
    assert actual == expected


def test_migration_error_values_are_snake_case_of_name() -> None:
    """Stable CLI/log output: each enum's string value is its name lowercased.

    Changing these strings is a breaking change for log aggregators, so the
    test pins the convention explicitly.
    """
    for member in MigrationError:
        assert member.value == member.name.lower()


def test_migration_error_equals_string_form() -> None:
    """(str, Enum) instances compare equal to their underlying string value."""
    assert MigrationError.INVALID_ID == "invalid_id"
    assert "invalid_date" == MigrationError.INVALID_DATE
    assert MigrationError.EMPTY_SOURCE_REFS == "empty_source_refs"


def test_migration_error_is_hashable() -> None:
    """Enum values must work as dict keys (registry lookup tables)."""
    table = {MigrationError.INVALID_ID: "bad id"}
    assert table[MigrationError.INVALID_ID] == "bad id"


# ---------------------------------------------------------------------------
# Migration Protocol
# ---------------------------------------------------------------------------


def _make_stub_article() -> Article:
    """Tiny valid v1 article used as a return value in the fake migration.

    The test does not care about the contents, only that the Protocol's
    return type is respected.
    """
    return Article(
        schema_version=1,
        article_id="stub",
        article_type="concept",
        title="Stub",
        captured_at="2026-04-09",
        knowledge_time=KnowledgeTime(valid_from="2026-04-09", valid_to=None),
        status="current",
        sources=(),
        relations=Relations(),
        claims=(),
        claim_refs=(),
        generated_by=GeneratedBy(
            tool="test",
            version=1,
            generated_at="2026-04-09T00:00:00Z",
        ),
        extensions={},
        tags=(),
        body="",
    )


class _FakeMigration:
    """A minimal Migration-shaped object used to exercise the Protocol.

    This is intentionally *not* imported from the module under test — the
    goal is to verify that any duck-typed implementation satisfies the
    Protocol. If we imported a real migration here we would only be
    asserting that the module's own classes conform, which is a weaker
    guarantee.
    """

    from_version = 0
    to_version = 1

    def up(
        self, mapping: Mapping[str, object], body: str
    ) -> Ok[Article] | Err[MigrationError]:
        return Ok(_make_stub_article())

    def down(
        self, article: Article
    ) -> Ok[Mapping[str, object]] | Err[MigrationError]:
        return Ok({"title": article.title})

    def validate(
        self, article: Article
    ) -> Ok[Article] | Err[MigrationError]:
        return Ok(article)


def test_fake_migration_satisfies_protocol() -> None:
    """A duck-typed object with the three methods is accepted by isinstance."""
    m = _FakeMigration()
    assert isinstance(m, Migration)


def test_fake_migration_has_version_attributes() -> None:
    """Protocol-required attributes are surfaced on concrete migrations."""
    m = _FakeMigration()
    assert hasattr(m, "from_version")
    assert hasattr(m, "to_version")
    assert m.from_version == 0
    assert m.to_version == 1


def test_migration_up_returns_ok() -> None:
    m = _FakeMigration()
    result = m.up({}, "body")
    assert isinstance(result, Ok)
    assert result.value.article_id == "stub"


def test_migration_down_returns_ok() -> None:
    m = _FakeMigration()
    article = _make_stub_article()
    result = m.down(article)
    assert isinstance(result, Ok)
    assert result.value == {"title": "Stub"}


def test_migration_validate_returns_ok() -> None:
    m = _FakeMigration()
    article = _make_stub_article()
    result = m.validate(article)
    assert isinstance(result, Ok)
    assert result.value is article


def test_migration_up_can_return_err() -> None:
    """Input-driven failures must be surfaced via Err, never as exceptions."""

    class _BadMigration:
        from_version = 0
        to_version = 1

        def up(
            self, mapping: Mapping[str, object], body: str
        ) -> Ok[Article] | Err[MigrationError]:
            return Err(MigrationError.MISSING_REQUIRED_FIELD, "no title")

        def down(
            self, article: Article
        ) -> Ok[Mapping[str, object]] | Err[MigrationError]:
            return Err(MigrationError.MISSING_REQUIRED_FIELD)

        def validate(
            self, article: Article
        ) -> Ok[Article] | Err[MigrationError]:
            return Ok(article)

    m = _BadMigration()
    assert isinstance(m, Migration)
    result = m.up({"body": "x"}, "")
    assert isinstance(result, Err)
    assert result.error == MigrationError.MISSING_REQUIRED_FIELD
    assert result.detail == "no title"


def test_migration_all_error_members_are_constructible_in_err() -> None:
    """Every MigrationError variant must be usable as an Err discriminator.

    This is the mirror of the exhaustiveness test above: we not only verify
    the enum exposes the expected names, we also verify each name can flow
    through ``Err`` at runtime without surprises.
    """
    for member in MigrationError:
        err: Err[MigrationError] = Err(member, detail=member.value)
        assert err.error is member
        assert err.detail == member.value


def test_non_migration_object_fails_isinstance() -> None:
    """A plain object that lacks the Protocol methods must not satisfy it."""

    class _NotAMigration:
        pass

    assert not isinstance(_NotAMigration(), Migration)
