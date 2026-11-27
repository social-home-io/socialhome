"""Opaque channels (v_51 private spaces, v_96 global ones too) — the
connection server side.

A private space whose members include households seated through an invite
link gets member publishing through a channel this server knows only by a
random 128-bit id and a channel public key. **This server never learns the
space**: no space id, no name, no space authority key, no owner household —
the channel key is HKDF-derived from the space seed on the households, so it
cannot be linked to the space key (see :mod:`socialhome.gfs_channel`). Wire
codecs: :mod:`socialhome.domain.gfs_channel`; rationale and residuals:
``docs/protocol/discovery.md`` ("Private spaces: opaque channels").

Requests and their checks — every refusal a uniform ``403`` at the route,
the reason at DEBUG, never logged with anything but the channel id:

* **register** (anonymous, ``POST /gfs/channels/register``) — addressed to
  this server, ``ts`` ±300 s, per-address limit, then proof of possession
  (``channel_sig`` under the key being registered). A new id pins the key
  (trust on first use); the same key refreshes; another key is a ``409``
  — there is no re-pin (a household starts a fresh channel instead). New
  registrations stop at :data:`MAX_CHANNELS` live rows (``503``); a channel
  that never got a notice or a seat is swept after
  :data:`~.domain.CHANNEL_UNUSED_TTL_SECONDS`, a used one after 30 idle
  days. ``nonce`` only makes each signature unique: replays inside the
  ``ts`` window are harmless no-ops, so it is not cached.
* **epoch notice** (anonymous, channel-key-signed) — ONE tier, because
  telling an owner from a delegated admin would mean learning who owns the
  channel: the first notice after registration sets any epoch up to
  :data:`MAX_FIRST_CHANNEL_EPOCH`; later the epoch rises by at most one per
  :data:`~.domain.CHANNEL_EPOCH_STEP_INTERVAL_S` since the last raise (a
  faster rotation gets ``429`` + ``Retry-After`` and lands on the retry).
  Optionally carries the channel writer key pin for that epoch (first pin
  per epoch wins).
* **unregister** (anonymous, channel-key-signed) — drops the channel and
  every seat.
* **subscribe** (household-signed, identified by its registered key)
  — must present a :class:`~socialhome.domain.gfs_channel.ChannelPass` the
  channel key issued to THIS household (its registered identity key) for an
  open epoch — households issue passes only to link-joined members, so only
  they take seats; the seat remembers that epoch, and a seat whose pass epoch is
  no longer open receives nothing. So the seats are exactly the member
  households, and a household removed at a rotation drops out after the
  grace.
* **publish-anon** (no identity — the only channel publish since v_90) —
  signed with the channel writer key pinned for that epoch;
  per-(channel, address), per-channel and per-writer-key limits, each
  cluster-wide (every node admits its :func:`~.public.node_share` over the
  live nodes); exact replays refused. The real item type is inside the
  ciphertext; receivers enforce the real scope.

Fan-out runs on the background workers (:mod:`.fan_out_workers`) and the
offline relay queue: ``{type: "relay", channel_id, event_type: "space_item", epoch,
payload}`` to every open seat (the publisher drops its own echo).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ..domain.gfs_channel import (
    CHANNEL_ROUTES,
    ChannelEpochNotice,
    ChannelPublishAnonRequest,
    ChannelRegisterRequest,
    ChannelSubscribeRequest,
    ChannelUnregisterRequest,
    ChannelUnsubscribeRequest,
    UnsupportedChannelSuite,
)
from ..domain.writer_key import UnsupportedWriterKeySuite
from ..gfs_channel import (
    InvalidChannelSignature,
    channel_pk_bytes,
    verify_channel_pass,
    verify_channel_writer_key_cert,
    verify_notice,
    verify_publish_anon,
    verify_register,
    verify_signature,
    verify_unregister,
)
from ..writer_key import verify_writer_sig
from .addressee import GfsAddressee
from .cluster_relay import AUDIENCE_CHANNEL
from .domain import GfsChannel
from .envelope_relay import ENVELOPE_QUEUE_TTL_SECONDS
from .federation import SeenPayloadCache
from .public import (
    ClientIpResolver,
    SlidingWindowCounter,
    build_window_limiter,
    node_share,
)

if TYPE_CHECKING:
    from .channel_ring import GfsChannelRing
    from .fan_out_workers import GfsFanOutWorkers
    from .federation import GfsFederationService
    from .repositories import AbstractGfsChannelRepo

log = logging.getLogger(__name__)

#: How far an anonymous statement's ``ts`` may be from this server's clock.
CHANNEL_TS_SKEW_S: int = 300

#: NEW channel registrations per client address per minute. A household
#: registers a channel once per channel space (and again only on an
#: authority rotation), so this is far above honest use while bounding how
#: many rows one address can create. A refresh of a known channel (an
#: owner re-registering all of its channels on a reconnect) spends none.
CHANNEL_REGISTER_MAX_PER_MINUTE_PER_IP: int = 10

#: Channel seats one registered household may hold. Far above what a family
#: joins or follows; it bounds how many fan-out targets — and queued items —
#: one registration can stand for.
MAX_CHANNEL_SUBSCRIPTIONS_PER_INSTANCE: int = 500

#: Accepted publishes per CHANNEL per minute, across every writer — bounds
#: the fan-out work (and queue writes) one channel can cause.
CHANNEL_PUBLISH_MAX_PER_MINUTE: int = 120

#: Accepted publishes per channel WRITER KEY (one epoch) per minute. The key
#: is shared by every writer of the epoch, so this bounds the channel as a
#: whole, not any one household.
CHANNEL_PUBLISH_MAX_PER_MINUTE_PER_WRITER_KEY: int = 120

#: Per-IP/minute cap on every ``/gfs/channels/*`` route, applied BEFORE any
#: signature work.
CHANNEL_ROUTE_MAX_PER_MINUTE_PER_IP: int = 120


#: Highest epoch a channel's FIRST notice may set. Channel epochs carry a
#: secret 40-bit offset (they are not wall-clock bound like a space owner's),
#: so the bound only keeps ``epoch + 1`` arithmetic far from overflow.
MAX_FIRST_CHANNEL_EPOCH: int = 2**62


#: Accepted anonymous publishes per (channel, client address) per minute,
#: cluster-wide. The writer key is shared by every writer of an epoch and
#: the publish names nobody, so the address is the only thing that tells one
#: key holder apart from the others: an eighth of the channel's
#: :data:`CHANNEL_PUBLISH_MAX_PER_MINUTE` (120), so
#: one holder needs EIGHT addresses (IPv6 counted per /64) to spend the whole
#: channel budget, where 30 a minute let four do it. Honest use is far below
#: it — a household's posts, comments and reactions, one every 4 s sustained;
#: a burst past it gets ``429`` and the household's retry queue lands it.
CHANNEL_ANON_MAX_PER_MINUTE_PER_ADDRESS: int = CHANNEL_PUBLISH_MAX_PER_MINUTE // 8

#: v_97 (owner Q5 B) — the per-address limit never drops below this per
#: node: on a 4-node cluster ``node_share(15, 4)`` is 4/min per node, which
#: refused a busy household's chat; the floor makes it ~32/min cluster-wide.
CHANNEL_ANON_PER_NODE_FLOOR: int = 8


#: Live channel rows this server keeps at most — anonymous registration's
#: server-wide bound (with the per-address limit and the unused-row TTL).
MAX_CHANNELS: int = 100_000


class ChannelRateLimited(Exception):
    """The client address, the writer key or the channel as a whole exceeded
    its per-minute budget (``429``)."""


class ChannelBusy(Exception):
    """The background fan-out is stopped or full, or the server-wide channel
    cap is reached (``503``)."""


class ChannelPinned(Exception):
    """The channel id is pinned to another key — ``409``. There is no re-pin."""


#: The least ``Retry-After`` for a pass whose epoch is not open YET.
SUBSCRIBE_NOT_YET_RETRY_S: int = 5


class ChannelEpochTooSoon(Exception):
    """The notice's epoch is ahead by more than the time-bounded allowance;
    ``retry_after_s`` says when it lands — ``429``."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(retry_after_s)
        self.retry_after_s = retry_after_s


def _open_epochs(ch: GfsChannel, *, now: int) -> set[int]:
    """The epochs a pass / seat may carry right now."""
    return ch.open_epochs(now=now)


class GfsChannelService:
    """Register, pin, step and relay opaque channels (see module docstring)."""

    __slots__ = (
        "_addressee",
        "_anon_ip_limiter",
        "_anon_seen",
        "_channel_limiter",
        "_channels",
        "_clock",
        "_fan_out",
        "_federation",
        "_max_channels",
        "_nodes",
        "_register_limiter",
        "_ring",
        "_writer_key_limiter",
    )

    def __init__(
        self,
        *,
        federation: GfsFederationService,
        channel_repo: AbstractGfsChannelRepo,
        fan_out: GfsFanOutWorkers,
        gfs_instance_id: str,
        addressee: GfsAddressee | None = None,
        clock: Callable[[], float] = time.time,
        register_limiter: SlidingWindowCounter | None = None,
        max_channels: int = MAX_CHANNELS,
        nodes: Callable[[], int] | None = None,
    ) -> None:
        self._federation = federation
        self._channels = channel_repo
        self._fan_out = fan_out
        #: This server's public id + its transitional aliases (see
        #: :mod:`.addressee`).
        self._addressee = addressee or GfsAddressee(gfs_instance_id)
        self._clock = clock
        self._max_channels = max_channels
        #: The cluster's live node count (``ClusterService.live_node_count``):
        #: each node admits its share of every publish limit, so the limits
        #: hold for the whole cluster behind the round-robin balancer.
        self._nodes = nodes
        self._register_limiter = register_limiter or SlidingWindowCounter(
            CHANNEL_REGISTER_MAX_PER_MINUTE_PER_IP
        )
        self._channel_limiter = SlidingWindowCounter(CHANNEL_PUBLISH_MAX_PER_MINUTE)
        self._writer_key_limiter = SlidingWindowCounter(
            CHANNEL_PUBLISH_MAX_PER_MINUTE_PER_WRITER_KEY
        )
        self._anon_ip_limiter = SlidingWindowCounter(
            CHANNEL_ANON_MAX_PER_MINUTE_PER_ADDRESS
        )
        self._anon_seen = SeenPayloadCache(ttl_s=float(2 * CHANNEL_TS_SKEW_S))
        #: v_97 — the channel rings; ``None`` keeps the v_96 behaviour.
        self._ring: GfsChannelRing | None = None

    def attach_ring(self, ring: GfsChannelRing) -> None:
        """Wire the v_97 channel ring (capability ``channel_ring``)."""
        self._ring = ring

    # ── Shared checks ────────────────────────────────────────────────────

    def _now(self) -> int:
        return int(self._clock())

    def _allow(
        self, counter: SlidingWindowCounter, key: str, *, floor: int = 1
    ) -> bool:
        """One hit on *counter* under this node's share of its limit.

        The counters live in this process; behind the round-robin balancer a
        channel's publishes spread over every node, so a per-node limit of L
        would let an N-node cluster admit N × L (480 items a minute into one
        channel on four nodes — 4.8 million queued or pushed frames a minute
        at 10 000 seats). Each node admitting ``ceil(L / N)`` keeps the
        cluster at about L; a balancer that pins a client to one node makes
        it stricter, never looser (:func:`~.public.node_share`)."""
        nodes = self._nodes() if self._nodes is not None else 1
        share = max(floor, node_share(counter.limit, nodes))
        return counter.allow(key, limit=min(share, counter.limit))

    def _addressed(self, gfs_instance_id: str, gfs_key: str | None = None) -> None:
        if not self._addressee.accepts(gfs_instance_id, gfs_key):
            raise PermissionError("request is addressed to another server")

    def _fresh(self, ts: str) -> int:
        try:
            parsed = datetime.fromisoformat(ts)
        except (TypeError, ValueError) as exc:
            raise PermissionError("bad ts") from exc
        if parsed.tzinfo is None:
            raise PermissionError("naive ts")
        at = parsed.timestamp()
        if abs(at - self._clock()) > CHANNEL_TS_SKEW_S:
            raise PermissionError("stale ts")
        return int(at)

    async def _channel(self, channel_id: str) -> GfsChannel:
        ch = await self._channels.get(channel_id)
        if ch is None:
            raise PermissionError("unknown channel")
        return ch

    @staticmethod
    def _pinned(ch: GfsChannel) -> bytes:
        try:
            return channel_pk_bytes(ch.channel_pk)
        except InvalidChannelSignature as exc:
            raise PermissionError("pinned channel key unusable") from exc

    @staticmethod
    def _writer_pk(ch: GfsChannel, epoch: int) -> bytes:
        """The channel writer key pinned for *epoch*, else
        :class:`PermissionError` (an unusable pin raises
        :class:`InvalidChannelSignature`)."""
        pinned = ch.writer_pk_for(epoch)
        if pinned is None:
            raise PermissionError("no writer key pinned for this epoch")
        return channel_pk_bytes(pinned)

    # ── Checks shared with the space media cache ─────────────────────────

    async def require_channel_in_use(self, channel_id: str) -> None:
        """:class:`PermissionError` unless *channel_id* is registered here AND
        in use: at least one active member household holds a seat whose pass
        is for an open epoch (the seats a publish fans out to — nothing new
        is recorded). One seat is enough: the owner household never
        subscribes to its own channel (third review A4). A channel's blobs
        count against their UPLOADER's address bucket for the cache's
        fairness (fourth review G1), never a seated member."""
        ch = await self._channel(channel_id)
        if not await self._channels.list_targets(
            channel_id, _open_epochs(ch, now=self._now())
        ):
            raise PermissionError("channel not in use")

    async def verify_channel_key(
        self, channel_id: str, *, prefix: bytes, body: dict, signature: str, suite: str
    ) -> None:
        """*signature* over ``prefix + canonical(body)`` verifies under the
        channel key pinned at registration — what registration, notices and
        unregister prove. :class:`PermissionError` otherwise."""
        ch = await self._channel(channel_id)
        try:
            verify_signature(
                prefix=prefix,
                body=body,
                signature=signature,
                public_key=self._pinned(ch),
                suite=suite,
            )
        except (InvalidChannelSignature, UnsupportedChannelSuite) as exc:
            raise PermissionError(f"channel signature refused: {exc}") from exc

    async def verify_writer_key(
        self,
        channel_id: str,
        *,
        epoch: int,
        message: bytes,
        writer_sig: str,
        suite: str,
    ) -> None:
        """Channel writer check: *writer_sig* (suite checked, never
        defaulted) signs *message* under the channel writer key pinned for
        *epoch*, and *epoch* is open. :class:`PermissionError` otherwise."""
        ch = await self._channel(channel_id)
        try:
            ok = verify_writer_sig(
                writer_pk=self._writer_pk(ch, epoch),
                message=message,
                writer_sig=writer_sig,
                suite=suite,
            )
        except (InvalidChannelSignature, UnsupportedWriterKeySuite, ValueError) as exc:
            raise PermissionError(f"writer signature refused: {exc}") from exc
        if not ok:
            raise PermissionError("writer signature does not verify")
        if not ch.admits(epoch, now=self._now()):
            raise PermissionError("writer key epoch is not open here")

    # ── Registration ─────────────────────────────────────────────────────

    async def register(self, req: ChannelRegisterRequest, *, client_ip: str) -> str:
        """Register / refresh (module docstring). Returns ``"registered"`` or
        ``"refreshed"``. Raises :class:`PermissionError`,
        :class:`ChannelPinned`, :class:`ChannelRateLimited` or
        :class:`ChannelBusy` (the server-wide cap)."""
        self._addressed(req.gfs_instance_id, req.gfs_key)
        self._fresh(req.ts)
        existing = await self._channels.get(req.channel_id)
        # Only a NEW channel spends the per-address register budget (it
        # bounds new rows), still before any signature work. A refresh of a
        # known one — an owner re-registering every channel it runs on a
        # reconnect, many at once — is no new row; it is bounded by the
        # per-address limit on every channel route (which also bounds the
        # one indexed read an unknown-id flood now costs before its 429).
        if existing is None and not self._allow(self._register_limiter, client_ip):
            raise ChannelRateLimited()
        try:
            verify_register(req)
        except (InvalidChannelSignature, UnsupportedChannelSuite) as exc:
            raise PermissionError(f"registration refused: {exc}") from exc
        now = self._now()
        if existing is None:
            if await self._channels.count() >= self._max_channels:
                log.warning(
                    "gfs.channel: %d channels live — refusing new registrations",
                    self._max_channels,
                )
                raise ChannelBusy()
            if await self._channels.register(
                req.channel_id,
                channel_suite=req.channel_suite,
                channel_pk=req.channel_pk,
                now=now,
            ):
                log.info("gfs.channel: registered channel=%s", req.channel_id)
                return "registered"
            existing = await self._channel(req.channel_id)
        if existing.channel_pk == req.channel_pk:
            await self._channels.touch(req.channel_id, now=now)
            return "refreshed"
        # No re-pin, ever: the household starts a fresh channel instead.
        raise ChannelPinned()

    async def unregister(self, req: ChannelUnregisterRequest) -> None:
        """Drop the channel (and its seats) on a channel-key-signed request.
        An unknown channel is a no-op success (idempotent)."""
        self._addressed(req.gfs_instance_id, req.gfs_key)
        self._fresh(req.ts)
        ch = await self._channels.get(req.channel_id)
        if ch is None:
            return
        try:
            verify_unregister(req, channel_pk=self._pinned(ch))
        except (InvalidChannelSignature, UnsupportedChannelSuite) as exc:
            raise PermissionError(f"unregister refused: {exc}") from exc
        await self._channels.delete(req.channel_id)
        log.info("gfs.channel: unregistered channel=%s", req.channel_id)

    # ── Epoch notice ─────────────────────────────────────────────────────

    async def note_epoch(self, notice: ChannelEpochNotice) -> dict:
        """Apply a channel-key-signed epoch notice (module docstring). Returns
        what this server now holds — ``{"epoch", "writer_pk"}`` (the pin for
        the notice's epoch) — so the channel key holder can tell when
        another key holder moved the channel past it. Raises
        :class:`PermissionError` or :class:`ChannelEpochTooSoon`."""
        self._addressed(notice.gfs_instance_id, notice.gfs_key)
        self._fresh(notice.ts)
        ch = await self._channel(notice.channel_id)
        pinned = self._pinned(ch)
        try:
            verify_notice(notice, channel_pk=pinned)
            writer_pk = (
                verify_channel_writer_key_cert(
                    notice.writer_key_cert,
                    channel_pk=pinned,
                    channel_id=notice.channel_id,
                    epoch=notice.epoch,
                )
                if notice.writer_key_cert is not None
                else None
            )
        except (
            InvalidChannelSignature,
            UnsupportedChannelSuite,
            UnsupportedWriterKeySuite,
        ) as exc:
            raise PermissionError(f"notice refused: {exc}") from exc
        now = self._now()
        if ch.epoch is None and notice.epoch > MAX_FIRST_CHANNEL_EPOCH:
            raise PermissionError("epoch is implausibly far ahead")
        wait = ch.step_allowance(notice.epoch, now=now)
        if wait > 0:
            raise ChannelEpochTooSoon(wait)
        if wait == 0:
            # Compare-and-set against what the allowance was computed on; a
            # racing notice that moved it first makes this a no-op.
            await self._channels.set_epoch(
                notice.channel_id, notice.epoch, expected=ch.epoch, now=now
            )
        state = await self._channels.get(notice.channel_id)
        if (
            writer_pk is not None
            and notice.writer_key_cert is not None
            and state is not None
            and state.epoch == notice.epoch
        ):
            await self._channels.pin_writer_key(
                notice.channel_id, notice.epoch, notice.writer_key_cert.writer_pk
            )
            state = await self._channels.get(notice.channel_id)
        await self._channels.touch(notice.channel_id, now=now)
        return {
            "epoch": state.epoch if state is not None else None,
            "writer_pk": state.writer_pk_for(notice.epoch) if state else None,
        }

    # ── Seats ────────────────────────────────────────────────────────────

    async def subscribe(self, req: ChannelSubscribeRequest) -> None:
        """Seat a member household that proves membership with its pass."""
        inst = await self._federation.verify_signed_request(
            req.instance_id, req.signing_payload(), signature=req.signature
        )
        if inst.status != "active":
            raise PermissionError("instance is not active")
        self._addressed(req.gfs_instance_id, req.gfs_key)
        ch = await self._channels.get(req.channel_id)
        if ch is None:
            # A registered household's seat for a channel not registered
            # here: its grant can beat the owner's register (or follow a
            # retirement): not yet — it retries, and drops the retry once
            # its grant moves on.
            raise ChannelEpochTooSoon(SUBSCRIBE_NOT_YET_RETRY_S)
        try:
            verify_channel_pass(
                req.channel_pass,
                channel_pk=self._pinned(ch),
                channel_id=req.channel_id,
                instance_pk=bytes.fromhex(inst.public_key),
            )
        except (InvalidChannelSignature, UnsupportedChannelSuite, ValueError) as exc:
            raise PermissionError(f"pass refused: {exc}") from exc
        now = self._now()
        epoch = req.channel_pass.epoch
        if epoch not in _open_epochs(ch, now=now):
            if ch.epoch is None or epoch > ch.epoch + 1:
                # A valid pass AHEAD of the open epochs (the owner rotated
                # again while its notice waits out the step bound, or the
                # channel's first notice has not landed): not yet — 429 with
                # when to retry, never a refusal the member gives up on.
                # Open once the owner's notice for THIS epoch lands (the
                # owner announces the newest epoch only).
                wait = 0 if ch.epoch is None else ch.step_allowance(epoch, now=now)
                raise ChannelEpochTooSoon(max(SUBSCRIBE_NOT_YET_RETRY_S, wait))
            raise PermissionError("pass epoch is not open here")
        if (
            not await self._channels.has_subscription(req.channel_id, inst.instance_id)
            and await self._channels.count_subscriptions(inst.instance_id)
            >= MAX_CHANNEL_SUBSCRIPTIONS_PER_INSTANCE
        ):
            raise PermissionError("too many channel subscriptions")
        await self._channels.add_subscriber(
            req.channel_id,
            inst.instance_id,
            pass_epoch=req.channel_pass.epoch,
            now=now,
        )

    async def unsubscribe(self, req: ChannelUnsubscribeRequest) -> None:
        await self._federation.verify_signed_request(
            req.instance_id, req.signing_payload(), signature=req.signature
        )
        self._addressed(req.gfs_instance_id, req.gfs_key)
        await self._channels.remove_subscriber(req.channel_id, req.instance_id)

    # ── Publish ──────────────────────────────────────────────────────────

    async def audience_targets(self, channel_id: str) -> list[str]:
        """The seats a forwarded channel item reaches (the cluster audience
        resolver, :mod:`.cluster_relay`): the same open-epoch seats as a
        local fan-out."""
        ch = await self._channels.get(channel_id)
        if ch is None:
            return []
        return await self._channels.list_targets(
            channel_id, _open_epochs(ch, now=self._now())
        )

    def _resolver(self, channel_id: str, *, push_all: bool = False):
        async def _resolve() -> tuple[list[str], set[str]]:
            ch = await self._channels.get(channel_id)
            if ch is None:
                return [], set()
            epochs = _open_epochs(ch, now=self._now())
            targets = await self._channels.list_targets(channel_id, epochs)
            # v_97 — a household that reads the ring gets no offline copy
            # (it pulls), except of a ``push_all`` item every seat gets.
            recent = await self._channels.list_recently_seen(
                channel_id,
                epochs,
                within_s=ENVELOPE_QUEUE_TTL_SECONDS,
                include_ring=push_all or self._ring is None,
            )
            return targets, recent

        return _resolve

    def _submit(self, channel_id: str, publisher_id: str, frame: dict) -> None:
        if not self._fan_out.submit(
            f"ch:{channel_id}",
            publisher_id=publisher_id,
            frame=frame,
            resolve=self._resolver(channel_id, push_all=frame.get("push_all") is True),
            audience=(AUDIENCE_CHANNEL, channel_id),
        ):
            raise ChannelBusy()

    async def publish_anon(
        self, req: ChannelPublishAnonRequest, *, client_ip: str
    ) -> None:
        """Anonymous publish (the only channel publish)."""
        self._addressed(req.gfs_instance_id, req.gfs_key)
        self._fresh(req.ts)
        ch = await self._channel(req.channel_id)
        try:
            verify_publish_anon(req, writer_pk=self._writer_pk(ch, req.epoch))
        except (
            InvalidChannelSignature,
            UnsupportedChannelSuite,
            UnsupportedWriterKeySuite,
        ) as exc:
            raise PermissionError(f"writer signature refused: {exc}") from exc
        if not self._allow(
            self._anon_ip_limiter,
            f"{req.channel_id}\x00{client_ip}",
            floor=CHANNEL_ANON_PER_NODE_FLOOR,
        ):
            raise ChannelRateLimited()
        if not self._allow(self._channel_limiter, req.channel_id):
            raise ChannelRateLimited()
        if not self._allow(
            self._writer_key_limiter, f"{req.channel_id}\x00{req.epoch}"
        ):
            raise ChannelRateLimited()
        now = self._now()
        if not ch.admits(req.epoch, now=now):
            raise PermissionError("writer key epoch is not open here")
        digest = SeenPayloadCache.digest(req.to_wire())
        if self._anon_seen.seen(digest):
            raise PermissionError("replayed request")
        # v_97 — into the channel's ring first: the frame carries its place
        # (``seq`` / ``ring_id``). Without a ring the v_96 frame; a ring
        # field from a household is never honoured by a server without one.
        frame = (
            await self._ring.append(req)
            if self._ring is not None
            else req.fan_out_frame()
        )
        # No publisher to exclude: its own echo is dropped by the household.
        self._submit(req.channel_id, "", frame)
        self._anon_seen.record(digest)
        await self._channels.touch(req.channel_id, now=now)


def _is_channel_path(path: str) -> bool:
    return path in CHANNEL_ROUTES


def build_channel_rate_limit(
    resolver: ClientIpResolver, *, nodes: Callable[[], int] | None = None
):
    """Per-IP limiter on every ``/gfs/channels/*`` route, shedding a flood
    before any signature work. With *nodes* (the cluster's live node count)
    it is cluster-wide: each node allows its :func:`~.public.node_share`."""
    return build_window_limiter(
        resolver, CHANNEL_ROUTE_MAX_PER_MINUTE_PER_IP, _is_channel_path, nodes=nodes
    )
