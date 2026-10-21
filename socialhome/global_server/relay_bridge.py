"""In-memory relay bridge for the public-content GFS-proxy fallback.

When an anonymous guest can't open a direct WebRTC DataChannel to the
content author (symmetric NAT, no usable TURN, P2P blocked), the GFS
proxies the stream: the guest opens a chunked ``GET`` and waits; the GFS
asks the author over the existing SH↔GFS WebSocket to stream the framed
content back over a signed ``POST``; this bridge pipes the author's body
chunks straight to the guest's response.

The GFS **never** parses, buffers-to-disk, or persists the bytes — a
:class:`_Channel` is a transient in-memory ``asyncio.Queue`` that lives
only for the duration of one relayed stream. It is a pure byte
passthrough: chunk boundaries carry no meaning, the guest reassembles
the length-prefixed frames itself. This is used **only** for
already-public, opt-in content (published highlights, public moments) —
never for private/space/DM content.

``scope`` is a free-form diagnostic label (the highlight id), not a
routing key. The ``relay_id`` names the node that holds the channel
(:mod:`.node_route`), so in a cluster the balancer sends the author's upload
to the node holding the guest's GET — the bytes never cross nodes. One upload
claims a channel (:meth:`RelayBridge.claim`): a second one for the same id
is refused rather than interleaved into the guest's stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from .node_route import scoped_id

log = logging.getLogger(__name__)

#: A relay that the author never connects to (or never finishes) is
#: dropped after this many seconds so an abandoned guest GET can't leak a
#: queue forever. The guest-side GET applies its own author-connect
#: timeout; this is the backstop for the post-connect path.
RELAY_TTL_SECONDS: float = 120.0

#: Hard cap on concurrently-live relay channels. The guest GET is
#: anonymous, so without a ceiling a flood of requests would create
#: unbounded queues *and* fan a WS-push to each author. Over the cap,
#: ``create`` returns ``None`` and the route answers 503 — no channel,
#: no push.
MAX_LIVE_CHANNELS: int = 256


@dataclass(slots=True)
class _Channel:
    """One in-flight relayed stream. Not frozen — ``connected`` flips and
    the queue is drained as bytes flow."""

    relay_id: str
    target_instance_id: str
    scope: str
    queue: asyncio.Queue[bytes | None]
    connected: asyncio.Event
    #: An author upload is feeding this channel (:meth:`RelayBridge.claim`).
    claimed: bool = False
    created_at: float = field(default_factory=time.time)


class RelayBridge:
    """Transient guest⇄author byte pipes keyed by an opaque ``relay_id``."""

    __slots__ = ("_channels", "_route_tag")

    def __init__(self, *, route_tag: str = "") -> None:
        self._channels: dict[str, _Channel] = {}
        #: This node's ``[cluster] route_tag``, stamped into every relay id.
        self._route_tag = route_tag

    def create(self, *, target_instance_id: str, scope: str = "") -> str | None:
        """Open a channel the author ``target_instance_id`` may stream into.

        Returns the ``relay_id`` the GFS pushes to the author over the WS
        and the guest's GET holds open — or ``None`` when the live-channel
        ceiling is hit, so the anonymous route sheds load with a 503
        instead of growing memory unbounded. Bounded queue → backpressure:
        a slow guest reader throttles the author's upload.
        """
        self.gc_expired()
        if len(self._channels) >= MAX_LIVE_CHANNELS:
            return None
        relay_id = scoped_id(self._route_tag)
        self._channels[relay_id] = _Channel(
            relay_id=relay_id,
            target_instance_id=target_instance_id,
            scope=scope,
            queue=asyncio.Queue(maxsize=16),
            connected=asyncio.Event(),
        )
        return relay_id

    def get(self, relay_id: str) -> _Channel | None:
        return self._channels.get(relay_id)

    def claim(self, relay_id: str) -> bool:
        """Mark *relay_id* as fed by one upload. ``False`` when the channel
        is gone or another upload already claimed it — two uploads must not
        interleave their bytes into one guest stream."""
        ch = self._channels.get(relay_id)
        if ch is None or ch.claimed:
            return False
        ch.claimed = True
        return True

    async def feed(self, relay_id: str, chunk: bytes) -> bool:
        """Enqueue an author body chunk. Returns False if the channel is
        gone (guest hung up / GC'd) so the author route can stop early."""
        ch = self._channels.get(relay_id)
        if ch is None:
            return False
        ch.connected.set()
        await ch.queue.put(chunk)
        return True

    async def finish(self, relay_id: str) -> None:
        """Mark end-of-stream so the guest's ``consume`` loop terminates."""
        ch = self._channels.get(relay_id)
        if ch is None:
            return
        ch.connected.set()
        await ch.queue.put(None)

    async def consume(self, relay_id: str) -> AsyncIterator[bytes]:
        """Yield author chunks until end-of-stream, then drop the channel.

        The ``finally`` pop guarantees the channel is released whether the
        stream ends cleanly, the guest disconnects (write raises upstream),
        or the iterator is GC'd.
        """
        ch = self._channels.get(relay_id)
        if ch is None:
            return
        try:
            while True:
                chunk = await ch.queue.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            self._channels.pop(relay_id, None)

    def close(self, relay_id: str) -> None:
        self._channels.pop(relay_id, None)

    def gc_expired(self, *, now: float | None = None) -> int:
        """Drop channels the author never connected to within
        :data:`RELAY_TTL_SECONDS`. Returns the count evicted. Run on every
        :meth:`create` — belt-and-suspenders for abandoned GETs. A channel
        that is streaming is never dropped here, however long the stream
        runs: the guest's :meth:`consume` releases it when it ends."""
        cutoff = (now if now is not None else time.time()) - RELAY_TTL_SECONDS
        stale = [
            rid
            for rid, c in self._channels.items()
            if c.created_at < cutoff and not c.connected.is_set()
        ]
        for rid in stale:
            self._channels.pop(rid, None)
        return len(stale)
