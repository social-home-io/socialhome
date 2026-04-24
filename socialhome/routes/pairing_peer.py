"""Peer-to-peer pairing bootstrap routes (§11).

The §24.11 federation inbox path can't carry ``PAIRING_ACCEPT`` /
``PAIRING_CONFIRM`` during the initial handshake — neither side has
the other's directional session keys yet, and the inbound pipeline
starts with a :class:`RemoteInstance` lookup that fails before the
pair exists. These two routes are the dedicated bootstrap transport:
public (signature-authenticated), plaintext JSON, Ed25519-signed.

See ``docs/protocol/pairing.md`` for the full flow.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..app_keys import federation_service_key
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)


# Map a ``ValueError`` message fragment → HTTP status. First match wins.
_STATUS_RULES: tuple[tuple[str, int], ...] = (
    ("Missing required fields", 400),
    ("Malformed signature", 400),
    ("Malformed dh", 400),
    ("Malformed identity", 400),
    ("No pending pairing", 404),
    ("RemoteInstance not found", 404),
    ("signature verification failed", 403),
    ("does not match identity_pk", 403),
    ("does not match stored identity_pk", 403),
    ("has expired", 410),
    ("cannot accept peer-accept", 409),
)


def _classify(msg: str) -> int:
    for needle, status in _STATUS_RULES:
        if needle in msg:
            return status
    return 400


class PairingPeerAcceptView(BaseView):
    """``POST /api/pairing/peer-accept`` — B → A bootstrap.

    Public endpoint (envelope body signature is the auth). Delivers B's
    identity + DH public keys to A so A can materialise its local
    ``RemoteInstance`` and surface the SAS to its admin UI.
    """

    async def post(self) -> web.Response:
        try:
            body = await self.body()
        except Exception as exc:
            log.debug("peer-accept: body parse error: %s", exc)
            return web.json_response({"error": "bad_body"}, status=400)
        try:
            result = await self.svc(federation_service_key).handle_peer_accept(body)
        except ValueError as exc:
            msg = str(exc)
            status = _classify(msg)
            log.debug("peer-accept: rejected status=%d reason=%s", status, msg)
            return error_response(status, "UNPROCESSABLE", msg)
        return web.json_response(result, status=200)


class PairingPeerConfirmView(BaseView):
    """``POST /api/pairing/peer-confirm`` — A → B bootstrap.

    Public endpoint. Delivered after A's admin enters the SAS code;
    lets B flip its local ``PENDING_RECEIVED`` status to
    ``CONFIRMED`` and close the handshake loop.
    """

    async def post(self) -> web.Response:
        try:
            body = await self.body()
        except Exception as exc:
            log.debug("peer-confirm: body parse error: %s", exc)
            return web.json_response({"error": "bad_body"}, status=400)
        try:
            result = await self.svc(federation_service_key).handle_peer_confirm(body)
        except ValueError as exc:
            msg = str(exc)
            status = _classify(msg)
            log.debug("peer-confirm: rejected status=%d reason=%s", status, msg)
            return error_response(status, "UNPROCESSABLE", msg)
        return web.json_response(result, status=200)
