"""Per-peer memory of the binary media leg on the HTTPS inbox (proto v_81).

:meth:`FederationService.send_media_chunk` POSTs a chunk to a v_81 peer's
inbox as a binary body and falls back to the JSON chunk on any non-2xx.
Without memory, a peer whose inbox keeps refusing binary bodies — a proxy
or WAF that blocks NUL bytes / ``application/octet-stream``, a bug, or the
media bucket behind Home Assistant's relay, where every sender shares one
address — would cost a wasted ~513 KiB POST before every ~930 KB JSON chunk,
forever (~1.55x the old cost). This remembers, per peer, when to skip the
leg:

* **429** — back off for its ``Retry-After`` (capped), else a short
  default; not held against the peer, the next chunk after it probes again.
* **another 4xx** (413, 415, 400, …) — the peer refuses the shape: off for
  :data:`MEDIA_HTTPS_DISABLE_S` at once.
* **5xx / no answer** — off for :data:`MEDIA_HTTPS_DISABLE_S` after
  :data:`MEDIA_HTTPS_STRIKES` in a row.
* After a window the leg is probed with one chunk; a failed probe turns it
  off for another window straight away. A 2xx forgets everything.

In memory only, bounded to :data:`MEDIA_HTTPS_MAX_PEERS` (the oldest entry
goes first) — a restart simply probes again. Pure module, no I/O.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

#: Failed answers in a row (5xx / no answer) before the leg is skipped.
MEDIA_HTTPS_STRIKES: int = 3

#: How long the leg is skipped for a peer that refuses it, before a probe.
MEDIA_HTTPS_DISABLE_S: float = 10 * 60.0

#: Back-off after a 429 with no readable ``Retry-After``.
MEDIA_HTTPS_RETRY_AFTER_DEFAULT_S: float = 30.0

#: Upper bound on a 429's ``Retry-After``.
MEDIA_HTTPS_RETRY_AFTER_MAX_S: float = 5 * 60.0

#: Peers remembered at most.
MEDIA_HTTPS_MAX_PEERS: int = 1024


@dataclass(slots=True, frozen=True)
class MediaPostResult:
    """The answer one binary media POST got: ``ok`` on a 2xx, the status
    (``None`` when no answer came) and a 429's ``Retry-After`` in seconds."""

    ok: bool
    status_code: int | None
    retry_after_s: float | None = None


@dataclass(slots=True)
class _PeerState:
    strikes: int = 0
    skip_until: float = 0.0


class MediaHttpsBackoff:
    """Which peers get the binary HTTPS leg right now (see module doc)."""

    __slots__ = ("_clock", "_peers")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._peers: OrderedDict[str, _PeerState] = OrderedDict()

    def __len__(self) -> int:
        return len(self._peers)

    def allows(self, peer_id: str) -> bool:
        """Whether the next chunk to *peer_id* may try the binary leg."""
        state = self._peers.get(peer_id)
        return state is None or self._clock() >= state.skip_until

    def record(self, peer_id: str, result: MediaPostResult) -> None:
        """Remember how *peer_id*'s inbox answered a binary body."""
        if result.ok:
            self._peers.pop(peer_id, None)
            return
        state = self._peers.pop(peer_id, None) or _PeerState()
        now = self._clock()
        status = result.status_code
        if status == 429:
            wait = result.retry_after_s
            if wait is None:
                wait = MEDIA_HTTPS_RETRY_AFTER_DEFAULT_S
            state.skip_until = now + min(wait, MEDIA_HTTPS_RETRY_AFTER_MAX_S)
        else:
            refuses_shape = status is not None and 400 <= status < 500
            state.strikes = (
                MEDIA_HTTPS_STRIKES
                if refuses_shape
                else min(state.strikes + 1, MEDIA_HTTPS_STRIKES)
            )
            if state.strikes >= MEDIA_HTTPS_STRIKES:
                state.skip_until = now + MEDIA_HTTPS_DISABLE_S
        self._peers[peer_id] = state
        while len(self._peers) > MEDIA_HTTPS_MAX_PEERS:
            self._peers.popitem(last=False)
