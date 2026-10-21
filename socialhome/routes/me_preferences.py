"""User preferences route — ``GET /api/me/preferences`` + ``PATCH /api/me/preferences``."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import preferences_service_key
from ..security import error_response
from .base import BaseView

#: The per-user switches ``PATCH`` accepts (all booleans).
_USER_TOGGLES = (
    "hide_highlights",
    "hide_momentum",
    "hide_bazaar",
    "hide_profile_from_followers",
)


class MePreferencesView(BaseView):
    """``GET /api/me/preferences`` + ``PATCH /api/me/preferences``."""

    async def get(self) -> web.Response:
        user = self.user
        prefs = await self.svc(preferences_service_key).get_user(user.user_id)
        return self._json(
            {
                "user_id": prefs.user_id,
                "hide_highlights": prefs.hide_highlights,
                "hide_momentum": prefs.hide_momentum,
                "hide_bazaar": prefs.hide_bazaar,
                "hide_profile_from_followers": prefs.hide_profile_from_followers,
            }
        )

    async def patch(self) -> web.Response:
        user = self.user
        body = await self.body()
        toggles: dict[str, bool] = {}
        for key in _USER_TOGGLES:
            if key in body:
                value = body[key]
                if not isinstance(value, bool):
                    return error_response(
                        400, "UNPROCESSABLE", f"{key} must be a boolean."
                    )
                toggles[key] = value
        prefs = await self.svc(preferences_service_key).update_user(
            user.user_id,
            toggles=toggles,
        )
        return self._json(
            {
                "user_id": prefs.user_id,
                "hide_highlights": prefs.hide_highlights,
                "hide_momentum": prefs.hide_momentum,
                "hide_bazaar": prefs.hide_bazaar,
                "hide_profile_from_followers": prefs.hide_profile_from_followers,
            }
        )
