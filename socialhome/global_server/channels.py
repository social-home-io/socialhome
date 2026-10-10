"""Opaque channels for PRIVATE spaces (v_51) — the connection server side.

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
  Carries the publish mode (moves only with a notice at or above the
  current epoch, then forward in ``ts``) and optionally the
  channel writer key pin for that epoch (first pin per epoch wins).
* **unregister** (anonymous, channel-key-signed) — drops the channel and
  every seat.
* **subscribe** (household-signed, identified like a follower's subscribe)
  — must present a :class:`~socialhome.domain.gfs_channel.ChannelPass` the
  channel key issued to THIS household (its registered identity key) for an
  open epoch — households issue passes only to link-joined members, so only
  they take seats; the seat remembers that epoch, and a seat whose pass epoch is
  no longer open receives nothing. So the seats are exactly the member
  households, and a household removed at a rotation drops out after the
  grace.
* **publish** (trusted, household-signed) — refused in a strict channel; a
  :class:`~socialhome.domain.gfs_channel.ChannelCert` for this household,
  channel and an open epoch, scope ``comment`` at least (the real item type
  is inside the ciphertext, receivers enforce the real scope).
* **publish-anon** (strict, no identity) — signed with the channel writer
  key pinned for that epoch; per-(channel, address), per-channel and
  per-writer-key limits; exact replays refused.

Fan-out reuses the member-publish workers and the offline relay queue
unchanged: ``{type: "relay", channel_id, event_type: "space_item", epoch,
payload}`` to every open seat except a trusted publisher.
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
    ChannelPublishRequest,
    ChannelRegisterRequest,
    ChannelSubscribeRequest,
    ChannelUnregisterRequest,
    ChannelUnsubscribeRequest,
    UnsupportedChannelSuite,
)
from ..domain.writer_cert import WRITER_SCOPE_COMMENT
from ..domain.writer_key import UnsupportedWriterKeySuite
from ..gfs_channel import (
    InvalidChannelSignature,
    channel_pk_bytes,
    verify_channel_cert,
    verify_channel_pass,
    verify_channel_writer_key_cert,
    verify_notice,
    verify_publish_anon,
    verify_register,
    verify_unregister,
)
from .addressee import GfsAddressee
from .domain import CHANNEL_EPOCH_GRACE_S, GfsChannel
from .envelope_relay import ENVELOPE_QUEUE_TTL_SECONDS
from .federation import MAX_SUBSCRIPTIONS_PER_INSTANCE, SeenPayloadCache
from .member_publish import (
    MEMBER_PUBLISH_ANON_MAX_PER_MINUTE_PER_SPACE_IP,
    MEMBER_PUBLISH_MAX_PER_MINUTE,
    MEMBER_PUBLISH_MAX_PER_MINUTE_PER_IP,
    MEMBER_PUBLISH_MAX_PER_MINUTE_PER_SPACE,
    MEMBER_PUBLISH_MAX_PER_MINUTE_PER_WRITER_KEY,
    MemberPublishBusy,
    MemberPublishRateLimited,
)
from .public import ClientIpResolver, SlidingWindowCounter, build_window_limiter

if TYPE_CHECKING:
    from .federation import GfsFederationService
    from .member_publish import GfsMemberPublishService
    from .repositories import AbstractGfsChannelRepo

log = logging.getLogger(__name__)

#: How far an anonymous statement's ``ts`` may be from this server's clock.
CHANNEL_TS_SKEW_S: int = 300

#: Channel registrations per client address per minute. A household
#: registers a channel once per private space (and again only on an
#: authority rotation), so this is far above honest use while bounding how
#: many rows one address can create.
CHANNEL_REGISTER_MAX_PER_MINUTE_PER_IP: int = 10

#: Channel seats one registered household may hold (on top of, and as many
#: as, its space subscriptions).
MAX_CHANNEL_SUBSCRIPTIONS_PER_INSTANCE: int = MAX_SUBSCRIPTIONS_PER_INSTANCE


#: Highest epoch a channel's FIRST notice may set. Channel epochs carry a
#: secret 40-bit offset (they are not wall-clock bound like a space owner's),
#: so the bound only keeps ``epoch + 1`` arithmetic far from overflow.
MAX_FIRST_CHANNEL_EPOCH: int = 2**62


#: Live channel rows this server keeps at most — anonymous registration's
#: server-wide bound (with the per-address limit and the unused-row TTL).
MAX_CHANNELS: int = 100_000


class ChannelPinned(Exception):
    """The channel id is pinned to another key — ``409``. There is no re-pin."""


class ChannelEpochTooSoon(Exception):
    """The notice's epoch is ahead by more than the time-bounded allowance;
    ``retry_after_s`` says when it lands — ``429``."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(retry_after_s)
        self.retry_after_s = retry_after_s


def _open_epochs(ch: GfsChannel, *, now: int) -> set[int]:
    """The epochs a pass / cert / seat may carry right now."""
    if ch.epoch is None:
        return set()
    epochs = {ch.epoch, ch.epoch + 1}
    if (
        ch.epoch_prev is not None
        and now - (ch.epoch_raised_at or 0) <= CHANNEL_EPOCH_GRACE_S
    ):
        epochs.add(ch.epoch_prev)
    return epochs


class GfsChannelService:
    """Register, pin, step and relay opaque channels (see module docstring)."""

    __slots__ = (
        "_anon_ip_limiter",
        "_anon_seen",
        "_channels",
        "_channel_limiter",
        "_clock",
        "_federation",
        "_addressee",
        "_limiter",
        "_max_channels",
        "_member_publish",
        "_register_limiter",
        "_seen",
        "_writer_key_limiter",
    )

    def __init__(
        self,
        *,
        federation: "GfsFederationService",
        channel_repo: "AbstractGfsChannelRepo",
        member_publish: "GfsMemberPublishService",
        gfs_instance_id: str,
        addressee: GfsAddressee | None = None,
        clock: Callable[[], float] = time.time,
        register_limiter: SlidingWindowCounter | None = None,
        max_channels: int = MAX_CHANNELS,
    ) -> None:
        self._federation = federation
        self._channels = channel_repo
        self._member_publish = member_publish
        #: This server's public id + its transitional aliases (see
        #: :mod:`.addressee`).
        self._addressee = addressee or GfsAddressee(gfs_instance_id)
        self._clock = clock
        self._max_channels = max_channels
        self._register_limiter = register_limiter or SlidingWindowCounter(
            CHANNEL_REGISTER_MAX_PER_MINUTE_PER_IP
        )
        self._limiter = SlidingWindowCounter(MEMBER_PUBLISH_MAX_PER_MINUTE)
        self._channel_limiter = SlidingWindowCounter(
            MEMBER_PUBLISH_MAX_PER_MINUTE_PER_SPACE
        )
        self._writer_key_limiter = SlidingWindowCounter(
            MEMBER_PUBLISH_MAX_PER_MINUTE_PER_WRITER_KEY
        )
        self._anon_ip_limiter = SlidingWindowCounter(
            MEMBER_PUBLISH_ANON_MAX_PER_MINUTE_PER_SPACE_IP
        )
        self._seen = SeenPayloadCache()
        self._anon_seen = SeenPayloadCache(ttl_s=float(2 * CHANNEL_TS_SKEW_S))

    # ── Shared checks ────────────────────────────────────────────────────

    def _now(self) -> int:
        return int(self._clock())

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

    # ── Registration ─────────────────────────────────────────────────────

    async def register(self, req: ChannelRegisterRequest, *, client_ip: str) -> str:
        """Register / refresh (module docstring). Returns ``"registered"`` or
        ``"refreshed"``. Raises :class:`PermissionError`,
        :class:`ChannelPinned`, :class:`MemberPublishRateLimited` or
        :class:`MemberPublishBusy` (the server-wide cap)."""
        self._addressed(req.gfs_instance_id, req.gfs_key)
        self._fresh(req.ts)
        if not self._register_limiter.allow(client_ip):
            raise MemberPublishRateLimited()
        try:
            verify_register(req)
        except (InvalidChannelSignature, UnsupportedChannelSuite) as exc:
            raise PermissionError(f"registration refused: {exc}") from exc
        now = self._now()
        existing = await self._channels.get(req.channel_id)
        if existing is None:
            if await self._channels.count() >= self._max_channels:
                log.warning(
                    "gfs.channel: %d channels live — refusing new registrations",
                    self._max_channels,
                )
                raise MemberPublishBusy()
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
        at = self._fresh(notice.ts)
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
            # The mode moves only WITH a raise (a mode switch rotates): a
            # notice at the current epoch — a replay, a reconnect, a lagging
            # seed holder — never flips it.
            await self._channels.set_publish_mode(
                notice.channel_id, notice.publish_mode, at=at
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
        ch = await self._channel(req.channel_id)
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
        if req.channel_pass.epoch not in _open_epochs(ch, now=now):
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

    def _resolver(self, channel_id: str):
        async def _resolve() -> tuple[list[str], set[str]]:
            ch = await self._channels.get(channel_id)
            if ch is None:
                return [], set()
            epochs = _open_epochs(ch, now=self._now())
            targets = await self._channels.list_targets(channel_id, epochs)
            recent = await self._channels.list_recently_seen(
                channel_id, epochs, within_s=ENVELOPE_QUEUE_TTL_SECONDS
            )
            return targets, recent

        return _resolve

    def _submit(self, channel_id: str, publisher_id: str, frame: dict) -> None:
        if not self._member_publish.submit_fan_out(
            f"ch:{channel_id}",
            publisher_id=publisher_id,
            frame=frame,
            resolve=self._resolver(channel_id),
        ):
            raise MemberPublishBusy()

    async def publish(self, req: ChannelPublishRequest) -> bool:
        """Trusted publish. Returns ``False`` for a suppressed replay."""
        inst = await self._federation.verify_signed_request(
            req.instance_id, req.signing_payload(), signature=req.signature
        )
        if inst.status != "active":
            raise PermissionError("instance is not active")
        self._addressed(req.gfs_instance_id, req.gfs_key)
        if not self._limiter.allow(f"{inst.instance_id}\x00{req.channel_id}"):
            raise MemberPublishRateLimited()
        if not self._channel_limiter.allow(req.channel_id):
            raise MemberPublishRateLimited()
        ch = await self._channel(req.channel_id)
        if ch.strict:
            raise PermissionError("channel is in strict mode — identified refused")
        try:
            verify_channel_cert(
                req.channel_cert,
                channel_pk=self._pinned(ch),
                channel_id=req.channel_id,
                epoch=req.epoch,
                author_pk=bytes.fromhex(inst.public_key),
                required_scope=WRITER_SCOPE_COMMENT,
            )
        except (InvalidChannelSignature, UnsupportedChannelSuite, ValueError) as exc:
            raise PermissionError(f"channel cert refused: {exc}") from exc
        now = self._now()
        if not ch.admits(req.epoch, now=now):
            raise PermissionError("cert epoch is not open here")
        digest = SeenPayloadCache.digest(req.to_wire())
        if self._seen.seen(digest):
            return False
        self._submit(req.channel_id, inst.instance_id, req.fan_out_frame())
        self._seen.record(digest)
        await self._channels.touch(req.channel_id, now=now)
        return True

    async def publish_anon(
        self, req: ChannelPublishAnonRequest, *, client_ip: str
    ) -> None:
        """Strict (anonymous) publish."""
        self._addressed(req.gfs_instance_id, req.gfs_key)
        self._fresh(req.ts)
        ch = await self._channel(req.channel_id)
        pinned = ch.writer_pk_for(req.epoch)
        if pinned is None:
            raise PermissionError("no writer key pinned for this epoch")
        try:
            verify_publish_anon(req, writer_pk=channel_pk_bytes(pinned))
        except (
            InvalidChannelSignature,
            UnsupportedChannelSuite,
            UnsupportedWriterKeySuite,
        ) as exc:
            raise PermissionError(f"writer signature refused: {exc}") from exc
        if not self._anon_ip_limiter.allow(f"{req.channel_id}\x00{client_ip}"):
            raise MemberPublishRateLimited()
        if not self._channel_limiter.allow(req.channel_id):
            raise MemberPublishRateLimited()
        if not self._writer_key_limiter.allow(f"{req.channel_id}\x00{req.epoch}"):
            raise MemberPublishRateLimited()
        now = self._now()
        if not ch.admits(req.epoch, now=now):
            raise PermissionError("writer key epoch is not open here")
        digest = SeenPayloadCache.digest(req.to_wire())
        if self._anon_seen.seen(digest):
            raise PermissionError("replayed request")
        # No publisher to exclude: its own echo is dropped by the household.
        self._submit(req.channel_id, "", req.fan_out_frame())
        self._anon_seen.record(digest)
        await self._channels.touch(req.channel_id, now=now)


def _is_channel_path(path: str) -> bool:
    return path in CHANNEL_ROUTES


def build_channel_rate_limit(resolver: ClientIpResolver):
    """Per-IP limiter on every ``/gfs/channels/*`` route, shedding a flood
    before any signature work."""
    return build_window_limiter(
        resolver, MEMBER_PUBLISH_MAX_PER_MINUTE_PER_IP, _is_channel_path
    )
