"""Unit tests for lib/service/file_lock.py (FileLock Protocol + implementations).

The file lock is the single serialization point for:

* article_id atomic allocation (``lib/service/wiki_repo.py``)
* querylog.jsonl concurrent appends (``lib/service/querylog.py``)
* migrate.py per-article writes during v0→v1 migration
* review.py audit trail appends

Tests cover:

* the Protocol contract (subclass/structural typing via ``runtime_checkable``)
* the production ``RealFileLock`` (wrapped around the ``filelock`` library):
  acquire / release / re-entry / contention blocking
* the test-double ``FakeFileLock``: no real filesystem, configurable
  contention scenarios for unit tests that don't touch disk
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from lib.service.file_lock import (
    FakeFileLock,
    FileLock,
    FileLockTimeout,
    RealFileLock,
)


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


def test_real_file_lock_satisfies_protocol() -> None:
    lock = RealFileLock()
    assert isinstance(lock, FileLock)


def test_fake_file_lock_satisfies_protocol() -> None:
    lock = FakeFileLock()
    assert isinstance(lock, FileLock)


# ---------------------------------------------------------------------------
# RealFileLock — single-process acquire / release
# ---------------------------------------------------------------------------


def test_real_file_lock_acquire_and_release(tmp_path: Path) -> None:
    lock = RealFileLock()
    lock_path = str(tmp_path / "repo.lock")
    with lock.acquire(lock_path, timeout=1.0):
        assert os.path.exists(lock_path)
    # After release, the lock file may still exist (filelock leaves it) but
    # re-acquiring from a fresh instance must succeed.
    with lock.acquire(lock_path, timeout=1.0):
        pass


def test_real_file_lock_blocks_contender(tmp_path: Path) -> None:
    lock_path = str(tmp_path / "contend.lock")
    lock_a = RealFileLock()
    lock_b = RealFileLock()
    with lock_a.acquire(lock_path, timeout=1.0):
        with pytest.raises(FileLockTimeout):
            with lock_b.acquire(lock_path, timeout=0.1):
                pass


def test_real_file_lock_serializes_threads(tmp_path: Path) -> None:
    """Two threads contending for the same lock must serialize."""

    lock_path = str(tmp_path / "thread.lock")
    order: list[str] = []

    def worker(tag: str, hold_for: float) -> None:
        lock = RealFileLock()
        with lock.acquire(lock_path, timeout=5.0):
            order.append(f"{tag}-enter")
            time.sleep(hold_for)
            order.append(f"{tag}-exit")

    t1 = threading.Thread(target=worker, args=("A", 0.1))
    t2 = threading.Thread(target=worker, args=("B", 0.1))
    t1.start()
    time.sleep(0.02)  # ensure t1 grabs the lock first
    t2.start()
    t1.join()
    t2.join()
    # Entry order must alternate cleanly — no interleaving between enter/exit
    assert order[0] == "A-enter"
    assert order[1] == "A-exit"
    assert order[2] == "B-enter"
    assert order[3] == "B-exit"


def test_real_file_lock_cross_process_mutual_exclusion(tmp_path: Path) -> None:
    """WSL2 smoke test: two separate Python processes contending for the
    same lock must block one of them."""

    lock_path = tmp_path / "cross.lock"
    helper = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from lib.service.file_lock import RealFileLock, FileLockTimeout
        lock = RealFileLock()
        try:
            with lock.acquire({str(lock_path)!r}, timeout=0.2):
                print("ACQUIRED")
        except FileLockTimeout:
            print("BLOCKED")
        """
    )
    holder = RealFileLock()
    with holder.acquire(str(lock_path), timeout=1.0):
        result = subprocess.run(
            [sys.executable, "-c", helper],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert "BLOCKED" in result.stdout, (
        f"expected contender to be blocked, got: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# FakeFileLock — deterministic test double
# ---------------------------------------------------------------------------


def test_fake_file_lock_default_never_blocks() -> None:
    lock = FakeFileLock()
    with lock.acquire("/any/path", timeout=0.01):
        pass
    with lock.acquire("/any/other", timeout=0.01):
        pass


def test_fake_file_lock_records_acquire_history() -> None:
    lock = FakeFileLock()
    with lock.acquire("/a", timeout=0.1):
        pass
    with lock.acquire("/b", timeout=0.2):
        pass
    assert lock.history == [("/a", 0.1), ("/b", 0.2)]


def test_fake_file_lock_simulates_timeout() -> None:
    lock = FakeFileLock(always_times_out=True)
    with pytest.raises(FileLockTimeout):
        with lock.acquire("/blocked", timeout=0.01):
            pass


def test_fake_file_lock_simulates_contention_count() -> None:
    """Fail the first N acquires, then succeed."""
    lock = FakeFileLock(fail_first_n=2)
    with pytest.raises(FileLockTimeout):
        with lock.acquire("/a", timeout=0.01):
            pass
    with pytest.raises(FileLockTimeout):
        with lock.acquire("/a", timeout=0.01):
            pass
    # third attempt succeeds
    with lock.acquire("/a", timeout=0.01):
        pass


def test_fake_file_lock_tracks_open_paths_for_debugging() -> None:
    lock = FakeFileLock()
    with lock.acquire("/x", timeout=0.1):
        assert "/x" in lock.currently_held
    assert "/x" not in lock.currently_held
