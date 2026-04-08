"""Domain types for the Source-Agnostic Knowledge Pipeline (Phase 0).

All types defined here are:

* **frozen dataclasses** — immutable by default, eq=True by value
* **pure** — no I/O, no side effects, no timestamps generated here
* **stdlib-only** — zero third-party imports to keep the domain layer thin

Provenance timestamps (``GeneratedBy.generated_at``, ``Source.fetched_at``,
``ReviewAuditEntry.resolved_at``) are plain strings in ISO8601 UTC form. The
values are **produced in the service layer** (``lib/service/provenance.py``,
``lib/service/clock.py``) so that the domain module remains time-free and
testable without clock mocks.

Design rationale
----------------

* ``status`` is a closed ``Literal`` of 4 values. There is no ``conflicted``
  (Validator writes ``disputed``) and no ``draft`` (information-poor articles
  use ``unverified``). This is the single status model.
* ``Relations.superseded_by`` is an ``article_id`` string. The pair
  ``status=historical`` + ``relations.superseded_by="art-..."`` expresses "the
  current replacement is art-...".
* ``generated_by`` has **exactly three fields** (tool / version /
  generated_at). Review audit trails live under
  ``extensions["review"]["audit"]`` (see ``ReviewAuditEntry``), never inside
  ``generated_by``.
* ``Article.extensions`` is a mutable ``dict[str, object]`` purely because
  Python stdlib has no frozen mapping that co-exists cleanly with
  ``dataclass(frozen=True)``. Immutability is enforced by **runtime
  convention**: only ``review.py`` / ``wiki-compile`` services mutate this
  field, and even then via copy-then-replace (no in-place append).
* ``Result[T, E] = Ok[T] | Err[E]`` expresses expected failures. Exceptions
  are reserved for truly exceptional conditions (OS errors, programming
  bugs). Use the ``is_ok`` / ``is_err`` predicates or ``match`` statements to
  narrow.

All tuple-typed fields must be populated with ``tuple(...)`` literals (not
``list``) to satisfy ``frozen=True`` semantics and to survive YAML
round-trips through the service layer (``lib/service/schema.py`` converts
lists back to tuples when loading an article).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar


# ---------------------------------------------------------------------------
# Closed literal sets (single source of truth)
# ---------------------------------------------------------------------------

ArticleType = Literal["decision", "runbook", "reference", "concept"]
"""View / required-field-set selector. Mutable on an article — ``article_id``
is the only identity that must not change."""

Status = Literal["current", "historical", "disputed", "unverified"]
"""Persisted status values. The only values accepted by ``wiki review resolve``
and the only values written by the Validator."""

SchemaVersion = Literal[1]
"""Current wiki schema version. Extend to ``Literal[1, 2]`` on the next
breaking migration."""


# ---------------------------------------------------------------------------
# Provenance-adjacent primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One upstream document that contributes evidence for an article.

    ``content_hash`` is a ``sha256:<hex>`` string (prefix included so the
    algorithm is self-describing). ``source_version`` is monotonic and
    Fetcher-managed; it increments each time the same logical source is
    re-ingested with a different content hash.
    """

    id: str
    type: str  # e.g. "slack_thread", "file", "url"
    ref: str  # relative path inside .wiki/
    source_version: int
    content_hash: str  # "sha256:..." format
    fetched_at: str  # ISO8601 UTC (Service layer injects)
    permalink: str | None = None


@dataclass(frozen=True)
class GeneratedBy:
    """Audit-light record of which tool and which tool version produced the
    current representation of the article.

    **Strictly three fields.** Review audits live under
    ``Article.extensions["review"]["audit"]``; do not add audit entries here.
    """

    tool: str
    version: int
    generated_at: str  # ISO8601 UTC


# ---------------------------------------------------------------------------
# Conversation normalization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One utterance from a conversation (Slack message, email, transcript
    line). The speaker-level structure must be preserved into the compiler so
    that intent classification and claim extraction can reason about speakers
    and reply structure."""

    speaker: str
    speaker_type: Literal["user", "bot", "system"]
    ts: str  # e.g. Slack "1700000000.000100"
    content: str
    edited_at: str | None = None
    deleted: bool = False
    orphan: bool = False  # parent message missing (reply without head)
    reply_to: str | None = None


# ---------------------------------------------------------------------------
# Time and relations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeTime:
    """The validity period of a claim or article.

    ``valid_to is None`` means *"still current, no known end"*. This is
    different from "expired at an unknown time" — use ``status=unverified``
    + a ``ReviewAuditEntry`` to express that case.
    """

    valid_from: str | None  # ISO date (YYYY-MM-DD)
    valid_to: str | None  # ISO date; None = still current


@dataclass(frozen=True)
class Relations:
    """Cross-article graph edges. All fields default to empty tuples so that
    an article with no relations round-trips cleanly through the schema."""

    supersedes: tuple[str, ...] = ()
    superseded_by: str | None = None
    caused_by: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()
    implements: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    related_to: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Claim (Phase 3 compiler writes these; Phase 0 stores them as empty tuples)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One atomic fact extracted from the sources.

    ``claim_id`` must be deterministic: the canonical form is
    ``{article_id}#c-{sha256(canonical(subject, attribute, period, predicate))[:8]}``
    where ``canonical()`` NFC-normalizes and trims ``subject`` / ``attribute``
    / ``predicate``, serializes ``period`` as ISO8601, and feeds the whole
    thing through JSON canonical form before hashing. Determinism prevents
    claim id drift when an article is re-compiled.

    ``source_refs`` holds the ``Source.id`` values this claim was extracted
    from. The referential integrity (``source_refs[i] in article.sources``)
    is enforced by the Validator, not by the dataclass itself.
    """

    claim_id: str
    subject: str
    attribute: str
    period: KnowledgeTime
    predicate: str
    source_refs: tuple[str, ...]


# ---------------------------------------------------------------------------
# Review audit trail (lives under extensions["review"]["audit"])
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewAuditEntry:
    """One entry in the ``wiki review resolve`` audit trail.

    Written by ``skills/wiki/scripts/review.py`` in append-only fashion via
    copy-on-write tuple replacement (never in-place mutation).
    """

    resolver: str  # user name or "system"
    resolved_at: str  # ISO8601 UTC
    status_before: Status
    status_after: Status
    reason: str = ""
    superseded_by_id: str | None = None


# ---------------------------------------------------------------------------
# Article — the top-level domain aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Article:
    """The canonical in-memory representation of a wiki article (v1 schema).

    ``extensions`` is a regular ``dict`` by necessity (see module docstring).
    Known namespaces:

    * ``extensions["review"]["audit"]: tuple[ReviewAuditEntry, ...]``
      — append-only audit trail of ``wiki review resolve`` decisions.
    """

    schema_version: int
    article_id: str  # immutable identity
    article_type: ArticleType
    title: str
    captured_at: str  # ISO date
    knowledge_time: KnowledgeTime
    status: Status
    sources: tuple[Source, ...]
    relations: Relations
    claims: tuple[Claim, ...]
    claim_refs: tuple[str, ...]  # other articles' claim_ids
    generated_by: GeneratedBy
    extensions: dict[str, object]  # read-only by convention
    tags: tuple[str, ...]
    body: str


# ---------------------------------------------------------------------------
# Result type (expected-failure channel)
# ---------------------------------------------------------------------------

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True)
class Ok(Generic[T]):
    """Success branch of ``Result[T, E]``."""

    value: T


@dataclass(frozen=True)
class Err(Generic[E]):
    """Failure branch of ``Result[T, E]``.

    ``error`` is the caller-defined discriminator (enum, string, etc.).
    ``detail`` is an optional human-readable context string that may be
    surfaced in CLI error output; it must not contain secrets.
    """

    error: E
    detail: str = ""


# ``Result[T, E]`` is used in annotations as ``Ok[T] | Err[E]``. A TypeAlias
# is intentionally not declared here because Python 3.10 does not support
# generic type aliases without ``TypeAlias``, and the added indirection buys
# nothing for call sites that can just write ``Ok[int] | Err[str]``.


def is_ok(result: Ok[T] | Err[E]) -> bool:
    """Type-guard predicate for the success branch.

    Prefer ``match`` statements for exhaustive narrowing; this helper exists
    for call sites that need a boolean (``if is_ok(r): ...``).
    """

    return isinstance(result, Ok)


def is_err(result: Ok[T] | Err[E]) -> bool:
    """Type-guard predicate for the failure branch."""

    return isinstance(result, Err)
