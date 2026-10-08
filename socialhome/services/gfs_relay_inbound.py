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
* **Invite bootstrap bodies** (``KIND_REDEEM*``) — delegated to the
  :class:`~socialhome.federation.invite_token_redeem
  .SpaceInviteTokenRedeemCoordinator`, which owns their validation.

Throttles reuse the coordinator's limiter keys and constants unchanged: a
process-wide bucket BEFORE the unseal (a flood costs a list append, not an
AES-GCM open) and a per-family bucket after it, so space traffic and
redeems never starve each other.

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
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import orjson

from ..federation.gfs_relay_transport import is_relay_envelope_body
from ..federation.inbound_validator import TRANSPORT_GFS_RELAY
from ..federation.invite_bootstrap import unseal_envelope_body
from ..federation.invite_token_redeem import (
    BOOTSTRAP_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_WINDOW_S,
    RELAY_ENVELOPE_INBOUND_LIMIT,
)
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from .gfs_envelope_sender import normalize_gfs_base

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
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


class GfsRelayInbound:
    """Open one relayed frame and route it to the pipeline or the
    invite-bootstrap coordinator."""

    __slots__ = (
        "_federation",
        "_keywrap_private_key",
        "_invite",
        "_gfs_repo",
        "_rate_limiter",
    )

    def __init__(
        self,
        *,
        federation: "FederationService",
        keywrap_private_key: bytes,
        invite_coordinator: "SpaceInviteTokenRedeemCoordinator",
        gfs_connection_repo: AbstractGfsConnectionRepo,
        rate_limiter: "RateLimiter | None" = None,
    ) -> None:
        self._federation = federation
        self._keywrap_private_key = keywrap_private_key
        self._invite = invite_coordinator
        self._gfs_repo = gfs_connection_repo
        self._rate_limiter = rate_limiter

    async def handle_frame(self, frame: dict, *, gfs_url: str = "") -> dict:
        """Inbound entry point for one relayed ``envelope`` frame.

        ``gfs_url`` is the base URL of the connection server whose socket
        carried the frame (bound per connection by the WebSocket
        supervisor). Raises :class:`ValueError` on any validation failure;
        the caller drops the blob.
        """
        if not self._keywrap_private_key:
            raise ValueError("no key-wrap key — cannot open relayed envelopes")
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
        return await self._invite.handle_bootstrap_body(body, gfs_url=gfs_url)

    async def _dispatch_envelope(self, body: dict[str, Any], *, gfs_url: str) -> dict:
        """Run one relayed §24.11 envelope through the normal pipeline.

        The sender is taken from the envelope's own ``from_instance`` and
        used only to LOOK UP the row; the pipeline's signature step then
        requires that row's identity key to have signed these exact bytes,
        so a claimed id buys nothing.
        """
        inner = body.get("envelope")
        if not isinstance(inner, dict):
            raise ValueError("relayed federation envelope missing envelope body")
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


__all__ = ["RELAY_DELIVERED_VIA", "GfsRelayInbound"]
