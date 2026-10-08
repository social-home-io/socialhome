"""Household routes — preferences + household name (§22), household chat."""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from .base import BaseView


class HouseholdPreferencesView(BaseView):
    """``GET /api/household/preferences`` + ``PUT /api/household/preferences``."""

    async def get(self) -> web.Response:
        self.user
        svc = self.svc(K.preferences_service_key)
        prefs = await svc.get_household()
        return self._json(asdict(prefs))

    async def put(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        svc = self.svc(K.preferences_service_key)
        prefs = await svc.update_household(
            actor_is_admin=ctx.is_admin,
            household_name=body.get("household_name"),
            toggles=body.get("toggles"),
            tz=body.get("tz"),
        )
        return self._json(asdict(prefs))


class HouseholdChatView(BaseView):
    """``GET /api/household/chat`` — the household chat for the caller.

    ``{enabled, conversation_id, unread, notif_level, muted_until,
    last_read_at}``. Any
    active local user; the chat (a system group conversation of every
    local user) is created and its seats reconciled on the way. While
    ``feat_household_chat`` is off: ``{"enabled": false,
    "conversation_id": null, ...}`` and nothing is created. Messages, reads,
    reactions, edits, deletes, mute and level use the existing
    ``/api/conversations/{id}/...`` routes with ``conversation_id``.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        summary = await self.svc(K.household_chat_service_key).summary(ctx.username)
        return self._json(asdict(summary))
