"""WebSocket route — single entry point for realtime push (§5.3).

Endpoint: ``GET /api/ws`` (with ``Upgrade: websocket``).  Token auth via
the standard ``Authorization: Bearer`` header — when the browser sends
the WS handshake it can't include arbitrary headers, so we accept a
``?token=`` query parameter as the fallback per
:class:`BearerTokenStrategy`.

Inbound frames:

* ``"ping"`` (text) -> ``"pong"`` keepalive.
* JSON ``{"type":"typing","conversation_id":...}`` -> forwarded to the
  TypingService which fans out a ``conversation.user_typing`` frame to
  the other members (local + remote via ``DM_USER_TYPING``).

Anything else is ignored — outbound is the primary direction.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

from .. import app_keys as K
from .base import BaseView

log = logging.getLogger(__name__)


class WebSocketView(BaseView):
    """``GET /api/ws`` — upgrade to WebSocket for realtime push."""

    async def get(self) -> web.StreamResponse:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)

        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(self.request)
        manager = self.svc(K.ws_manager_key)
        await manager.register(ctx.user_id, ws)
        log.info(
            "ws connected: user=%s total=%d",
            ctx.user_id,
            manager.connection_count(),
        )

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if msg.data == "ping":
                        await ws.send_str("pong")
                    else:
                        await self._on_text(ctx, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.warning("ws error from %s: %s", ctx.user_id, ws.exception())
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ws loop error for %s: %s", ctx.user_id, exc)
        finally:
            await manager.unregister(ctx.user_id, ws)
            log.info(
                "ws disconnected: user=%s total=%d",
                ctx.user_id,
                manager.connection_count(),
            )

        return ws

    async def _on_text(self, ctx, data: str) -> None:
        """Handle an inbound text frame."""
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        cmd = payload.get("type")
        if cmd == "typing":
            cid = payload.get("conversation_id")
            if not cid:
                return
            typing_svc = self.request.app.get(K.typing_service_key)
            if typing_svc is None:
                return
            try:
                await typing_svc.user_started_typing(
                    conversation_id=str(cid),
                    sender_user_id=ctx.user_id,
                    sender_username=ctx.username,
                )
            except Exception as exc:  # defensive
                log.debug("typing dispatch failed: %s", exc)
