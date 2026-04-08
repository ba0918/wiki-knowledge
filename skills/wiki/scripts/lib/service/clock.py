"""Clock Protocol + production / test implementations.

Every piece of code that needs "now" — :class:`~lib.domain.types.GeneratedBy`
stamping, :class:`~lib.domain.types.Source.fetched_at`,
:class:`~lib.domain.types.ReviewAuditEntry.resolved_at`, querylog entries —
must receive a :class:`Clock` instance via dependency injection. This is
the *only* way the wiki pipeline learns the current time.

* Production code uses :class:`SystemClock`, which formats
  ``datetime.now(timezone.utc)`` as ISO8601 with a trailing ``Z``.
* Unit tests use :class:`FixedClock`, which returns a caller-supplied
  string verbatim. ``advance()`` lets a single test move the clock forward
  to simulate timeline-dependent behavior without constructing multiple
  clock instances.

Both implementations emit the **same** on-the-wire format
(``YYYY-MM-DDTHH:MM:SSZ`` or ``YYYY-MM-DDTHH:MM:SS.fffZ``) so that a test
value and a production value are byte-for-byte interchangeable. This is
enforced at the ``FixedClock`` constructor by a regex.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


_ISO8601_UTC_Z_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)


@runtime_checkable
class Clock(Protocol):
    """Protocol for "now" providers.

    Implementations must return an ISO8601 UTC timestamp with a trailing
    ``Z`` suffix and no timezone offset form. Sub-second precision is
    optional (``SystemClock`` emits microseconds; ``FixedClock`` accepts
    any precision the caller provides).
    """

    def now(self) -> str: ...  # pragma: no cover - protocol


# ---------------------------------------------------------------------------
# SystemClock — production
# ---------------------------------------------------------------------------


class SystemClock:
    """Real wall-clock, formatted as ISO8601 UTC with Z suffix.

    ``datetime.now(timezone.utc).isoformat()`` emits ``...+00:00``; we
    replace the offset with ``Z`` for consistency with the rest of the
    pipeline (schema files, querylog, audit entries all use the Z form).
    """

    def now(self) -> str:
        stamp = datetime.now(timezone.utc).isoformat()
        # datetime.isoformat() yields '...+00:00'; normalize to Z form so
        # every wiki timestamp is byte-compatible with the regex that
        # validates it.
        if stamp.endswith("+00:00"):
            stamp = stamp[:-6] + "Z"
        return stamp


# ---------------------------------------------------------------------------
# FixedClock — test double
# ---------------------------------------------------------------------------


class FixedClock:
    """Test clock returning a fixed, caller-supplied ISO8601 UTC string.

    Rejects any value that does not match the production-compatible format
    so tests cannot accidentally diverge from on-disk representation.

    ``advance(new)`` replaces the stored value, enforcing monotonic
    forward movement — backwards advances raise :class:`ValueError` to
    catch test-logic bugs early.
    """

    def __init__(self, *, now: str) -> None:
        if not _ISO8601_UTC_Z_RE.fullmatch(now):
            raise ValueError(
                f"FixedClock expects ISO8601 UTC with Z suffix, got {now!r}"
            )
        self._now = now

    def now(self) -> str:
        return self._now

    def advance(self, new: str) -> None:
        """Move the clock forward to ``new``.

        Raises :class:`ValueError` if ``new`` is not strictly after the
        current value (string comparison is safe because both are ISO8601
        UTC with the same Z suffix and identical length format).
        """

        if not _ISO8601_UTC_Z_RE.fullmatch(new):
            raise ValueError(
                f"FixedClock.advance expects ISO8601 UTC with Z, got {new!r}"
            )
        if new <= self._now:
            raise ValueError(
                f"FixedClock.advance must move forward: {self._now!r} -> {new!r}"
            )
        self._now = new
