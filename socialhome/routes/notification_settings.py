"""Settings → Notifications — ``/api/me/notification-settings`` (redesign D4,
D10).

* ``GET  /api/me/notification-settings`` — the eight kinds (choice, default,
  the choices each offers) and "Muted and changed".
* ``PATCH /api/me/notification-settings`` — ``{kind, choice}``: set one kind.
* ``POST /api/me/notification-settings/muted-posts`` — ``{object_key}``:
  mute a post (a row's "Mute this post").
* ``DELETE /api/me/notification-settings/muted-posts/{object_key}`` — unmute.

Space levels stay on ``/api/spaces/{id}/notif-prefs``, chat levels and mutes
on ``/api/conversations/{id}/notif-prefs`` / ``…/mute``.
"""

from __future__ import annotations

from dataclasses import asdict
from urllib.parse import unquote

from aiohttp import web

from ..app_keys import notification_settings_service_key
from ..domain.notification_kinds import (
    KIND_CHOICES,
    KIND_DEFAULTS,
    NotifKind,
    NotifSettings,
)
from ..security import error_response
from .base import BaseView


def kinds_json(settings: NotifSettings) -> list[dict]:
    """The eight kinds, in Settings order: what applies, the default and
    the choices each offers."""
    return [
        {
            "kind": kind.value,
            "choice": settings.delivery(kind).value,
            "default": KIND_DEFAULTS[kind].value,
            "choices": [c.value for c in KIND_CHOICES[kind]],
        }
        for kind in NotifKind
    ]


class NotificationSettingsView(BaseView):
    """``GET`` / ``PATCH /api/me/notification-settings``."""

    async def get(self) -> web.Response:
        svc = self.svc(notification_settings_service_key)
        settings = await svc.settings(self.user.username)
        user = await svc.get_user(self.user.username)
        changes = await svc.changes(user)
        return self._json(
            {
                "kinds": kinds_json(settings),
                "changed": [asdict(c) for c in changes],
            }
        )

    async def patch(self) -> web.Response:
        body = await self.body()
        kind, choice = body.get("kind"), body.get("choice")
        if not isinstance(kind, str) or not isinstance(choice, str):
            return error_response(422, "UNPROCESSABLE", "kind and choice are required")
        settings = await self.svc(notification_settings_service_key).set_kind(
            self.user.username, kind, choice
        )
        return self._json({"kinds": kinds_json(settings)})


class MutedPostsView(BaseView):
    """``POST /api/me/notification-settings/muted-posts``."""

    async def post(self) -> web.Response:
        body = await self.body()
        key = body.get("object_key")
        if not isinstance(key, str):
            return error_response(422, "UNPROCESSABLE", "object_key is required")
        svc = self.svc(notification_settings_service_key)
        user = await svc.get_user(self.user.username)
        await svc.mute_post(user, key)
        return self._json({"object_key": key, "muted": True})


class MutedPostView(BaseView):
    """``DELETE /api/me/notification-settings/muted-posts/{key}``."""

    async def delete(self) -> web.Response:
        key = unquote(self.match("key"))
        await self.svc(notification_settings_service_key).unmute_post(
            self.user.username, key
        )
        return self._json({"object_key": key, "muted": False})
