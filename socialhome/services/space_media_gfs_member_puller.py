"""Space media on demand — the member fallback, member side.

When a GFS reader misses an expired picture of a LISTED space, the GFS asks
the space's owner household first (v_83). If the owner is offline, too slow
or busy, a GFS that proves ``on_demand.member_pull`` in its signed
capability offers the want to the space's members — who pull it
ANONYMOUSLY: ``POST /gfs/media/member-pull`` signed by the space's per-epoch
writer GROUP key (the key every writer member shares; never this
household's key, never a writer cert, never ``X-SH-Instance``), over the
cookie-less session. The GFS learns "some writer-key holder of space S
pulled" — not which household.

This scheduler keeps a few long-poll loops for (GFS, space) pairs where:

* this household is a member of the space but NOT its owner (the owner is
  asked by name over its socket already);
* it holds the space's writer group key for the current epoch (followers
  never do);
* the GFS is on the owner's signed list for the space AND is one of this
  household's own active, registered connections;
* the GFS proves ``member_pull``.

At most :data:`MAX_PULL_LOOPS` pairs (a random sample), re-picked every
:data:`REFRESH_S`. The first pick waits a random :data:`START_DELAY_S` so the
anonymous pulls are not timed to the authenticated socket's connect. A pull
that returns a want is answered at once by
:meth:`~socialhome.services.space_media_gfs_responder.SpaceMediaGfsResponder
.answer_member_want` (the owner path's file rules, caps and budgets), then
the loop pulls again; nothing within the hold → again after
:data:`IDLE_JITTER_S`; no slot / rate limited → :data:`BACKOFF_FULL_S`;
anything else → :data:`BACKOFF_REFUSED_S`.

Lifecycle: the ``_stop: asyncio.Event`` scheduler pattern
(``infrastructure/replay_cache_scheduler.py``).
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING

from ..domain.gfs_space_media import SCOPE_SPACE
from ..domain.gfs_space_media_on_demand import (
    InvalidOnDemandWire,
    MemberPullRequest,
    MemberWant,
)
from ..domain.writer_key import WRITER_KEY_SUITE_ED25519
from ..writer_key import sign_with_writer_key

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption
    from .space_media_gfs_responder import SpaceMediaGfsResponder
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)

#: How often the pairs are picked again.
REFRESH_S: float = 600.0

#: Random wait before the first pick (seconds, low / high).
START_DELAY_S: tuple[float, float] = (30.0, 120.0)

#: Pull loops at most (one per (GFS, space) pair).
MAX_PULL_LOOPS: int = 4

#: A pull's HTTP timeout beyond the GFS's hold.
PULL_TIMEOUT_PAD_S: float = 10.0

#: Waits before the next pull (seconds, low / high): nothing within the
#: hold; no slot or rate limited; refused, malformed or unreachable.
IDLE_JITTER_S: tuple[float, float] = (0.0, 2.0)
BACKOFF_FULL_S: tuple[float, float] = (600.0, 1200.0)
BACKOFF_REFUSED_S: tuple[float, float] = (300.0, 600.0)

#: Pulls this household sends one GFS per minute, every loop together —
#: below the GFS's per-address limit (12 / min), so a burst of wants (a
#: reader asking for many pictures, or junk) paces the pulls instead of
#: tripping that limit and its 10–20 min back-off.
MAX_PULLS_PER_MINUTE: int = 10

#: How long :meth:`SpaceMediaGfsMemberPuller.stop` waits for the loops
#: before it cancels them (a pull may be held open for its whole hold).
STOP_GRACE_S: float = 5.0


class SpaceMediaGfsMemberPuller:
    """Long-poll the GFSs for wants of listed spaces this household is a
    writer member of (module docstring)."""

    __slots__ = (
        "_certs",
        "_crypto",
        "_federation",
        "_gfs",
        "_loops",
        "_pulled",
        "_responder",
        "_rng",
        "_spaces",
        "_start_delay",
        "_stop",
        "_task",
        "_wanted",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        gfs_service: GfsConnectionService,
        writer_certs: SpaceWriterCertService,
        space_crypto: SpaceContentEncryption,
        responder: SpaceMediaGfsResponder,
        rng: random.Random | None = None,
        start_delay: tuple[float, float] = START_DELAY_S,
    ) -> None:
        self._spaces = space_repo
        self._gfs = gfs_service
        self._certs = writer_certs
        self._crypto = space_crypto
        self._responder = responder
        self._rng = rng or random.SystemRandom()
        self._start_delay = start_delay
        self._federation: FederationService | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        #: (GFS connection id, space id) → its pull loop.
        self._loops: dict[tuple[str, str], asyncio.Task] = {}
        #: The pairs picked last; a loop whose pair left ends on its own.
        self._wanted: set[tuple[str, str]] = set()
        #: GFS connection id → monotonic times of the pulls of the last
        #: minute (:data:`MAX_PULLS_PER_MINUTE`).
        self._pulled: dict[str, deque[float]] = {}

    def attach_federation(self, federation: FederationService) -> None:
        self._federation = federation

    async def start(self) -> None:
        """Start the scheduler. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the scheduler and every pull loop."""
        self._stop.set()
        self._wanted = set()
        tasks = [t for t in (self._task, *self._loops.values()) if t is not None]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=STOP_GRACE_S)
            for task in pending:
                # A pull held open by the GFS: nothing is written locally
                # mid-request, so cutting it short is safe.
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._task = None
        self._loops.clear()

    async def _run(self) -> None:
        if await self._wait(self._rng.uniform(*self._start_delay)):
            return
        while not self._stop.is_set():
            try:
                await self.refresh()
            except Exception:  # pragma: no cover - defensive
                log.warning("space media member pull: refresh failed", exc_info=True)
            if await self._wait(REFRESH_S):
                return

    async def _wait(self, seconds: float) -> bool:
        """Wait *seconds* or until stopped; ``True`` when stopped."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            return False
        return True

    async def refresh(self) -> None:
        """Pick the pairs again: start a loop for a new one; a loop whose
        pair was not picked ends after its current pull."""
        picked = await self.pick()
        wanted = {(c.id, s) for c, s in picked}
        if wanted != self._wanted:
            log.info("space media member pull: %d pull loop(s)", len(wanted))
        self._wanted = wanted
        for conn, space_id in picked:
            key = (conn.id, space_id)
            task = self._loops.get(key)
            if task is None or task.done():
                self._loops[key] = asyncio.create_task(self._loop(conn, space_id))

    async def pick(self) -> list[tuple[GfsConnection, str]]:
        """At most :data:`MAX_PULL_LOOPS` of :meth:`eligible`, at random."""
        pairs = await self.eligible()
        if len(pairs) <= MAX_PULL_LOOPS:
            return pairs
        return self._rng.sample(pairs, MAX_PULL_LOOPS)

    async def eligible(self) -> list[tuple[GfsConnection, str]]:
        """Every (GFS, space) pair this household may pull for (module
        docstring)."""
        own = self._federation.own_instance_id if self._federation else ""
        if not own:
            return []
        pairs: list[tuple[GfsConnection, str]] = []
        for space in await self._spaces.list_all():
            if space.owner_instance_id == own:
                continue
            if await self._writer_seed(space.id) is None:
                continue
            targets = await self._gfs.server_targets(space.id)
            for conn in targets or ():
                if not conn.registered or conn.status != "active":
                    continue
                if await self._gfs.space_media_member_pull_hold(conn) is None:
                    continue
                pairs.append((conn, space.id))
        return pairs

    async def _writer_seed(self, space_id: str) -> tuple[int, bytes] | None:
        epoch = await self._crypto.get_current_epoch(space_id)
        if epoch is None:
            return None
        seed = await self._certs.own_writer_key(space_id, epoch)
        return (epoch, seed) if seed is not None else None

    async def _loop(self, conn: GfsConnection, space_id: str) -> None:
        key = (conn.id, space_id)
        try:
            while not self._stop.is_set() and key in self._wanted:
                wait = self._pace(conn.id)
                if wait > 0:
                    # Over this GFS's pull budget: wait, then try for a slot
                    # again (another loop may take it first).
                    if await self._wait(wait):
                        return
                    continue
                try:
                    delay = await self.pull_once(conn, space_id)
                except Exception:  # pragma: no cover - defensive
                    log.warning("space media member pull failed", exc_info=True)
                    delay = self._rng.uniform(*BACKOFF_REFUSED_S)
                if delay > 0 and await self._wait(delay):
                    return
        finally:
            if self._loops.get(key) is asyncio.current_task():
                self._loops.pop(key, None)

    def _pace(self, gfs_id: str) -> float:
        """Seconds until a pull to *gfs_id* fits in
        :data:`MAX_PULLS_PER_MINUTE` (``0``: now — and it is counted)."""
        now = time.monotonic()
        times = self._pulled.setdefault(gfs_id, deque())
        while times and times[0] <= now - 60.0:
            times.popleft()
        if len(times) >= MAX_PULLS_PER_MINUTE:
            return times[0] + 60.0 - now
        times.append(now)
        return 0.0

    async def pull_once(self, conn: GfsConnection, space_id: str) -> float:
        """One anonymous pull for *space_id* on *conn*'s GFS (and the answer
        to the want it returns); the seconds to wait before the next."""
        writer = await self._writer_seed(space_id)
        hold = await self._gfs.space_media_member_pull_hold(conn)
        if writer is None or hold is None:
            return self._rng.uniform(*BACKOFF_REFUSED_S)
        epoch, seed = writer
        unsigned = MemberPullRequest(
            scope=SCOPE_SPACE,
            scope_id=space_id,
            epoch=epoch,
            gfs_instance_id=conn.gfs_instance_id,
            gfs_key=self._gfs.addressee_key_for(conn),
            ts=int(time.time()),
            nonce=secrets.token_hex(16),
            sig="",
            sig_suite=WRITER_KEY_SUITE_ED25519,
        )
        req = replace(
            unsigned, sig=sign_with_writer_key(seed, unsigned.signing_bytes())
        )
        status, wants = await self._gfs.pull_space_media_wants(
            conn, req.to_wire(), timeout_s=hold + PULL_TIMEOUT_PAD_S
        )
        if status == 200:
            for raw in wants[:1]:
                try:
                    want = MemberWant.from_wire(raw)
                except InvalidOnDemandWire as exc:
                    log.info("space media member want dropped: %s", exc)
                    continue
                await self._responder.answer_member_want(
                    want, conn, space_id=space_id, epoch=epoch, writer_seed=seed
                )
            return 0.0
        if status == 204:
            return self._rng.uniform(*IDLE_JITTER_S)
        if status in (429, 503):
            return self._rng.uniform(*BACKOFF_FULL_S)
        log.info(
            "space media member pull for %s at GFS %s: HTTP %s — backing off",
            space_id,
            conn.id,
            status,
        )
        return self._rng.uniform(*BACKOFF_REFUSED_S)


__all__ = [
    "BACKOFF_FULL_S",
    "BACKOFF_REFUSED_S",
    "IDLE_JITTER_S",
    "MAX_PULLS_PER_MINUTE",
    "MAX_PULL_LOOPS",
    "PULL_TIMEOUT_PAD_S",
    "REFRESH_S",
    "START_DELAY_S",
    "SpaceMediaGfsMemberPuller",
]
