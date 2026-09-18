"""Tests for the shared retry backoff math.

The outbox drains persist ``next_attempt_at`` at one-second
granularity and pick due rows with ``<= datetime('now')``, so the
contract these tests pin is: **a computed backoff is never small enough
to round to "due now"**. The previous full-jitter formula
(``random.uniform(0, window)``) could return ~0 s, which put a failed
row straight back in the due set on the next scheduler tick.
"""

from __future__ import annotations

import random

import pytest

from socialhome.services.backoff import (
    MIN_BACKOFF_SECONDS,
    jittered_backoff_seconds,
)


def _call(attempts: int, base: float = 30.0, cap: float = 1800.0) -> float:
    return jittered_backoff_seconds(
        attempts=attempts, base_seconds=base, cap_seconds=cap
    )


def test_minimum_sample_still_backs_off(monkeypatch):
    """The unluckiest jitter roll still yields half the window.

    Regression: with full jitter this returned ~0 s, so the row was due
    again immediately — no backoff at all.
    """
    monkeypatch.setattr(random, "uniform", lambda lo, _hi: lo)
    assert _call(1) == pytest.approx(15.0)
    assert _call(2) == pytest.approx(30.0)
    assert _call(3) == pytest.approx(60.0)


def test_never_below_one_second_even_with_a_tiny_base(monkeypatch):
    """The floor protects callers whose base is under 2 s."""
    monkeypatch.setattr(random, "uniform", lambda lo, _hi: lo)
    assert (
        jittered_backoff_seconds(attempts=1, base_seconds=0.1, cap_seconds=60.0)
        == MIN_BACKOFF_SECONDS
    )


def test_maximum_sample_equals_the_window(monkeypatch):
    """The unluckiest-the-other-way roll tops out at the full window."""
    monkeypatch.setattr(random, "uniform", lambda _lo, hi: hi)
    assert _call(1) == pytest.approx(30.0)
    assert _call(2) == pytest.approx(60.0)


def test_window_doubles_per_attempt_and_clamps_at_the_cap(monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda _lo, hi: hi)
    # 30 · 2^6 = 1920 > the 1800 cap.
    assert _call(7) == pytest.approx(1800.0)
    assert _call(20) == pytest.approx(1800.0)


def test_jitter_stays_inside_the_half_window_band():
    """Real RNG: every sample lands in ``[window / 2, window]``.

    The random half is what de-synchronises a swarm of simultaneously
    failing rows — equal jitter keeps that spread, it only removes the
    zero-delay tail.
    """
    seen = {_call(1) for _ in range(500)}
    assert all(15.0 <= d <= 30.0 for d in seen)
    # Actually jittered, not a constant.
    assert len(seen) > 1
