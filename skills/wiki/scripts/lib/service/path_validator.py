"""Single canonical path validator for the wiki pipeline.

Every I/O boundary (``wiki_repo``, ``fetchers``, ``migrations``,
``token_resolver``, ``stats``, ``review``) must route user-supplied path
strings through :func:`resolve_safe_path` (or :func:`sanitize_id` /
:func:`validate_backup_timestamp` for non-path identifiers). This prevents:

* path traversal via ``..``
* absolute path injection (``/etc/passwd`` etc.)
* NUL byte smuggling
* over-long path buffers
* symlinks that escape a configured base directory

The module has **no global state** and **no side effects beyond the
filesystem lookups that** :meth:`pathlib.Path.resolve` **itself performs**.
All failures are returned as :class:`~lib.domain.types.Err` values so that
callers can branch on them explicitly; exceptions are never raised for
validation failures — only for genuinely exceptional situations (e.g. the
caller handed in something that is not a path-like at all, which is
programming error rather than user error, and even that is surfaced as an
Err rather than raised).

Design notes
------------

* ``PathValidationError`` is an ``enum.StrEnum`` so Err values are both
  type-safe and human-readable when logged.
* ``sanitize_id`` is used for article_ids, slugs, backup directory leaves
  (e.g. ``customer-a-ops``). It enforces a lowercase slug space, 1–128
  characters, alphanumeric + ``-`` + ``_``, never starting or ending with
  a separator.
* ``validate_backup_timestamp`` enforces the compact ISO8601 basic format
  ``YYYYMMDDTHHMMSSZ`` (no separators) which is what
  ``migrations/backup.py`` writes to ``.wiki/backups/<timestamp>/``.
* ``ID_PATTERN`` is publicly exported so tests can pin the regex against
  silent widening.
"""

from __future__ import annotations

import os
import re
import unicodedata
from enum import Enum
from pathlib import Path

from lib.domain.types import Err, Ok


MAX_PATH_LEN = 1024
MAX_ID_LEN = 128


class PathValidationError(str, Enum):
    """Discriminator for path-validation failures."""

    EMPTY = "empty"
    ABSOLUTE = "absolute"
    OUTSIDE_BASE = "outside_base"
    NUL_BYTE = "nul_byte"
    TOO_LONG = "too_long"
    SYMLINK_ESCAPE = "symlink_escape"
    INVALID_TYPE = "invalid_type"
    INVALID_ID = "invalid_id"
    INVALID_TIMESTAMP = "invalid_timestamp"


# Slug / id pattern:
#   single char: [a-z0-9]
#   otherwise: start/end alphanumeric, inner may contain - or _
#   total length 1..128
ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,126}[a-z0-9]$|^[a-z0-9]$"
_ID_RE = re.compile(ID_PATTERN)

# Compact ISO8601 basic: YYYYMMDDTHHMMSSZ — uppercase T and Z, no separators
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def resolve_safe_path(
    *, base: Path, relative: str
) -> Ok[Path] | Err[PathValidationError]:
    """Resolve ``relative`` against ``base`` and return the absolute path.

    Returns an :class:`Ok` containing the resolved absolute :class:`Path` on
    success. Returns an :class:`Err` with a :class:`PathValidationError`
    discriminator on any validation failure. Does **not** create files or
    directories; the returned path may or may not exist on disk.

    Parameters
    ----------
    base:
        The absolute base directory that the resolved path must stay under.
        Callers are expected to pass a directory that they trust (e.g.
        ``.wiki/concepts/`` or ``.wiki/backups/``).
    relative:
        The user-supplied string to resolve. Must be a ``str`` (not a
        :class:`pathlib.Path`) so that the validation entry point is an
        explicit contract — Paths from upstream code are allowed to contain
        absolute segments or traversal, and we want to force explicit
        serialization before validation.
    """

    if not isinstance(relative, str):
        return Err(
            error=PathValidationError.INVALID_TYPE,
            detail=f"expected str, got {type(relative).__name__}",
        )

    if relative == "":
        return Err(error=PathValidationError.EMPTY)

    if "\x00" in relative:
        return Err(error=PathValidationError.NUL_BYTE)

    # Normalize first so that NFD inputs hash / compare consistently with
    # the rest of the pipeline (wiki_repo, fetchers).
    normalized = unicodedata.normalize("NFC", relative)

    if len(normalized.encode("utf-8")) > MAX_PATH_LEN:
        return Err(error=PathValidationError.TOO_LONG)

    if os.path.isabs(normalized):
        return Err(error=PathValidationError.ABSOLUTE)

    # Compose and resolve. ``Path.resolve(strict=False)`` canonicalizes
    # ``..`` segments without requiring the path to exist, and follows
    # symlinks for any segments that do exist.
    base_resolved = base.resolve()
    candidate = (base_resolved / normalized).resolve()

    # Containment check — the resolved path must be the base itself or a
    # descendant.
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        # If the candidate was assembled via symlinks that point outside,
        # distinguish that specifically for clearer error reporting.
        if _has_symlink_segment(base_resolved / normalized):
            return Err(error=PathValidationError.SYMLINK_ESCAPE)
        return Err(error=PathValidationError.OUTSIDE_BASE)

    return Ok(value=candidate)


def _has_symlink_segment(path: Path) -> bool:
    """Walk up the unresolved path and return True if any existing segment
    is a symbolic link. Used to distinguish symlink-escape from
    traversal-escape in error reporting."""

    current = path
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def sanitize_id(value: str) -> Ok[str] | Err[PathValidationError]:
    """Validate a slug / article_id string against :data:`ID_PATTERN`.

    Returns Ok(value) unchanged on success, Err(INVALID_ID) otherwise. The
    pattern enforces lowercase alphanumeric with optional ``-``/``_``
    separators, 1–128 characters, never starting or ending with a separator.
    This is deliberately narrow so that slugs are filesystem-safe on every
    OS we care about (Linux, macOS, Windows, WSL2).
    """

    if not isinstance(value, str) or not value:
        return Err(error=PathValidationError.INVALID_ID, detail="empty or non-string")
    if len(value) > MAX_ID_LEN:
        return Err(error=PathValidationError.INVALID_ID, detail="too long")
    if _ID_RE.fullmatch(value) is None:
        return Err(error=PathValidationError.INVALID_ID, detail="pattern mismatch")
    return Ok(value=value)


def validate_backup_timestamp(
    value: str,
) -> Ok[str] | Err[PathValidationError]:
    """Validate a compact ISO8601 basic UTC timestamp (``YYYYMMDDTHHMMSSZ``).

    This is the format used for ``.wiki/backups/<timestamp>/`` directory
    names. The Z suffix and uppercase T separator are mandatory so that
    directory ordering is lexicographically chronological and there is no
    ambiguity with local timezones.
    """

    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        return Err(
            error=PathValidationError.INVALID_TIMESTAMP,
            detail="expected YYYYMMDDTHHMMSSZ",
        )
    return Ok(value=value)
