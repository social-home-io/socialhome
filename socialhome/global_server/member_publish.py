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
only the newest epoch they hold (or the previous one for 10 minutes). The GFS
keeps two tiers (:class:`~.domain.GfsSpaceEpoch`), because it can tell the
space OWNER apart (its registered household key) but not a legitimate
delegated admin from a demoted one whose seed still matches until the
re-pin:

* the **confirmed** epoch moves only on the OWNER's household-signed notice
  (``POST /gfs/spaces/{id}/epoch`` with ``owning_instance``), by any amount
  up to :func:`~.domain.epoch_ceiling` (the v_44 post-restore jump to unix
  seconds lands); the confirmed epoch before it stays open for the grace;
* the **current** epoch is raised by seed-only statements — a delegated
  admin's authority-signed notice, the plaintext ``epoch`` of an authorized
  ``space_post_public`` relay — by exactly +1, at most once a minute;
* writer certs never raise anything.

A cert is admitted from ``confirmed`` up to ``current + 1``, or back to the
previous confirmed epoch during the grace. So no seed holder can lock writers
out: seed-only raises never move the floor. The cost is stated plainly: a
writer removed by a DELEGATED ADMIN's rotation stays relayable here (receivers
still drop it) until the owner confirms the new epoch, which a household does
on its next GFS connection. The state is cleared whenever the space authority
key is re-pinned, so the owner re-sends the current notice right after one.
"""

from __future__ import annotations

import asyncio
import hashlib
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
    owner_epoch_notice_signing_payload,
)
from ..domain.writer_cert import MAX_WRITER_CERT_EPOCH, WRITER_SCOPE_COMMENT
from ..writer_cert import (
    InvalidWriterCert,
    UnsupportedWriterCertSuite,
    verify_writer_cert,
)
from .domain import MIN_EPOCH_STEP_INTERVAL_S, epoch_ceiling
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

#: Background fan-out workers. Each space is pinned to one worker (by a hash
#: of its id), so one space's items go out in publish order.
FAN_OUT_WORKERS: int = 2

#: Accepted items one worker may hold, and the share of that one SPACE may
#: take. Past either the route answers 503 (the household's retry queue
#: backs off) — so one busy space can't refuse every other space.
FAN_OUT_BACKLOG: int = 128
FAN_OUT_BACKLOG_PER_SPACE: int = 16

#: How long :meth:`GfsMemberPublishService.stop` waits for accepted items to
#: go out before giving up on the rest (seconds).
FAN_OUT_STOP_DRAIN_S: float = 5.0

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
    background, and track the space content epoch.

    Lifecycle follows the CLAUDE.md scheduler pattern: :meth:`start` spawns
    :data:`FAN_OUT_WORKERS` workers that loop ``while not self._stop
    .is_set()``; :meth:`stop` first stops accepting (``_closing``), lets the
    workers drain what was accepted (bounded by :data:`FAN_OUT_STOP_DRAIN_S`),
    then sets ``_stop`` and waits for them — a worker always finishes the
    item it is on instead of being cancelled mid-write."""

    __slots__ = (
        "_clock",
        "_closing",
        "_epoch_repo",
        "_fed_repo",
        "_federation",
        "_gfs_instance_id",
        "_limiter",
        "_pending",
        "_queues",
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
        self._closing = asyncio.Event()
        self._queues: list[asyncio.Queue[_FanOutJob]] = [
            asyncio.Queue(maxsize=FAN_OUT_BACKLOG) for _ in range(FAN_OUT_WORKERS)
        ]
        #: Accepted-but-not-yet-sent items per space (the per-space share).
        self._pending: dict[str, int] = {}
        self._tasks: list[asyncio.Task[None]] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._closing.clear()
        self._tasks = [
            asyncio.create_task(self._worker(q), name=f"gfs-member-fanout-{i}")
            for i, q in enumerate(self._queues)
        ]

    async def stop(self) -> None:
        """Stop accepting, drain accepted items for up to
        :data:`FAN_OUT_STOP_DRAIN_S`, then stop the workers. Items that
        still could not go out are counted at WARNING (their subscribers
        catch up through space sync)."""
        self._closing.set()
        tasks, self._tasks = self._tasks, []
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(q.join() for q in self._queues)),
                    timeout=FAN_OUT_STOP_DRAIN_S,
                )
            except TimeoutError:
                pass
        self._stop.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        dropped = 0
        for q in self._queues:
            while not q.empty():
                q.get_nowait()
                q.task_done()
                dropped += 1
        self._pending.clear()
        if dropped:
            log.warning(
                "gfs.member_publish: %d accepted item(s) not fanned out before "
                "shutdown — their subscribers catch up through space sync",
                dropped,
            )

    async def wait_idle(self) -> None:
        """Wait until every accepted item has been fanned out (tests, and a
        graceful drain)."""
        await asyncio.gather(*(q.join() for q in self._queues))

    @staticmethod
    def _worker_index(space_id: str) -> int:
        digest = hashlib.blake2b(space_id.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % FAN_OUT_WORKERS

    def _submit(self, job: _FanOutJob) -> bool:
        if self._closing.is_set() or not self._tasks:
            return False
        if self._pending.get(job.space_id, 0) >= FAN_OUT_BACKLOG_PER_SPACE:
            return False
        try:
            self._queues[self._worker_index(job.space_id)].put_nowait(job)
        except asyncio.QueueFull:
            return False
        self._pending[job.space_id] = self._pending.get(job.space_id, 0) + 1
        return True

    async def _worker(self, queue: "asyncio.Queue[_FanOutJob]") -> None:
        while not self._stop.is_set():
            try:
                job = await asyncio.wait_for(queue.get(), timeout=FAN_OUT_IDLE_POLL_S)
            except TimeoutError:
                continue
            try:
                await self._fan_out(job)
            except Exception:
                log.exception("gfs.member_publish: fan-out failed")
            finally:
                left = self._pending.get(job.space_id, 1) - 1
                if left > 0:
                    self._pending[job.space_id] = left
                else:
                    self._pending.pop(job.space_id, None)
                queue.task_done()

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
        """Epoch freshness for a verified writer cert. A cert never raises
        the stored epoch: any seed holder (a demoted one too, until the
        re-pin) can issue one, so letting certs raise would let them strand
        the writers on the real epoch. Without any owner-confirmed epoch yet
        the cert passes (receivers enforce freshness exactly)."""
        state = await self._epoch_repo.get(space_id)
        if state is None:
            return
        if not state.admits(
            epoch, now=int(self._clock()), grace_s=MEMBER_PUBLISH_EPOCH_GRACE_S
        ):
            raise PermissionError("writer cert epoch is not open here")

    # ── Epoch notices ─────────────────────────────────────────────────────

    async def note_owner_epoch(
        self,
        space_id: str,
        *,
        owning_instance: str,
        gfs_instance_id: str,
        epoch: object,
        ts: str,
        signature: str,
    ) -> None:
        """The space OWNER's household-signed epoch notice: confirms
        ``epoch`` (any raise up to :func:`epoch_ceiling`). Signed over
        :func:`owner_epoch_notice_signing_payload` with ``ts`` (±300 s) and
        this server's id, verified against the owner's REGISTERED key; the
        signer must be the space's ``owning_instance``. Raises
        :class:`PermissionError` on any refusal."""
        epoch_int = _valid_epoch(epoch)
        if gfs_instance_id != self._gfs_instance_id:
            raise PermissionError("notice is addressed to another server")
        await self._federation.verify_signed_request(
            owning_instance,
            owner_epoch_notice_signing_payload(
                owning_instance=owning_instance,
                gfs_instance_id=gfs_instance_id,
                space_id=space_id,
                epoch=epoch_int,
                ts=ts,
            ),
            signature=signature,
        )
        space = await self._fed_repo.get_space(space_id)
        if space is None or space.status == "banned":
            raise PermissionError("space not published or banned")
        if space.owning_instance != owning_instance:
            raise PermissionError("only the space owner may confirm an epoch")
        now = int(self._clock())
        state = await self._epoch_repo.get(space_id)
        if epoch_int > epoch_ceiling(state.confirmed if state else None, now):
            raise PermissionError("epoch notice is implausibly far ahead")
        await self._epoch_repo.confirm(space_id, epoch_int, now=now)

    async def note_epoch(
        self,
        space_id: str,
        epoch: object,
        authority_sig: str,
        authority_sig_suite: str,
    ) -> None:
        """A seed-only (space-authority-signed) epoch notice — what a
        delegated admin sends. It may raise ``current`` by exactly +1, at
        most once a minute, and never the owner-confirmed floor; anything
        else is a 200 that changes nothing (a replay, a delegated rotation
        racing another). Raises :class:`PermissionError` on a bad signature."""
        epoch_int = _valid_epoch(epoch)
        space = await self._fed_repo.get_space(space_id)
        if space is None or space.status == "banned":
            raise PermissionError("space not published or banned")
        if not space.identity_public_key:
            raise PermissionError("no pinned authority key for this space")
        try:
            ok = verify_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
                space_id=space_id,
                payload={"space_id": space_id, "epoch": epoch_int},
                authority_sig=authority_sig,
                authority_sig_suite=authority_sig_suite,
                space_public_key=bytes.fromhex(space.identity_public_key),
            )
        except (UnsupportedAuthoritySuite, ValueError) as exc:
            raise PermissionError(f"epoch notice refused: {exc}") from exc
        if not ok:
            raise PermissionError("invalid authority signature")
        await self._epoch_repo.step(
            space_id,
            epoch_int,
            now=int(self._clock()),
            min_interval_s=MIN_EPOCH_STEP_INTERVAL_S,
        )

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


def _valid_epoch(epoch: object) -> int:
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or not 0 <= epoch <= MAX_WRITER_CERT_EPOCH
    ):
        raise PermissionError("invalid epoch")
    return epoch


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
