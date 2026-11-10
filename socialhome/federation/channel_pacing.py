"""Bounded send pacing for streaming DataChannels.

aiolibdatachannel's ``DataChannel.send`` waits for its own drain event by
default (``wait_for_drain=True``). The library clears that event whenever
bytes are still buffered after a send and sets it again only when the
buffer crosses the low threshold from above — a buffer of small frames
never does, so the next waiting send hangs forever (#1347; upstream fix
social-home-io/aiolibdatachannel#25).

Streaming senders (space sync, public highlights) therefore write with
``wait_for_drain=False`` and pace themselves here instead: before each
frame, poll ``buffered_amount`` until it is under the sender's high-water
mark, bounded by :data:`SEND_ROOM_TIMEOUT_S`. This is load pacing, not a
privacy delay.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

#: How often a sender over its high-water mark re-reads ``buffered_amount``.
SEND_ROOM_POLL_S: float = 0.02

#: How long a sender waits for its buffer to drain under the high-water mark
#: before giving up on the channel. 1 MiB in 30 s is ~35 KB/s — a peer that
#: cannot take that is stalled, and the caller fails the stream as it does
#: for a closed channel.
SEND_ROOM_TIMEOUT_S: float = 30.0


class BufferedChannel(Protocol):
    """The two ``aiolibdatachannel.DataChannel`` properties pacing reads."""

    @property
    def buffered_amount(self) -> int: ...

    @property
    def is_closed(self) -> bool: ...


async def wait_for_send_room(channel: BufferedChannel, *, high_water: int) -> None:
    """Return once ``channel.buffered_amount`` is under *high_water*.

    Raises :class:`ConnectionError` if the channel closes while waiting or
    the buffer is still at or over *high_water* after
    :data:`SEND_ROOM_TIMEOUT_S`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SEND_ROOM_TIMEOUT_S
    while (buffered := channel.buffered_amount) >= high_water:
        if channel.is_closed:
            raise ConnectionError("DataChannel closed while waiting to send")
        if loop.time() >= deadline:
            raise ConnectionError(
                f"DataChannel stalled (buffered={buffered}, hwm={high_water}, "
                f"waited {SEND_ROOM_TIMEOUT_S:g} s)",
            )
        await asyncio.sleep(SEND_ROOM_POLL_S)
