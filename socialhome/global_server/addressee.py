"""Which ``gfs_instance_id`` a household-signed request may be addressed to.

A household signs this server's PUBLIC id — ``[server] instance_id``, served
by ``GET /gfs/info`` and signed into its capability block — into every member
publish, epoch notice and channel request, so a request captured here can
never be replayed at another connection server. The id is pinned at pairing.

``[server] instance_id_aliases`` is a migration bridge: when an operator
changes ``instance_id`` (e.g. a cluster whose nodes once each had their own id
moves to one shared id), households still holding an old id keep being
served until they re-read ``/gfs/info`` and adopt the new one (they rebind
on a matching key — the capability block lists the aliases, signed, as
``replaces``, and a household moves only off an id listed there). An alias
is accepted as an ADDRESSEE; it is never the served id. A request may also
bind the server's KEY (``gfs_key``), which closes the replay gap an alias
opens (``gfs-0`` may be another operator's real id). Every request that
used an alias is logged at INFO (rate-limited per alias, with a count), so an
operator can watch adoption and drop the aliases once they go quiet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

log = logging.getLogger(__name__)

#: At most one INFO line per alias per this many seconds.
ALIAS_LOG_INTERVAL_S: float = 600.0


class GfsAddressee:
    """This server's public id plus the transitional aliases it answers to."""

    __slots__ = (
        "_primary",
        "_aliases",
        "_key",
        "_node_id",
        "_hits",
        "_logged_at",
        "_clock",
        "_stop",
        "_task",
    )

    def __init__(
        self,
        instance_id: str,
        aliases: Iterable[str] = (),
        *,
        public_key_hex: str = "",
        node_id: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary = instance_id
        #: This node's cluster ``node_id``, named in the alias log line so an
        #: operator sees which alloc still gets old-id traffic.
        self._node_id = node_id
        #: This server's identity key (hex) — a request that names the key it
        #: is addressed to (``gfs_key``) must name exactly this one.
        self._key = public_key_hex.lower()
        self._aliases = frozenset(a for a in aliases if a and a != instance_id)
        #: Alias hits since the last INFO line, per alias.
        self._hits: dict[str, int] = {}
        self._logged_at: dict[str, float] = {}
        self._clock = clock
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def instance_id(self) -> str:
        """The public id — the only one ever served or signed."""
        return self._primary

    @property
    def aliases(self) -> frozenset[str]:
        return self._aliases

    def accepts(self, gfs_instance_id: str, gfs_key: str | None = None) -> bool:
        """Whether a request signed for *gfs_instance_id* — and, when the
        household bound it, for the server key *gfs_key* — is addressed here.

        ``gfs_key`` closes the gap aliases open: a generic old id such as
        ``gfs-0`` may be another operator's real id, so a request captured
        there could otherwise be replayed here. A request carrying the field
        must name THIS server's key; one without it (an older household) is
        judged by the id alone, as before."""
        if gfs_key is not None and (not self._key or gfs_key.lower() != self._key):
            return False
        if gfs_instance_id == self._primary:
            return True
        if gfs_instance_id not in self._aliases:
            return False
        self._note_alias(gfs_instance_id)
        return True

    def _note_alias(self, alias: str) -> None:
        self._hits[alias] = self._hits.get(alias, 0) + 1
        now = self._clock()
        last = self._logged_at.get(alias)
        if last is not None and now - last < ALIAS_LOG_INTERVAL_S:
            return
        self._log(alias, now)

    def _log(self, alias: str, now: float) -> None:
        count = self._hits.pop(alias, 0)
        if not count:
            return
        self._logged_at[alias] = now
        log.info(
            "gfs: node %s: %d request(s) addressed to the alias %r instead of "
            "the instance_id %r — households that have not re-read /gfs/info "
            "yet. Drop the alias from [server] instance_id_aliases once these "
            "stop.",
            self._node_id or "-",
            count,
            alias,
            self._primary,
        )

    def flush(self) -> None:
        """Log every alias hit counted since its last line — so the last
        stragglers before traffic stops (or before shutdown) are seen."""
        now = self._clock()
        for alias in sorted(self._hits):
            self._log(alias, now)

    async def start(self) -> None:
        """Flush pending alias counts every :data:`ALIAS_LOG_INTERVAL_S`.
        No-op without aliases. Idempotent."""
        if not self._aliases or (self._task is not None and not self._task.done()):
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="gfs-alias-log")

    async def stop(self) -> None:
        """Stop the timer and flush what is pending."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None
        self.flush()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=ALIAS_LOG_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                self.flush()
