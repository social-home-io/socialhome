"""Storage routes — /api/storage/usage."""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from .base import BaseView


def _usage_json(svc, u) -> dict:
    return {
        "used_bytes": u.used_bytes,
        "quota_bytes": u.quota_bytes,
        "available_bytes": u.available_bytes,
        "percent_used": round(u.percent_used, 2),
        # Storage for one GFS space's synced files (0 = only what is opened).
        "space_sync_bytes": svc.space_sync_bytes,
    }


class StorageUsageView(BaseView):
    """``GET /api/storage/usage`` — current storage quota info."""

    async def get(self) -> web.Response:
        self.require_user()
        svc = self.svc(K.storage_quota_service_key)
        return self._json(_usage_json(svc, await svc.usage()))


class StorageQuotaView(BaseView):
    """``PUT /api/admin/storage/quota`` — admin sets the household's
    storage byte budget at runtime. Body: ``{quota_bytes?: int,
    space_sync_bytes?: int}`` (at least one). ``quota_bytes`` 0 disables
    the check entirely; ``space_sync_bytes`` is the storage for one GFS
    space's synced files, 0 = only what is opened.
    """

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return web.json_response({"error": "admin_only"}, status=403)
        body = await self.body()
        if body.get("quota_bytes") is None and body.get("space_sync_bytes") is None:
            return web.json_response(
                {"error": "quota_bytes is required"},
                status=422,
            )
        values: dict[str, int] = {}
        for field in ("quota_bytes", "space_sync_bytes"):
            raw = body.get(field)
            if raw is None:
                continue
            try:
                value = int(raw)
            except TypeError, ValueError:
                return web.json_response(
                    {"error": f"{field} must be an integer"},
                    status=422,
                )
            if value < 0:
                return web.json_response(
                    {"error": f"{field} must be >= 0"},
                    status=422,
                )
            values[field] = value
        svc = self.svc(K.storage_quota_service_key)
        if "quota_bytes" in values:
            svc.set_quota_bytes(values["quota_bytes"])
        if "space_sync_bytes" in values:
            svc.set_space_sync_bytes(values["space_sync_bytes"])
        return self._json(_usage_json(svc, await svc.usage()))
