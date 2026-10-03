"""Tests for GFS domain behaviour (pure)."""

from __future__ import annotations

import pytest

from socialhome.global_server.domain import (
    MAX_EPOCH_CLOCK_LEAD_S,
    MAX_EPOCH_STEP,
    MIN_EPOCH_STEP_INTERVAL_S,
    GfsSpaceEpoch,
    epoch_ceiling,
)


def _state(current=5, confirmed=5, previous=4, confirmed_at=1000, raised_at=1000):
    return GfsSpaceEpoch(
        space_id="sp",
        current=current,
        confirmed=confirmed,
        previous=previous,
        confirmed_at=confirmed_at,
        raised_at=raised_at,
    )


@pytest.mark.parametrize("epoch", [5, 6])
def test_the_confirmed_epoch_and_one_ahead_are_always_admitted(epoch):
    assert _state().admits(epoch, now=10**9, grace_s=600)


def test_more_than_one_ahead_of_current_is_never_admitted():
    assert not _state().admits(7, now=1000, grace_s=600)


def test_everything_from_confirmed_to_current_stays_admitted():
    """Seed-only raises move ``current`` but never strand the floor."""
    state = _state(current=9, confirmed=5)
    for epoch in range(5, 11):
        assert state.admits(epoch, now=10**9, grace_s=600)


def test_the_previous_confirmed_epoch_is_admitted_only_within_the_grace():
    assert _state().admits(4, now=1000 + 600, grace_s=600)
    assert not _state().admits(4, now=1000 + 601, grace_s=600)


def test_an_epoch_older_than_the_previous_is_never_admitted():
    assert not _state().admits(3, now=1000, grace_s=600)


def test_without_a_previous_epoch_nothing_below_confirmed_passes():
    assert not _state(previous=None).admits(4, now=1000, grace_s=600)


def test_may_step_only_by_one_and_once_a_minute():
    state = _state(raised_at=1000)
    assert state.may_step(6, now=1000 + MIN_EPOCH_STEP_INTERVAL_S)
    assert not state.may_step(6, now=1000 + MIN_EPOCH_STEP_INTERVAL_S - 1)
    assert not state.may_step(7, now=10**9)
    assert not state.may_step(5, now=10**9)


def test_epoch_ceiling_allows_a_thousand_steps_or_a_day_past_the_clock():
    now = 1_800_000_000
    assert epoch_ceiling(5, now) == now + MAX_EPOCH_CLOCK_LEAD_S
    far = now * 2
    assert epoch_ceiling(far, now) == far + MAX_EPOCH_STEP
    assert epoch_ceiling(None, now) == now + MAX_EPOCH_CLOCK_LEAD_S
