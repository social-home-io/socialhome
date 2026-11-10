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

Throttles reuse the coordinator's limiter keys and constants unchanged: a
process-wide bucket BEFORE the unseal (a flood costs a list append, not an
AES-GCM open) and a per-family bucket after it, so space traffic,
redeems and pairing bodies never starve each other (pairing bodies get the
bootstrap-body budget, :data:`BOOTSTRAP_BODY_INBOUND_LIMIT`, under their
own key).

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

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import orjson

from ..domain.errors import WrongTypeError
from ..federation.gfs_mailbox import MailboxFlooded, MailboxKeys
from ..federation.gfs_relay_media import open_relay_media
from ..federation.gfs_relay_transport import is_relay_envelope_body
from ..federation.inbound_validator import TRANSPORT_GFS_MAILBOX, TRANSPORT_GFS_RELAY
from ..federation.invite_bootstrap import JOIN_REQUEST_KINDS, unseal_envelope_body
from ..federation.invite_token_redeem import (
    BOOTSTRAP_BODY_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_WINDOW_S,
    RELAY_ENVELOPE_INBOUND_LIMIT,
)
from ..federation.multi_recipient import (
    is_multi_sealed,
    is_valid_kid,
    open_multi,
    require_mr_suite,
)
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

#: v_90 — resolves a multi-recipient frame's ``kid`` to ``(space_id, epoch,
#: key)`` over our recent space epochs, or ``None``
#: (``SpaceContentEncryption.mr_key_for_kid``).
MrKeyForKid = Callable[[str], Awaitable[tuple[str, int, bytes] | None]]


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
MAILBOX_INBOUND_LIMIT: int = RELAY_ENVELOPE_INBOUND_LIMIT
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


class GfsRelayInbound:
    """Open one relayed frame and route it to the pipeline or the
    invite-bootstrap coordinator."""

    __slots__ = (
        "_federation",
        "_gfs_repo",
        "_invite",
        "_join",
        "_keywrap_private_key",
        "_mr_key_for_kid",
        "_rate_limiter",
    )

    def __init__(
        self,
        *,
        federation: FederationService,
        keywrap_private_key: bytes,
        invite_coordinator: SpaceInviteTokenRedeemCoordinator,
        gfs_connection_repo: AbstractGfsConnectionRepo,
        rate_limiter: RateLimiter | None = None,
        mr_key_for_kid: MrKeyForKid | None = None,
    ) -> None:
        self._federation = federation
        self._keywrap_private_key = keywrap_private_key
        self._invite = invite_coordinator
        self._gfs_repo = gfs_connection_repo
        self._rate_limiter = rate_limiter
        #: v_90 — kid → ``(space_id, epoch, key)`` of a multi-recipient
        #: frame (``SpaceContentEncryption.mr_key_for_kid``). Unwired, multi
        #: frames are refused.
        self._mr_key_for_kid = mr_key_for_kid
        self._join: GfsJoinRequestCoordinator | None = None

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
        if isinstance(frame, dict) and frame.get("type") == ENVELOPE_MEDIA_FRAME:
            # v_87 media rides its own bucket, ahead of (and never charged
            # to) the process-wide one the control envelopes share.
            return await self._handle_media(frame.get("body"))
        # Process-wide throttle BEFORE the unseal — the cheapest place to
        # shed a flood, and the only one available before we know which
        # family the blob belongs to.
        if self._rate_limiter is not None and not self._rate_limiter.is_allowed(
            _PROCESS_WIDE_KEY,
            limit=BOOTSTRAP_INBOUND_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        ):
            raise ValueError("invite bootstrap inbound rate limit exceeded")
        body = unseal_envelope_body(
            envelope={
                "sealed": frame.get("sealed") if isinstance(frame, dict) else None
            },
            keywrap_private_key=self._keywrap_private_key,
        )
        if is_relay_envelope_body(body):
            # Its OWN bucket — see ``RELAY_ENVELOPE_INBOUND_LIMIT``. No
            # per-sender throttle: the only id available before the
            # pipeline runs is the envelope's UNVERIFIED ``from_instance``,
            # and keying a budget on it would let anyone starve a
            # household's traffic by claiming its id.
            if self._rate_limiter is not None and not self._rate_limiter.is_allowed(
                _ENVELOPES_KEY,
                limit=RELAY_ENVELOPE_INBOUND_LIMIT,
                window_s=BOOTSTRAP_INBOUND_WINDOW_S,
            ):
                raise ValueError("invite bootstrap envelopes rate limit exceeded")
            return await self._dispatch_envelope(body, gfs_url=gfs_url)
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

        v_90: a ``sealed`` that names an ``mr_suite`` is a multi-recipient
        frame (one body the GFS fanned out to many mailboxes). Charged to the
        same bucket first; its ``kid`` resolves to one of our space epochs'
        multi-recipient keys, which opens it, and the pipeline then requires
        ``to_instance == "*"`` and that space, and decrypts under that key.
        Raises :class:`ValueError` on any refusal."""
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
        multi: tuple[str, bytes] | None = None
        if is_multi_sealed(sealed):
            body, multi = await self._open_multi(sealed)
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
        try:
            return await self._federation.handle_inbound_rtc(
                from_instance,
                orjson.dumps(inner),
                transport=TRANSPORT_GFS_MAILBOX,
                multi=multi,
            )
        except ValueError as exc:
            # The routing facts only — never the payload, never the mailbox.
            log.info(
                "gfs_mailbox: rejected %r envelope from %s: %s",
                inner.get("event_type"),
                from_instance,
                exc,
            )
            raise

    async def _open_multi(self, sealed: dict) -> tuple[dict, tuple[str, bytes]]:
        """Open a v_90 multi-recipient seal → ``(relay body, (space_id,
        key))``. The suite is judged first (no lookup for an unknown one),
        then the kid resolves over our recent space epochs; an unknown kid
        is refused. Logs the refusal type only — never the kid or the
        mailbox."""
        require_mr_suite(sealed)
        if self._mr_key_for_kid is None:
            raise ValueError("multi-recipient frames are not wired on this household")
        kid = sealed.get("kid")
        if not is_valid_kid(kid):
            # The shape first: a junk kid never costs a lookup (or a cache
            # rebuild).
            raise ValueError("multi-recipient frame with a malformed kid")
        hit = await self._mr_key_for_kid(kid)
        if hit is None:
            log.info(
                "gfs_mailbox: dropped a multi-recipient frame under no key we hold"
            )
            raise ValueError("multi-recipient frame under no space key we hold")
        space_id, _epoch, key = hit
        return open_multi(sealed, key), (space_id, key)

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
            return await self._federation.handle_inbound_media_relay(
                media_body,
                transport=TRANSPORT_GFS_MAILBOX,
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
            raise ValueError("relayed media rate limit exceeded")
        try:
            media_body = open_relay_media(
                bytes(raw),
                own_instance_id=self._federation.own_instance_id,
                keywrap_private_key=self._keywrap_private_key,
            )
            result = await self._federation.handle_inbound_media_relay(media_body)
        except ValueError:
            if limiter is not None:
                limiter.is_allowed(
                    _MEDIA_JUNK_KEY,
                    limit=RELAY_MEDIA_JUNK_LIMIT,
                    window_s=BOOTSTRAP_INBOUND_WINDOW_S,
                )
            raise
        if self._rate_limiter is not None:
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
    "RELAY_DELIVERED_VIA",
    "RELAY_MEDIA_INBOUND_LIMIT",
    "RELAY_MEDIA_JUNK_LIMIT",
    "GfsRelayInbound",
]
