"""Storage routes — /api/storage/usage."""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from .base import BaseView


class StorageUsageView(BaseView):
    """``GET /api/storage/usage`` — current storage quota info."""

    async def get(self) -> web.Response:
        self.user
        svc = self.svc(K.storage_quota_service_key)
        u = await svc.usage()
        return self._json(
            {
                "used_bytes": u.used_bytes,
                "quota_bytes": u.quota_bytes,
                "available_bytes": u.available_bytes,
                "percent_used": round(u.percent_used, 2),
            }
        )


class StorageQuotaView(BaseView):
    """``PUT /api/admin/storage/quota`` — admin sets the household's
    storage byte budget at runtime. Body: ``{quota_bytes: int}``. A
    value of 0 disables the check entirely.
    """

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return web.json_response({"error": "admin_only"}, status=403)
        body = await self.body()
        raw = body.get("quota_bytes")
        if raw is None:
            return web.json_response(
                {"error": "quota_bytes is required"},
                status=422,
            )
        try:
            value = int(raw)
        except TypeError, ValueError:
            return web.json_response(
                {"error": "quota_bytes must be an integer"},
                status=422,
            )
        if value < 0:
            return web.json_response(
                {"error": "quota_bytes must be >= 0"},
                status=422,
            )
        svc = self.svc(K.storage_quota_service_key)
        svc.set_quota_bytes(value)
        u = await svc.usage()
        return self._json(
            {
                "used_bytes": u.used_bytes,
                "quota_bytes": u.quota_bytes,
                "available_bytes": u.available_bytes,
                "percent_used": round(u.percent_used, 2),
            }
        )
