"""Bot-bridge routes — /api/bot-bridge/*.

Two endpoints, two auth models:

* ``POST /api/bot-bridge/spaces/{id}`` — authenticated by a per-bot
  Bearer token. The route is listed in :data:`auth._DEFAULT_PUBLIC_PATHS`
  so the normal user-token middleware skips it; auth happens inline by
  hashing the incoming token and looking it up in ``space_bots``.
* ``POST /api/bot-bridge/conversations/{id}`` — authenticated by the
  caller's regular user API token (standard middleware path).

Both reject requests carrying ``X-Ingress-User`` so a locally-authenticated
UI user can't impersonate the HA integration through this path.
"""

from __future__ import annotations

import hashlib

from aiohttp import web

from ..app_keys import (
    bot_bridge_service_key,
    conversation_repo_key,
    space_bot_repo_key,
)
from ..domain.space_bot import SpaceBotDisabledError
from ..security import error_response
from .base import BaseView


def _extract_bearer(request: web.Request) -> str | None:
    """Return the raw Bearer token from the Authorization header, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :].strip()
    return token or None


def _reject_ingress_user(request: web.Request) -> None:
    """The bot-bridge endpoints are for automations, not UI users.

    ``X-Ingress-User`` is set by Home Assistant's Ingress when a
    browser-authenticated user hits the add-on — presence of that header
    means the request is coming from the UI, not from an HA automation's
    HTTP call. Explicit rejection keeps the auth surface narrow.
    """
    if request.headers.get("X-Ingress-User"):
        raise web.HTTPForbidden(
            reason="bot-bridge endpoints require Bearer token auth only"
        )


class BotBridgeSpacePostView(web.View):
    """``POST /api/bot-bridge/spaces/{id}`` — post as a SpaceBot.

    This view does NOT extend :class:`BaseView` because it is listed as
    a public path (normal auth middleware is skipped). Auth happens
    inline by resolving the Bearer token via the space_bot_repo.
    """

    async def post(self) -> web.Response:
        _reject_ingress_user(self.request)
        raw_token = _extract_bearer(self.request)
        if raw_token is None:
            return error_response(401, "UNAUTHORIZED", "Bearer token required.")
        space_id = self.request.match_info["id"]
        bot_repo = self.request.app[space_bot_repo_key]
        bot = await bot_repo.get_by_token_hash(
            hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        )
        # Two separate checks so a token leaked for space A can't be
        # used against space B even if the attacker guesses a valid path.
        if bot is None or bot.space_id != space_id:
            return error_response(401, "UNAUTHORIZED", "Invalid bot token.")
        try:
            body = await self.request.json()
        except Exception:
            return error_response(400, "BAD_REQUEST", "Invalid JSON body.")
        svc = self.request.app[bot_bridge_service_key]
        try:
            post = await svc.notify_space(
                bot,
                title=body.get("title"),
                message=body.get("message", ""),
            )
        except SpaceBotDisabledError as exc:
            return error_response(403, "BOT_DISABLED", str(exc))
        except KeyError as exc:
            return error_response(404, "NOT_FOUND", str(exc).strip("'\""))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(
            {
                "id": post.id,
                "space_id": bot.space_id,
                "bot_id": bot.bot_id,
                "author": post.author,
                "created_at": post.created_at.isoformat(),
            },
            status=201,
        )


class BotBridgeConversationPostView(BaseView):
    """``POST /api/bot-bridge/conversations/{id}`` — system post in a DM."""

    async def post(self) -> web.Response:
        _reject_ingress_user(self.request)
        ctx = self.user
        conv_repo = self.svc(conversation_repo_key)
        svc = self.svc(bot_bridge_service_key)
        conversation_id = self.match("id")
        body = await self.body()
        # Build the recipient list for the DmMessageCreated event so the
        # push/WS fan-out targets every participant except the system-ish
        # sender — in practice "except the caller" because SYSTEM_AUTHOR
        # isn't a user in the conversation_members table.
        members = await conv_repo.list_members(conversation_id)
        recipients = tuple(m.username for m in members if m.username != ctx.username)
        try:
            msg = await svc.notify_conversation(
                conversation_id=conversation_id,
                sender_user_id=ctx.user_id,
                recipient_user_ids=recipients,
                title=body.get("title"),
                message=body.get("message", ""),
            )
        except SpaceBotDisabledError as exc:
            return error_response(403, "BOT_DISABLED", str(exc))
        return web.json_response(
            {
                "id": msg.id,
                "conversation_id": msg.conversation_id,
                "sender_user_id": msg.sender_user_id,
                "created_at": msg.created_at.isoformat(),
            },
            status=201,
        )
