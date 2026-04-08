"""Schema v1 YAML frontmatter I/O + schema version detection.

This module is the single conversion layer between :class:`Article`
instances in memory and the on-disk Markdown+YAML-frontmatter representation
used by ``.wiki/concepts/*.md``. Responsibilities:

* **dump**: serialize an ``Article`` to a frontmatter+body string.
* **load**: parse a frontmatter+body string back into an ``Article``,
  returning ``Err`` on any validation failure.
* **detect_schema_version**: peek at a raw article text and report whether
  it is v1 (has ``schema_version: 1``), v0 (legacy layout with ``type: wiki``),
  or unknown. Used by ``migrate.py`` before deciding whether to route the
  file through a migration.
* **Legacy (v0) articles** are **not** loaded here — they surface as
  :attr:`SchemaError.LEGACY_SCHEMA` so that the caller routes them through
  :mod:`lib.service.migrations.v0_to_v1`. This keeps schema.py v1-only and
  avoids silently "upgrading" data that a migration audit should own.

YAML / tuple handling
---------------------

``yaml`` / ``python-frontmatter`` serialize tuples as lists. The loader is
therefore responsible for the inverse conversion: every field that
``Article`` declares as ``tuple[X, ...]`` must be re-wrapped after load.
The conversion helpers below walk the known schema deterministically so
there is one unambiguous place to audit when a new tuple field is added.

Forward compatibility
---------------------

Unknown top-level frontmatter fields are **preserved** into
``extensions["_unknown"]`` so that a future writer can round-trip them.
This lets us add minor optional fields without breaking older readers.
Unknown fields inside ``sources[]``, ``claims[]``, ``relations`` etc. are
**not** preserved — those structures are schema-pinned.
"""

from __future__ import annotations

from dataclasses import asdict
from enum import Enum
from typing import Any, Mapping, cast

import frontmatter

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
    Source,
    Status,
)


# ---------------------------------------------------------------------------
# Error discriminator
# ---------------------------------------------------------------------------


class SchemaError(str, Enum):
    """Discriminator for :func:`load_article` failures."""

    PARSE_ERROR = "parse_error"
    MISSING_FIELD = "missing_field"
    INVALID_TYPE = "invalid_type"
    INVALID_STATUS = "invalid_status"
    INVALID_ARTICLE_TYPE = "invalid_article_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    LEGACY_SCHEMA = "legacy_schema"  # v0 article, route through migration


# ---------------------------------------------------------------------------
# Known literal sets (kept in sync with lib.domain.types)
# ---------------------------------------------------------------------------

_VALID_STATUS: frozenset[str] = frozenset(
    {"current", "historical", "disputed", "unverified"}
)
_VALID_ARTICLE_TYPE: frozenset[str] = frozenset(
    {"decision", "runbook", "reference", "concept"}
)
_SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

# Top-level frontmatter fields that schema.py recognizes. Any other key is
# treated as forward-compatible and stashed under ``extensions["_unknown"]``
# instead of being rejected.
_KNOWN_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "schema_version",
        "article_id",
        "article_type",
        "title",
        "captured_at",
        "knowledge_time",
        "status",
        "sources",
        "relations",
        "claims",
        "claim_refs",
        "generated_by",
        "extensions",
        "tags",
    }
)


# ---------------------------------------------------------------------------
# Schema version detection (pre-flight, string input)
# ---------------------------------------------------------------------------


def detect_schema_version(text: str) -> int | None:
    """Return ``1`` for a v1 article, ``0`` for a legacy (v0) article, or
    ``None`` if the text has no recognizable frontmatter at all.

    This never raises; parse errors degrade to ``None``. Callers use it
    only as a preflight hint — actual validation happens in
    :func:`load_article` (v1) or :mod:`lib.service.migrations.v0_to_v1` (v0).
    """

    try:
        post = frontmatter.loads(text)
    except Exception:  # pragma: no cover - frontmatter is forgiving
        return None
    meta = post.metadata
    if not isinstance(meta, Mapping):
        return None
    version = meta.get("schema_version")
    if isinstance(version, int) and version in _SUPPORTED_SCHEMA_VERSIONS:
        return version
    if meta.get("type") == "wiki" and "source_refs" in meta:
        return 0
    return None


# ---------------------------------------------------------------------------
# Dump: Article -> frontmatter+body string
# ---------------------------------------------------------------------------


def dump_article(article: Article) -> str:
    """Serialize an :class:`Article` to a frontmatter+body string.

    The output is deterministic with respect to field ordering: YAML /
    python-frontmatter sort top-level keys alphabetically. That is intended
    — byte-stability of serialized articles is valuable for diff review,
    and alphabetical order is the one ordering both tools agree on.
    """

    mapping = _article_to_mapping(article)
    # Use dict-based frontmatter.Post; python-frontmatter will serialize
    # metadata via PyYAML. We intentionally strip a trailing newline from
    # ``body`` before handing it to frontmatter, because
    # :meth:`frontmatter.loads` already strips the terminating newline of
    # the content region. Round-trip stability comes from the loader
    # re-adding a single trailing ``\n`` whenever the loaded body is
    # non-empty (see :func:`load_article`).
    body = article.body.rstrip("\n")
    post = frontmatter.Post(body, **mapping)
    return frontmatter.dumps(post)


def _article_to_mapping(article: Article) -> dict[str, Any]:
    """Flatten an :class:`Article` into a JSON/YAML-friendly dict."""

    mapping: dict[str, Any] = {
        "schema_version": article.schema_version,
        "article_id": article.article_id,
        "article_type": article.article_type,
        "title": article.title,
        "captured_at": article.captured_at,
        "knowledge_time": {
            "valid_from": article.knowledge_time.valid_from,
            "valid_to": article.knowledge_time.valid_to,
        },
        "status": article.status,
        "sources": [_source_to_mapping(s) for s in article.sources],
        "relations": _relations_to_mapping(article.relations),
        "claims": [_claim_to_mapping(c) for c in article.claims],
        "claim_refs": list(article.claim_refs),
        "generated_by": {
            "tool": article.generated_by.tool,
            "version": article.generated_by.version,
            "generated_at": article.generated_by.generated_at,
        },
        "extensions": _extensions_to_mapping(article.extensions),
        "tags": list(article.tags),
    }
    return mapping


def _source_to_mapping(s: Source) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": s.id,
        "type": s.type,
        "ref": s.ref,
        "source_version": s.source_version,
        "content_hash": s.content_hash,
        "fetched_at": s.fetched_at,
    }
    if s.permalink is not None:
        out["permalink"] = s.permalink
    return out


def _relations_to_mapping(r: Relations) -> dict[str, Any]:
    return {
        "supersedes": list(r.supersedes),
        "superseded_by": r.superseded_by,
        "caused_by": list(r.caused_by),
        "derived_from": list(r.derived_from),
        "implements": list(r.implements),
        "depends_on": list(r.depends_on),
        "related_to": list(r.related_to),
    }


def _claim_to_mapping(c: Claim) -> dict[str, Any]:
    return {
        "claim_id": c.claim_id,
        "subject": c.subject,
        "attribute": c.attribute,
        "period": {
            "valid_from": c.period.valid_from,
            "valid_to": c.period.valid_to,
        },
        "predicate": c.predicate,
        "source_refs": list(c.source_refs),
    }


def _extensions_to_mapping(ext: dict[str, object]) -> dict[str, Any]:
    """Serialize extensions with special handling for the ``review.audit``
    tuple of :class:`ReviewAuditEntry`."""

    out: dict[str, Any] = {}
    for key, value in ext.items():
        if key == "_unknown":
            # Unknown forward-compat fields are spread back at the top
            # level during dump. We skip them here because they are already
            # merged in `_finalize_mapping_with_unknown()` below.
            continue
        if key == "review" and isinstance(value, Mapping) and "audit" in value:
            audit = value["audit"]
            if isinstance(audit, (tuple, list)):
                out["review"] = {
                    "audit": [_audit_entry_to_mapping(e) for e in audit]
                }
                # Merge any extra review-namespace keys besides "audit".
                for sub_key, sub_value in value.items():
                    if sub_key != "audit":
                        out["review"][sub_key] = sub_value
                continue
        out[key] = _deep_convert(value)
    return out


def _audit_entry_to_mapping(entry: ReviewAuditEntry | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(entry, ReviewAuditEntry):
        return {
            "resolver": entry.resolver,
            "resolved_at": entry.resolved_at,
            "status_before": entry.status_before,
            "status_after": entry.status_after,
            "reason": entry.reason,
            "superseded_by_id": entry.superseded_by_id,
        }
    # Already a mapping (e.g., round-tripped unknown extension); pass through.
    return dict(entry)


def _deep_convert(value: Any) -> Any:
    """Convert tuples to lists recursively for YAML serialization."""

    if isinstance(value, tuple):
        return [_deep_convert(v) for v in value]
    if isinstance(value, list):
        return [_deep_convert(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _deep_convert(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Load: frontmatter+body string -> Article
# ---------------------------------------------------------------------------


def load_article(text: str) -> Ok[Article] | Err[SchemaError]:
    """Parse a v1 article text into an :class:`Article`.

    Legacy v0 articles return ``Err(SchemaError.LEGACY_SCHEMA)`` so that
    the caller can route them through the dedicated migration. Any other
    validation failure returns a specific ``SchemaError`` discriminator.
    """

    try:
        post = frontmatter.loads(text)
    except Exception as exc:
        return Err(error=SchemaError.PARSE_ERROR, detail=str(exc))

    meta_raw = post.metadata
    if not isinstance(meta_raw, Mapping):
        return Err(error=SchemaError.PARSE_ERROR, detail="metadata not a mapping")
    meta: dict[str, Any] = dict(meta_raw)

    # v0 detection first — legacy articles do not carry schema_version
    if "schema_version" not in meta:
        if meta.get("type") == "wiki" or "source_refs" in meta:
            return Err(
                error=SchemaError.LEGACY_SCHEMA,
                detail="v0 article; route through migrations.v0_to_v1",
            )
        return Err(
            error=SchemaError.MISSING_FIELD,
            detail="schema_version is required",
        )

    version = meta["schema_version"]
    if not isinstance(version, int) or version not in _SUPPORTED_SCHEMA_VERSIONS:
        return Err(
            error=SchemaError.UNSUPPORTED_SCHEMA_VERSION,
            detail=f"unsupported schema_version={version!r}",
        )

    # Required top-level fields
    required = (
        "article_id",
        "article_type",
        "title",
        "captured_at",
        "knowledge_time",
        "status",
        "sources",
        "relations",
        "claims",
        "claim_refs",
        "generated_by",
        "extensions",
        "tags",
    )
    for name in required:
        if name not in meta:
            return Err(
                error=SchemaError.MISSING_FIELD,
                detail=f"missing required field: {name}",
            )

    status = meta["status"]
    if status not in _VALID_STATUS:
        return Err(error=SchemaError.INVALID_STATUS, detail=f"status={status!r}")

    article_type = meta["article_type"]
    if article_type not in _VALID_ARTICLE_TYPE:
        return Err(
            error=SchemaError.INVALID_ARTICLE_TYPE,
            detail=f"article_type={article_type!r}",
        )

    # Build nested structures
    try:
        knowledge_time = _knowledge_time_from_mapping(meta["knowledge_time"])
        sources = tuple(_source_from_mapping(s) for s in meta["sources"])
        relations = _relations_from_mapping(meta["relations"])
        claims = tuple(_claim_from_mapping(c) for c in meta["claims"])
        generated_by = _generated_by_from_mapping(meta["generated_by"])
        extensions = _extensions_from_mapping(meta["extensions"])
    except _LoadFieldError as exc:
        return Err(error=exc.kind, detail=exc.message)

    # Preserve unknown top-level fields under extensions["_unknown"]
    unknown_keys = set(meta.keys()) - _KNOWN_TOP_LEVEL
    if unknown_keys:
        unknown = {k: _deep_convert(meta[k]) for k in sorted(unknown_keys)}
        # Do not overwrite an existing _unknown key from extensions.
        existing = extensions.get("_unknown")
        if isinstance(existing, Mapping):
            merged = dict(existing)
            merged.update(unknown)
            extensions["_unknown"] = merged
        else:
            extensions["_unknown"] = unknown

    # Re-append a terminating newline for non-empty bodies so that the
    # on-disk convention (articles end with ``\n``) is preserved across
    # ``load → dump → load`` without diverging from the dump path, which
    # strips any trailing newline before handing content to
    # ``frontmatter.Post``.
    body_content = post.content
    if body_content:
        body_content = body_content + "\n"

    article = Article(
        schema_version=version,
        article_id=str(meta["article_id"]),
        article_type=cast(ArticleType, article_type),
        title=str(meta["title"]),
        captured_at=str(meta["captured_at"]),
        knowledge_time=knowledge_time,
        status=cast(Status, status),
        sources=sources,
        relations=relations,
        claims=claims,
        claim_refs=tuple(str(x) for x in meta["claim_refs"]),
        generated_by=generated_by,
        extensions=extensions,
        tags=tuple(str(x) for x in meta["tags"]),
        body=body_content,
    )
    return Ok(value=article)


# ---------------------------------------------------------------------------
# Load helpers (raise _LoadFieldError on bad shape; caller converts to Err)
# ---------------------------------------------------------------------------


class _LoadFieldError(Exception):
    def __init__(self, kind: SchemaError, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _LoadFieldError(
            SchemaError.INVALID_TYPE,
            f"{field_name} expected mapping, got {type(value).__name__}",
        )
    return value


def _knowledge_time_from_mapping(value: Any) -> KnowledgeTime:
    m = _require_mapping(value, "knowledge_time")
    return KnowledgeTime(
        valid_from=_optional_str(m.get("valid_from"), "knowledge_time.valid_from"),
        valid_to=_optional_str(m.get("valid_to"), "knowledge_time.valid_to"),
    )


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise _LoadFieldError(
        SchemaError.INVALID_TYPE,
        f"{field_name} expected string or null, got {type(value).__name__}",
    )


def _source_from_mapping(value: Any) -> Source:
    m = _require_mapping(value, "sources[]")
    for required in ("id", "type", "ref", "source_version", "content_hash", "fetched_at"):
        if required not in m:
            raise _LoadFieldError(
                SchemaError.MISSING_FIELD,
                f"sources[].{required} missing",
            )
    return Source(
        id=str(m["id"]),
        type=str(m["type"]),
        ref=str(m["ref"]),
        source_version=int(m["source_version"]),
        content_hash=str(m["content_hash"]),
        fetched_at=str(m["fetched_at"]),
        permalink=_optional_str(m.get("permalink"), "sources[].permalink"),
    )


def _relations_from_mapping(value: Any) -> Relations:
    m = _require_mapping(value, "relations")

    def _tuple(field: str) -> tuple[str, ...]:
        raw = m.get(field, ())
        if raw is None:
            return ()
        if not isinstance(raw, (list, tuple)):
            raise _LoadFieldError(
                SchemaError.INVALID_TYPE,
                f"relations.{field} expected list, got {type(raw).__name__}",
            )
        return tuple(str(x) for x in raw)

    return Relations(
        supersedes=_tuple("supersedes"),
        superseded_by=_optional_str(m.get("superseded_by"), "relations.superseded_by"),
        caused_by=_tuple("caused_by"),
        derived_from=_tuple("derived_from"),
        implements=_tuple("implements"),
        depends_on=_tuple("depends_on"),
        related_to=_tuple("related_to"),
    )


def _claim_from_mapping(value: Any) -> Claim:
    m = _require_mapping(value, "claims[]")
    for required in ("claim_id", "subject", "attribute", "period", "predicate", "source_refs"):
        if required not in m:
            raise _LoadFieldError(
                SchemaError.MISSING_FIELD,
                f"claims[].{required} missing",
            )
    refs = m["source_refs"]
    if not isinstance(refs, (list, tuple)):
        raise _LoadFieldError(
            SchemaError.INVALID_TYPE,
            "claims[].source_refs expected list",
        )
    return Claim(
        claim_id=str(m["claim_id"]),
        subject=str(m["subject"]),
        attribute=str(m["attribute"]),
        period=_knowledge_time_from_mapping(m["period"]),
        predicate=str(m["predicate"]),
        source_refs=tuple(str(x) for x in refs),
    )


def _generated_by_from_mapping(value: Any) -> GeneratedBy:
    m = _require_mapping(value, "generated_by")
    for required in ("tool", "version", "generated_at"):
        if required not in m:
            raise _LoadFieldError(
                SchemaError.MISSING_FIELD,
                f"generated_by.{required} missing",
            )
    return GeneratedBy(
        tool=str(m["tool"]),
        version=int(m["version"]),
        generated_at=str(m["generated_at"]),
    )


def _extensions_from_mapping(value: Any) -> dict[str, object]:
    if value is None:
        return {}
    m = _require_mapping(value, "extensions")
    out: dict[str, object] = {}
    for key, raw in m.items():
        if key == "review" and isinstance(raw, Mapping) and "audit" in raw:
            audit_raw = raw["audit"]
            if not isinstance(audit_raw, (list, tuple)):
                raise _LoadFieldError(
                    SchemaError.INVALID_TYPE,
                    "extensions.review.audit expected list",
                )
            audit = tuple(_audit_entry_from_mapping(e) for e in audit_raw)
            review_out: dict[str, object] = {"audit": audit}
            for sub_key, sub_value in raw.items():
                if sub_key != "audit":
                    review_out[str(sub_key)] = _deep_convert(sub_value)
            out["review"] = review_out
            continue
        out[str(key)] = _deep_convert(raw)
    return out


def _audit_entry_from_mapping(value: Any) -> ReviewAuditEntry:
    m = _require_mapping(value, "extensions.review.audit[]")
    for required in ("resolver", "resolved_at", "status_before", "status_after"):
        if required not in m:
            raise _LoadFieldError(
                SchemaError.MISSING_FIELD,
                f"extensions.review.audit[].{required} missing",
            )
    status_before = m["status_before"]
    status_after = m["status_after"]
    if status_before not in _VALID_STATUS or status_after not in _VALID_STATUS:
        raise _LoadFieldError(
            SchemaError.INVALID_STATUS,
            f"audit status must be one of {sorted(_VALID_STATUS)}",
        )
    return ReviewAuditEntry(
        resolver=str(m["resolver"]),
        resolved_at=str(m["resolved_at"]),
        status_before=cast(Status, status_before),
        status_after=cast(Status, status_after),
        reason=str(m.get("reason", "")),
        superseded_by_id=_optional_str(
            m.get("superseded_by_id"), "extensions.review.audit[].superseded_by_id"
        ),
    )
