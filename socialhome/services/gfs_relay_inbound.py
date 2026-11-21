"""Inbound leg of the connection-server envelope relay.

Every ``{type: "envelope", sealed}`` frame a connection server (GFS) pushes
down our WebSocket lands here. The frame is identity-free by design — the
GFS saw only ``{to_instance, sealed}`` — so everything this service learns
comes from inside the seal, opened with THIS household's static key-wrap
private key.

Two families ride the one socket, told apart by the sealed plaintext's
``kind`` marker (never by sniffing field shapes):

* **Relayed §24.11 envelopes** (``kind = space_relay_envelope``, built by
  :class:`~socialhome.federation.gfs_relay_transport.GfsRelayTransport`)
  — from a household seated from an invite link, or from a paired
  household reaching us over the relay fallback. They go straight into
  the **unmodified** §24.11 pipeline
  (:meth:`~socialhome.federation.federation_service.FederationService
  .handle_inbound_rtc` with ``transport = TRANSPORT_GFS_RELAY``): row
  lookup, peer-class and relay-opt-in gates, timestamp, Ed25519 verify
  under the pair key, replay, decrypt, idempotency, ban. Riding the relay
  buys no exemption. This path does not depend on the invite-link
  bootstrap being configured — a paired peer's relayed envelope needs
  only our key-wrap key and the pair's session keys.
* **Pairing bodies** (``kind = pairing_peer_accept`` /
  ``pairing_peer_confirm``) — the §11 QR handshake between households
  whose pairing code named a bootstrap GFS (``reach = url_gfs / gfs``).
  The inner body is the exact Ed25519-signed body the inbox path carries;
  it goes to the very same handler
  (:meth:`~socialhome.federation.federation_service.FederationService
  .handle_peer_accept` / ``handle_peer_confirm``) with ``relayed_via`` =
  the delivering connection, which the handler requires to be the one
  the pairing session was issued with. A frame from a server that is not
  one of our active connections is dropped before any handler runs.
* **Binary media frames** (v_87, ``\\x00SHR``, handed over by the WS
  client as ``{type: "envelope_media", body}``) — opened with our key-wrap
  key (:func:`~socialhome.federation.gfs_relay_media.open_relay_media`:
  addressed to us, kem-suite byte known, AEAD over the recipient id) and
  run through the inbox's §24.11 media tail
  (:meth:`~socialhome.federation.federation_service.FederationService
  .handle_inbound_media_relay`). Their own bucket
  (:data:`RELAY_MEDIA_INBOUND_LIMIT`), checked BEFORE and never charged to
  the process-wide one, and only for a frame that went through.
* **Invite bootstrap bodies** (``KIND_REDEEM*``) — delegated to the
  :class:`~socialhome.federation.invite_token_redeem
  .SpaceInviteTokenRedeemCoordinator`, which owns their validation.

Throttles reuse the coordinator's limiter keys and constants: a
process-wide bucket BEFORE the unseal (a flood costs a list append, not an
AES-GCM open) and a per-family bucket after it, so space traffic,
redeems and pairing bodies never starve each other (pairing bodies get the
bootstrap-body budget, :data:`BOOTSTRAP_BODY_INBOUND_LIMIT`, under their
own key). Once the pipeline verified the sender, it holds that household
to its own budget (§24.11 step 5a,
:func:`~socialhome.federation.inbound_validator.make_check_instance_rate`
— one per household, shared with its HTTPS inbox traffic).

Held, not dropped
-----------------

The GFS deletes a frame from its queue the moment the frame went out on
our socket — there is no acknowledgement to withhold, so a frame refused
here for a BUDGET (the process-wide or envelope ceiling, the media budget,
the sender's per-household budget) would be lost for good. Such a frame is
instead **held** in memory — bounded by :data:`RELAY_DEFER_MAX_FRAMES` and
:data:`RELAY_DEFER_MAX_BYTES`, a quarter of each per budget, at most
:data:`RELAY_DEFER_MAX_AGE_S` — and run again from the stage that refused
it (never charged to a bucket twice) once that budget frees. Past those
bounds, or while shutting down, it is dropped as before (sync heals it). A
frame refused for anything else (signature, replay, a gate) is final, as
always. While a sender has frames held, its next envelope queues behind
them, so one household's frames keep their order; across households the
relay never promised one.

``RELAY_DELIVERED_VIA``
-----------------------

The supervisor binds each WebSocket's GFS base URL into the frame handler.
For the duration of one relayed envelope's dispatch, :data:`RELAY_DELIVERED_VIA`
holds the ``gfs_connections.id`` (THIS household's own row) of the
connection server that delivered it — ``None`` everywhere else, and when
the URL matches no active connection. A handler that needs to answer on
the same server (a route probe's ack) reads it; nothing puts it on a wire.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import orjson

from ..domain.errors import WrongTypeError
from ..federation.gfs_mailbox import MailboxFlooded, MailboxKeys
from ..federation.gfs_relay_media import open_relay_media
from ..federation.gfs_relay_transport import is_relay_envelope_body
from ..federation.inbound_validator import (
    TRANSPORT_GFS_MAILBOX,
    TRANSPORT_GFS_RELAY,
    InboundRateLimitedError,
    instance_bucket,
)
from ..federation.invite_bootstrap import JOIN_REQUEST_KINDS, unseal_envelope_body
from ..federation.invite_token_redeem import (
    BOOTSTRAP_BODY_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_WINDOW_S,
    RELAY_ENVELOPE_INBOUND_LIMIT,
)
from ..federation.multi_recipient import is_multi_sealed, open_multi
from ..federation.peer_pairing_client import (
    KIND_PAIRING_PEER_ACCEPT,
    is_pairing_relay_body,
    pairing_body_from_relay,
)
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from .gfs_envelope_sender import normalize_gfs_base
from .gfs_ws_client import ENVELOPE_MEDIA_FRAME

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..federation.gfs_join_request import GfsJoinRequestCoordinator
    from ..federation.invite_token_redeem import SpaceInviteTokenRedeemCoordinator
    from ..rate_limiter import RateLimiter

log = logging.getLogger(__name__)


#: The ``gfs_connections.id`` of the connection server that delivered the
#: relayed envelope currently being dispatched — set by
#: :meth:`GfsRelayInbound.handle_frame` around the §24.11 dispatch only,
#: reset afterwards. ``None`` outside a relayed dispatch (RTC, HTTPS inbox)
#: or when the delivering server is not one of our active connections.
#: Local-only: it names OUR row, and never goes on a wire.
#:
#: Read it SYNCHRONOUSLY inside the event handler. A task spawned during
#: the dispatch (``asyncio.create_task``) runs in a *copy* of the context
#: taken at spawn time — it would still see the value, but only by
#: accident of timing, and a task scheduled from elsewhere (the outbox, a
#: bus subscriber running later) sees ``None``. Capture the value into a
#: local first and pass it along explicitly.
RELAY_DELIVERED_VIA: ContextVar[str | None] = ContextVar(
    "gfs_relay_delivered_via",
    default=None,
)

#: Limiter keys — identical to the ones the invite coordinator used when it
#: owned this entry point, so the budgets are exactly what they were.
_PROCESS_WIDE_KEY = "invite-bootstrap:inbound"
_ENVELOPES_KEY = "invite-bootstrap:envelopes"
#: Relayed §11 pairing bodies — their own bucket, the bootstrap-body budget.
_PAIRING_KEY = "invite-bootstrap:pairing"
#: v_89 — ONE bucket per mailbox (envelopes) and one for its media frames,
#: the same budget the instance-addressed relay has. Every frame on it —
#: junk, a failed open, a pipeline refusal, a good envelope — is charged,
#: checked before any open. A flood (by whoever learned the id, e.g. a
#: removed member) shuts only that mailbox, never the instance relay's
#: budget; the refusal is :class:`MailboxFlooded`, which the mailbox
#: service counts and answers with a rotation.
_MAILBOX_KEY = "gfs-mailbox"
_MAILBOX_MEDIA_KEY = "gfs-mailbox-media"
#: Frames per window on ONE mailbox. Unlike the household-wide ceilings
#: (:data:`BOOTSTRAP_INBOUND_LIMIT`, :data:`RELAY_ENVELOPE_INBOUND_LIMIT`)
#: it stays at 600: one mailbox is one GFS's share of our spaces, and past
#: it the frame is a flood signal the mailbox service answers with a
#: rotation, not a budget to wait out.
MAILBOX_INBOUND_LIMIT: int = 600
#: v_87 — binary relay media frames, their own bucket.
_MEDIA_KEY = "gfs-relay:media"
#: v_87 — media frames that failed to open or failed their checks.
_MEDIA_JUNK_KEY = "gfs-relay:media-junk"

#: Failed media frames per window. Charged on every failure; while it is
#: full, media frames are dropped BEFORE any AES-GCM open, so a junk flood
#: costs a dict lookup per frame, not an open.
RELAY_MEDIA_JUNK_LIMIT: int = 120

#: Binary relay media frames this household takes per window (v_87). Its
#: OWN bucket, never the process-wide one: media must not starve relayed
#: control, pairing and invite envelopes. One sender is held to 64 MiB/min
#: per GFS node (~124 full frames), so 1200 a minute (~10 MiB/s) takes about
#: ten senders at their cap at once. Charged only for a frame that went
#: through (a refused frame spends nothing — it cannot crowd real ones out).
RELAY_MEDIA_INBOUND_LIMIT: int = 1200

#: What :meth:`GfsRelayInbound.handle_frame` / ``handle_mailbox_frame``
#: answer for a frame held over a budget (see "Held, not dropped" above).
DEFERRED_RESPONSE: dict = {"status": "deferred"}

#: The hold's bounds — whichever is reached first drops the NEW frame, as
#: an over-budget frame was before. 2000 frames is the GFS's own queue cap
#: for one recipient (``ENVELOPE_QUEUE_MAX_PER_RECIPIENT``): a burst the GFS
#: would have queued for us is never lost here instead. 16 MiB bounds the
#: memory (a typical envelope is a few KiB; ~30 full media frames).
RELAY_DEFER_MAX_FRAMES: int = 2000
RELAY_DEFER_MAX_BYTES: int = 16 * 1024 * 1024
#: One budget's share of the hold (a quarter): a household kept over its
#: budget — or the pre-unseal ceiling under a junk flood — can't fill the
#: hold and push every other household's held frames out.
RELAY_DEFER_MAX_PER_BUDGET: int = RELAY_DEFER_MAX_FRAMES // 4
RELAY_DEFER_MAX_BYTES_PER_BUDGET: int = RELAY_DEFER_MAX_BYTES // 4
#: How long a frame may stay held (limiter clock). A budget frees within its
#: 60 s window, so a frame still held after this is on a budget kept
#: saturated: dropped (sync heals it), never kept forever.
RELAY_DEFER_MAX_AGE_S: float = 300.0
#: How often the background drain looks at held frames. A frame is run
#: again only once the budget that held it frees (its ``Retry-After``), so
#: this bounds the extra latency, not the retry rate.
RELAY_DEFER_POLL_S: float = 1.0
#: At most one "hold full" WARNING per this many seconds.
RELAY_DEFER_WARN_INTERVAL_S: float = 60.0


class _OverBudget(Exception):
    """A stage refused a relayed frame for a budget (internal).

    Carries the budget (``bucket``), when it frees, and how to run the
    frame again from that stage — so a retry never pays the buckets the
    earlier stages already charged. Never a ``ValueError``: nothing that
    drops refusals may swallow it.
    """

    def __init__(
        self,
        *,
        bucket: str,
        retry_after_s: float,
        resume: Callable[[], Awaitable[dict]],
    ) -> None:
        super().__init__(bucket)
        self.bucket = bucket
        self.retry_after_s = retry_after_s
        self.resume = resume


#: Set while :meth:`GfsRelayInbound.run_deferred` runs a held frame, so that
#: frame is not queued behind its own sender's held frames (itself).
_RUNNING_HELD: ContextVar[bool] = ContextVar("gfs_relay_running_held", default=False)


@dataclass(slots=True, eq=False)
class _Held:
    """One held frame (module-local, mutable: a retry refused again keeps
    its place with the new stage). Compared by identity."""

    resume: Callable[[], Awaitable[dict]]
    bucket: str
    size: int
    held_at: float


def _frame_bytes(frame: object) -> int:
    """About how much memory holding *frame* costs (its sealed strings or
    its bytes)."""
    if isinstance(frame, bytes | bytearray):
        return len(frame)
    if not isinstance(frame, dict):
        return 0
    body = frame.get("body")
    if isinstance(body, bytes | bytearray):
        return len(body)
    sealed = frame.get("sealed")
    if isinstance(sealed, dict):
        return sum(len(v) for v in sealed.values() if isinstance(v, str)) + sum(
            len(str(v)) for v in sealed.values() if isinstance(v, dict)
        )
    return 0


class GfsRelayInbound:
    """Open one relayed frame and route it to the pipeline or the
    invite-bootstrap coordinator."""

    __slots__ = (
        "_drain_task",
        "_due",
        "_federation",
        "_gfs_repo",
        "_held",
        "_held_bytes",
        "_held_on",
        "_hold_full_drops",
        "_hold_warned_at",
        "_invite",
        "_join",
        "_keywrap_private_key",
        "_rate_limiter",
        "_stop",
    )

    def __init__(
        self,
        *,
        federation: FederationService,
        keywrap_private_key: bytes,
        invite_coordinator: SpaceInviteTokenRedeemCoordinator,
        gfs_connection_repo: AbstractGfsConnectionRepo,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._federation = federation
        self._keywrap_private_key = keywrap_private_key
        self._invite = invite_coordinator
        self._gfs_repo = gfs_connection_repo
        self._rate_limiter = rate_limiter
        self._join: GfsJoinRequestCoordinator | None = None
        #: Frames held over a budget, oldest first, and their total size.
        self._held: deque[_Held] = deque()
        self._held_bytes = 0
        #: bucket → (frames, bytes) held on it (the per-budget share).
        self._held_on: dict[str, tuple[int, int]] = {}
        #: bucket → when (limiter clock) the frames held on it may run again.
        self._due: dict[str, float] = {}
        self._drain_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._hold_full_drops = 0
        self._hold_warned_at = float("-inf")

    def attach_join_requests(self, coordinator: GfsJoinRequestCoordinator) -> None:
        """Route "Ask to join" bodies (v_84) to *coordinator*. Unwired, they
        are dropped like any unknown body."""
        self._join = coordinator

    async def handle_frame(self, frame: dict, *, gfs_url: str = "") -> dict:
        """Inbound entry point for one relayed ``envelope`` frame.

        ``gfs_url`` is the base URL of the connection server whose socket
        carried the frame (bound per connection by the WebSocket
        supervisor). Raises :class:`ValueError` on any validation failure;
        the caller drops the blob.
        """
        if not self._keywrap_private_key:
            raise ValueError("no key-wrap key — cannot open relayed envelopes")
        return await self._run_or_hold(
            lambda: self._open_frame(frame, gfs_url=gfs_url), size=_frame_bytes(frame)
        )

    async def _open_frame(self, frame: dict, *, gfs_url: str) -> dict:
        if isinstance(frame, dict) and frame.get("type") == ENVELOPE_MEDIA_FRAME:
            # v_87 media rides its own bucket, ahead of (and never charged
            # to) the process-wide one the control envelopes share.
            return await self._handle_media(frame.get("body"))
        # Process-wide throttle BEFORE the unseal — the cheapest place to
        # shed a flood, and the only one available before we know which
        # family the blob belongs to. Over it, the frame is held (a list
        # append, still no open) and opened once the ceiling frees.
        self._charge(
            _PROCESS_WIDE_KEY,
            limit=BOOTSTRAP_INBOUND_LIMIT,
            resume=lambda: self._open_frame(frame, gfs_url=gfs_url),
        )
        body = unseal_envelope_body(
            envelope={
                "sealed": frame.get("sealed") if isinstance(frame, dict) else None
            },
            keywrap_private_key=self._keywrap_private_key,
        )
        if is_relay_envelope_body(body):
            return await self._relay_envelope(body, gfs_url=gfs_url)
        if is_pairing_relay_body(body):
            if self._rate_limiter is not None and not self._rate_limiter.is_allowed(
                _PAIRING_KEY,
                limit=BOOTSTRAP_BODY_INBOUND_LIMIT,
                window_s=BOOTSTRAP_INBOUND_WINDOW_S,
            ):
                raise ValueError("relayed pairing rate limit exceeded")
            return await self._dispatch_pairing(body, gfs_url=gfs_url)
        if body.get("kind") in JOIN_REQUEST_KINDS:
            # "Ask to join" a global space (v_84). No household-wide bucket
            # here: a flood at one space would starve every other one. The
            # coordinator budgets per sender and PER SPACE once the
            # signature proved who sent it (the process-wide throttle above
            # still sheds a raw flood before any unseal).
            if self._join is None:
                raise ValueError("gfs join requests are not wired on this household")
            return await self._join.handle_body(body, gfs_url=gfs_url)
        return await self._invite.handle_bootstrap_body(body, gfs_url=gfs_url)

    async def _relay_envelope(self, body: dict[str, Any], *, gfs_url: str) -> dict:
        """A relayed §24.11 envelope: its OWN bucket — see
        ``RELAY_ENVELOPE_INBOUND_LIMIT`` — then the pipeline. No per-sender
        budget here: the only id available before the pipeline runs is the
        envelope's UNVERIFIED ``from_instance``, and keying a budget on it
        would let anyone starve a household's traffic by claiming its id.
        The pipeline holds the verified sender to its own (step 5a)."""
        self._charge(
            _ENVELOPES_KEY,
            limit=RELAY_ENVELOPE_INBOUND_LIMIT,
            resume=lambda: self._relay_envelope(body, gfs_url=gfs_url),
        )
        return await self._dispatch_envelope(body, gfs_url=gfs_url)

    async def handle_mailbox_frame(
        self, frame: dict | bytes, *, keys: MailboxKeys
    ) -> dict:
        """Inbound entry point for one frame on OUR GFS mailbox (v_89, one per
        GFS, shared by our spaces), handed over by its anonymous claim socket.

        A JSON ``{type: "envelope", sealed}`` frame is opened with the
        mailbox's own X25519 key (never the key-wrap key) and must be a
        relayed §24.11 envelope — no invite, pairing or join bodies ride a
        mailbox. It goes to the unmodified pipeline with ``transport =
        TRANSPORT_GFS_MAILBOX``, so ``check_mailbox_sender`` takes it only
        from a sender whose member card we hold for its space. A
        binary ``\x00SHR`` frame is a media chunk addressed to the mailbox
        id, opened with the same key. Same budgets as the instance-addressed
        relay.

        v_91: a ``sealed`` that names an ``mr_suite`` is a multi-recipient
        frame (one body the GFS fanned out to many mailboxes, plus this
        mailbox's ``wrap`` of its key). Charged to the same bucket first;
        the wrap opens under the same mailbox key to the body key *K*, which
        opens the body; the pipeline then requires our tag in the ``"*:"``
        audience and a named space, and takes the payload as-is.
        Raises :class:`ValueError` on any refusal; a frame over its
        sender's budget is held (:data:`DEFERRED_RESPONSE`)."""
        return await self._run_or_hold(
            lambda: self._open_mailbox_frame(frame, keys=keys),
            size=_frame_bytes(frame),
        )

    async def _open_mailbox_frame(
        self, frame: dict | bytes, *, keys: MailboxKeys
    ) -> dict:
        if isinstance(frame, bytes | bytearray):
            return await self._handle_media(frame, keys=keys)
        if not isinstance(frame, dict) or frame.get("type") != "envelope":
            raise ValueError("unexpected frame on a mailbox")
        # Every frame is charged to this mailbox's bucket BEFORE any open
        # (see ``MAILBOX_INBOUND_LIMIT``).
        if self._rate_limiter is not None and not self._rate_limiter.is_allowed(
            f"{_MAILBOX_KEY}:{keys.mailbox_id}",
            limit=MAILBOX_INBOUND_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        ):
            raise MailboxFlooded("mailbox rate limit exceeded — frame dropped")
        sealed = frame.get("sealed")
        multi = False
        if is_multi_sealed(sealed):
            multi = True
            body = open_multi(sealed, keys.kem_private)
        else:
            body = unseal_envelope_body(
                envelope={"sealed": sealed},
                keywrap_private_key=keys.kem_private,
            )
        if not is_relay_envelope_body(body):
            raise ValueError("a mailbox carries relayed envelopes only")
        inner = body.get("envelope")
        if not isinstance(inner, dict):
            raise WrongTypeError("relayed federation envelope missing envelope body")
        from_instance = inner.get("from_instance")
        if not isinstance(from_instance, str) or not from_instance:
            raise ValueError("relayed federation envelope missing from_instance")
        return await self._dispatch_mailbox(
            from_instance, orjson.dumps(inner), multi=multi
        )

    async def _dispatch_mailbox(
        self, from_instance: str, raw: bytes, *, multi: bool
    ) -> dict:
        """One opened mailbox envelope through the pipeline. Its sender over
        its budget: held, to run again from here (the mailbox bucket and the
        open are already paid)."""
        self._wait_behind(
            from_instance,
            resume=lambda: self._dispatch_mailbox(from_instance, raw, multi=multi),
        )
        try:
            return await self._federation.handle_inbound_rtc(
                from_instance,
                raw,
                transport=TRANSPORT_GFS_MAILBOX,
                multi=multi,
            )
        except InboundRateLimitedError as exc:
            raise _OverBudget(
                bucket=exc.bucket,
                retry_after_s=exc.retry_after_s,
                resume=lambda: self._dispatch_mailbox(from_instance, raw, multi=multi),
            ) from None
        except ValueError as exc:
            # The routing facts only — never the payload, never the mailbox.
            log.info(
                "gfs_mailbox: rejected %r envelope from %s: %s",
                orjson.loads(raw).get("event_type"),
                from_instance,
                exc,
            )
            raise

    async def _handle_media(
        self, raw: object, *, keys: MailboxKeys | None = None
    ) -> dict:
        """Open one binary relay media frame and run it through the §24.11
        media tail — addressed to our instance id (key-wrap key) or, with
        *keys*, to one of our mailboxes (its own key). Raises
        :class:`ValueError` on any refusal (the WS client logs the reason,
        never the bytes)."""
        if not isinstance(raw, bytes | bytearray):
            raise TypeError("relayed media frame carries no body")
        limiter = self._rate_limiter
        if keys is not None:
            # A mailbox's media: its own bucket, every frame charged (see
            # ``MAILBOX_INBOUND_LIMIT``).
            if limiter is not None and not limiter.is_allowed(
                f"{_MAILBOX_MEDIA_KEY}:{keys.mailbox_id}",
                limit=RELAY_MEDIA_INBOUND_LIMIT,
                window_s=BOOTSTRAP_INBOUND_WINDOW_S,
            ):
                raise MailboxFlooded(
                    "mailbox media rate limit exceeded — frame dropped"
                )
            media_body = open_relay_media(
                bytes(raw),
                own_instance_id=keys.mailbox_id,
                keywrap_private_key=keys.kem_private,
            )
            return await self._dispatch_media(
                media_body, transport=TRANSPORT_GFS_MAILBOX, charge=False
            )
        if limiter is not None and not limiter.has_room(
            _MEDIA_JUNK_KEY,
            limit=RELAY_MEDIA_JUNK_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        ):
            raise ValueError("relayed media junk budget exhausted — frame dropped")
        if limiter is not None and not limiter.has_room(
            _MEDIA_KEY,
            limit=RELAY_MEDIA_INBOUND_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        ):
            # Held until the media budget frees (nothing charged yet).
            raise _OverBudget(
                bucket=_MEDIA_KEY,
                retry_after_s=limiter.retry_after_s(
                    _MEDIA_KEY, window_s=BOOTSTRAP_INBOUND_WINDOW_S
                ),
                resume=lambda: self._handle_media(raw),
            )
        try:
            media_body = open_relay_media(
                bytes(raw),
                own_instance_id=self._federation.own_instance_id,
                keywrap_private_key=self._keywrap_private_key,
            )
            # A sender over its budget is an ``_OverBudget``, never junk.
            return await self._dispatch_media(
                media_body, transport=TRANSPORT_GFS_RELAY, charge=True
            )
        except ValueError:
            if limiter is not None:
                limiter.is_allowed(
                    _MEDIA_JUNK_KEY,
                    limit=RELAY_MEDIA_JUNK_LIMIT,
                    window_s=BOOTSTRAP_INBOUND_WINDOW_S,
                )
            raise

    async def _dispatch_media(
        self, media_body: bytes, *, transport: str, charge: bool
    ) -> dict:
        """One opened media body through the §24.11 media tail; ``charge``
        spends the instance relay's media budget for a frame that went
        through. Its sender over its budget: held, to run again from here."""
        try:
            result = await self._federation.handle_inbound_media_relay(
                media_body, transport=transport
            )
        except InboundRateLimitedError as exc:
            raise _OverBudget(
                bucket=exc.bucket,
                retry_after_s=exc.retry_after_s,
                resume=lambda: self._dispatch_media(
                    media_body, transport=transport, charge=charge
                ),
            ) from None
        if charge and self._rate_limiter is not None:
            # Charged only now: a frame that went through.
            self._rate_limiter.is_allowed(
                _MEDIA_KEY,
                limit=RELAY_MEDIA_INBOUND_LIMIT,
                window_s=BOOTSTRAP_INBOUND_WINDOW_S,
            )
        return result

    async def _dispatch_pairing(self, body: dict[str, Any], *, gfs_url: str) -> dict:
        """Hand one relayed §11 pairing body to the inbox path's handler.

        No exemption for having ridden the relay: the handler runs the
        identical token / signature / expiry / single-use checks and, on
        top, requires the delivering connection to be the one the pairing
        session was issued with.
        """
        kind, inner = pairing_body_from_relay(body)
        delivered_via = await self._connection_id_for(gfs_url)
        if delivered_via is None:
            raise ValueError(
                "relayed pairing body did not arrive through one of our "
                "connection servers",
            )
        token = RELAY_DELIVERED_VIA.set(delivered_via)
        try:
            if kind == KIND_PAIRING_PEER_ACCEPT:
                return await self._federation.handle_peer_accept(
                    inner,
                    relayed_via=delivered_via,
                )
            return await self._federation.handle_peer_confirm(
                inner,
                relayed_via=delivered_via,
            )
        except ValueError as exc:
            # The reason only — never the body (it carries a token).
            log.info("gfs_relay: rejected a relayed %s: %s", kind, exc)
            raise
        finally:
            RELAY_DELIVERED_VIA.reset(token)

    async def _dispatch_envelope(self, body: dict[str, Any], *, gfs_url: str) -> dict:
        """Run one relayed §24.11 envelope through the normal pipeline.

        The sender is taken from the envelope's own ``from_instance`` and
        used only to LOOK UP the row; the pipeline's signature step then
        requires that row's identity key to have signed these exact bytes,
        so a claimed id buys nothing.
        """
        inner = body.get("envelope")
        if not isinstance(inner, dict):
            raise WrongTypeError("relayed federation envelope missing envelope body")
        from_instance = inner.get("from_instance")
        if not isinstance(from_instance, str) or not from_instance:
            raise ValueError("relayed federation envelope missing from_instance")
        log.debug(
            "gfs_relay: inbound %r envelope from %s",
            inner.get("event_type"),
            from_instance,
        )
        self._wait_behind(
            from_instance,
            resume=lambda: self._dispatch_envelope(body, gfs_url=gfs_url),
        )
        delivered_via = await self._connection_id_for(gfs_url)
        token = RELAY_DELIVERED_VIA.set(delivered_via)
        try:
            return await self._federation.handle_inbound_rtc(
                from_instance,
                orjson.dumps(inner),
                # These bytes may have sat in the relay's queue for up to
                # its TTL before the socket came back, so the timestamp
                # step judges them against the wider relay window.
                transport=TRANSPORT_GFS_RELAY,
            )
        except InboundRateLimitedError as exc:
            # The verified sender is over its budget: held, to run again
            # from here (the relay buckets and the unseal are paid).
            raise _OverBudget(
                bucket=exc.bucket,
                retry_after_s=exc.retry_after_s,
                resume=lambda: self._dispatch_envelope(body, gfs_url=gfs_url),
            ) from None
        except ValueError as exc:
            # Dropped, but never silently: the reason and the routing
            # fields only — never the payload. INFO because a rejection is
            # a normal outcome (a stale queued frame, an unknown peer, a
            # peer we did not opt into the relay with).
            log.info(
                "gfs_relay: rejected %r envelope from %s: %s",
                inner.get("event_type"),
                from_instance,
                exc,
            )
            raise
        finally:
            RELAY_DELIVERED_VIA.reset(token)

    # ── Held frames ──────────────────────────────────────────────────────

    @property
    def deferred_count(self) -> int:
        """Frames held over a budget right now."""
        return len(self._held)

    def _now(self) -> float:
        """The clock budgets are judged on: the limiter's (``Retry-After``
        is in its terms), else the monotonic one."""
        return (
            self._rate_limiter.now
            if self._rate_limiter is not None
            else time.monotonic()
        )

    def _charge(
        self, key: str, *, limit: int, resume: Callable[[], Awaitable[dict]]
    ) -> None:
        """Spend one of *key*'s budget, or raise :class:`_OverBudget` with
        *resume* (nothing is charged on a refusal)."""
        limiter = self._rate_limiter
        if limiter is None or limiter.is_allowed(
            key, limit=limit, window_s=BOOTSTRAP_INBOUND_WINDOW_S
        ):
            return
        raise _OverBudget(
            bucket=key,
            retry_after_s=limiter.retry_after_s(
                key, window_s=BOOTSTRAP_INBOUND_WINDOW_S
            ),
            resume=resume,
        )

    async def _run_or_hold(
        self, run: Callable[[], Awaitable[dict]], *, size: int
    ) -> dict:
        try:
            return await run()
        except _OverBudget as over:
            self._hold(over, size=size)
            return dict(DEFERRED_RESPONSE)

    def _wait_behind(
        self, sender: str, *, resume: Callable[[], Awaitable[dict]]
    ) -> None:
        """Per-sender order: while *sender* (the envelope's claimed id —
        the pipeline verifies it) has frames held on its budget, a new frame
        of its queues behind them instead of taking a slot that just freed
        (a delete must not land before the create it refers to). A claimed
        id only ever holds the claimant's own frame, inside that budget's
        share."""
        bucket = instance_bucket(sender, media=False)
        if _RUNNING_HELD.get() or not self._held_on.get(bucket):
            return
        raise _OverBudget(bucket=bucket, retry_after_s=0, resume=resume)

    def _track(self, bucket: str, size: int, sign: int) -> None:
        frames, nbytes = self._held_on.get(bucket, (0, 0))
        frames, nbytes = frames + sign, nbytes + sign * size
        if frames > 0:
            self._held_on[bucket] = (frames, nbytes)
        else:
            self._held_on.pop(bucket, None)
        self._held_bytes += sign * size

    def _hold(self, over: _OverBudget, *, size: int) -> None:
        """Hold one over-budget frame, or drop it (``ValueError``) past the
        hold's bounds — the whole hold's, or its budget's share — or once
        stopped."""
        if self._stop.is_set():
            raise ValueError("relay frame over budget, not held (shutting down)")
        frames_on, bytes_on = self._held_on.get(over.bucket, (0, 0))
        if (
            len(self._held) >= RELAY_DEFER_MAX_FRAMES
            or self._held_bytes + size > RELAY_DEFER_MAX_BYTES
            or frames_on >= RELAY_DEFER_MAX_PER_BUDGET
            or bytes_on + size > RELAY_DEFER_MAX_BYTES_PER_BUDGET
        ):
            self._note_hold_full()
            raise ValueError("relay frame over budget, not held (the hold is full)")
        self._held.append(
            _Held(
                resume=over.resume, bucket=over.bucket, size=size, held_at=self._now()
            )
        )
        self._track(over.bucket, size, +1)
        # ``retry_after_s`` is 0 for a frame queued behind its sender's held
        # ones: it keeps their due time rather than pushing it back.
        self._due[over.bucket] = max(
            self._due.get(over.bucket, float("-inf")),
            self._now() + over.retry_after_s,
        )
        log.debug("gfs_relay: holding a frame over the %s budget", over.bucket)
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.get_running_loop().create_task(
                self._drain_loop(), name="gfs-relay-held-frames"
            )

    def _note_hold_full(self) -> None:
        """A dropped frame is a real loss (sync heals it at best): WARN, at
        most once a minute, with the count — never the frame."""
        self._hold_full_drops += 1
        now = time.monotonic()
        if now - self._hold_warned_at < RELAY_DEFER_WARN_INTERVAL_S:
            return
        log.warning(
            "gfs_relay: %d relayed frame(s) over budget dropped — %d held "
            "(%d bytes) already",
            self._hold_full_drops,
            len(self._held),
            self._held_bytes,
        )
        self._hold_full_drops = 0
        self._hold_warned_at = now

    async def run_deferred(self) -> int:
        """One pass over the held frames: run each whose budget has freed,
        oldest first; one refused again keeps its place (and holds back the
        rest on that budget this pass). Returns how many went through.
        Exposed for tests; the background drain calls it."""
        ran = 0
        for item in list(self._held):
            if self._now() - item.held_at > RELAY_DEFER_MAX_AGE_S:
                # Its budget stayed saturated: never kept forever.
                log.info("gfs_relay: dropped a frame held too long (%s)", item.bucket)
                self._release(item)
                continue
            if self._due.get(item.bucket, float("-inf")) > self._now():
                continue
            running = _RUNNING_HELD.set(True)
            try:
                await item.resume()
            except _OverBudget as over:
                self._track(item.bucket, item.size, -1)
                item.resume, item.bucket = over.resume, over.bucket
                self._track(item.bucket, item.size, +1)
                self._due[over.bucket] = self._now() + max(over.retry_after_s, 1)
                continue
            except ValueError as exc:
                # Refused for good on its retry (a gate, a stale timestamp):
                # the reason only, never the frame.
                log.info("gfs_relay: dropped a held frame: %s", exc)
            except Exception:
                log.warning("gfs_relay: a held frame failed", exc_info=True)
            else:
                ran += 1
            finally:
                _RUNNING_HELD.reset(running)
            self._release(item)
        if not self._held:
            self._due.clear()
        return ran

    def _release(self, item: _Held) -> None:
        self._held.remove(item)
        self._track(item.bucket, item.size, -1)

    async def _drain_loop(self) -> None:
        """Run held frames as their budgets free, until none is left or
        :meth:`stop` (the ``_stop`` event)."""
        while self._held and not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RELAY_DEFER_POLL_S)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.run_deferred()
            except Exception:  # pragma: no cover - run_deferred catches per frame
                log.warning("gfs_relay: held-frame pass failed", exc_info=True)

    async def stop(self) -> None:
        """Stop the held-frame drain (app cleanup, before the database
        closes). Frames still held are dropped — sync heals them, as for a
        frame lost to a restart."""
        self._stop.set()
        task, self._drain_task = self._drain_task, None
        if task is not None:
            try:
                # On timeout ``wait_for`` cancels the drain; a cancellation
                # of this caller propagates.
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                log.warning("gfs_relay: held-frame drain did not stop in time")
        if self._held:
            log.info(
                "gfs_relay: dropping %d held frame(s) on shutdown", len(self._held)
            )
        self._held.clear()
        self._held_bytes = 0
        self._held_on.clear()
        self._due.clear()

    async def _connection_id_for(self, gfs_url: str) -> str | None:
        """Our own ``gfs_connections.id`` for the server at *gfs_url*."""
        if not gfs_url:
            return None
        want = normalize_gfs_base(gfs_url)
        for conn in await self._gfs_repo.list_active():
            if conn.status == "active" and normalize_gfs_base(conn.inbox_url) == want:
                return conn.id
        return None


__all__ = [
    "DEFERRED_RESPONSE",
    "RELAY_DELIVERED_VIA",
    "RELAY_MEDIA_INBOUND_LIMIT",
    "RELAY_MEDIA_JUNK_LIMIT",
    "GfsRelayInbound",
]
