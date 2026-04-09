"""Migration Protocol and error discriminator.

Every concrete migration (``v0_to_v1.py``, future ``v1_to_v2.py``, ...)
conforms to the :class:`Migration` Protocol declared here. The Protocol is
deliberately minimal — three methods, two class-level version attributes —
and knows nothing about timestamps, filesystems, or logging.

Design rationale
----------------

**Why a Protocol, not an ABC?** The migration subsystem prizes duck typing
so that concrete migrations can live next to their per-version field mapping
(e.g. the ``V0ToV1Migration`` class is colocated with the v0→v1 spec). An
ABC would force every implementation to inherit from a single base, which
couples the class hierarchy to the service layer unnecessarily. The
``@runtime_checkable`` Protocol gives us the ergonomics of structural typing
while still supporting ``isinstance`` checks in tests and in the registry.

**Why ``int`` for ``from_version`` / ``to_version``?** The v1 JSON schema
(`.wiki/schema/page-template-v1.json`) declares ``schema_version`` as an
integer. Keeping the migration identifiers in the same type avoids
string/int mismatches at the registry lookup boundary. v0 (the legacy
``type: "wiki"`` frontmatter) is represented by the integer ``0``, even
though v0 articles do not carry a ``schema_version`` field — the ``0`` is a
migration-subsystem convention, not a frontmatter value.

**Why no ``file_reader`` or ``clock`` in the Protocol?** Dependency
injection is concrete-migration-specific. A hypothetical migration that
renames a tag needs neither a file reader nor a clock; forcing every
migration to carry those parameters would leak v0→v1 concerns into the
Protocol. Instead, concrete migrations accept their dependencies through
``__init__`` (see :class:`V0ToV1Migration`), and the Protocol only pins the
pure in/out shape.

**Why the ``up`` / ``down`` / ``validate`` triple?**

* ``up`` performs the forward migration from a legacy mapping (the output
  of a frontmatter parser) + raw body into a v1 :class:`Article`. This is
  the hot path.
* ``down`` is the inverse, used only by :mod:`migrate.py` for round-trip
  verification before committing a destructive change. It is **not** a
  general-purpose downgrade feature — if ``extensions.legacy`` is missing
  because the article was originally authored under v1, ``down`` returns
  ``Err(MISSING_REQUIRED_FIELD)``.
* ``validate`` runs integrity checks on an already-materialised Article —
  typically "do the invariants that I promised in ``up`` still hold?". It
  is separated from ``up`` so that re-running a migration over existing v1
  articles can be a read-only no-op.

All three methods return ``Result`` types; raising exceptions is reserved
for programmer errors (e.g. a Python exception escaping an injected
``file_reader``). The caller — usually ``migrate.py`` — decides whether to
convert an ``Err`` into a process exit code.

Error taxonomy
--------------

The :class:`MigrationError` enum enumerates every *expected* failure mode
across the migration pipeline. See
``.wiki/schema/migrations/v0-to-v1.md`` §MigrationError enum for the full
mapping between v0 fields and each discriminator. New discriminators are
added by appending members (never by repurposing existing ones) so that
log aggregators and CLI dashboards keep working across releases.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping, Protocol, runtime_checkable

from lib.domain.types import Article, Err, Ok


class MigrationError(str, Enum):
    """Discriminator enum for expected migration failures.

    Inherits from ``str`` so that instances compare equal to their string
    value (``MigrationError.INVALID_ID == "invalid_id"``), which keeps CLI
    output and log-parsing pipelines readable without any extra encoding.

    Member values are the lowercase form of the member names. This is an
    intentional redundancy: it pins the string representation in the
    Python source so a refactor that renames a member cannot silently
    change the string that downstream tooling greps for.

    Stability
    ---------
    Each member of this enum is part of the **public** CLI/log contract.
    Adding members is non-breaking; renaming or removing them is a breaking
    change and must go through a schema-version bump in lockstep with
    downstream tooling.
    """

    # Identity / shape errors
    INVALID_ID = "invalid_id"
    """The derived article_id fails :func:`path_validator.sanitize_id`."""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    """A required v0 field (title / source_refs / created / category / tags)
    is absent from the input mapping, or a required ``extensions.legacy.*``
    field is missing during a ``down`` call."""

    INVALID_DATE = "invalid_date"
    """A date field is not ``YYYY-MM-DD``."""

    INVALID_TYPE_CONST = "invalid_type_const"
    """The v0 ``type`` field is not the expected ``"wiki"`` constant."""

    INVALID_RELATED = "invalid_related"
    """An entry in ``related[]`` cannot be resolved to a valid article_id
    after stem extraction and sanitisation."""

    # Source / content errors
    SOURCE_READ_FAILURE = "source_read_failure"
    """Reserved for ``--strict`` mode: a source file was requested but the
    file reader returned ``None``. Lossy mode tolerates missing sources by
    writing a zero hash (see the spec); strict mode elevates to this
    discriminator."""

    EMPTY_SOURCE_REFS = "empty_source_refs"
    """``source_refs`` exists but contains zero entries. v0 schema requires
    ``minItems: 1``, so an empty list is a user error, not a silent default."""

    # Registry / routing errors
    UNSUPPORTED_VERSION = "unsupported_version"
    """The migration was asked to handle a schema version pair it does not
    claim. Raised by the registry when lookup fails, not by individual
    migrations."""


@runtime_checkable
class Migration(Protocol):
    """Structural contract implemented by every concrete migration.

    Subclasses / duck-typed classes must expose the two class-level version
    attributes and the three methods below. ``isinstance(x, Migration)``
    only checks the *methods* (standard Protocol behaviour) — class
    attribute presence is verified at registration time by
    :func:`registry.register_migration`, where the error surface is better.

    Concrete migrations are typically instantiated with their dependencies
    injected through ``__init__`` (e.g. a file reader and a clock). The
    Protocol itself stays free of those concerns so that a hypothetical
    migration with different dependencies can still conform without
    leaking its needs into unrelated code paths.
    """

    from_version: int
    """Integer schema version this migration reads from. v0 (legacy
    ``type: "wiki"`` frontmatter without a ``schema_version`` field) is
    represented by ``0``."""

    to_version: int
    """Integer schema version this migration writes. Must be greater than
    ``from_version``; the registry enforces monotonicity."""

    def up(
        self, mapping: Mapping[str, object], body: str
    ) -> Ok[Article] | Err[MigrationError]:
        """Forward migration: ``from_version`` mapping + body → ``to_version`` Article.

        ``mapping`` is the dictionary produced by a frontmatter parser (the
        raw key/value pairs from the YAML header). ``body`` is the markdown
        content that follows the frontmatter, preserved verbatim.

        Input-driven failures (missing fields, invalid dates, unresolvable
        related entries) return ``Err(MigrationError, detail=...)``.
        Programmer errors (TypeError, KeyError on internal state, injected
        callable raising) are allowed to propagate so that tests surface
        them immediately.
        """
        ...  # pragma: no cover - protocol

    def down(
        self, article: Article
    ) -> Ok[Mapping[str, object]] | Err[MigrationError]:
        """Reverse migration used exclusively for round-trip verification
        during ``migrate.py --rollback`` and for protocol tests.

        Returns the v0-equivalent mapping. Bodies are **not** returned —
        callers reconstruct them from the Article.body field, which is
        preserved losslessly through ``up``.

        If the Article was never produced by ``up`` (e.g. it lacks the
        ``extensions.legacy.*`` stash), returns
        ``Err(MigrationError.MISSING_REQUIRED_FIELD)``.
        """
        ...  # pragma: no cover - protocol

    def validate(
        self, article: Article
    ) -> Ok[Article] | Err[MigrationError]:
        """Idempotent integrity check on an already-materialised Article.

        Returns ``Ok(article)`` if every invariant this migration promised
        in ``up`` still holds — re-running ``validate`` across multiple
        invocations is explicitly allowed and must be side-effect free.
        """
        ...  # pragma: no cover - protocol
