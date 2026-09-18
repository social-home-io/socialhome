"""SH↔GFS WebSocket push route (``GET /gfs/ws``, spec §24.12).

A paired Social Home household opens one persistent WebSocket against
this endpoint so the GFS can push relay events to it without an HTTPS
callback to ``inbox_url``. The WS is **one-way (GFS → SH)**: after the
signed hello frame the SH never sends application frames. Heartbeat is
WebSocket-protocol-level via aiohttp's ``heartbeat=30.0``.

SH → GFS calls (pair, publish, subscribe, report, appeal) keep using
the existing REST endpoints in :mod:`.relay` — request/response semantics
fit those calls and a WS RPC envelope would only add ``request_id``
bookkeeping for no gain.

Hello frame (must arrive within 5 s of upgrade)::

    {"type": "hello", "instance_id": "<id>", "ts": <unix>, "sig": "<b64url>"}

``sig`` is ``Ed25519(public_key, f"{instance_id}|{ts}")`` and ``ts`` must
be within ±300 s of the GFS clock — same convention as §24.11. On
verification failure the GFS closes with WebSocket close code ``4401``;
on hello timeout, ``4408``.

Push frames (sent by :class:`GfsFederationService._fan_out`)::

    {"type": "relay", "space_id": ..., "event_type": ..., "payload": ...}

The frame is deliberately identity-free: the GFS never learns which
household relayed a space event, so it has nothing to forward.

Fire-and-forget — the SH never acks at the application layer.
"""

from __future__ import annotations

import asyncio
import orjson
import logging
import time

from aiohttp import WSMsgType, web

from ... import crypto
from .. import app_keys as K

log = logging.getLogger(__name__)


HELLO_TIMEOUT_SECONDS = 5.0
TIMESTAMP_WINDOW_SECONDS = 300

WS_CLOSE_AUTH_FAILED = 4401
WS_CLOSE_HELLO_TIMEOUT = 4408
WS_CLOSE_PROTOCOL_VIOLATION = 4400


class GfsWebSocketView(web.View):
    """``GET /gfs/ws`` — upgrade to a GFS→SH push WebSocket."""

    async def get(self) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(self.request)

        instance_id = await self._authenticate(ws)
        if instance_id is None:
            return ws

        registry = self.request.app[K.gfs_ws_registry_key]
        fed_repo = self.request.app[K.gfs_fed_repo_key]
        await registry.register(instance_id, ws)
        await fed_repo.upsert_rtc_connection(instance_id, transport="websocket")

        # Phase 5b-d — now that this household's socket is up, ask the owner of
        # every space it subscribes to to re-run the content-key handoff: a
        # handoff fanned out while this socket was down was lost outright (the
        # HTTPS-inbox fallback can't carry a relay frame). Strictly AFTER the
        # hello verified and the socket registered — an unauthenticated caller
        # must never be able to trigger a fan-out. Dispatched as a background
        # task so the handshake never waits on it, and fail-soft throughout.
        self.request.app[K.gfs_federation_key].schedule_subscriber_connected(
            instance_id
        )

        try:
            async for msg in ws:
                # The SH does not send application frames; aiohttp handles
                # ping/pong control frames natively. Anything else is
                # logged and ignored — a polite client shouldn't send it.
                if msg.type == WSMsgType.TEXT:
                    log.debug(
                        "gfs.ws: ignored unexpected TEXT frame from %s (len=%d)",
                        instance_id,
                        len(msg.data),
                    )
                elif msg.type == WSMsgType.ERROR:
                    log.warning(
                        "gfs.ws.error: instance=%s exc=%s",
                        instance_id,
                        ws.exception(),
                    )
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "gfs.ws.loop_error: instance=%s exc=%s",
                instance_id,
                exc,
            )
        finally:
            await registry.unregister(instance_id, ws)
            await fed_repo.upsert_rtc_connection(instance_id, transport="https")
        return ws

    # ─── Hello-frame authentication ──────────────────────────────────────

    async def _authenticate(self, ws: web.WebSocketResponse) -> str | None:
        """Receive + verify the hello frame; close with ``4401``/``4408`` on failure.

        Returns the verified ``instance_id`` on success, ``None`` after closing.
        """
        try:
            msg = await ws.receive(timeout=HELLO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await ws.close(
                code=WS_CLOSE_HELLO_TIMEOUT,
                message=b"hello-timeout",
            )
            return None

        if msg.type is not WSMsgType.TEXT:
            await ws.close(
                code=WS_CLOSE_PROTOCOL_VIOLATION,
                message=b"hello-required",
            )
            return None

        try:
            payload = orjson.loads(msg.data)
        except orjson.JSONDecodeError:
            await ws.close(
                code=WS_CLOSE_PROTOCOL_VIOLATION,
                message=b"hello-not-json",
            )
            return None

        if not isinstance(payload, dict) or payload.get("type") != "hello":
            await ws.close(
                code=WS_CLOSE_PROTOCOL_VIOLATION,
                message=b"hello-required",
            )
            return None

        instance_id = str(payload.get("instance_id") or "")
        ts_raw = payload.get("ts")
        sig = str(payload.get("sig") or "")
        if not instance_id or ts_raw is None or not sig:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"missing-fields")
            return None

        try:
            ts = int(ts_raw)
        except TypeError, ValueError:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"bad-ts")
            return None

        now = int(time.time())
        if abs(now - ts) > TIMESTAMP_WINDOW_SECONDS:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"ts-skew")
            return None

        fed_repo = self.request.app[K.gfs_fed_repo_key]
        instance = await fed_repo.get_instance(instance_id)
        if instance is None or instance.status != "active":
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"unknown-instance")
            return None

        try:
            raw_key = bytes.fromhex(instance.public_key)
            raw_sig = crypto.b64url_decode(sig)
        except ValueError, TypeError:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"bad-signature")
            return None

        message = f"{instance_id}|{ts}".encode("utf-8")
        if not crypto.verify_ed25519(raw_key, message, raw_sig):
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"bad-signature")
            return None

        log.info("gfs.ws.hello accepted: instance=%s", instance_id)
        return instance_id
