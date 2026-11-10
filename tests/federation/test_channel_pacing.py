"""Tests for :mod:`socialhome.federation.channel_pacing`."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.federation import channel_pacing
from socialhome.federation.channel_pacing import wait_for_send_room

HWM = 1000


class _Ch:
    def __init__(self, buffered: int, *, closed: bool = False) -> None:
        self.buffered_amount = buffered
        self.is_closed = closed


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(channel_pacing, "SEND_ROOM_POLL_S", 0.005)
    monkeypatch.setattr(channel_pacing, "SEND_ROOM_TIMEOUT_S", 0.05)


async def test_under_the_hwm_returns_at_once():
    await asyncio.wait_for(wait_for_send_room(_Ch(HWM - 1), high_water=HWM), 0.01)


async def test_waits_until_the_buffer_drains_under_the_hwm():
    ch = _Ch(HWM)

    async def _drain() -> None:
        await asyncio.sleep(0.02)
        ch.buffered_amount = HWM - 1

    drain = asyncio.create_task(_drain())
    await asyncio.wait_for(wait_for_send_room(ch, high_water=HWM), 1.0)
    await drain
    assert ch.buffered_amount == HWM - 1


async def test_stalled_buffer_raises_after_the_bound():
    with pytest.raises(ConnectionError, match="stalled"):
        await asyncio.wait_for(wait_for_send_room(_Ch(HWM), high_water=HWM), 1.0)


async def test_closed_channel_raises_instead_of_waiting():
    with pytest.raises(ConnectionError, match="closed"):
        await wait_for_send_room(_Ch(HWM, closed=True), high_water=HWM)
