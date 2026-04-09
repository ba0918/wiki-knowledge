"""Tests for :mod:`lib.service.migrations.backup`.

These tests exercise real filesystem operations under ``tmp_path`` because
the whole point of the backup module is to survive partial-failure
scenarios on disk — mocking ``shutil.copytree`` would defeat the
testability goal.

Scope:

* ``create()`` with explicit and clock-derived timestamps
* ``verify()`` success + TAMPERED + META_MISSING cases
* ``restore()`` round-trip + tamper guard
* ``tree_sha256`` determinism and sensitivity
* timestamp compact-form conversion
* path_validator integration (INVALID_TIMESTAMP surface)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.domain.types import Err, Ok
from lib.service.clock import FixedClock
from lib.service.migrations.backup import (
    BackupError,
    BackupManager,
    BackupMeta,
    compact_timestamp,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def wiki_tree(tmp_path: Path) -> Path:
    """A fake wiki_root with a couple of concept articles to back up."""
    root = tmp_path / ".wiki"
    concepts = root / "concepts"
    concepts.mkdir(parents=True)
    (concepts / "alpha.md").write_text(
        "---\ntitle: Alpha\n---\nbody alpha\n", encoding="utf-8"
    )
    (concepts / "beta.md").write_text(
        "---\ntitle: Beta\n---\nbody beta\n", encoding="utf-8"
    )
    # Ensure backups/ is absent initially so create() has to mkdir it.
    return root


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(now="2026-04-09T12:34:56Z")


@pytest.fixture
def manager(wiki_tree: Path, fixed_clock: FixedClock) -> BackupManager:
    return BackupManager(
        wiki_root=wiki_tree,
        clock=fixed_clock,
        cli_version="wiki-migrate/1",
    )


# ---------------------------------------------------------------------------
# compact_timestamp helper
# ---------------------------------------------------------------------------


def test_compact_timestamp_basic() -> None:
    assert compact_timestamp("2026-04-09T12:34:56Z") == "20260409T123456Z"


def test_compact_timestamp_with_microseconds() -> None:
    """Sub-second precision is discarded — directory names must be second-precision."""
    assert compact_timestamp("2026-04-09T12:34:56.789Z") == "20260409T123456Z"


def test_compact_timestamp_rejects_bad_format() -> None:
    with pytest.raises(ValueError):
        compact_timestamp("2026-04-09 12:34:56")
    with pytest.raises(ValueError):
        compact_timestamp("20260409T123456Z")  # already compact
    with pytest.raises(ValueError):
        compact_timestamp("2026-04-09T12:34:56+00:00")  # offset form


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


def test_create_with_clock_timestamp(
    manager: BackupManager, wiki_tree: Path
) -> None:
    result = manager.create()
    assert isinstance(result, Ok)
    meta = result.value
    assert meta.timestamp == "20260409T123456Z"
    assert meta.article_count == 2
    assert meta.cli_version == "wiki-migrate/1"
    assert meta.tree_sha256.startswith("sha256:")

    backup_dir = wiki_tree / "backups" / "20260409T123456Z"
    assert (backup_dir / "concepts" / "alpha.md").is_file()
    assert (backup_dir / "concepts" / "beta.md").is_file()
    meta_path = backup_dir / ".meta.json"
    assert meta_path.is_file()
    on_disk = json.loads(meta_path.read_text("utf-8"))
    assert on_disk["timestamp"] == "20260409T123456Z"
    assert on_disk["article_count"] == 2
    assert on_disk["cli_version"] == "wiki-migrate/1"
    assert on_disk["tree_sha256"] == meta.tree_sha256


def test_create_with_explicit_timestamp(
    manager: BackupManager, wiki_tree: Path
) -> None:
    result = manager.create(timestamp="20260101T000000Z")
    assert isinstance(result, Ok)
    assert result.value.timestamp == "20260101T000000Z"
    assert (wiki_tree / "backups" / "20260101T000000Z" / ".meta.json").is_file()


def test_create_rejects_invalid_explicit_timestamp(manager: BackupManager) -> None:
    result = manager.create(timestamp="not-a-timestamp")
    assert isinstance(result, Err)
    assert result.error == BackupError.INVALID_TIMESTAMP


def test_create_fails_when_concepts_missing(
    tmp_path: Path, fixed_clock: FixedClock
) -> None:
    empty_root = tmp_path / ".wiki_empty"
    empty_root.mkdir()
    mgr = BackupManager(
        wiki_root=empty_root, clock=fixed_clock, cli_version="wiki-migrate/1"
    )
    result = mgr.create()
    assert isinstance(result, Err)
    assert result.error == BackupError.SOURCE_MISSING


def test_create_fails_on_duplicate_timestamp(
    manager: BackupManager, wiki_tree: Path
) -> None:
    first = manager.create()
    assert isinstance(first, Ok)
    second = manager.create()
    assert isinstance(second, Err)
    assert second.error == BackupError.BACKUP_ALREADY_EXISTS


def test_create_leaves_no_tmp_dir_behind(
    manager: BackupManager, wiki_tree: Path
) -> None:
    """Atomic rename path should clean up the .tmp prefix."""
    manager.create()
    backups = wiki_tree / "backups"
    tmp_dirs = [p for p in backups.iterdir() if p.name.startswith(".")]
    assert tmp_dirs == []


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


def test_verify_success(manager: BackupManager) -> None:
    created = manager.create()
    assert isinstance(created, Ok)
    verified = manager.verify(timestamp=created.value.timestamp)
    assert isinstance(verified, Ok)
    assert verified.value == created.value


def test_verify_detects_tampered_content(
    manager: BackupManager, wiki_tree: Path
) -> None:
    manager.create()
    # Mutate a backed-up article behind the manager's back.
    tampered = wiki_tree / "backups" / "20260409T123456Z" / "concepts" / "alpha.md"
    tampered.write_text("tampered content\n", encoding="utf-8")
    result = manager.verify(timestamp="20260409T123456Z")
    assert isinstance(result, Err)
    assert result.error == BackupError.TAMPERED


def test_verify_missing_meta(manager: BackupManager, wiki_tree: Path) -> None:
    manager.create()
    (wiki_tree / "backups" / "20260409T123456Z" / ".meta.json").unlink()
    result = manager.verify(timestamp="20260409T123456Z")
    assert isinstance(result, Err)
    assert result.error == BackupError.META_MISSING


def test_verify_missing_backup_dir(manager: BackupManager) -> None:
    result = manager.verify(timestamp="20260101T000000Z")
    assert isinstance(result, Err)
    assert result.error == BackupError.META_MISSING


def test_verify_rejects_invalid_timestamp(manager: BackupManager) -> None:
    result = manager.verify(timestamp="foo")
    assert isinstance(result, Err)
    assert result.error == BackupError.INVALID_TIMESTAMP


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------


def test_restore_round_trip(manager: BackupManager, wiki_tree: Path) -> None:
    manager.create()
    concepts = wiki_tree / "concepts"
    # Overwrite the live concept files with garbage.
    (concepts / "alpha.md").write_text("garbage\n", encoding="utf-8")
    (concepts / "beta.md").unlink()
    result = manager.restore(timestamp="20260409T123456Z")
    assert isinstance(result, Ok)
    assert result.value == 2  # restored article count
    assert (
        (concepts / "alpha.md").read_text("utf-8")
        == "---\ntitle: Alpha\n---\nbody alpha\n"
    )
    assert (concepts / "beta.md").is_file()


def test_restore_blocks_on_tampered_backup(
    manager: BackupManager, wiki_tree: Path
) -> None:
    manager.create()
    tampered = wiki_tree / "backups" / "20260409T123456Z" / "concepts" / "alpha.md"
    tampered.write_text("tampered\n", encoding="utf-8")
    result = manager.restore(timestamp="20260409T123456Z")
    assert isinstance(result, Err)
    assert result.error == BackupError.TAMPERED


def test_restore_fails_on_missing_backup(manager: BackupManager) -> None:
    result = manager.restore(timestamp="20260101T000000Z")
    assert isinstance(result, Err)
    assert result.error == BackupError.META_MISSING


# ---------------------------------------------------------------------------
# list_backups()
# ---------------------------------------------------------------------------


def test_list_backups_returns_timestamps_sorted(
    manager: BackupManager, wiki_tree: Path
) -> None:
    manager.create(timestamp="20260101T000000Z")
    manager.create(timestamp="20260301T000000Z")
    manager.create(timestamp="20260201T000000Z")
    assert manager.list_backups() == [
        "20260101T000000Z",
        "20260201T000000Z",
        "20260301T000000Z",
    ]


def test_list_backups_empty_when_no_backups_dir(
    tmp_path: Path, fixed_clock: FixedClock
) -> None:
    root = tmp_path / ".wiki_fresh"
    (root / "concepts").mkdir(parents=True)
    mgr = BackupManager(
        wiki_root=root, clock=fixed_clock, cli_version="wiki-migrate/1"
    )
    assert mgr.list_backups() == []


def test_list_backups_ignores_non_timestamp_entries(
    manager: BackupManager, wiki_tree: Path
) -> None:
    manager.create()
    # Create a stray non-timestamp directory; it must be silently ignored
    # so that users can't break `list_backups` by dropping files in.
    (wiki_tree / "backups" / "README.txt").write_text("hi", encoding="utf-8")
    (wiki_tree / "backups" / "stray-dir").mkdir()
    assert manager.list_backups() == ["20260409T123456Z"]


# ---------------------------------------------------------------------------
# tree_sha256 determinism
# ---------------------------------------------------------------------------


def test_tree_sha256_is_deterministic(
    manager: BackupManager, wiki_tree: Path, fixed_clock: FixedClock
) -> None:
    first = manager.create(timestamp="20260101T000000Z")
    assert isinstance(first, Ok)

    # Build a second, byte-identical wiki tree and back it up with a
    # fresh manager; the tree_sha256 must match.
    other_root = wiki_tree.parent / ".wiki_twin"
    (other_root / "concepts").mkdir(parents=True)
    for p in (wiki_tree / "concepts").iterdir():
        (other_root / "concepts" / p.name).write_bytes(p.read_bytes())
    other_mgr = BackupManager(
        wiki_root=other_root,
        clock=fixed_clock,
        cli_version="wiki-migrate/1",
    )
    second = other_mgr.create(timestamp="20260101T000000Z")
    assert isinstance(second, Ok)
    assert second.value.tree_sha256 == first.value.tree_sha256


def test_tree_sha256_changes_on_content_change(
    manager: BackupManager, wiki_tree: Path
) -> None:
    first = manager.create(timestamp="20260101T000000Z")
    assert isinstance(first, Ok)
    (wiki_tree / "concepts" / "alpha.md").write_text("different\n", encoding="utf-8")
    second = manager.create(timestamp="20260102T000000Z")
    assert isinstance(second, Ok)
    assert second.value.tree_sha256 != first.value.tree_sha256


# ---------------------------------------------------------------------------
# BackupMeta frozen
# ---------------------------------------------------------------------------


def test_backup_meta_is_frozen() -> None:
    meta = BackupMeta(
        timestamp="20260409T123456Z",
        cli_version="wiki-migrate/1",
        article_count=2,
        tree_sha256="sha256:" + "0" * 64,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        meta.article_count = 99  # type: ignore[misc]
