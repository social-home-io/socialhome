"""Theme routes — household + per-space visual preferences (§23.125)."""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from .base import BaseView


class HouseholdThemeView(BaseView):
    """``GET /api/theme`` + ``PUT /api/theme``."""

    async def get(self) -> web.Response:
        self.user
        svc = self.svc(K.theme_service_key)
        theme = await svc.get_household_theme()
        d = asdict(theme)
        d.setdefault("is_default", False)
        return self._json(d)

    async def put(self) -> web.Response:
        ctx = self.user
        data = await self.body()
        if not isinstance(data, dict):
            return web.json_response(
                {"error": "body must be an object"},
                status=422,
            )
        svc = self.svc(K.theme_service_key)
        theme = await svc.update_household_theme(
            actor_user_id=ctx.user_id,
            actor_is_admin=ctx.is_admin,
            patch=data,
        )
        d = asdict(theme)
        d.setdefault("is_default", False)
        return self._json(d)


class SpaceThemeView(BaseView):
    """``GET /api/spaces/{space_id}/theme`` + ``PUT /api/spaces/{space_id}/theme``."""

    async def get(self) -> web.Response:
        self.user
        space_id = self.match("space_id")
        svc = self.svc(K.theme_service_key)
        theme = await svc.get_space_theme(space_id)
        if theme is None:
            household = await svc.get_household_theme()
            out = asdict(household)
            out.setdefault("is_default", False)
            out["is_default"] = True
            return self._json(out)
        d = asdict(theme)
        d["is_default"] = False
        return self._json(d)

    async def put(self) -> web.Response:
        ctx = self.user
        space_id = self.match("space_id")
        data = await self.body()
        if not isinstance(data, dict):
            return web.json_response(
                {"error": "body must be an object"},
                status=422,
            )
        svc = self.svc(K.theme_service_key)
        theme = await svc.update_space_theme(
            space_id=space_id,
            actor_user_id=ctx.user_id,
            patch=data,
        )
        d = asdict(theme)
        d["is_default"] = False
        return self._json(d)
