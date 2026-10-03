"""Trusted-mode member publish (v_49) — ``POST /gfs/member-publish``.

A space member household publishes its own item over this connection server
with no host signature: it proves its GFS identity (household-signed request,
the same scheme as subscribe / unsubscribe) and carries a writer cert the
space AUTHORITY key signed for it. The wire shape and its codec live in
:mod:`socialhome.domain.gfs_member_publish`.

**What this server learns** — the owner-decided trusted-mode trade-off: which
registered household published into which listed space, at which content
epoch, and when. **What it never learns**: the content or the real item type.
``payload`` is ciphertext under the space content key and the outer event type
is always ``space_item``. That also means the scope this server can check is
only the weakest one (``comment``): it cannot tell a post (needs ``write``)
from a comment. RECEIVERS decrypt, read the real type and enforce the scope it
needs — a ``comment``-scoped follower that dresses a post up as a
``space_item`` is relayed here and dropped by every receiver.

Authorization, in order — every refusal is the same uniform ``403`` at the
route, with the reason at DEBUG:

1. the household signature verifies against the REGISTERED
   ``client_instances.public_key`` of ``instance_id``, its ``ts`` is within
   ±300 s, the instance is ``active`` (not pending, not banned), and the
   signed ``gfs_instance_id`` is THIS server's (no cross-server replay);
2. the per-(household, space) and per-space rate limits (``429``, after
   authentication only);
3. the space is published here, not moderator-banned, publicly readable
   (``allow_subscribers``), and has a pinned authority key;
4. :func:`~socialhome.writer_cert.verify_writer_cert` against that pinned key,
   for ``target`` and the request ``epoch``, with ``author_pk`` = the
   publishing household's REGISTERED identity key — so a household can only
   publish with a cert issued to itself;
5. epoch freshness against the newest content epoch this server has seen
   proven (:meth:`GfsMemberPublishService._check_cert_epoch`).

Then a content-blind replay guard (BLAKE2b digest of the whole signed body,
TTL'd like the ``ts`` window), and the item is handed to a background
fan-out (bounded workers and backlog; ``503`` when full) so the request
never waits on N subscribers. Every active subscriber except the publisher
gets ``{type: "relay", **frame}`` on its live socket; one without a socket
gets the frame queued for 24 h only if it was seen within that TTL, under
per-recipient and server-wide byte caps (``GfsEnvelopeRelay.fan_out_relay``)
— so self-subscribed sybils that never connect cost no disk. The frame never
names the publisher.

**Epoch freshness.** A writer cert binds one content epoch; receivers accept
only the newest epoch they hold (or the previous one for 10 minutes). This
server learns epochs only from authority-signed statements — a verified writer
cert, the plaintext ``epoch`` of an authority-signed ``space_post_public``
relay, and the authority-signed epoch NOTICE a seed holder posts to
``POST /gfs/spaces/{id}/epoch`` on rotation (:meth:`GfsMemberPublishService
.note_epoch`). Because ANY seed holder — a demoted one too, until the
authority re-pin — can sign those, no statement may inflate the epoch
without bound: a cert raises it by one step at most, and a notice or relay
no further than :func:`~.domain.epoch_ceiling` (``current + 1000`` or a day
past wall clock, which keeps the v_44 post-restore jump to unix seconds
working). The state is cleared whenever the space authority key is
re-pinned, so seed holders re-send the current notice right after one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
    UnsupportedAuthoritySuite,
    verify_authority_event,
)
from ..domain.gfs_member_publish import (
    MEMBER_PUBLISH_EPOCH_GRACE_S,
    MEMBER_PUBLISH_ROUTE,
    MemberPublishRequest,
)
from ..domain.writer_cert import MAX_WRITER_CERT_EPOCH, WRITER_SCOPE_COMMENT
from ..writer_cert import (
    InvalidWriterCert,
    UnsupportedWriterCertSuite,
    verify_writer_cert,
)
from .domain import epoch_ceiling
from .envelope_relay import ENVELOPE_QUEUE_TTL_SECONDS
from .federation import SeenPayloadCache
from .public import ClientIpResolver, SlidingWindowCounter, build_window_limiter


if TYPE_CHECKING:
    from .domain import GlobalSpace
    from .envelope_relay import GfsEnvelopeRelay
    from .federation import GfsFederationService
    from .repositories import AbstractGfsFederationRepo, AbstractGfsSpaceEpochRepo

log = logging.getLogger(__name__)

#: Accepted member publishes per (household, space) per minute. A household
#: posting by hand stays orders of magnitude below; it bounds what one
#: compromised or buggy member can push into every subscriber's socket/queue.
MEMBER_PUBLISH_MAX_PER_MINUTE: int = 30

#: Accepted member publishes per SPACE per minute, across every household. The
#: per-household limit alone lets N writer households multiply it by N; this
#: bounds the fan-out work (and queue writes) one space can cause.
MEMBER_PUBLISH_MAX_PER_MINUTE_PER_SPACE: int = 120

#: Per-IP/minute cap on the member-publish and epoch-notice routes, applied
#: BEFORE any signature work (the per-household limit needs a verified
#: identity first).
MEMBER_PUBLISH_MAX_PER_MINUTE_PER_IP: int = 120

#: Route of the authority-signed content-epoch notice.
EPOCH_NOTICE_ROUTE: str = "/gfs/spaces/{space_id}/epoch"

#: Background fan-out workers, and how many accepted items may wait for them.
#: Past the backlog the route answers 503 (the household's retry queue backs
#: off) instead of holding the request open.
FAN_OUT_WORKERS: int = 2
FAN_OUT_BACKLOG: int = 256

#: How often an idle worker re-checks the stop event (seconds).
FAN_OUT_IDLE_POLL_S: float = 0.5


class MemberPublishRateLimited(Exception):
    """The household (or the space as a whole) exceeded its per-minute
    budget. Raised only after the household signature verified."""


class MemberPublishBusy(Exception):
    """The background fan-out is stopped or its backlog is full."""


@dataclass(slots=True, frozen=True)
class _FanOutJob:
    """One accepted item waiting for the background fan-out."""

    space_id: str
    publisher_id: str
    frame: dict


class GfsMemberPublishService:
    """Authorize trusted-mode member publishes, fan them out in the
    background, and track the proven content epoch.

    Lifecycle follows the CLAUDE.md scheduler pattern: :meth:`start` spawns
    :data:`FAN_OUT_WORKERS` workers that loop ``while not self._stop
    .is_set()``; :meth:`stop` sets the event and waits for them, so a worker
    finishes the item it is on instead of being cancelled mid-write."""

    __slots__ = (
        "_clock",
        "_epoch_repo",
        "_fed_repo",
        "_federation",
        "_gfs_instance_id",
        "_limiter",
        "_queue",
        "_relay",
        "_seen",
        "_space_limiter",
        "_stop",
        "_tasks",
    )

    def __init__(
        self,
        *,
        federation: "GfsFederationService",
        fed_repo: "AbstractGfsFederationRepo",
        epoch_repo: "AbstractGfsSpaceEpochRepo",
        relay: "GfsEnvelopeRelay",
        gfs_instance_id: str,
        limiter: SlidingWindowCounter | None = None,
        space_limiter: SlidingWindowCounter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._federation = federation
        self._fed_repo = fed_repo
        self._epoch_repo = epoch_repo
        self._relay = relay
        self._gfs_instance_id = gfs_instance_id
        self._limiter = limiter or SlidingWindowCounter(MEMBER_PUBLISH_MAX_PER_MINUTE)
        self._space_limiter = space_limiter or SlidingWindowCounter(
            MEMBER_PUBLISH_MAX_PER_MINUTE_PER_SPACE
        )
        self._seen = SeenPayloadCache()
        self._clock = clock
        self._stop = asyncio.Event()
        self._queue: asyncio.Queue[_FanOutJob] = asyncio.Queue(maxsize=FAN_OUT_BACKLOG)
        self._tasks: list[asyncio.Task[None]] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._worker(), name=f"gfs-member-fanout-{i}")
            for i in range(FAN_OUT_WORKERS)
        ]

    async def stop(self) -> None:
        """Stop the workers after their current item. Items still waiting
        are dropped (logged): their subscribers catch up through space sync,
        and the households' own retries re-deliver what was not yet fanned
        out."""
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        dropped = 0
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            dropped += 1
        if dropped:
            log.info("gfs.member_publish: dropped %d unsent item(s) on stop", dropped)

    async def wait_idle(self) -> None:
        """Wait until every accepted item has been fanned out (tests, and a
        graceful drain)."""
        await self._queue.join()

    def _submit(self, job: _FanOutJob) -> bool:
        if self._stop.is_set() or not self._tasks:
            return False
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            return False
        return True

    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = await asyncio.wait_for(
                    self._queue.get(), timeout=FAN_OUT_IDLE_POLL_S
                )
            except TimeoutError:
                continue
            try:
                await self._fan_out(job)
            except Exception:
                log.exception("gfs.member_publish: fan-out failed")
            finally:
                self._queue.task_done()

    async def _fan_out(self, job: _FanOutJob) -> None:
        subscribers = await self._fed_repo.list_subscribers(job.space_id)
        # Never echo an item back to its publisher.
        targets = [
            s.instance_id for s in subscribers if s.instance_id != job.publisher_id
        ]
        if not targets:
            return
        # Offline copies only for subscribers seen within the queue TTL — a
        # household that never connected (or vanished long ago) would only
        # hold rows until they expire.
        recent = await self._fed_repo.list_recently_seen_subscribers(
            job.space_id, within_s=ENVELOPE_QUEUE_TTL_SECONDS
        )
        reached = await self._relay.fan_out_relay(
            targets, queue_ok=recent, frame=job.frame
        )
        log.debug(
            "gfs.member_publish: space=%s reached=%d of %d",
            job.space_id,
            reached,
            len(targets),
        )

    # ── Publish ───────────────────────────────────────────────────────────

    async def publish(self, req: MemberPublishRequest) -> bool:
        """Authorize ``req`` and hand it to the background fan-out. Returns
        ``False`` for a suppressed replay (nothing new to send).

        Raises :class:`PermissionError` on any authorization failure,
        :class:`MemberPublishRateLimited` past a per-minute budget and
        :class:`MemberPublishBusy` when the fan-out cannot take it."""
        inst = await self._federation.verify_signed_request(
            req.instance_id, req.signing_payload(), signature=req.signature
        )
        if inst.status != "active":
            raise PermissionError("instance is not active")
        if req.gfs_instance_id != self._gfs_instance_id:
            # Signed for another connection server: never replayable here.
            raise PermissionError("request is addressed to another server")
        if not self._limiter.allow(f"{inst.instance_id}\x00{req.target}"):
            raise MemberPublishRateLimited()
        if not self._space_limiter.allow(req.target):
            raise MemberPublishRateLimited()
        space = await self._readable_space(req.target)
        try:
            space_pk = bytes.fromhex(space.identity_public_key)
            author_pk = bytes.fromhex(inst.public_key)
            verify_writer_cert(
                req.writer_cert,
                space_pubkey=space_pk,
                space_id=req.target,
                epoch=req.epoch,
                author_pk=author_pk,
                # The real item type is hidden inside the ciphertext, so the
                # weakest scope is all this server can require; receivers
                # enforce the scope the real type needs.
                required_scope=WRITER_SCOPE_COMMENT,
            )
        except (InvalidWriterCert, UnsupportedWriterCertSuite, ValueError) as exc:
            raise PermissionError(f"writer cert refused: {exc}") from exc
        await self._check_cert_epoch(req.target, req.epoch)

        # Replay guard AFTER authorization (a refused body must never mute a
        # legitimate one). The digest covers the signature, so it is exactly
        # the captured request; the TTL matches the ±300 s ``ts`` window.
        digest = SeenPayloadCache.digest(req.to_wire())
        if self._seen.seen(digest):
            log.debug("gfs.member_publish: suppressing a replayed request")
            return False
        job = _FanOutJob(
            space_id=req.target,
            publisher_id=inst.instance_id,
            frame=req.fan_out_frame(),
        )
        if not self._submit(job):
            raise MemberPublishBusy()
        self._seen.record(digest)
        return True

    async def _check_cert_epoch(self, space_id: str, epoch: int) -> None:
        """Epoch freshness for a verified writer cert, and the +1 rule.

        A cert is the authority's statement that its epoch exists, but any
        seed holder (a demoted one too, until the re-pin) can issue one — so
        a cert may raise the proven epoch by ONE step only, and a first cert
        no further than :func:`epoch_ceiling`. Bigger jumps (a v_44 restore)
        come from the epoch notice or a host relay."""
        now = int(self._clock())
        state = await self._epoch_repo.get(space_id)
        if state is None:
            if epoch > epoch_ceiling(None, now):
                raise PermissionError("writer cert epoch is implausibly far ahead")
            await self._epoch_repo.advance(space_id, epoch, seen_at=now)
            return
        if epoch > state.current + 1:
            raise PermissionError("writer cert epoch skips ahead of the proven one")
        if not state.admits(epoch, now=now, grace_s=MEMBER_PUBLISH_EPOCH_GRACE_S):
            raise PermissionError("writer cert epoch is stale")
        if epoch > state.current:
            await self._epoch_repo.advance(space_id, epoch, seen_at=now)

    # ── Epoch notice ──────────────────────────────────────────────────────

    async def note_epoch(
        self,
        space_id: str,
        epoch: object,
        authority_sig: str,
        authority_sig_suite: str,
    ) -> None:
        """Record an authority-signed content-epoch notice (monotonic).

        The notice is ``{space_id, epoch}`` signed with the space seed under
        :data:`AUTHORITY_EVENT_SPACE_EPOCH_NOTICE`. It carries no timestamp on
        purpose: replaying one can never move the epoch backwards. A raise
        past :func:`epoch_ceiling` is refused. Raises :class:`PermissionError`
        on any refusal."""
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or not 0 <= epoch <= MAX_WRITER_CERT_EPOCH
        ):
            raise PermissionError("invalid epoch")
        space = await self._fed_repo.get_space(space_id)
        if space is None or space.status == "banned":
            raise PermissionError("space not published or banned")
        if not space.identity_public_key:
            raise PermissionError("no pinned authority key for this space")
        try:
            ok = verify_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
                space_id=space_id,
                payload={"space_id": space_id, "epoch": epoch},
                authority_sig=authority_sig,
                authority_sig_suite=authority_sig_suite,
                space_public_key=bytes.fromhex(space.identity_public_key),
            )
        except (UnsupportedAuthoritySuite, ValueError) as exc:
            raise PermissionError(f"epoch notice refused: {exc}") from exc
        if not ok:
            raise PermissionError("invalid authority signature")
        now = int(self._clock())
        state = await self._epoch_repo.get(space_id)
        if epoch > epoch_ceiling(state.current if state else None, now):
            raise PermissionError("epoch notice is implausibly far ahead")
        await self._epoch_repo.advance(space_id, epoch, seen_at=now)

    async def _readable_space(self, space_id: str) -> "GlobalSpace":
        space = await self._fed_repo.get_space(space_id)
        if space is None:
            raise PermissionError("space not published")
        if space.status == "banned":
            raise PermissionError("space is banned")
        if not space.allow_subscribers:
            # Listed for discovery but not publicly readable: no readership,
            # and its owner relays no content here either.
            raise PermissionError("space is not publicly readable")
        if not space.identity_public_key:
            raise PermissionError("no pinned authority key for this space")
        return space


def _is_member_publish_path(path: str) -> bool:
    return path == MEMBER_PUBLISH_ROUTE or (
        path.startswith("/gfs/spaces/") and path.endswith("/epoch")
    )


def build_member_publish_rate_limit(resolver: ClientIpResolver):
    """Per-IP limiter for ``POST /gfs/member-publish`` and the epoch notice,
    shedding a flood before any signature verification."""
    return build_window_limiter(
        resolver,
        MEMBER_PUBLISH_MAX_PER_MINUTE_PER_IP,
        _is_member_publish_path,
    )
