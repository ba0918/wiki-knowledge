"""Migration registry — central lookup for concrete migrations.

The registry is a lookup table keyed by a ``(from_version, to_version)``
tuple. Concrete migrations declare their version pair via
:func:`register_migration`, which attaches the class to a :class:`Registry`
instance and also writes the version pair as class attributes so that
downstream code can introspect a migration without going through the
registry.

Design rationale
----------------

**Why instance-based, not module-level globals?** Each :class:`Registry`
instance holds its own entries dict. Module-level globals are convenient
but murder testability — a test that registers ``(0, 1)`` would leak into
every subsequent test. Instead we expose one explicit singleton,
:data:`DEFAULT_REGISTRY`, and arrange for every test to either isolate
into a fresh :class:`Registry` or use the autouse fixture in
``test_registry.py`` that clears the default before/after each test.

**Why register *classes*, not instances?** Concrete migrations (v0→v1 in
particular) require constructor-injected dependencies — a file reader and
a clock. The registry has no business knowing or manufacturing those. It
stores the class, and the CLI handler (``migrate.py``, Phase 0.11)
instantiates it with the right DI graph at call time. This keeps the
registry framework-agnostic and lets tests instantiate migrations with
fakes without touching the registry at all.

**Why does the decorator mutate ``cls.from_version`` / ``cls.to_version``?**
Convenience. After registration, any consumer that has a reference to the
class (e.g. a registry lookup result, or an import of the concrete class)
can immediately read the version pair off the class. The alternative —
requiring consumers to carry the tuple around alongside the class — is
needlessly awkward.

**Why ``RegistryError`` is an Exception, not a ``MigrationError``?**
Duplicate registrations and monotonicity violations are *programmer*
errors: the registering code has a bug that needs fixing before a test
run can even start. Turning these into ``Result.Err`` would force every
caller to handle an "impossible" error; raising makes the bug loud.
Input-driven failures during actual migration stay on the ``Result``
channel via :class:`MigrationError`.
"""

from __future__ import annotations

from typing import Callable

from lib.domain.types import Err, Ok
from lib.service.migrations.base import MigrationError


class RegistryError(Exception):
    """Programmer-error raised by registry validation.

    Reserved for conditions that are bugs in the code performing the
    registration (duplicate pair, monotonicity violation, missing
    Protocol methods). Actual migration failures are reported via
    :class:`MigrationError` through the ``Result`` channel.
    """


# ---------------------------------------------------------------------------
# Registry class
# ---------------------------------------------------------------------------


class Registry:
    """Lookup table from ``(from_version, to_version)`` to migration class.

    Instances are isolated: construct a fresh :class:`Registry` for
    per-test state. The module-level :data:`DEFAULT_REGISTRY` is the
    target of the :func:`register_migration` decorator when no explicit
    registry is supplied.
    """

    def __init__(self) -> None:
        # Ordered dict preserves insertion order (Python 3.7+), which
        # matters for deterministic diagnostics in ``list_all``.
        self._entries: dict[tuple[int, int], type] = {}

    # -- mutation ---------------------------------------------------------

    def register(
        self,
        from_version: int,
        to_version: int,
        migration_cls: type,
    ) -> Ok[type]:
        """Register a migration class for the given version pair.

        Validates that:

        * ``to_version > from_version`` (strict monotonicity — same-version
          and reverse migrations are forbidden at the registry level).
        * The pair is not already registered.
        * ``migration_cls`` exposes the three Protocol methods (``up``,
          ``down``, ``validate``). A structural check is used rather than
          ``isinstance(cls(), Migration)`` because instantiating the
          class here would require knowing its DI graph.

        On success, writes ``from_version`` / ``to_version`` as class
        attributes on ``migration_cls`` so downstream code can introspect
        the pair from a class reference.

        Raises :class:`RegistryError` on any validation failure.
        """
        if to_version <= from_version:
            raise RegistryError(
                f"monotonicity violation: {from_version} -> {to_version} "
                f"(to_version must be strictly greater than from_version)"
            )

        key = (from_version, to_version)
        if key in self._entries:
            existing = self._entries[key].__name__
            raise RegistryError(
                f"migration already registered for {from_version} -> {to_version}: "
                f"existing={existing!r}, new={migration_cls.__name__!r}"
            )

        for method in ("up", "down", "validate"):
            if not callable(getattr(migration_cls, method, None)):
                raise RegistryError(
                    f"class {migration_cls.__name__!r} is missing required method "
                    f"{method!r} — every migration must implement the full "
                    "Migration Protocol (up / down / validate)"
                )

        # Attach version attributes to the class so downstream lookups
        # can introspect without a registry round-trip.
        migration_cls.from_version = from_version  # type: ignore[attr-defined]
        migration_cls.to_version = to_version  # type: ignore[attr-defined]

        self._entries[key] = migration_cls
        return Ok(migration_cls)

    def clear(self) -> None:
        """Remove all entries. Intended for test isolation."""
        self._entries.clear()

    # -- lookups ----------------------------------------------------------

    def lookup(
        self, from_version: int, to_version: int
    ) -> Ok[type] | Err[MigrationError]:
        """Return the migration class for the pair, or an Err on miss.

        A miss is reported via :class:`MigrationError.UNSUPPORTED_VERSION`
        so that callers can distinguish "the registry is correctly wired
        but does not know how to move between these versions" from a
        programmer error.
        """
        key = (from_version, to_version)
        if key not in self._entries:
            return Err(
                MigrationError.UNSUPPORTED_VERSION,
                detail=f"no migration registered for {from_version} -> {to_version}",
            )
        return Ok(self._entries[key])

    def list_all(self) -> list[tuple[int, int]]:
        """Return registered version pairs in insertion order."""
        return list(self._entries.keys())


# ---------------------------------------------------------------------------
# Module-level default registry + decorator
# ---------------------------------------------------------------------------


DEFAULT_REGISTRY = Registry()
"""Module-level singleton used by :func:`register_migration` when no
explicit registry is passed. Concrete migrations elsewhere in this
package auto-register into this instance at import time so that the CLI
handler can look them up without any explicit wiring.

Tests should construct their own :class:`Registry` instance for isolation,
or use the autouse fixture in ``test_registry.py`` to reset this
singleton around each test.
"""


def register_migration(
    from_version: int,
    to_version: int,
    *,
    registry: Registry = DEFAULT_REGISTRY,
) -> Callable[[type], type]:
    """Class decorator that registers a migration into a :class:`Registry`.

    Usage::

        @register_migration(0, 1)
        class V0ToV1Migration:
            def up(self, mapping, body): ...
            def down(self, article): ...
            def validate(self, article): ...

    By default the class is registered into :data:`DEFAULT_REGISTRY`.
    Tests and advanced callers can supply a custom registry via the
    keyword-only ``registry`` parameter to keep state isolated.

    The decorator returns the class **unchanged** (aside from the
    version attributes attached by :meth:`Registry.register`), so
    stacking with other decorators is safe.
    """

    def _decorator(cls: type) -> type:
        registry.register(from_version, to_version, cls)
        return cls

    return _decorator
