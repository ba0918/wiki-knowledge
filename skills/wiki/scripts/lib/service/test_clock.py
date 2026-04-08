"""Unit tests for lib/service/clock.py (Clock Protocol + System/Fixed clocks).

The clock is the single injection point for "now" across the service layer.
Domain code never calls ``datetime.now()`` or ``time.time()`` directly; it
receives a :class:`Clock` and calls ``clock.now()``. This is what lets us
test Provenance, Migration audit, Review audit, and Querylog timestamps
deterministically.
"""

from __future__ import annotations

import re

import pytest

from lib.service.clock import Clock, FixedClock, SystemClock


ISO8601_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_system_clock_satisfies_protocol() -> None:
    clock = SystemClock()
    assert isinstance(clock, Clock)


def test_fixed_clock_satisfies_protocol() -> None:
    clock = FixedClock(now="2026-04-08T09:12:00Z")
    assert isinstance(clock, Clock)


# ---------------------------------------------------------------------------
# SystemClock
# ---------------------------------------------------------------------------


def test_system_clock_returns_iso8601_utc_z_form() -> None:
    clock = SystemClock()
    stamp = clock.now()
    assert isinstance(stamp, str)
    assert ISO8601_UTC_RE.fullmatch(stamp), f"bad format: {stamp!r}"
    assert stamp.endswith("Z")  # UTC, not offset form like +00:00


def test_system_clock_monotonic_non_decreasing_on_consecutive_calls() -> None:
    clock = SystemClock()
    a = clock.now()
    b = clock.now()
    # String comparison works because ISO8601 compact is lexicographically
    # sortable when all values are UTC with the same Z suffix.
    assert a <= b


# ---------------------------------------------------------------------------
# FixedClock (for tests)
# ---------------------------------------------------------------------------


def test_fixed_clock_returns_exact_value() -> None:
    clock = FixedClock(now="2026-04-08T09:12:00Z")
    assert clock.now() == "2026-04-08T09:12:00Z"
    assert clock.now() == "2026-04-08T09:12:00Z"  # stable across calls


def test_fixed_clock_rejects_non_z_form() -> None:
    """Fixed clock enforces the same ISO8601 UTC Z form that SystemClock
    emits, so that tests cannot accidentally use a format that production
    would never produce."""
    with pytest.raises(ValueError):
        FixedClock(now="2026-04-08T09:12:00+00:00")


def test_fixed_clock_rejects_empty() -> None:
    with pytest.raises(ValueError):
        FixedClock(now="")


def test_fixed_clock_advance() -> None:
    """Tests that need a monotonically advancing clock can call ``advance``
    to bump the stored value without constructing a new clock."""
    clock = FixedClock(now="2026-04-08T09:12:00Z")
    clock.advance("2026-04-08T10:00:00Z")
    assert clock.now() == "2026-04-08T10:00:00Z"


def test_fixed_clock_advance_rejects_backwards() -> None:
    clock = FixedClock(now="2026-04-08T09:12:00Z")
    with pytest.raises(ValueError):
        clock.advance("2026-04-08T08:00:00Z")
