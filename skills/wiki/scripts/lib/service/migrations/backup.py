"""Backup manager for destructive migration runs.

Before ``migrate.py --apply`` rewrites any article on disk, it creates a
backup of ``.wiki/concepts/`` under ``.wiki/backups/{timestamp}/`` via
:class:`BackupManager`. The backup is a plain filesystem copy — no
checksums, no compression, no diffing — because:

* the wiki is small (O(100) articles in the foreseeable future), so copy
  throughput is not a concern;
* a raw copy is trivially inspectable by a human during rollback triage;
* recovery paths stay simple: ``cp -r`` in reverse.

On top of the copy, a ``.meta.json`` sidecar captures:

* the compact UTC timestamp that names the backup directory,
* the CLI version string that produced it,
* the number of articles covered,
* a ``tree_sha256`` computed over ``sorted([(relpath, sha256(content))])``
  so that :meth:`BackupManager.verify` can detect tampering or corruption
  before a restore would make the bad state active.

The tamper check is a **warning** during dogfooding (it surfaces as an
``Err(BackupError.TAMPERED)`` from :meth:`verify` / :meth:`restore`, but
the CLI handler is free to downgrade to a warning until the tool is
shared with a team). The hash itself is decoupled from that policy so
the same on-disk layout can be promoted to hard-block mode later without
rewriting backups.

Atomicity
---------

:meth:`create` builds each backup inside a hidden sibling directory
(``backups/.{timestamp}.tmp``) and promotes it via a single
:func:`os.rename` at the very end. Rename within the same filesystem is
atomic on POSIX, so a crash mid-copy leaves either:

* the previous state (no backup directory), or
* a complete backup directory with a matching ``.meta.json``.

It can never leave a half-populated ``{timestamp}`` directory that a
naive ``list_backups`` would pick up.

Dependency injection
--------------------

:class:`BackupManager` is a frozen dataclass that takes ``wiki_root``,
``clock``, and ``cli_version`` up front. All filesystem operations are
rooted at ``wiki_root`` — the manager never reads environment variables
or touches ``os.getcwd()``, so tests can instantiate it under ``tmp_path``
and exercise the real code paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lib.domain.types import Err, Ok
from lib.service.clock import Clock
from lib.service.path_validator import validate_backup_timestamp


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class BackupError(str, Enum):
    """Expected failures from :class:`BackupManager` operations."""

    SOURCE_MISSING = "source_missing"
    """``<wiki_root>/concepts`` does not exist — nothing to back up."""

    BACKUP_ALREADY_EXISTS = "backup_already_exists"
    """A directory at the target ``{timestamp}`` already exists. Callers
    must choose a different timestamp or clean up explicitly; the manager
    will never silently clobber an existing backup."""

    META_MISSING = "meta_missing"
    """``.meta.json`` is absent. Returned by ``verify`` / ``restore`` when
    the requested backup does not exist at all, or when it exists but is
    missing its sidecar (which means it was not produced by this
    manager)."""

    META_INVALID = "meta_invalid"
    """``.meta.json`` exists but cannot be parsed or is missing required
    fields."""

    TAMPERED = "tampered"
    """The recomputed ``tree_sha256`` does not match the value recorded in
    ``.meta.json``. The backup is not safe to restore."""

    INVALID_TIMESTAMP = "invalid_timestamp"
    """The supplied ``timestamp`` does not match ``YYYYMMDDTHHMMSSZ``."""


# ---------------------------------------------------------------------------
# BackupMeta value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupMeta:
    """Metadata persisted alongside each backup.

    All fields are primitive so the dataclass round-trips cleanly through
    ``json.dumps`` / ``json.loads``.
    """

    timestamp: str
    cli_version: str
    article_count: int
    tree_sha256: str  # "sha256:<hex>" format


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


_EXTENDED_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$"
)


def compact_timestamp(extended: str) -> str:
    """Convert ``2026-04-09T12:34:56[.fff]Z`` to ``20260409T123456Z``.

    The wiki's standard clock (``SystemClock`` / ``FixedClock``) emits the
    extended ISO8601 form because that is what human logs and on-the-wire
    metadata use. Backup directory names, however, must be filesystem-safe
    on every OS we care about and must sort lexicographically in time
    order — so we strip the punctuation and drop sub-second precision.

    Raises :class:`ValueError` on any format deviation. The caller (a
    :class:`BackupManager` method) wraps this into an ``Err`` before
    returning to user code.
    """

    m = _EXTENDED_ISO_RE.fullmatch(extended)
    if m is None:
        raise ValueError(
            f"expected extended ISO8601 UTC (YYYY-MM-DDTHH:MM:SS[.fff]Z), "
            f"got {extended!r}"
        )
    y, mo, d, h, mi, s = m.groups()
    return f"{y}{mo}{d}T{h}{mi}{s}Z"


# ---------------------------------------------------------------------------
# Tree hash — deterministic fingerprint of a directory
# ---------------------------------------------------------------------------


def _compute_tree_sha256(root: Path) -> str:
    """Return ``sha256:<hex>`` fingerprint of every file under ``root``.

    Files are enumerated in sorted POSIX-relative-path order and the
    accumulator is fed ``<relpath>\\0<sha256(content)>\\0`` per file. This
    means:

    * identical content + identical layout → identical hash,
    * renaming a file → different hash,
    * mutating a file → different hash,
    * the order in which files appear in ``rglob`` does not matter.

    Symlinks and non-regular files are ignored — the backup layout only
    ever contains regular files produced by ``shutil.copytree``.
    """

    accumulator = hashlib.sha256()
    if not root.exists():
        return "sha256:" + accumulator.hexdigest()

    files = sorted(
        (p for p in root.rglob("*") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    for f in files:
        relpath = f.relative_to(root).as_posix().encode("utf-8")
        content_hash = hashlib.sha256(f.read_bytes()).hexdigest().encode("ascii")
        accumulator.update(relpath)
        accumulator.update(b"\0")
        accumulator.update(content_hash)
        accumulator.update(b"\0")
    return "sha256:" + accumulator.hexdigest()


# ---------------------------------------------------------------------------
# BackupManager
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupManager:
    """Orchestrates create / verify / restore of concept-directory backups.

    ``BackupManager`` is a stateless service: all inputs arrive via method
    arguments or its DI fields, and all state lives on disk under
    ``wiki_root / 'backups'``. Constructing a fresh manager in another
    test gives you a clean slate without touching the one you already have.

    Parameters
    ----------
    wiki_root:
        Absolute path to the ``.wiki`` directory.
    clock:
        :class:`Clock` implementation used by :meth:`create` when no
        explicit timestamp is supplied.
    cli_version:
        Free-form identifier written to ``.meta.json``. The CLI handler
        sets this to its own tool version so that rollback triage can
        tell which release produced a given backup.
    """

    wiki_root: Path
    clock: Clock
    cli_version: str

    # -- paths -----------------------------------------------------------

    def _concepts_dir(self) -> Path:
        return self.wiki_root / "concepts"

    def _backups_root(self) -> Path:
        return self.wiki_root / "backups"

    def _backup_dir(self, timestamp: str) -> Path:
        return self._backups_root() / timestamp

    def _tmp_backup_dir(self, timestamp: str) -> Path:
        # Hidden sibling so that an interrupted create() is invisible to
        # list_backups(). Same parent as the final dir so that the atomic
        # rename stays within one filesystem.
        return self._backups_root() / f".{timestamp}.tmp"

    # -- create ----------------------------------------------------------

    def create(
        self, *, timestamp: str | None = None
    ) -> Ok[BackupMeta] | Err[BackupError]:
        """Create a fresh backup of ``<wiki_root>/concepts/``.

        ``timestamp`` defaults to the current clock value converted to
        compact UTC form. Any supplied timestamp is validated against the
        ``YYYYMMDDTHHMMSSZ`` pattern before the filesystem is touched.

        The operation is atomic: either the new
        ``<backups>/{timestamp}/`` directory exists with a matching
        ``.meta.json``, or nothing was added (a crash mid-copy leaves
        only the hidden ``.tmp`` sibling, which the next successful run
        will ignore and a future cleanup pass can sweep).
        """

        # Resolve / validate the timestamp.
        if timestamp is None:
            try:
                ts = compact_timestamp(self.clock.now())
            except ValueError as exc:
                return Err(
                    BackupError.INVALID_TIMESTAMP,
                    detail=f"clock returned unexpected format: {exc}",
                )
        else:
            ts = timestamp
        ts_result = validate_backup_timestamp(ts)
        if isinstance(ts_result, Err):
            return Err(BackupError.INVALID_TIMESTAMP, detail=ts_result.detail)

        # Validate source presence.
        src = self._concepts_dir()
        if not src.is_dir():
            return Err(
                BackupError.SOURCE_MISSING,
                detail=f"{src} does not exist",
            )

        # Duplicate guard — never overwrite.
        final_dir = self._backup_dir(ts)
        if final_dir.exists():
            return Err(
                BackupError.BACKUP_ALREADY_EXISTS,
                detail=f"{final_dir} already exists",
            )

        backups_root = self._backups_root()
        backups_root.mkdir(parents=True, exist_ok=True)

        # Staging directory — copytree the concepts into it, then rename.
        tmp_dir = self._tmp_backup_dir(ts)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)  # leftover from an earlier crashed run
        tmp_dir.mkdir(parents=True)
        tmp_concepts = tmp_dir / "concepts"
        shutil.copytree(src, tmp_concepts)

        # Compute metadata over the staged copy (not the live concepts
        # directory) so that the hash describes exactly what the backup
        # contains, not what the live tree looked like a moment ago.
        article_count = sum(
            1 for p in tmp_concepts.iterdir() if p.is_file() and p.suffix == ".md"
        )
        tree_hash = _compute_tree_sha256(tmp_concepts)
        meta = BackupMeta(
            timestamp=ts,
            cli_version=self.cli_version,
            article_count=article_count,
            tree_sha256=tree_hash,
        )
        meta_path = tmp_dir / ".meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "timestamp": meta.timestamp,
                    "cli_version": meta.cli_version,
                    "article_count": meta.article_count,
                    "tree_sha256": meta.tree_sha256,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        # Promote atomically.
        os.rename(tmp_dir, final_dir)
        return Ok(meta)

    # -- verify ----------------------------------------------------------

    def verify(self, *, timestamp: str) -> Ok[BackupMeta] | Err[BackupError]:
        """Recompute ``tree_sha256`` and compare against ``.meta.json``.

        Returns ``Ok(meta)`` if the backup is intact, or an ``Err`` with
        the discriminator that describes the failure.
        """

        ts_result = validate_backup_timestamp(timestamp)
        if isinstance(ts_result, Err):
            return Err(BackupError.INVALID_TIMESTAMP, detail=ts_result.detail)

        backup_dir = self._backup_dir(timestamp)
        meta_path = backup_dir / ".meta.json"
        if not meta_path.is_file():
            return Err(
                BackupError.META_MISSING,
                detail=f"{meta_path} does not exist",
            )

        try:
            raw = json.loads(meta_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            return Err(BackupError.META_INVALID, detail=str(exc))

        required = {"timestamp", "cli_version", "article_count", "tree_sha256"}
        if not required.issubset(raw.keys()):
            return Err(
                BackupError.META_INVALID,
                detail=f"missing fields: {sorted(required - raw.keys())}",
            )

        stored = BackupMeta(
            timestamp=raw["timestamp"],
            cli_version=raw["cli_version"],
            article_count=int(raw["article_count"]),
            tree_sha256=raw["tree_sha256"],
        )

        concepts = backup_dir / "concepts"
        current_hash = _compute_tree_sha256(concepts)
        if current_hash != stored.tree_sha256:
            return Err(
                BackupError.TAMPERED,
                detail=(
                    f"tree_sha256 mismatch: stored={stored.tree_sha256}, "
                    f"recomputed={current_hash}"
                ),
            )
        return Ok(stored)

    # -- restore ---------------------------------------------------------

    def restore(self, *, timestamp: str) -> Ok[int] | Err[BackupError]:
        """Overwrite ``<wiki_root>/concepts/`` from the given backup.

        ``verify`` is invoked before any destructive action, so a
        tampered or missing backup cannot trigger a restore. On success,
        returns ``Ok(restored_article_count)``.

        This method is intentionally **not atomic** against the live
        concepts directory: it removes the live directory and then
        copytrees the backup into place. A crash mid-restore leaves the
        live tree in a partial state — recovery is to re-run ``restore``
        (the backup is still intact), or to restore a different backup.
        This is acceptable during the dogfooding phase; a more robust
        implementation would stage to a sibling and swap at the very end,
        but that introduces complexity that the 12-article scale does
        not justify.
        """

        verification = self.verify(timestamp=timestamp)
        if isinstance(verification, Err):
            return Err(verification.error, detail=verification.detail)

        backup_concepts = self._backup_dir(timestamp) / "concepts"
        live_concepts = self._concepts_dir()
        if live_concepts.exists():
            shutil.rmtree(live_concepts)
        shutil.copytree(backup_concepts, live_concepts)

        restored = sum(
            1
            for p in live_concepts.iterdir()
            if p.is_file() and p.suffix == ".md"
        )
        return Ok(restored)

    # -- list ------------------------------------------------------------

    def list_backups(self) -> list[str]:
        """Return the compact timestamps of all valid-looking backups.

        Only directories whose name matches ``YYYYMMDDTHHMMSSZ`` and that
        contain a ``.meta.json`` file are listed. Stray files and
        half-written ``.tmp`` directories are silently ignored so that
        the CLI stays trustworthy even if users drop scratch content
        into ``.wiki/backups/``.
        """

        backups_root = self._backups_root()
        if not backups_root.is_dir():
            return []

        results: list[str] = []
        for entry in backups_root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            if isinstance(validate_backup_timestamp(entry.name), Err):
                continue
            if not (entry / ".meta.json").is_file():
                continue
            results.append(entry.name)
        return sorted(results)
