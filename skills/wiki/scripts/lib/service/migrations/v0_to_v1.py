"""Concrete migration: v0 (legacy ``type: wiki``) → v1 schema.

This is the first (and, for Phase 0, the only) concrete implementation of
the :class:`Migration` Protocol. It converts a v0 frontmatter mapping + raw
body into a fully-populated v1 :class:`Article` domain object.

The authoritative field-mapping specification is
``.wiki/schema/migrations/v0-to-v1.md``. This module exists to *execute*
that spec; any divergence between the spec and this code is a bug and both
must move together.

Design rationale
----------------

**Why constructor-injected ``file_reader`` and ``clock``?** The Migration
Protocol pins ``up(mapping, body)`` so that the registry and handler stay
oblivious to per-migration dependencies. ``V0ToV1Migration`` needs to
compute ``content_hash`` (requires reading source files) and to stamp
``generated_by.generated_at`` / ``sources[].fetched_at`` (requires time).
Both are injected through ``__init__`` so that the ``up()`` body remains a
pure function of ``(mapping, body, file_reader, clock)`` — fully
deterministic when given a ``FixedClock`` and a fake reader.

**Why is ``_article_id`` passed inside ``mapping``?** The v0 frontmatter
does not carry an article_id field; the identity is derived from the
filename stem (``Path("querylog.md").stem → "querylog"``). The caller
(``migrate.py``) performs that extraction and injects the result as
``mapping["_article_id"]``. The ``_`` prefix signals a synthetic / caller-
supplied key rather than a persisted frontmatter field.

**Article type mapping**: The category→article_type table is a local
constant ``_CATEGORY_TO_ARTICLE_TYPE`` (not configurable). Adding a new
mapping is a code change + spec change because the mapping has semantic
meaning (it determines which ``x-body-sections`` template applies).

**Status**: ``current`` for all articles where at least one source file is
present. ``unverified`` only when *every* source is missing (the article
cannot be verified against any evidence). This avoids blanket-demoting real
content during dogfooding.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Callable, Mapping, Optional

from lib.domain.types import (
    Article,
    Err,
    GeneratedBy,
    KnowledgeTime,
    Ok,
    Relations,
    Source,
)
from lib.service.migrations.base import MigrationError
from lib.service.clock import Clock
from lib.service.path_validator import sanitize_id


# ---------------------------------------------------------------------------
# Type alias for the injected file reader
# ---------------------------------------------------------------------------

FileReader = Callable[[str], Optional[bytes]]
"""``(ref: str) -> bytes | None``. The caller resolves the ref against
``wiki_root`` and returns the raw content, or ``None`` if the file is
absent. The reader must validate the path via
``path_validator.resolve_safe_path`` to prevent traversal — that
responsibility lives in the caller, not in this module."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CATEGORY_TO_ARTICLE_TYPE: dict[str, str] = {
    "concepts": "concept",
    "tools": "reference",
    "practices": "runbook",
    "references": "reference",
}

_V0_KNOWN_KEYS = frozenset({
    "title",
    "type",
    "source_refs",
    "created",
    "updated",
    "category",
    "tags",
    "related",
})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_ZERO_HASH = "sha256:" + "0" * 64


# ---------------------------------------------------------------------------
# V0ToV1Migration
# ---------------------------------------------------------------------------


class V0ToV1Migration:
    """Migrate a single article from v0 (``type: wiki``) to v1 schema.

    Instantiate with your DI graph, then call ``up`` / ``down`` /
    ``validate`` as needed. The class does not register itself on import —
    ``migrate.py`` or a test does that explicitly via
    :func:`register_migration`.
    """

    from_version: int = 0
    to_version: int = 1

    def __init__(
        self,
        *,
        file_reader: FileReader,
        clock: Clock,
    ) -> None:
        self._file_reader = file_reader
        self._clock = clock

    # -- up --------------------------------------------------------------

    def up(
        self, mapping: Mapping[str, object], body: str
    ) -> Ok[Article] | Err[MigrationError]:
        """Forward migration: v0 mapping + body → v1 Article."""

        # ---- article_id ------------------------------------------------
        raw_id = mapping.get("_article_id")
        if raw_id is None or not isinstance(raw_id, str):
            return Err(
                MigrationError.MISSING_REQUIRED_FIELD,
                detail="_article_id must be supplied by the caller",
            )
        id_result = sanitize_id(raw_id)
        if isinstance(id_result, Err):
            return Err(MigrationError.INVALID_ID, detail=f"article_id: {raw_id!r}")

        article_id: str = id_result.value

        # ---- required v0 fields ----------------------------------------
        title = mapping.get("title")
        if not title or not isinstance(title, str):
            return Err(MigrationError.MISSING_REQUIRED_FIELD, detail="title")

        v0_type = mapping.get("type")
        if v0_type != "wiki":
            return Err(
                MigrationError.INVALID_TYPE_CONST,
                detail=f"expected 'wiki', got {v0_type!r}",
            )

        source_refs = mapping.get("source_refs")
        if source_refs is None:
            return Err(MigrationError.MISSING_REQUIRED_FIELD, detail="source_refs")
        if not isinstance(source_refs, (list, tuple)):
            return Err(MigrationError.MISSING_REQUIRED_FIELD, detail="source_refs must be a list")
        if len(source_refs) == 0:
            return Err(MigrationError.EMPTY_SOURCE_REFS, detail="source_refs is empty")

        created = mapping.get("created")
        if created is None or not isinstance(created, str):
            return Err(MigrationError.MISSING_REQUIRED_FIELD, detail="created")
        # python-frontmatter may parse YYYY-MM-DD as datetime.date
        created = str(created)
        if not _DATE_RE.fullmatch(created):
            return Err(MigrationError.INVALID_DATE, detail=f"created: {created!r}")

        updated = mapping.get("updated")
        if updated is None or not isinstance(updated, str):
            # updated is required in v0 schema but we're lenient here:
            # treat missing as same as created
            updated = created
        else:
            updated = str(updated)
        if not _DATE_RE.fullmatch(updated):
            return Err(MigrationError.INVALID_DATE, detail=f"updated: {updated!r}")

        category = mapping.get("category")
        if category is None or not isinstance(category, str):
            return Err(MigrationError.MISSING_REQUIRED_FIELD, detail="category")

        v0_tags: list[str] = list(mapping.get("tags") or [])  # type: ignore[arg-type]

        # ---- article_type derivation -----------------------------------
        article_type: str = _CATEGORY_TO_ARTICLE_TYPE.get(category, "concept")
        is_fallback = category not in _CATEGORY_TO_ARTICLE_TYPE

        # ---- tags construction -----------------------------------------
        tag_list = _dedup_preserving_order(v0_tags)
        if is_fallback and "legacy-unmapped-category" not in tag_list:
            tag_list.append("legacy-unmapped-category")
        if "legacy-v0" not in tag_list:
            tag_list.append("legacy-v0")

        # ---- relations conversion --------------------------------------
        related_raw: list[object] = list(mapping.get("related") or [])  # type: ignore[arg-type]
        related_ids: list[str] = []
        for entry in related_raw:
            if not isinstance(entry, str):
                return Err(MigrationError.INVALID_RELATED, detail=f"non-string: {entry!r}")
            stem = PurePosixPath(entry).stem
            stem_result = sanitize_id(stem)
            if isinstance(stem_result, Err):
                return Err(MigrationError.INVALID_RELATED, detail=f"related stem: {stem!r}")
            related_ids.append(stem_result.value)

        # ---- sources[] construction ------------------------------------
        now = self._clock.now()
        sources: list[Source] = []
        any_source_found = False
        for i, ref in enumerate(source_refs):
            ref_str = str(ref)
            raw_bytes = self._file_reader(ref_str)
            if raw_bytes is not None:
                any_source_found = True
                content_hash = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
            else:
                content_hash = _ZERO_HASH
            sources.append(
                Source(
                    id=f"legacy-{i + 1}",
                    type="file",
                    ref=ref_str,
                    source_version=1,
                    content_hash=content_hash,
                    fetched_at=now,
                    permalink=None,
                )
            )

        # ---- status ----------------------------------------------------
        status = "current" if any_source_found else "unverified"

        # ---- extensions.legacy stash -----------------------------------
        legacy: dict[str, object] = {
            "type": v0_type,
            "category": category,
            "updated": updated,
        }
        # Stash unknown v0 fields (future-proofing)
        unknown = {
            k: v
            for k, v in mapping.items()
            if k not in _V0_KNOWN_KEYS and not str(k).startswith("_")
        }
        if unknown:
            legacy["_unknown"] = unknown

        extensions: dict[str, object] = {"legacy": legacy}

        # ---- assemble Article ------------------------------------------
        article = Article(
            schema_version=1,
            article_id=article_id,
            article_type=article_type,  # type: ignore[arg-type]
            title=title,
            captured_at=created,
            knowledge_time=KnowledgeTime(
                valid_from=created,
                valid_to=None,
            ),
            status=status,  # type: ignore[arg-type]
            sources=tuple(sources),
            relations=Relations(
                related_to=tuple(related_ids),
            ),
            claims=(),
            claim_refs=(),
            generated_by=GeneratedBy(
                tool="wiki-migrate",
                version=1,
                generated_at=now,
            ),
            extensions=extensions,
            tags=tuple(tag_list),
            body=body,
        )
        return Ok(article)

    # -- down ------------------------------------------------------------

    def down(
        self, article: Article
    ) -> Ok[Mapping[str, object]] | Err[MigrationError]:
        """Reverse migration for rollback verification.

        Reconstructs the v0-shaped mapping from a migrated Article. If the
        Article was never produced by ``up()`` (missing ``extensions.legacy``
        stash), returns ``Err(MISSING_REQUIRED_FIELD)``.
        """
        ext = article.extensions
        if not isinstance(ext, dict) or "legacy" not in ext:
            return Err(
                MigrationError.MISSING_REQUIRED_FIELD,
                detail="extensions.legacy is missing — article was not migrated from v0",
            )
        legacy = ext["legacy"]
        if not isinstance(legacy, dict):
            return Err(
                MigrationError.MISSING_REQUIRED_FIELD,
                detail="extensions.legacy must be a dict",
            )

        for required in ("type", "category", "updated"):
            if required not in legacy:
                return Err(
                    MigrationError.MISSING_REQUIRED_FIELD,
                    detail=f"extensions.legacy.{required} is missing",
                )

        # Reconstruct tags: remove migration tombstones
        tags = [
            t
            for t in article.tags
            if t not in ("legacy-v0", "legacy-unmapped-category")
        ]

        # Reconstruct related paths from article_ids
        related = [
            f"concepts/{aid}.md" for aid in article.relations.related_to
        ]

        v0: dict[str, object] = {
            "title": article.title,
            "type": legacy["type"],
            "source_refs": [s.ref for s in article.sources],
            "created": article.captured_at,
            "updated": legacy["updated"],
            "category": legacy["category"],
            "tags": tags,
            "related": related,
        }
        return Ok(v0)

    # -- validate --------------------------------------------------------

    def validate(
        self, article: Article
    ) -> Ok[Article] | Err[MigrationError]:
        """Idempotent integrity check on a migrated Article.

        Verifies that the invariants promised by ``up()`` still hold:
        schema_version is 1, generated_by.tool is wiki-migrate, and the
        legacy stash is present. Returns ``Ok(article)`` unchanged on
        success.
        """
        if article.schema_version != 1:
            return Err(
                MigrationError.UNSUPPORTED_VERSION,
                detail=f"expected schema_version=1, got {article.schema_version}",
            )
        if article.generated_by.tool != "wiki-migrate":
            return Err(
                MigrationError.UNSUPPORTED_VERSION,
                detail=f"generated_by.tool is {article.generated_by.tool!r}, expected 'wiki-migrate'",
            )
        ext = article.extensions
        if not isinstance(ext, dict) or "legacy" not in ext:
            return Err(
                MigrationError.MISSING_REQUIRED_FIELD,
                detail="extensions.legacy is missing",
            )
        return Ok(article)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dedup_preserving_order(items: list[str]) -> list[str]:
    """Remove exact duplicates while preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
