"""Canonical repository for ``.wiki/concepts/*.md``.

:class:`WikiRepo` is the single adapter between the in-memory :class:`Article`
type and the on-disk filesystem layout. It combines three responsibilities
that, in a simpler codebase, would live in separate modules:

1. **Filesystem layout**: articles live at
   ``{wiki_root}/concepts/{article_id}.md``. The repo is the only place
   that knows this path shape.
2. **Atomic ID allocation**: :meth:`allocate_id` serializes on a per-repo
   FileLock and writes a stub article immediately to claim the id. The
   stub is always a valid v1 article with ``status="unverified"`` so that
   downstream tooling (lint, graph_gen) can ignore it cleanly until the
   caller fills in real content.
3. **Atomic write**: :meth:`save` uses ``tempfile.NamedTemporaryFile`` +
   ``os.replace`` so that a crash mid-write leaves either the old or the
   new content on disk — never a half-written file. This is critical for
   :mod:`lib.service.migrations.v0_to_v1` which must be safe against
   SIGINT.

All external dependencies (filesystem path, file lock, clock) are injected
via the constructor so tests can substitute :class:`~lib.service.file_lock.FakeFileLock`
and :class:`~lib.service.clock.FixedClock` without touching the real
filesystem outside ``tmp_path``.

Design note — why allocate_id writes a stub
-------------------------------------------

Without a stub claim, two concurrent callers could both read the same
listing, both pick "20260408163658-ops-2", release the lock, and then race
on :meth:`save`. Writing the stub *inside* the lock scope closes the race:
the second caller observes the first's file before releasing the lock.
The stub's ``status="unverified"`` makes it distinguishable from an
intentionally-written article.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar

from lib.domain.types import (
    Article,
    Err,
    GeneratedBy,
    KnowledgeTime,
    Ok,
    Relations,
)
from lib.service.clock import Clock
from lib.service.file_lock import FileLock, FileLockTimeout
from lib.service.path_validator import sanitize_id
from lib.service.schema import SchemaError, dump_article, load_article


_TIMESTAMP_RE = re.compile(r"^[0-9]{14}$")
_ALLOCATION_LOCK_NAME = ".allocation.lock"
_STUB_TOOL = "wiki-repo-stub"
_MAX_COLLISION_SUFFIX = 99


class RepoError(str, Enum):
    """Discriminator for :class:`WikiRepo` failures."""

    IO_ERROR = "io_error"
    NOT_FOUND = "not_found"
    INVALID_ID = "invalid_id"
    SCHEMA_ERROR = "schema_error"
    LOCK_TIMEOUT = "lock_timeout"


@dataclass
class WikiRepo:
    """Filesystem-backed article repository.

    Construction takes a ``wiki_root`` (the ``.wiki`` directory, not the
    ``.wiki/concepts`` subdirectory), a :class:`FileLock` implementation,
    and a :class:`Clock`. The repo does not create ``.wiki`` itself — the
    caller (e.g. ``wiki init``) is responsible for the top-level layout.
    ``.wiki/concepts/`` is created on demand if missing.
    """

    wiki_root: Path
    file_lock: FileLock
    clock: Clock

    _DEFAULT_LOCK_TIMEOUT: ClassVar[float] = 5.0

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _concepts_dir(self) -> Path:
        d = self.wiki_root / "concepts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _article_path(self, article_id: str) -> Path:
        return self._concepts_dir() / f"{article_id}.md"

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_article_ids(self) -> list[str]:
        """Return article_ids for every ``.md`` file in ``concepts/``.

        Hidden files (``.*``) and non-``.md`` files are skipped. Results
        are sorted lexicographically for deterministic iteration.
        """

        concepts = self._concepts_dir()
        out: list[str] = []
        for path in concepts.iterdir():
            if path.is_file() and path.suffix == ".md" and not path.name.startswith("."):
                out.append(path.stem)
        out.sort()
        return out

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self, article_id: str) -> Ok[Article] | Err[RepoError]:
        """Load a single article by id.

        Returns ``Err(NOT_FOUND)`` if the file is absent, ``Err(SCHEMA_ERROR)``
        if it is not a valid v1 article (including the legacy v0 layout).
        """

        path = self._article_path(article_id)
        if not path.exists():
            return Err(error=RepoError.NOT_FOUND, detail=str(path))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return Err(error=RepoError.IO_ERROR, detail=str(exc))
        result = load_article(text)
        if isinstance(result, Err):
            detail = result.detail
            if result.error == SchemaError.LEGACY_SCHEMA:
                detail = f"legacy v0 article (route through migrations): {detail}"
            return Err(error=RepoError.SCHEMA_ERROR, detail=detail)
        return result

    # ------------------------------------------------------------------
    # Save (atomic)
    # ------------------------------------------------------------------

    def save(self, article: Article) -> Ok[Article] | Err[RepoError]:
        """Write an article to disk atomically.

        Uses ``tempfile.NamedTemporaryFile`` in the same directory as the
        target so that ``os.replace`` is an atomic rename on POSIX. On a
        crash between writing and renaming, the incomplete ``.tmp`` file
        may linger — ``migrate.py`` is expected to clean these up before
        reading the repo.
        """

        validated = sanitize_id(article.article_id)
        if isinstance(validated, Err):
            return Err(error=RepoError.INVALID_ID, detail=validated.detail)

        text = dump_article(article)
        path = self._article_path(article.article_id)
        try:
            # Same-dir temp ensures os.replace is rename(2) atomic.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.stem}.",
                suffix=".tmp",
                delete=False,
            ) as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
                tmp_path = Path(fh.name)
            os.replace(tmp_path, path)
        except OSError as exc:
            return Err(error=RepoError.IO_ERROR, detail=str(exc))
        return Ok(value=article)

    # ------------------------------------------------------------------
    # Allocate (atomic id + stub claim)
    # ------------------------------------------------------------------

    def allocate_id(
        self, *, slug: str, timestamp: str
    ) -> Ok[str] | Err[RepoError]:
        """Compute a non-conflicting ``article_id`` and claim it on disk.

        The returned id has the form ``{timestamp}-{slug}`` or, on
        collision, ``{timestamp}-{slug}-{n}`` where ``n`` starts at 2.
        A stub article is written inside the lock scope so that
        concurrent callers observe the claim and pick the next suffix.
        """

        slug_result = sanitize_id(slug)
        if isinstance(slug_result, Err):
            return Err(error=RepoError.INVALID_ID, detail=slug_result.detail)
        if not _TIMESTAMP_RE.fullmatch(timestamp):
            return Err(
                error=RepoError.INVALID_ID,
                detail=f"timestamp must match YYYYMMDDHHMMSS, got {timestamp!r}",
            )

        base = f"{timestamp}-{slug_result.value}"
        concepts = self._concepts_dir()
        lock_path = str(concepts / _ALLOCATION_LOCK_NAME)

        try:
            with self.file_lock.acquire(
                lock_path, timeout=self._DEFAULT_LOCK_TIMEOUT
            ):
                existing = set(self.list_article_ids())
                for i in range(1, _MAX_COLLISION_SUFFIX + 1):
                    candidate = base if i == 1 else f"{base}-{i}"
                    if candidate in existing:
                        continue
                    # Write stub immediately to claim the id.
                    stub = self._build_stub(candidate, timestamp)
                    write_result = self._write_stub(stub)
                    if isinstance(write_result, Err):
                        return write_result
                    return Ok(value=candidate)
                return Err(
                    error=RepoError.IO_ERROR,
                    detail=(
                        f"exhausted {_MAX_COLLISION_SUFFIX} collision suffixes for {base}"
                    ),
                )
        except FileLockTimeout as exc:
            return Err(error=RepoError.LOCK_TIMEOUT, detail=str(exc))

    # ------------------------------------------------------------------
    # Stub construction
    # ------------------------------------------------------------------

    def _build_stub(self, article_id: str, timestamp: str) -> Article:
        """Build a minimal valid v1 article for id-claim purposes.

        ``status="unverified"`` distinguishes the stub from user content.
        ``captured_at`` is derived from the allocation timestamp so the
        stub is self-dating; ``generated_by.generated_at`` is taken from
        the injected :class:`Clock` so tests stay deterministic.
        """

        captured_at = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        return Article(
            schema_version=1,
            article_id=article_id,
            article_type="concept",
            title="(stub)",
            captured_at=captured_at,
            knowledge_time=KnowledgeTime(valid_from=captured_at, valid_to=None),
            status="unverified",
            sources=(),
            relations=Relations(),
            claims=(),
            claim_refs=(),
            generated_by=GeneratedBy(
                tool=_STUB_TOOL,
                version=1,
                generated_at=self.clock.now(),
            ),
            extensions={},
            tags=(),
            body="",
        )

    def _write_stub(self, stub: Article) -> Ok[Article] | Err[RepoError]:
        """Write a stub article bypassing the outer lock (already held)."""

        text = dump_article(stub)
        path = self._article_path(stub.article_id)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.stem}.",
                suffix=".tmp",
                delete=False,
            ) as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
                tmp_path = Path(fh.name)
            os.replace(tmp_path, path)
        except OSError as exc:
            return Err(error=RepoError.IO_ERROR, detail=str(exc))
        return Ok(value=stub)
