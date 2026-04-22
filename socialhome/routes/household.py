"""Household routes — feature toggles + household name (§22)."""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from .base import BaseView


class HouseholdFeaturesView(BaseView):
    """``GET /api/household/features`` + ``PUT /api/household/features``."""

    async def get(self) -> web.Response:
        self.user
        svc = self.svc(K.household_features_service_key)
        feats = await svc.get()
        return self._json(asdict(feats))

    async def put(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        svc = self.svc(K.household_features_service_key)
        feats = await svc.update(
            actor_is_admin=ctx.is_admin,
            household_name=body.get("household_name"),
            toggles=body.get("toggles"),
        )
        return self._json(asdict(feats))
