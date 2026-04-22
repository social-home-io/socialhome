"""Space-bot routes — /api/spaces/{id}/bots.

CRUD for named bot personas. Delegates to :class:`SpaceBotService`.
Plaintext bot tokens are returned in the response for ``POST`` (create)
and ``POST /token`` (rotate) and only there — the DB only ever stores
a sha256 digest (``token_hash`` in :class:`SpaceBot`, sanitised out by
:mod:`security` before responses go on the wire).
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import space_bot_service_key
from ..domain.space_bot import BotScope, SpaceBot
from ..security import sanitise_for_api
from .base import BaseView


def _bot_to_json(bot: SpaceBot) -> dict:
    """Serialise a bot for API responses. Never includes ``token_hash``.

    The field is already in ``SENSITIVE_FIELDS`` via
    :func:`sanitise_for_api`, but we also drop it explicitly here so a
    missed entry in that frozenset can't silently leak the hash.
    """
    return {
        "bot_id": bot.bot_id,
        "space_id": bot.space_id,
        "scope": bot.scope.value,
        "slug": bot.slug,
        "name": bot.name,
        "icon": bot.icon,
        "created_by": bot.created_by,
        "created_at": bot.created_at.isoformat(),
    }


class SpaceBotCollectionView(BaseView):
    """``GET /api/spaces/{id}/bots`` + ``POST /api/spaces/{id}/bots``."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_bot_service_key)
        space_id = self.match("id")
        bots = await svc.list_bots(space_id, actor_username=ctx.username)
        return self._json([_bot_to_json(b) for b in bots])

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_bot_service_key)
        space_id = self.match("id")
        body = await self.body()
        scope_raw = body.get("scope", "member")
        try:
            scope = BotScope(scope_raw)
        except ValueError as exc:
            raise ValueError(f"invalid scope: {scope_raw!r}") from exc
        bot, raw_token = await svc.create_bot(
            space_id,
            actor_username=ctx.username,
            scope=scope,
            slug=body.get("slug", ""),
            name=body.get("name", ""),
            icon=body.get("icon", ""),
        )
        return web.json_response(
            sanitise_for_api(
                {
                    **_bot_to_json(bot),
                    # Plaintext token — shown once, never retrievable again.
                    "token": raw_token,
                }
            ),
            status=201,
        )


class SpaceBotDetailView(BaseView):
    """``PATCH /api/spaces/{id}/bots/{bot_id}`` +
    ``DELETE /api/spaces/{id}/bots/{bot_id}``."""

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_bot_service_key)
        body = await self.body()
        bot = await svc.update_bot(
            self.match("id"),
            self.match("bot_id"),
            actor_username=ctx.username,
            name=body.get("name"),
            icon=body.get("icon"),
        )
        return self._json(_bot_to_json(bot))

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_bot_service_key)
        await svc.delete_bot(
            self.match("id"),
            self.match("bot_id"),
            actor_username=ctx.username,
        )
        return web.Response(status=204)


class SpaceBotTokenView(BaseView):
    """``POST /api/spaces/{id}/bots/{bot_id}/token`` — rotate token."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_bot_service_key)
        bot, raw_token = await svc.rotate_token(
            self.match("id"),
            self.match("bot_id"),
            actor_username=ctx.username,
        )
        return web.json_response(
            sanitise_for_api({**_bot_to_json(bot), "token": raw_token}),
        )
