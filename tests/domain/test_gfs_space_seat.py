"""Tests for socialhome.domain.gfs_space_seat."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.gfs_space_seat import GfsSpaceSeat


def test_is_frozen_with_unbound_defaults():
    seat = GfsSpaceSeat(space_id="sp", gfs_instance_id="g")
    assert (seat.gfs_connection_id, seat.gfs_public_key, seat.gfs_inbox_url) == (
        None,
        None,
        None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        seat.space_id = "other"  # type: ignore[misc]
