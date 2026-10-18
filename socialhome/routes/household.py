"""Household routes — preferences + household name (§22), household chat,
the household's space directory."""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from ..domain.space import HostedSpaceListing, normalize_category
from ..security import sanitise_for_api
from .base import BaseView


class HouseholdPreferencesView(BaseView):
    """``GET /api/household/preferences`` + ``PUT /api/household/preferences``."""

    async def get(self) -> web.Response:
        self.require_user()
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


class HouseholdSpacesView(BaseView):
    """``GET /api/household/spaces`` — Browse › Your household.

    The spaces THIS household hosts that the caller may find
    (:meth:`SpaceService.household_directory`): every one they hold a seat
    in, a household space they are not in (anyone in the home may join it),
    and a public / global space they can join or request. Private spaces
    are never listed. Each row carries the real ``member_count`` and the
    caller's ``role`` (``null`` = no seat).
    """

    async def get(self) -> web.Response:
        ctx = self.user
        rows = await self.svc(K.space_service_key).household_directory(ctx.user_id)
        return self._json([_listing_view(r) for r in rows])


def _listing_view(row: HostedSpaceListing) -> dict:
    """The directory-entry fields of a space's landing page (see
    ``routes.spaces._public_space_view``) plus the caller's seat."""
    space = row.space
    return sanitise_for_api(
        {
            "space_id": space.id,
            "name": space.name,
            "description": space.description,
            "emoji": space.emoji,
            "space_type": space.space_type.value,
            "join_mode": space.join_mode.value,
            "category": normalize_category(space.category),
            "allow_subscribers": space.features.allow_subscribers,
            "min_age": space.min_age,
            "member_count": row.member_count,
            "role": row.caller_role,
        }
    )
