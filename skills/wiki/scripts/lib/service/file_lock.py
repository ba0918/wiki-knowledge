"""FileLock Protocol + production / fake implementations.

The wiki pipeline needs a cross-process file lock in four places:

1. ``wiki_repo`` atomic article_id allocation — only one process may be
   choosing the next available ``-2`` suffix at a time.
2. ``querylog.jsonl`` append — two processes appending NDJSON records
   concurrently must not interleave bytes inside a single line.
3. ``migrate.py`` per-article write during v0→v1 migration — SIGINT during
   a write must leave either the pre-write or post-write state on disk,
   never a half-written file.
4. ``review.py`` audit trail append — the ``extensions.review.audit`` tuple
   is copy-on-write and must not race with a concurrent resolve.

We wrap the third-party ``filelock`` library rather than calling ``fcntl``
directly so that:

* WSL2 edge cases (stale advisory locks across ``exec()`` boundaries) are
  handled by the upstream project rather than our code.
* macOS / Windows / Linux all work with the same API surface.
* Tests can substitute a ``FakeFileLock`` without monkey-patching fcntl.

Contract
--------

The :class:`FileLock` protocol exposes a single method, :meth:`acquire`,
which returns a context manager. Callers use it like::

    from lib.service.file_lock import RealFileLock

    lock = RealFileLock()
    with lock.acquire("/tmp/mywork.lock", timeout=5.0):
        do_work()

On contention, :class:`FileLockTimeout` is raised. The lock is always
released when the context manager exits, even on exception.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Protocol, runtime_checkable

import filelock as _filelock


class FileLockTimeout(Exception):
    """Raised when a :meth:`FileLock.acquire` call exceeds its timeout.

    This mirrors ``filelock.Timeout`` from the third-party library but is
    exposed under our own type so that callers can catch it without having
    to import the third-party package directly.
    """


@runtime_checkable
class FileLock(Protocol):
    """Protocol for cross-process file locks.

    ``acquire`` returns a context manager that blocks until the lock is
    held, up to ``timeout`` seconds. On timeout, :class:`FileLockTimeout`
    must be raised.
    """

    def acquire(
        self, path: str, *, timeout: float
    ) -> "_AcquireContext":  # pragma: no cover - protocol
        ...


# A tiny structural alias for "context manager returning None" so that
# implementations can annotate their return type consistently. This is the
# same contract as ``contextlib.AbstractContextManager[None]`` but avoids
# an extra import for the public surface.
class _AcquireContext(Protocol):
    def __enter__(self) -> None: ...  # pragma: no cover - protocol

    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# RealFileLock — thin wrapper around the ``filelock`` library
# ---------------------------------------------------------------------------


class RealFileLock:
    """Production :class:`FileLock` backed by ``filelock.FileLock``.

    Instances are cheap; you may construct one per call site. The underlying
    OS lock is identified by ``path``, not by the Python object.
    """

    @contextmanager
    def acquire(self, path: str, *, timeout: float) -> Iterator[None]:
        lock = _filelock.FileLock(path)
        try:
            lock.acquire(timeout=timeout)
        except _filelock.Timeout as exc:
            raise FileLockTimeout(
                f"could not acquire lock on {path!r} within {timeout}s"
            ) from exc
        try:
            yield None
        finally:
            lock.release()


# ---------------------------------------------------------------------------
# FakeFileLock — deterministic test double
# ---------------------------------------------------------------------------


class FakeFileLock:
    """Fake file lock for unit tests.

    Features useful for testing contention scenarios without touching disk:

    * ``always_times_out=True`` — every ``acquire`` raises
      :class:`FileLockTimeout`.
    * ``fail_first_n=N`` — the first ``N`` acquires raise, subsequent ones
      succeed. Useful for testing retry loops.
    * ``history`` — list of ``(path, timeout)`` tuples, every acquire
      appended in order.
    * ``currently_held`` — set of paths currently inside an ``acquire``
      context. Useful for assertions inside nested logic.
    """

    def __init__(
        self,
        *,
        always_times_out: bool = False,
        fail_first_n: int = 0,
    ) -> None:
        self._always_times_out = always_times_out
        self._fail_remaining = fail_first_n
        self.history: list[tuple[str, float]] = []
        self.currently_held: set[str] = set()

    @contextmanager
    def acquire(self, path: str, *, timeout: float) -> Iterator[None]:
        self.history.append((path, timeout))
        if self._always_times_out:
            raise FileLockTimeout(f"fake: always_times_out for {path!r}")
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise FileLockTimeout(
                f"fake: simulated contention on {path!r}, remaining={self._fail_remaining}"
            )
        self.currently_held.add(path)
        try:
            yield None
        finally:
            self.currently_held.discard(path)
