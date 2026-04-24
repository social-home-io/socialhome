"""Federation inbound envelope routes — /federation/inbox/{inbox_id} (section 24.11).

This is the single inbound entry point for federation. The full
validation pipeline (JSON parse -> timestamp skew -> instance lookup ->
signature verify -> replay cache -> decrypt -> dispatch) lives in
:class:`FederationService.handle_inbound_envelope` — this route is a
thin shim that forwards the raw body and converts the service's
canonical ``ValueError`` rejections into HTTP 400 / 403 / 410.

Authentication: the path is in ``_DEFAULT_PUBLIC_PATHS`` because the
envelope is itself authenticated (Ed25519 over the canonical bytes).
The auth middleware bypass is the contract — no ``Authorization``
header is expected.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .. import app_keys as K
from .base import BaseView

log = logging.getLogger(__name__)


# Substrings within a ValueError message that map to specific status codes.
# Order matters — first match wins.
_STATUS_CODE_RULES: tuple[tuple[str, int], ...] = (
    ("Invalid JSON", 400),
    ("Missing required fields", 400),
    ("Unparseable timestamp", 400),
    ("Unknown event_type", 400),
    ("No instance found", 404),
    ("Timestamp skew too large", 410),  # gone — too old
    ("Replay detected", 410),  # gone — already saw this msg_id
    ("Invalid envelope signature", 403),
    ("banned from space", 403),
    ("Failed to decrypt", 400),
    ("Decrypted payload", 400),
    ("Malformed encrypted payload", 400),
)


def _classify(msg: str) -> int:
    for needle, status in _STATUS_CODE_RULES:
        if needle in msg:
            return status
    return 400


class FederationInboxView(BaseView):
    """POST /federation/inbox/{inbox_id} — federation envelope arrives here.

    Returns ``{"status":"ok"}`` on successful dispatch (200), or
    an error code on validation failure. All errors are silent
    on the client side beyond the status — we never echo back the
    envelope or details that would help an attacker probe.
    """

    async def post(self) -> web.Response:
        inbox_id = self.match("inbox_id")
        try:
            raw_body = await self.request.read()
        except Exception as exc:
            log.debug("federation inbox: body read error: %s", exc)
            return web.json_response({"error": "bad_body"}, status=400)

        if len(raw_body) > 1 * 1024 * 1024:  # 1 MiB cap
            return web.json_response(
                {"error": "envelope_too_large"},
                status=413,
            )

        federation_service = self.request.app.get(K.federation_service_key)
        if federation_service is None:
            log.warning(
                "federation inbox: service not yet wired (inbox_id=%s)",
                inbox_id,
            )
            return web.json_response(
                {"error": "service_unavailable"},
                status=503,
            )

        try:
            result = await federation_service.handle_inbound_envelope(
                inbox_id,
                raw_body,
            )
        except ValueError as exc:
            status = _classify(str(exc))
            log.debug(
                "federation inbox: rejected inbox_id=%s status=%d reason=%s",
                inbox_id,
                status,
                exc,
            )
            return web.json_response({"error": str(exc)}, status=status)
        except Exception:
            log.exception(
                "federation inbox: unexpected error (inbox_id=%s)",
                inbox_id,
            )
            return web.json_response(
                {"error": "internal"},
                status=500,
            )

        return web.json_response(result)
