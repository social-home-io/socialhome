"""Refill a space file the GFS relay accepted but never delivered (v_87).

A ``SPACE_MEDIA_BLOB`` chunk pushed to a household seated from an invite
link rides the GFS relay as a binary frame. The relay answers every frame
with the same uniform ``202`` before it even looks at the recipient, so the
sender's media outbox counts the chunk as sent — and can never learn that
the relay dropped it later (saturated, the recipient's media queue full, the
1 h queue TTL) or that the receiving household refused it (its media
budget). Nothing else re-sends it: the outbox row is gone.

The receiving side heals it on the next VIEW. When the media route misses a
content-addressed file (``sc-…``, the only names a v_85+ push lands under),
:meth:`RelayMediaRefill.schedule` looks up the live space rows that show it
(:meth:`~socialhome.repositories.media_reference_repo
.SqliteMediaReferenceRepo.space_rows_of`) and, when the space's host is a
relay-seated peer this household takes binary relay media from
(:meth:`~socialhome.federation.federation_service.FederationService
.relay_media_ok`), asks it for ``SPACE_SYNC_RESUME`` since a minute before
that row. The host replays the space's rows from then (the receiver
dedups them by id) and re-pushes their files through the relay; the missing
one lands under the same content name, checked against its hash.

Bounded: one request per space per :data:`REFILL_COOLDOWN_S` (the host
throttles the same (household, space) pair on its side too), at most
:data:`MAX_REFILL_TASKS` lookups in flight, never on a paired host (direct
delivery is confirmed) and never for a legacy name. The generic "send me
this one file" request (``SPACE_MEDIA_WANTED``) comes with the GFS redesign;
this is the minimal recovery until then.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.federation import InstanceSource
from ..domain.media_constraints import SPACE_CONTENT_MEDIA_PREFIX

if TYPE_CHECKING:
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

#: One refill request per space per this many seconds — the host's own
#: ``SPACE_SYNC_RESUME`` cooldown for a (household, space) pair.
REFILL_COOLDOWN_S: float = 30 * 60.0

#: How far before the row showing the file the replay starts, for clock
#: skew between the two households.
REFILL_LOOKBACK_S: int = 60

#: Background lookups in flight at once; past it a view schedules nothing.
MAX_REFILL_TASKS: int = 8

#: Spaces whose last request time is remembered.
MAX_TRACKED_SPACES: int = 1024


class _Refs(Protocol):
    async def space_rows_of(self, basename: str) -> list[tuple[str, str]]: ...


class _Resume(Protocol):
    async def send_request(
        self, *, space_id: str, instance_id: str, since: str
    ) -> None: ...


class _RelayMediaPath(Protocol):
    async def relay_media_ok(self, instance: Any) -> bool: ...


def _since(created_at: str) -> str | None:
    """*created_at* (naive UTC ``YYYY-MM-DD HH:MM:SS`` or ISO-8601) minus
    :data:`REFILL_LOOKBACK_S`, as tz-aware ISO-8601, or ``None``."""
    try:
        at = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (at - timedelta(seconds=REFILL_LOOKBACK_S)).isoformat()


class RelayMediaRefill:
    """Ask a relay-seated host to re-push a space file that never arrived."""

    __slots__ = (
        "_clock",
        "_federation",
        "_federation_repo",
        "_last",
        "_refs",
        "_resume",
        "_spaces",
        "_tasks",
    )

    def __init__(
        self,
        *,
        media_refs: _Refs,
        space_repo: AbstractSpaceRepo,
        federation_repo: AbstractFederationRepo,
        federation: _RelayMediaPath,
        resume: _Resume,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._refs = media_refs
        self._spaces = space_repo
        self._federation_repo = federation_repo
        self._federation = federation
        self._resume = resume
        self._clock = clock
        self._last: OrderedDict[str, float] = OrderedDict()
        self._tasks: set[asyncio.Task[int]] = set()

    def schedule(self, basename: str) -> None:
        """Fire-and-forget :meth:`request` for a view that missed
        *basename*. Never raises, never delays the view."""
        if not basename.startswith(SPACE_CONTENT_MEDIA_PREFIX):
            return
        if len(self._tasks) >= MAX_REFILL_TASKS:
            return
        task = asyncio.get_running_loop().create_task(
            self._safe_request(basename), name="relay-media-refill"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for the scheduled lookups (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _safe_request(self, basename: str) -> int:
        try:
            return await self.request(basename)
        except Exception:
            log.warning("relay media refill failed for %s", basename, exc_info=True)
            return 0

    async def request(self, basename: str) -> int:
        """Ask the host of each space whose live row shows *basename* to
        replay it, when that host pushes media to us through the relay.
        Returns how many requests went out."""
        if not basename.startswith(SPACE_CONTENT_MEDIA_PREFIX):
            return 0
        sent = 0
        for space_id, created_at in await self._refs.space_rows_of(basename):
            now = self._clock()
            last = self._last.get(space_id)
            if last is not None and now - last < REFILL_COOLDOWN_S:
                continue
            since = _since(created_at)
            space = await self._spaces.get(space_id)
            if since is None or space is None:
                continue
            host = await self._federation_repo.get_instance(space.owner_instance_id)
            if host is None or host.source is not InstanceSource.SPACE_SESSION:
                continue
            if not await self._federation.relay_media_ok(host):
                continue
            await self._resume.send_request(
                space_id=space_id, instance_id=host.id, since=since
            )
            log.info(
                "relay media refill: asked %s to replay space %s since %s "
                "(a pushed file never arrived)",
                host.id,
                space_id,
                since,
            )
            self._last[space_id] = now
            self._last.move_to_end(space_id)
            while len(self._last) > MAX_TRACKED_SPACES:
                self._last.popitem(last=False)
            sent += 1
        return sent


__all__ = [
    "MAX_REFILL_TASKS",
    "REFILL_COOLDOWN_S",
    "REFILL_LOOKBACK_S",
    "RelayMediaRefill",
]
