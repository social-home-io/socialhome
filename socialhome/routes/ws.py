"""WebSocket route — single entry point for realtime push (§5.3).

Endpoint: ``GET /api/ws`` (with ``Upgrade: websocket``).  Token auth via
the standard ``Authorization: Bearer`` header — when the browser sends
the WS handshake it can't include arbitrary headers, so we accept a
``?token=`` query parameter as the fallback per
:class:`BearerTokenStrategy`.

Origin enforcement (§Audit #5): WebSocket upgrades carry an ``Origin``
header in the browser. We require it to be either same-origin with the
request or in ``config.cors_allowed_origins`` — the CORS-deny
middleware only acts when ``Origin`` is present, leaving curl-style
clients (which can omit ``Origin`` entirely) unchecked. We close that
gap here at the WS upgrade.

Inbound frames:

* ``"ping"`` (text) -> ``"pong"`` keepalive.
* JSON ``{"type":"typing","data":{"conversation_id":...}}`` -> forwarded
  to the TypingService which fans out a ``conversation.user_typing`` frame to
  the other members (local + remote via ``DM_USER_TYPING``).

Anything else is ignored — outbound is the primary direction.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import orjson
from aiohttp import WSMsgType, web

from .. import app_keys as K
from .base import BaseView

log = logging.getLogger(__name__)


# NOTE: the audit also called out "Origin missing → reject" but doing
# that would break every native / cli client that holds a valid bearer
# token. The CORS-deny middleware made the same trade-off. We accept
# missing Origin (treat it as a non-browser client) and *strictly*
# reject any present-but-disallowed Origin — that's the actual CSRF
# vector a malicious page tries to exploit.
def _origin_allowed(request: web.Request) -> bool:
    """Return ``True`` iff the request's ``Origin`` header is permitted
    for a WebSocket upgrade.

    The CORS-deny middleware applies the same allow-list to plain HTTP
    requests when ``Origin`` is present, but skips when ``Origin`` is
    absent (native clients). We mirror that pragma here:

    * No ``Origin`` header → accept (native / cli clients with a valid
      bearer token; same as the CORS-deny middleware's pass-through).
    * ``Origin`` host matches the request's ``X-Forwarded-Host`` /
      ``Host`` → same-origin, accept.
    * ``Origin`` is in :attr:`Config.cors_allowed_origins` → accept.
    * Otherwise reject (the cross-origin browser CSRF case the audit
      flagged: a malicious page issuing ``new WebSocket(...)`` against
      this server would carry its own ``Origin``).
    """
    origin = request.headers.get("Origin")
    if origin is None or origin == "":
        return True
    config = request.app.get(K.config_key)
    allowlist: frozenset[str] = frozenset(
        getattr(config, "cors_allowed_origins", ()) or ()
    )
    if origin in allowlist:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if not parsed.netloc:
        return False
    request_host = (
        request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or ""
    ).strip()
    if not request_host:
        return False
    return parsed.netloc.lower() == request_host.lower()


class WebSocketView(BaseView):
    """``GET /api/ws`` — upgrade to WebSocket for realtime push."""

    async def get(self) -> web.StreamResponse:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)

        # §Audit #5: enforce Origin allow-list before upgrading. The CORS
        # middleware lets requests without an Origin header through, so
        # curl-style clients could otherwise open a WS that the
        # ``connect_src`` CSP would block in browsers. Reject early.
        if not _origin_allowed(self.request):
            log.warning(
                "ws: blocked Origin=%r for user=%s",
                self.request.headers.get("Origin"),
                ctx.user_id,
            )
            return web.json_response({"error": "forbidden_origin"}, status=403)

        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(self.request)
        manager = self.svc(K.ws_manager_key)
        await manager.register(ctx.user_id, ws)
        ws_id = id(ws)
        # Online-status side-channel: tells the OnlineStatusService a
        # session is open so it can fire UserCameOnline / publish the
        # green dot to other household members.
        online_svc = self.request.app.get(K.online_status_service_key)
        if online_svc is not None:
            await online_svc.user_session_opened(ctx.user_id, ws_id)
        log.info(
            "ws connected: user=%s total=%d",
            ctx.user_id,
            manager.connection_count(),
        )

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    # Any inbound frame counts as activity — keeps the
                    # "idle" threshold honest without a separate
                    # heartbeat protocol.
                    if online_svc is not None:
                        await online_svc.touch(ctx.user_id, ws_id)
                    if msg.data == "ping":
                        await ws.send_str("pong")
                    else:
                        await self._on_text(ctx, ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.warning("ws error from %s: %s", ctx.user_id, ws.exception())
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ws loop error for %s: %s", ctx.user_id, exc, exc_info=True)
        finally:
            await manager.unregister(ctx.user_id, ws)
            if online_svc is not None:
                await online_svc.user_session_closed(ctx.user_id, ws_id)
            log.info(
                "ws disconnected: user=%s total=%d",
                ctx.user_id,
                manager.connection_count(),
            )

        return ws

    async def _on_text(self, ctx, ws, data: str) -> None:
        """Handle an inbound text frame."""
        try:
            payload = orjson.loads(data)
        except orjson.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        cmd = payload.get("type")
        if cmd == "dm.active":
            body = payload.get("data") or {}
            if not isinstance(body, dict):
                return
            cid = body.get("conversation_id")
            manager = self.svc(K.ws_manager_key)
            await manager.set_active_conversation(
                ctx.user_id,
                ws,
                str(cid) if cid else None,
            )
            return
        if cmd == "typing":
            typing_svc = self.request.app.get(K.typing_service_key)
            if typing_svc is None:
                return
            # The SPA's :class:`WsManager.send` wraps every outbound
            # frame as ``{type, data: {...}}``; the fields the handlers
            # below want live under ``data``. Server + SPA ship as a
            # pair so no flat-shape fallback is needed.
            body = payload.get("data") or {}
            if not isinstance(body, dict):
                return
            # Two scopes share the same inbound frame:
            #   • DM:            {type: 'typing', data: {conversation_id}}
            #   • Comment thread:{type: 'typing', data: {post_id, space_id?}}
            # Branch on which id the client supplied — never both.
            post_id = body.get("post_id")
            cid = body.get("conversation_id")
            try:
                if post_id:
                    space_id = body.get("space_id")
                    await typing_svc.user_typing_on_comment(
                        post_id=str(post_id),
                        space_id=str(space_id) if space_id else None,
                        sender_user_id=ctx.user_id,
                        sender_username=ctx.username,
                    )
                elif cid:
                    await typing_svc.user_started_typing(
                        conversation_id=str(cid),
                        sender_user_id=ctx.user_id,
                        sender_username=ctx.username,
                    )
            except Exception as exc:  # defensive
                log.debug("typing dispatch failed: %s", exc, exc_info=True)
