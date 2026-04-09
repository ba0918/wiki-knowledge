"""Tests for :mod:`lib.service.migrations.registry`.

The registry is a small lookup table keyed by ``(from_version, to_version)``
plus a decorator that populates it. These tests lock down:

* **Isolation**: Registry instances do not share state with each other or
  with :data:`DEFAULT_REGISTRY`.
* **Validation**: monotonicity, duplicate detection, and Protocol-method
  presence are enforced at registration time, never at lookup time.
* **Lookup contract**: hits return ``Ok[type]``, misses return
  ``Err(MigrationError.UNSUPPORTED_VERSION)``.
* **Decorator ergonomics**: ``@register_migration(0, 1)`` targets
  ``DEFAULT_REGISTRY`` by default but accepts a ``registry=`` kwarg for
  test isolation.
* **Insertion order**: :meth:`Registry.list_all` preserves order so that
  diagnostics in the CLI handler are deterministic.
"""

from __future__ import annotations

import pytest

from lib.domain.types import (
    Article,
    Err,
    GeneratedBy,
    KnowledgeTime,
    Ok,
    Relations,
)
from lib.service.migrations.base import MigrationError
from lib.service.migrations.registry import (
    DEFAULT_REGISTRY,
    Registry,
    RegistryError,
    register_migration,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _dummy_article() -> Article:
    return Article(
        schema_version=1,
        article_id="stub",
        article_type="concept",
        title="Stub",
        captured_at="2026-04-09",
        knowledge_time=KnowledgeTime(valid_from=None, valid_to=None),
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


def _make_migration_class(name: str = "TestMigration") -> type:
    """Create a minimal Migration-shaped class for registration tests.

    Does NOT set ``from_version`` / ``to_version`` as class attributes on
    purpose — the decorator / register() call is responsible for that.
    """

    class _M:
        def up(self, mapping, body):
            return Ok(_dummy_article())

        def down(self, article):
            return Ok({})

        def validate(self, article):
            return Ok(article)

    _M.__name__ = name
    _M.__qualname__ = name
    return _M


@pytest.fixture(autouse=True)
def _reset_default_registry():
    """Ensure ``DEFAULT_REGISTRY`` is empty before and after every test.

    This prevents cross-test pollution when tests exercise the decorator
    with its default target. We do NOT reset custom Registry instances —
    tests that need them construct their own.
    """
    DEFAULT_REGISTRY.clear()
    yield
    DEFAULT_REGISTRY.clear()


# ---------------------------------------------------------------------------
# Registry core behaviour
# ---------------------------------------------------------------------------


def test_registry_starts_empty() -> None:
    assert Registry().list_all() == []


def test_register_adds_class_and_sets_version_attrs() -> None:
    r = Registry()
    cls = _make_migration_class()
    result = r.register(0, 1, cls)
    assert isinstance(result, Ok)
    assert result.value is cls
    assert cls.from_version == 0
    assert cls.to_version == 1
    assert r.list_all() == [(0, 1)]


def test_register_rejects_duplicate_pair() -> None:
    r = Registry()
    r.register(0, 1, _make_migration_class("First"))
    with pytest.raises(RegistryError, match="already registered"):
        r.register(0, 1, _make_migration_class("Second"))


def test_register_rejects_monotonicity_violation() -> None:
    r = Registry()
    with pytest.raises(RegistryError, match="monotonicity"):
        r.register(2, 1, _make_migration_class())


def test_register_rejects_self_migration() -> None:
    r = Registry()
    with pytest.raises(RegistryError, match="monotonicity"):
        r.register(1, 1, _make_migration_class())


def test_register_rejects_class_missing_protocol_methods() -> None:
    r = Registry()

    class _Incomplete:
        def up(self, mapping, body):
            return Ok(_dummy_article())

        # deliberately missing .down and .validate

    with pytest.raises(RegistryError, match="missing required method"):
        r.register(0, 1, _Incomplete)


def test_register_returns_the_class_unchanged() -> None:
    """register() is idempotent on the class object itself (decorator pattern)."""
    r = Registry()
    cls = _make_migration_class()
    result = r.register(0, 1, cls)
    assert result.value is cls


# ---------------------------------------------------------------------------
# Lookup contract
# ---------------------------------------------------------------------------


def test_lookup_returns_ok_on_hit() -> None:
    r = Registry()
    cls = _make_migration_class()
    r.register(0, 1, cls)
    result = r.lookup(0, 1)
    assert isinstance(result, Ok)
    assert result.value is cls


def test_lookup_returns_err_unsupported_version_on_miss() -> None:
    result = Registry().lookup(0, 1)
    assert isinstance(result, Err)
    assert result.error == MigrationError.UNSUPPORTED_VERSION


def test_lookup_distinct_pairs_independently() -> None:
    r = Registry()
    cls_a = _make_migration_class("A")
    cls_b = _make_migration_class("B")
    r.register(0, 1, cls_a)
    r.register(1, 2, cls_b)
    assert r.lookup(0, 1).value is cls_a
    assert r.lookup(1, 2).value is cls_b
    # Neither entry "implies" any other pair
    assert isinstance(r.lookup(0, 2), Err)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_registries_are_independent() -> None:
    r1 = Registry()
    r2 = Registry()
    r1.register(0, 1, _make_migration_class())
    assert r1.list_all() == [(0, 1)]
    assert r2.list_all() == []


def test_clear_resets_state() -> None:
    r = Registry()
    r.register(0, 1, _make_migration_class("A"))
    r.register(1, 2, _make_migration_class("B"))
    assert len(r.list_all()) == 2
    r.clear()
    assert r.list_all() == []


def test_list_all_preserves_insertion_order() -> None:
    r = Registry()
    r.register(2, 3, _make_migration_class("A"))
    r.register(0, 1, _make_migration_class("B"))
    r.register(1, 2, _make_migration_class("C"))
    assert r.list_all() == [(2, 3), (0, 1), (1, 2)]


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def test_register_migration_decorator_targets_default_registry() -> None:
    @register_migration(0, 1)
    class _Decorated:
        def up(self, mapping, body):
            return Ok(_dummy_article())

        def down(self, article):
            return Ok({})

        def validate(self, article):
            return Ok(article)

    result = DEFAULT_REGISTRY.lookup(0, 1)
    assert isinstance(result, Ok)
    assert result.value is _Decorated
    assert _Decorated.from_version == 0
    assert _Decorated.to_version == 1


def test_register_migration_decorator_with_custom_registry() -> None:
    r = Registry()

    @register_migration(0, 1, registry=r)
    class _Decorated:
        def up(self, mapping, body):
            return Ok(_dummy_article())

        def down(self, article):
            return Ok({})

        def validate(self, article):
            return Ok(article)

    assert r.lookup(0, 1).value is _Decorated
    # DEFAULT_REGISTRY must not have been touched
    assert DEFAULT_REGISTRY.list_all() == []


def test_register_migration_decorator_returns_the_class() -> None:
    """The decorator must not wrap or replace the class (test isolation)."""
    r = Registry()

    class _Plain:
        def up(self, mapping, body):
            return Ok(_dummy_article())

        def down(self, article):
            return Ok({})

        def validate(self, article):
            return Ok(article)

    decorated = register_migration(0, 1, registry=r)(_Plain)
    assert decorated is _Plain


def test_register_migration_decorator_propagates_monotonicity_error() -> None:
    r = Registry()
    with pytest.raises(RegistryError, match="monotonicity"):

        @register_migration(2, 1, registry=r)
        class _BadVersion:
            def up(self, mapping, body):
                return Ok(_dummy_article())

            def down(self, article):
                return Ok({})

            def validate(self, article):
                return Ok(article)
