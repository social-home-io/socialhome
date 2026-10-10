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

**Strict mode (v_50)** — ``POST /gfs/member-publish-anon``
(:meth:`GfsMemberPublishService.publish_anon`). The request names no
household: no ``instance_id``, no household signature, no plaintext writer
cert (the cert rides inside the ciphertext). It is authorized by
``writer_sig``, made with the space's per-epoch writer GROUP key — shared by
every household allowed to publish anything there — and verified against the
key the space AUTHORITY pinned for that epoch with a ``writer_key_cert``
(:mod:`socialhome.writer_key`). This server learns only that *some* publisher
of the space posted, at which epoch, when, and in which size bucket.

* **Pinning.** A ``writer_key_cert`` rides the epoch notice — no new endpoint.
  The OWNER's notice pins (or corrects) the key of the epoch it confirms. A
  delegated admin's seed-only notice pins only an epoch that the +1 rule
  already let ``current`` reach (``confirmed <= epoch <= current``), only
  above the newest pin, and never over an existing pin — so a seed holder
  cannot pin a key for an inflated epoch, nor swap the owner's. The current
  and previous pins are kept; the previous one rides the same epoch grace as
  certs (:meth:`~.domain.GfsSpaceEpoch.admits`). Pins are forgotten on an
  authority re-pin.
* **Checks**, in order (every refusal the same ``403``): addressed to this
  server; ``ts`` within ±300 s; the space is listed, not banned, publicly
  readable and pinned; a writer key is pinned for ``epoch``; ``writer_sig``
  verifies under it (suite checked, never defaulted); the per-(space,
  client address), per-space and per-writer-key rate limits (``429``, after
  the signature — there is no household identity to limit by, so the
  address is what keeps one key holder from starving the others); epoch
  freshness; a replay guard over
  the whole signed body (``nonce`` + ``ts`` make every legitimate attempt
  unique, so an exact copy is refused).
* **Mode enforcement.** The owner's notice carries the space's
  ``publish_mode``. In a ``strict`` space :meth:`publish` (identified)
  refuses every request, so an older v_49 household or a misconfigured one
  cannot publish identified into it; its post takes the host path. In a
  ``trusted`` space both endpoints are accepted: the anonymous one only where
  a writer key is pinned, and it never reveals more than the identified one —
  so a member that learned of a switch before this server did is not
  refused in either direction.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
    UnsupportedAuthoritySuite,
    verify_authority_event,
)
from ..crypto import b64url_decode
from ..domain.gfs_member_publish import (
    GFS_PUBLISH_MODES,
    MEMBER_PUBLISH_ANON_ROUTE,
    MEMBER_PUBLISH_EPOCH_GRACE_S,
    MEMBER_PUBLISH_ROUTE,
    MemberPublishAnonRequest,
    MemberPublishRequest,
    owner_epoch_notice_signing_payload,
)
from ..domain.writer_cert import MAX_WRITER_CERT_EPOCH, WRITER_SCOPE_COMMENT
from ..domain.writer_key import UnsupportedWriterKeySuite, WriterKeyCert
from ..writer_cert import (
    InvalidWriterCert,
    UnsupportedWriterCertSuite,
    verify_writer_cert,
)
from ..writer_key import InvalidWriterKey, verify_writer_key_cert, verify_writer_sig
from .addressee import GfsAddressee
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

#: Accepted anonymous (strict-mode) publishes per WRITER KEY per minute. The
#: key is shared by every publisher of the space at one epoch, so this bounds
#: the space as a whole, not any one household. Rotating the key does NOT stop
#: an abusive key holder: the rotation hands it the new key too.
MEMBER_PUBLISH_MAX_PER_MINUTE_PER_WRITER_KEY: int = 120

#: Accepted anonymous publishes per (space, client IP address) per minute.
#: Without a household identity, the address is the only thing that tells one
#: key holder's flood apart from the other writers' posts — so one household
#: can't burn the space-wide budget and starve everyone else. Well under the
#: per-space limit. IP correlation is already the stated residual of every
#: anonymous GFS path; the key lives only in this process's sliding window.
MEMBER_PUBLISH_ANON_MAX_PER_MINUTE_PER_SPACE_IP: int = 30

#: How far an anonymous request's ``ts`` may be from this server's clock
#: (seconds) — the same window as every signed household request.
ANON_TS_SKEW_S: int = 300

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


#: Resolves a fan-out job's targets in the worker: ``(targets, queue_ok)`` —
#: the households to push to, and the ones that may get an offline copy.
FanOutResolver = Callable[[], Awaitable[tuple[list[str], set[str]]]]


@dataclass(slots=True, frozen=True)
class _FanOutJob:
    """One accepted item waiting for the background fan-out. ``space_id`` is
    the fairness / ordering key (a space id, or ``"ch:" + channel_id`` for a
    private channel, v_51); ``resolve`` replaces the space subscriber lookup
    for a job that brings its own (a channel's seats)."""

    space_id: str
    publisher_id: str
    frame: dict
    resolve: FanOutResolver | None = None


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
        "_addressee",
        "_limiter",
        "_pending",
        "_queues",
        "_anon_ip_limiter",
        "_anon_seen",
        "_relay",
        "_seen",
        "_space_limiter",
        "_stop",
        "_tasks",
        "_writer_key_limiter",
    )

    def __init__(
        self,
        *,
        federation: "GfsFederationService",
        fed_repo: "AbstractGfsFederationRepo",
        epoch_repo: "AbstractGfsSpaceEpochRepo",
        relay: "GfsEnvelopeRelay",
        gfs_instance_id: str,
        addressee: GfsAddressee | None = None,
        limiter: SlidingWindowCounter | None = None,
        space_limiter: SlidingWindowCounter | None = None,
        writer_key_limiter: SlidingWindowCounter | None = None,
        anon_ip_limiter: SlidingWindowCounter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._federation = federation
        self._fed_repo = fed_repo
        self._epoch_repo = epoch_repo
        self._relay = relay
        #: This server's public id + its transitional aliases (see
        #: :mod:`.addressee`) — what a household-signed request must name.
        self._addressee = addressee or GfsAddressee(gfs_instance_id)
        self._limiter = limiter or SlidingWindowCounter(MEMBER_PUBLISH_MAX_PER_MINUTE)
        self._space_limiter = space_limiter or SlidingWindowCounter(
            MEMBER_PUBLISH_MAX_PER_MINUTE_PER_SPACE
        )
        self._writer_key_limiter = writer_key_limiter or SlidingWindowCounter(
            MEMBER_PUBLISH_MAX_PER_MINUTE_PER_WRITER_KEY
        )
        self._anon_ip_limiter = anon_ip_limiter or SlidingWindowCounter(
            MEMBER_PUBLISH_ANON_MAX_PER_MINUTE_PER_SPACE_IP
        )
        self._seen = SeenPayloadCache()
        #: Anonymous requests' replay guard. A ``ts`` up to ±300 s off is
        #: accepted, so a body stays valid for 600 s — the guard must
        #: remember it at least that long.
        self._anon_seen = SeenPayloadCache(ttl_s=float(2 * ANON_TS_SKEW_S))
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

    def submit_fan_out(
        self, key: str, *, publisher_id: str, frame: dict, resolve: FanOutResolver
    ) -> bool:
        """Hand an accepted item with its own target resolver (a private
        channel's seats, v_51) to the SAME bounded workers, backlog and
        per-key share as member publishes. ``False`` when stopped or full."""
        return self._submit(
            _FanOutJob(
                space_id=key, publisher_id=publisher_id, frame=frame, resolve=resolve
            )
        )

    async def _fan_out(self, job: _FanOutJob) -> None:
        if job.resolve is not None:
            resolved, queue_ok = await job.resolve()
            targets = [t for t in resolved if t != job.publisher_id]
            if targets:
                reached = await self._relay.fan_out_relay(
                    targets, queue_ok=queue_ok, frame=job.frame
                )
                log.debug(
                    "gfs.channel: fan-out reached=%d of %d", reached, len(targets)
                )
            return
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
        if not self._addressee.accepts(req.gfs_instance_id, req.gfs_key):
            # Signed for another connection server: never replayable here.
            raise PermissionError("request is addressed to another server")
        if not self._limiter.allow(f"{inst.instance_id}\x00{req.target}"):
            raise MemberPublishRateLimited()
        if not self._space_limiter.allow(req.target):
            raise MemberPublishRateLimited()
        space = await self._readable_space(req.target)
        strict = await self._epoch_repo.get_strict(req.target)
        if strict is not None and strict.strict:
            # The owner chose strict mode: nothing identified is relayed for
            # this space — an older or misconfigured household's post takes
            # the host path instead.
            raise PermissionError("space is in strict mode — identified refused")
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

    async def publish_anon(
        self, req: MemberPublishAnonRequest, *, client_ip: str
    ) -> None:
        """Authorize a strict-mode (anonymous) ``req`` and hand it to the
        background fan-out — see the module docstring for the checks.

        Raises :class:`PermissionError` on any refusal (a replay included),
        :class:`MemberPublishRateLimited` past a per-minute budget and
        :class:`MemberPublishBusy` when the fan-out cannot take it."""
        if not self._addressee.accepts(req.gfs_instance_id, req.gfs_key):
            raise PermissionError("request is addressed to another server")
        self._check_anon_ts(req.ts)
        await self._readable_space(req.target)
        strict = await self._epoch_repo.get_strict(req.target)
        pinned = strict.writer_pk_for(req.epoch) if strict is not None else None
        if pinned is None:
            raise PermissionError("no writer key pinned for this epoch")
        try:
            ok = verify_writer_sig(
                writer_pk=b64url_decode(pinned),
                message=req.signing_bytes(),
                writer_sig=req.writer_sig,
                suite=req.writer_sig_suite,
            )
        except (UnsupportedWriterKeySuite, ValueError) as exc:
            raise PermissionError(f"writer signature refused: {exc}") from exc
        if not ok:
            raise PermissionError("writer signature does not verify")
        # The per-address bound first: an abusive key holder is stopped here
        # and never reaches the space-wide budget the other writers share.
        if not self._anon_ip_limiter.allow(f"{req.target}\x00{client_ip}"):
            raise MemberPublishRateLimited()
        if not self._space_limiter.allow(req.target):
            raise MemberPublishRateLimited()
        if not self._writer_key_limiter.allow(f"{req.target}\x00{req.epoch}"):
            raise MemberPublishRateLimited()
        state = await self._epoch_repo.get(req.target)
        if state is None or not state.admits(
            req.epoch, now=int(self._clock()), grace_s=MEMBER_PUBLISH_EPOCH_GRACE_S
        ):
            raise PermissionError("writer key epoch is not open here")
        digest = SeenPayloadCache.digest(req.to_wire())
        if self._anon_seen.seen(digest):
            raise PermissionError("replayed request")
        # No publisher to exclude: the publisher's own echo comes back and is
        # dropped by the household (the origin is inside the ciphertext).
        job = _FanOutJob(
            space_id=req.target, publisher_id="", frame=req.fan_out_frame()
        )
        if not self._submit(job):
            raise MemberPublishBusy()
        self._anon_seen.record(digest)

    def _check_anon_ts(self, ts: str) -> None:
        try:
            parsed = datetime.fromisoformat(ts)
        except (TypeError, ValueError) as exc:
            raise PermissionError("bad ts") from exc
        if parsed.tzinfo is None:
            raise PermissionError("naive ts")
        if abs(parsed.timestamp() - self._clock()) > ANON_TS_SKEW_S:
            raise PermissionError("stale ts")

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
        publish_mode: object = None,
        writer_key_cert: object = None,
        gfs_key: object = None,
    ) -> None:
        """The space OWNER's household-signed epoch notice: confirms
        ``epoch`` (any raise up to :func:`epoch_ceiling`). Signed over
        :func:`owner_epoch_notice_signing_payload` with ``ts`` (±300 s) and
        this server's id, verified against the owner's REGISTERED key; the
        signer must be the space's ``owning_instance``.

        v_50, both optional and signed when present: ``publish_mode`` sets
        the space's mode (only forward in ``ts``); ``writer_key_cert`` pins
        the writer group key of ``epoch`` — when ``epoch`` is the confirmed
        one after this notice (a stale notice pins nothing) — replacing a
        delegated admin's pin of the same epoch. Everything is verified
        before anything is written. Raises :class:`PermissionError` on any
        refusal."""
        epoch_int = _valid_epoch(epoch)
        if gfs_key is not None and not isinstance(gfs_key, str):
            raise PermissionError("malformed gfs_key")
        if not self._addressee.accepts(gfs_instance_id, gfs_key):
            raise PermissionError("notice is addressed to another server")
        if publish_mode is not None and publish_mode not in GFS_PUBLISH_MODES:
            raise PermissionError("unknown publish mode")
        if writer_key_cert is not None and not isinstance(writer_key_cert, dict):
            raise PermissionError("malformed writer key cert")
        mode = str(publish_mode) if publish_mode is not None else None
        wkc = writer_key_cert if isinstance(writer_key_cert, dict) else None
        await self._federation.verify_signed_request(
            owning_instance,
            owner_epoch_notice_signing_payload(
                owning_instance=owning_instance,
                gfs_instance_id=gfs_instance_id,
                space_id=space_id,
                epoch=epoch_int,
                ts=ts,
                publish_mode=mode,
                writer_key_cert=wkc,
                gfs_key=gfs_key,
            ),
            signature=signature,
        )
        space = await self._fed_repo.get_space(space_id)
        if space is None or space.status == "banned":
            raise PermissionError("space not published or banned")
        if space.owning_instance != owning_instance:
            raise PermissionError("only the space owner may confirm an epoch")
        writer_pk = (
            self._verified_writer_pk(space, epoch_int, wkc) if wkc is not None else None
        )
        now = int(self._clock())
        state = await self._epoch_repo.get(space_id)
        if epoch_int > epoch_ceiling(state.confirmed if state else None, now):
            raise PermissionError("epoch notice is implausibly far ahead")
        await self._epoch_repo.confirm(space_id, epoch_int, now=now)
        if mode is not None:
            await self._epoch_repo.set_publish_mode(space_id, mode, at=_ts_unix(ts))
        if writer_pk is not None:
            state = await self._epoch_repo.get(space_id)
            if state is not None and state.confirmed == epoch_int:
                await self._epoch_repo.pin_writer_key(
                    space_id, epoch_int, writer_pk, replace=True
                )

    @staticmethod
    def _verified_writer_pk(space: "GlobalSpace", epoch: int, raw: object) -> str:
        """The b64url writer public key of a ``writer_key_cert`` that the
        space's PINNED authority key signed for ``epoch``, else
        :class:`PermissionError`."""
        if not space.identity_public_key:
            raise PermissionError("no pinned authority key for this space")
        try:
            cert = WriterKeyCert.from_wire(raw)
            verify_writer_key_cert(
                cert,
                space_pubkey=bytes.fromhex(space.identity_public_key),
                space_id=space.space_id,
                epoch=epoch,
            )
        except (UnsupportedWriterKeySuite, InvalidWriterKey, ValueError) as exc:
            raise PermissionError(f"writer key cert refused: {exc}") from exc
        return cert.writer_pk

    async def note_epoch(
        self,
        space_id: str,
        epoch: object,
        authority_sig: str,
        authority_sig_suite: str,
        writer_key_cert: object = None,
    ) -> None:
        """A seed-only (space-authority-signed) epoch notice — what a
        delegated admin sends. It may raise ``current`` by exactly +1, at
        most once a minute, and never the owner-confirmed floor; anything
        else is a 200 that changes nothing (a replay, a delegated rotation
        racing another). Raises :class:`PermissionError` on a bad signature.

        v_50: an optional ``writer_key_cert`` (authority-signed on its own)
        pins the writer key of ``epoch`` only where the +1 rule already let
        ``current`` reach it — ``confirmed <= epoch <= current`` after the
        step — above the newest pin, and never over an existing pin of that
        epoch (only the owner corrects one). Otherwise it changes nothing."""
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
        writer_pk = (
            self._verified_writer_pk(space, epoch_int, writer_key_cert)
            if writer_key_cert is not None
            else None
        )
        await self._epoch_repo.step(
            space_id,
            epoch_int,
            now=int(self._clock()),
            min_interval_s=MIN_EPOCH_STEP_INTERVAL_S,
        )
        if writer_pk is None:
            return
        state = await self._epoch_repo.get(space_id)
        if state is None or not state.confirmed <= epoch_int <= state.current:
            return
        await self._epoch_repo.pin_writer_key(
            space_id, epoch_int, writer_pk, replace=False
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


def _ts_unix(ts: str) -> int:
    """A verified notice's ``ts`` as unix seconds (the signature check has
    already proven it parses and is fresh)."""
    try:
        return int(datetime.fromisoformat(ts).timestamp())
    except (TypeError, ValueError) as exc:
        raise PermissionError("bad ts") from exc


def _is_member_publish_path(path: str) -> bool:
    return path in (MEMBER_PUBLISH_ROUTE, MEMBER_PUBLISH_ANON_ROUTE) or (
        path.startswith("/gfs/spaces/") and path.endswith("/epoch")
    )


def build_member_publish_rate_limit(resolver: ClientIpResolver):
    """Per-IP limiter for ``POST /gfs/member-publish``, ``POST
    /gfs/member-publish-anon`` and the epoch notice,
    shedding a flood before any signature verification."""
    return build_window_limiter(
        resolver,
        MEMBER_PUBLISH_MAX_PER_MINUTE_PER_IP,
        _is_member_publish_path,
    )
