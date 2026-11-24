"""Tests for socialhome.global_server.domain.

The space content-epoch state of the retired space-keyed member publish
(``GfsSpaceEpoch``, ``epoch_ceiling`` …) went in v_96 (P7) with its routes;
a channel's epoch state lives on :class:`GfsChannel`."""

from __future__ import annotations

import pytest

from socialhome.global_server import domain
from socialhome.global_server.domain import CHANNEL_EPOCH_GRACE_S, GfsChannel


@pytest.mark.parametrize(
    "name",
    [
        "GfsSpaceEpoch",
        "GfsSpaceWriterKeys",
        "GfsSubscriber",
        "GfsSubscriberWithKeys",
        "epoch_ceiling",
        "MAX_EPOCH_STEP",
        "MIN_EPOCH_STEP_INTERVAL_S",
    ],
)
def test_the_space_keyed_relay_state_is_gone(name):
    assert not hasattr(domain, name)


def test_a_channel_admits_its_epoch_and_the_previous_within_the_grace():
    ch = GfsChannel(
        channel_id="c" * 32,
        channel_suite="ed25519",
        channel_pk="pk",
        registered_at=1,
        last_active_at=1,
        epoch=5,
        epoch_prev=4,
        epoch_raised_at=1000,
    )
    assert ch.admits(5, now=1000)
    assert ch.admits(4, now=1000 + CHANNEL_EPOCH_GRACE_S)
    assert not ch.admits(4, now=1001 + CHANNEL_EPOCH_GRACE_S)
