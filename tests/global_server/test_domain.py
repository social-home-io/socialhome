"""Tests for GFS domain behaviour (pure)."""

from __future__ import annotations

import pytest

from socialhome.global_server.domain import (
    MAX_EPOCH_CLOCK_LEAD_S,
    MAX_EPOCH_STEP,
    GfsSpaceEpoch,
    epoch_ceiling,
)


def _state(current=5, previous=4, seen_at=1000) -> GfsSpaceEpoch:
    return GfsSpaceEpoch(
        space_id="sp", current=current, previous=previous, current_seen_at=seen_at
    )


@pytest.mark.parametrize("epoch", [5, 6, 99])
def test_the_newest_or_a_newer_epoch_is_always_admitted(epoch):
    assert _state().admits(epoch, now=10**9, grace_s=600)


def test_the_previous_epoch_is_admitted_only_within_the_grace():
    assert _state().admits(4, now=1000 + 600, grace_s=600)
    assert not _state().admits(4, now=1000 + 601, grace_s=600)


def test_an_epoch_older_than_the_previous_is_never_admitted():
    assert not _state().admits(3, now=1000, grace_s=600)


def test_skipped_epochs_between_previous_and_current_ride_the_grace():
    state = _state(current=7, previous=3)
    assert state.admits(5, now=1000, grace_s=600)
    assert not state.admits(5, now=2000, grace_s=600)


def test_without_a_previous_epoch_only_current_or_newer_is_admitted():
    state = _state(previous=None)
    assert not state.admits(4, now=1000, grace_s=600)
    assert state.admits(5, now=10**9, grace_s=600)


def test_epoch_ceiling_allows_a_thousand_steps_or_a_day_past_the_clock():
    now = 1_800_000_000
    assert epoch_ceiling(5, now) == now + MAX_EPOCH_CLOCK_LEAD_S
    far = now * 2
    assert epoch_ceiling(far, now) == far + MAX_EPOCH_STEP
    assert epoch_ceiling(None, now) == now + MAX_EPOCH_CLOCK_LEAD_S
