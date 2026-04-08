"""Unit tests for lib/service/path_validator.py.

The path validator is the *single canonical* entry point for every I/O
operation that takes a path from a user, a source spec, a backup timestamp,
or a migration descriptor. It rejects unsafe inputs before the filesystem is
touched.

Policy (mirrored in the module docstring):

* relative path only, resolved under a caller-supplied base directory
* no ``..`` traversal once resolved
* no absolute paths in the raw input
* no NUL bytes
* length ceiling (1024 bytes after encoding)
* empty string rejected
* unicode is allowed but normalized to NFC
* symlinks that escape the base are rejected (resolved path must stay under
  the base dir)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lib.domain.types import Err, Ok
from lib.service.path_validator import (
    ID_PATTERN,
    PathValidationError,
    resolve_safe_path,
    sanitize_id,
    validate_backup_timestamp,
)


# ---------------------------------------------------------------------------
# resolve_safe_path — happy paths
# ---------------------------------------------------------------------------


def test_resolve_safe_path_returns_absolute_path_under_base(tmp_path: Path) -> None:
    target = tmp_path / "concepts"
    target.mkdir()
    result = resolve_safe_path(base=tmp_path, relative="concepts/foo.md")
    assert isinstance(result, Ok)
    assert result.value == (tmp_path / "concepts" / "foo.md").resolve()


def test_resolve_safe_path_accepts_unicode(tmp_path: Path) -> None:
    result = resolve_safe_path(base=tmp_path, relative="concepts/顧客A.md")
    assert isinstance(result, Ok)
    assert result.value.name == "顧客A.md"


def test_resolve_safe_path_normalizes_nfc(tmp_path: Path) -> None:
    # 'é' as NFD (e + combining acute) should be normalized to NFC
    nfd = "concepts/caf\u0065\u0301.md"
    result = resolve_safe_path(base=tmp_path, relative=nfd)
    assert isinstance(result, Ok)
    assert "café.md" in str(result.value)  # NFC form


# ---------------------------------------------------------------------------
# resolve_safe_path — rejection cases (each returns Err, never raises)
# ---------------------------------------------------------------------------


def test_resolve_safe_path_rejects_empty_string(tmp_path: Path) -> None:
    result = resolve_safe_path(base=tmp_path, relative="")
    assert isinstance(result, Err)
    assert result.error == PathValidationError.EMPTY


def test_resolve_safe_path_rejects_absolute_path(tmp_path: Path) -> None:
    result = resolve_safe_path(base=tmp_path, relative="/etc/passwd")
    assert isinstance(result, Err)
    assert result.error == PathValidationError.ABSOLUTE


def test_resolve_safe_path_rejects_parent_traversal(tmp_path: Path) -> None:
    result = resolve_safe_path(base=tmp_path, relative="../outside.md")
    assert isinstance(result, Err)
    assert result.error == PathValidationError.OUTSIDE_BASE


def test_resolve_safe_path_rejects_deep_parent_traversal(tmp_path: Path) -> None:
    result = resolve_safe_path(base=tmp_path, relative="concepts/../../outside.md")
    assert isinstance(result, Err)
    assert result.error == PathValidationError.OUTSIDE_BASE


def test_resolve_safe_path_rejects_nul_byte(tmp_path: Path) -> None:
    result = resolve_safe_path(base=tmp_path, relative="concepts/foo\x00.md")
    assert isinstance(result, Err)
    assert result.error == PathValidationError.NUL_BYTE


def test_resolve_safe_path_rejects_overlong_path(tmp_path: Path) -> None:
    long_name = "a" * 1100 + ".md"
    result = resolve_safe_path(base=tmp_path, relative=long_name)
    assert isinstance(result, Err)
    assert result.error == PathValidationError.TOO_LONG


def test_resolve_safe_path_rejects_symlink_escaping_base(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_target.md"
    outside.write_text("secret", encoding="utf-8")
    base = tmp_path / "base"
    base.mkdir()
    link = base / "leak.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    try:
        result = resolve_safe_path(base=base, relative="leak.md")
        assert isinstance(result, Err)
        assert result.error == PathValidationError.SYMLINK_ESCAPE
    finally:
        outside.unlink(missing_ok=True)


def test_resolve_safe_path_allows_symlink_inside_base(tmp_path: Path) -> None:
    target = tmp_path / "real.md"
    target.write_text("data", encoding="utf-8")
    link = tmp_path / "alias.md"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    result = resolve_safe_path(base=tmp_path, relative="alias.md")
    assert isinstance(result, Ok)


def test_resolve_safe_path_rejects_non_string_relative(tmp_path: Path) -> None:
    # Callers sometimes pass Path by mistake. We require a string input for
    # strict contract enforcement at the boundary.
    result = resolve_safe_path(base=tmp_path, relative=Path("foo"))  # type: ignore[arg-type]
    assert isinstance(result, Err)
    assert result.error == PathValidationError.INVALID_TYPE


# ---------------------------------------------------------------------------
# sanitize_id — article_id / slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "20260408163658-customer-a-ops",
        "customer_a_ops",
        "wiki-knowledge-architecture",
        "a",
        "a1-b2_c3",
    ],
)
def test_sanitize_id_accepts_valid(value: str) -> None:
    result = sanitize_id(value)
    assert isinstance(result, Ok)
    assert result.value == value


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "../escape",  # slashes / traversal
        "/abs",
        "has space",
        "CapitalCase",  # upper-case forbidden to keep slug space lowercase
        "日本語",  # non-ASCII
        "a" * 130,  # too long (>128)
        "trailing-",  # hyphen at end (slug convention)
        "-leading",
        "foo.md",  # dot forbidden (file extension belongs to the path)
    ],
)
def test_sanitize_id_rejects_invalid(value: str) -> None:
    result = sanitize_id(value)
    assert isinstance(result, Err)


def test_sanitize_id_pattern_is_pinned() -> None:
    # Guard against silent pattern widening in refactors.
    assert ID_PATTERN == r"^[a-z0-9][a-z0-9_-]{0,126}[a-z0-9]$|^[a-z0-9]$"


# ---------------------------------------------------------------------------
# validate_backup_timestamp
# ---------------------------------------------------------------------------


def test_validate_backup_timestamp_accepts_iso_compact() -> None:
    result = validate_backup_timestamp("20260408T091200Z")
    assert isinstance(result, Ok)
    assert result.value == "20260408T091200Z"


@pytest.mark.parametrize(
    "value",
    [
        "20260408-091200Z",  # wrong separator
        "20260408T091200",  # missing Z
        "2026-04-08T09:12:00Z",  # ISO extended form
        "20260408t091200z",  # lower-case
        "../etc/passwd",
        "",
        "20260408T091200Z/../other",
    ],
)
def test_validate_backup_timestamp_rejects(value: str) -> None:
    result = validate_backup_timestamp(value)
    assert isinstance(result, Err)
