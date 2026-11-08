"""Anonymous GFS mailboxes — register + claim socket (GFS redesign PR 1).

* ``POST /gfs/mailbox/register`` — pin a mailbox key with proof of
  possession (:meth:`GfsMailboxService.register`). ``200 {"status":
  "registered", "mailbox_id"}`` for a new and an existing mailbox alike;
  ``400`` malformed / unknown ``sig_suite`` / stale ``ts`` / id not the
  key's hash, ``403`` bad signature, ``409`` replayed nonce or an id an
  instance holds, ``429`` per-address limits.
* ``GET /gfs/mailbox/ws`` — the anonymous claim socket, separate from the
  identified ``/gfs/ws``. The server speaks first::

      → {"type": "mailbox_challenge", "challenge": "<b64url 32 B>",
         "gfs_instance_id": "<id>", "sig_suites": ["ed25519"]}
      ← {"type": "mailbox_claim", "mailbox_id": "<id>", "sig_suite": "ed25519",
         "sig": "<b64url>", "accepts": ["envelope_media", ...]}
      → {"type": "mailbox_claimed"}

  then queued and live frames for that ONE mailbox: ``{"type": "envelope",
  "sealed": {...}}`` text frames and, when ``accepts`` lists
  ``envelope_media``, binary ``\\x00SHR`` media frames unchanged. A second
  claim on the same socket is ignored (one mailbox per socket — a shared
  socket would link a household's spaces live). Unknown mailbox and wrong
  key close alike with ``4401 auth-failed`` after the same work; an unknown
  ``sig_suite`` with ``4401 unsupported-suite``; no claim within
  :data:`CLAIM_TIMEOUT_SECONDS` with ``4408``. A newer claim of the same
  mailbox replaces the older socket (``4409``). aiohttp pings every 30 s,
  inside Cloudflare's 100 s idle timeout.

Neither route logs the mailbox id: an application log line next to the
access log's address would tie the two together.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

import orjson
from aiohttp import WSMsgType, web

from ... import crypto
from ...domain.gfs_mailbox import (
    FRAME_MAILBOX_CHALLENGE,
    FRAME_MAILBOX_CLAIM,
    FRAME_MAILBOX_CLAIMED,
    SUPPORTED_MAILBOX_SIG_SUITES,
    UnsupportedMailboxSigSuite,
    validate_mailbox_sig_suite,
)
from ...domain.subscriber_accepts import (
    ACCEPTS_ENVELOPE_MEDIA,
    ACCEPTS_WS_BINARY1,
    parse_accepts,
)
from .. import app_keys as K
from ..mailboxes import MailboxRefused
from .base import GfsBaseView
from .ws import (
    WS_AUTH_FAILED_REASON,
    WS_CLOSE_AUTH_FAILED,
    WS_CLOSE_HELLO_TIMEOUT,
    WS_CLOSE_PROTOCOL_VIOLATION,
)

log = logging.getLogger(__name__)

#: How long the claim may take after the challenge went out.
CLAIM_TIMEOUT_SECONDS: float = 5.0
#: The largest frame a mailbox client sends (its claim).
CLAIM_MAX_MSG_BYTES: int = 4096
#: Largest registration body.
REGISTER_MAX_BODY_BYTES: int = 2048
#: Protocol-level ping interval — inside Cloudflare's 100 s WS idle timeout.
MAILBOX_WS_HEARTBEAT_S: float = 30.0


class MailboxRegisterView(GfsBaseView):
    """``POST /gfs/mailbox/register``."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_mailbox_service_key)
        if not svc.enabled:
            raise web.HTTPNotFound()
        raw = await self.bounded_raw(REGISTER_MAX_BODY_BYTES)
        try:
            body = orjson.loads(raw)
        except ValueError:
            return web.json_response({"error": "invalid_json"}, status=400)
        resolver = self.svc(K.gfs_client_ip_key)
        try:
            mailbox_id = await svc.register(
                body, client_bucket=resolver.bucket(self.request)
            )
        except MailboxRefused as exc:
            resp = web.json_response({"error": exc.error}, status=exc.status)
            if exc.status == 429:
                resp.headers["Retry-After"] = "3600"
            return resp
        return web.json_response({"status": "registered", "mailbox_id": mailbox_id})


class MailboxWebSocketView(web.View):
    """``GET /gfs/mailbox/ws`` — the anonymous claim socket."""

    async def get(self) -> web.StreamResponse:
        app = self.request.app
        svc = app[K.gfs_mailbox_service_key]
        if not svc.enabled:
            raise web.HTTPNotFound()
        ws = web.WebSocketResponse(
            heartbeat=MAILBOX_WS_HEARTBEAT_S, max_msg_size=CLAIM_MAX_MSG_BYTES
        )
        await ws.prepare(self.request)
        claimed = await self._claim(ws, svc)
        if claimed is None:
            return ws
        mailbox_id, accepts = claimed
        registry = app[K.gfs_mailbox_ws_registry_key]
        await ws.send_json({"type": FRAME_MAILBOX_CLAIMED})
        await registry.register(
            mailbox_id,
            ws,
            binary=ACCEPTS_WS_BINARY1 in accepts,
            envelope_media=ACCEPTS_ENVELOPE_MEDIA in accepts,
        )
        try:
            await svc.note_claimed(mailbox_id)
            # Queued envelopes first (awaited, in order), then media in its
            # own background task — the relay's drain does both.
            await app[K.gfs_envelope_relay_key].drain(mailbox_id)
            async for msg in ws:
                # Nothing is expected from the client after its claim: a
                # second claim is ignored (one mailbox per socket), pings
                # are handled by aiohttp.
                if msg.type == WSMsgType.ERROR:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("gfs.mailbox.ws: socket loop failed", exc_info=True)
        finally:
            await registry.unregister(mailbox_id, ws)
        return ws

    async def _claim(
        self, ws: web.WebSocketResponse, svc
    ) -> tuple[str, tuple[str, ...]] | None:
        challenge = crypto.b64url_encode(secrets.token_bytes(32))
        await ws.send_json(
            {
                "type": FRAME_MAILBOX_CHALLENGE,
                "challenge": challenge,
                "gfs_instance_id": svc.gfs_instance_id,
                "sig_suites": sorted(SUPPORTED_MAILBOX_SIG_SUITES),
            }
        )
        try:
            msg = await ws.receive(timeout=CLAIM_TIMEOUT_SECONDS)
        except TimeoutError:
            await ws.close(code=WS_CLOSE_HELLO_TIMEOUT, message=b"claim-timeout")
            return None
        if msg.type is not WSMsgType.TEXT:
            await ws.close(code=WS_CLOSE_PROTOCOL_VIOLATION, message=b"claim-required")
            return None
        try:
            claim = orjson.loads(msg.data)
        except orjson.JSONDecodeError:
            await ws.close(code=WS_CLOSE_PROTOCOL_VIOLATION, message=b"claim-not-json")
            return None
        if not isinstance(claim, dict) or claim.get("type") != FRAME_MAILBOX_CLAIM:
            await ws.close(code=WS_CLOSE_PROTOCOL_VIOLATION, message=b"claim-required")
            return None
        try:
            validate_mailbox_sig_suite(claim.get("sig_suite"))
        except UnsupportedMailboxSigSuite:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"unsupported-suite")
            return None
        mailbox_id = await svc.verify_claim(claim, challenge)
        if mailbox_id is None:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=WS_AUTH_FAILED_REASON)
            return None
        return mailbox_id, parse_accepts(claim.get("accepts"))
