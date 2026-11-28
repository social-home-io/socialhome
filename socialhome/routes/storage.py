"""Storage routes — /api/storage/usage."""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..services.storage_quota_service import MAX_SETTING_BYTES, SETTINGS_FIELDS
from .base import BaseView


def _usage_json(svc, u) -> dict:
    return {
        "used_bytes": u.used_bytes,
        "quota_bytes": u.quota_bytes,
        "available_bytes": u.available_bytes,
        "percent_used": round(u.percent_used, 2),
        # Storage for one GFS space's synced files (0 = only what is
        # opened) and for every space's together (owner 2026-11-28).
        "space_sync_bytes": svc.space_sync_bytes,
        "space_sync_total_bytes": svc.space_sync_total_bytes,
    }


class StorageUsageView(BaseView):
    """``GET /api/storage/usage`` — current storage quota info."""

    async def get(self) -> web.Response:
        self.require_user()
        svc = self.svc(K.storage_quota_service_key)
        return self._json(_usage_json(svc, await svc.usage()))


class StorageQuotaView(BaseView):
    """``PUT /api/admin/storage/quota`` — an admin sets the household's
    storage settings, kept across restarts (the instance config). Body:
    any of ``{quota_bytes, space_sync_bytes, space_sync_total_bytes}``
    (integers >= 0, at least one). ``quota_bytes`` 0 disables the check;
    ``space_sync_bytes`` is the storage for one GFS space's synced files
    (0 = only what is opened), ``space_sync_total_bytes`` for every space's
    together. Synced files only ever use the quota's free space.
    """

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return web.json_response({"error": "admin_only"}, status=403)
        body = await self.body()
        values: dict[str, int] = {}
        for field in SETTINGS_FIELDS:
            raw = body.get(field)
            if raw is None:
                continue
            # A whole number only: JSON true / 2.5 / "7" are refused.
            if not isinstance(raw, int) or isinstance(raw, bool):
                return web.json_response(
                    {"error": f"{field} must be a whole number of bytes"},
                    status=422,
                )
            if not 0 <= raw <= MAX_SETTING_BYTES:
                return web.json_response(
                    {"error": f"{field} must be between 0 and {MAX_SETTING_BYTES}"},
                    status=422,
                )
            values[field] = raw
        if not values:
            return web.json_response(
                {"error": "one of " + ", ".join(SETTINGS_FIELDS) + " is required"},
                status=422,
            )
        svc = self.svc(K.storage_quota_service_key)
        await svc.update(**values)
        return self._json(_usage_json(svc, await svc.usage()))
