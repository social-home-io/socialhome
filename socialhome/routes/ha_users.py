"""Admin routes for mirroring Home Assistant users into Social Home.

In HA mode the household owner opts each HA ``person.*`` into Social
Home explicitly — no silent auto-provision. The three endpoints here
list the HA users alongside their provisioning state and let admins
flip them on / off.

Standalone mode has no HA user registry; the endpoints return
``501 Not Implemented`` so the frontend can hide the tab.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    config_key,
    platform_adapter_key,
    user_repo_key,
    user_service_key,
)
from .base import BaseView, error_response


def _require_admin(view: BaseView) -> None:
    if not getattr(view.user, "is_admin", False):
        raise PermissionError("household admin required")


class HaUsersCollectionView(BaseView):
    """GET /api/admin/ha-users — admin-only list of HA users + sync state."""

    async def get(self) -> web.Response:
        _require_admin(self)
        if self.svc(config_key).mode != "ha":
            return error_response(
                501, "NOT_IMPLEMENTED", "HA user sync only available in HA mode"
            )
        adapter = self.svc(platform_adapter_key)
        users = await adapter.list_external_users()
        user_repo = self.svc(user_repo_key)
        out: list[dict] = []
        for ext in users:
            local = await user_repo.get(ext.username)
            synced = (
                local is not None and local.state == "active" and local.source == "ha"
            )
            out.append(
                {
                    "username": ext.username,
                    "display_name": ext.display_name,
                    "picture_url": ext.picture_url,
                    "is_admin": bool(local and local.is_admin),
                    "synced": bool(synced),
                }
            )
        return web.json_response(out)


class HaUserProvisionView(BaseView):
    """POST + DELETE /api/admin/ha-users/{username}/provision."""

    async def post(self) -> web.Response:
        _require_admin(self)
        if self.svc(config_key).mode != "ha":
            return error_response(
                501, "NOT_IMPLEMENTED", "HA user sync only available in HA mode"
            )
        adapter = self.svc(platform_adapter_key)
        username = self.match("username")
        ext = await adapter.get_external_user(username)
        if ext is None:
            return error_response(404, "NOT_FOUND", f"HA user {username!r} not found")
        user_service = self.svc(user_service_key)
        user = await user_service.provision(
            username=ext.username,
            display_name=ext.display_name,
            is_admin=False,  # promote later via member tools
            email=ext.email,
            picture_url=ext.picture_url,
            source="ha",
        )
        return web.json_response(
            {
                "username": user.username,
                "user_id": user.user_id,
                "synced": True,
            },
            status=201,
        )

    async def delete(self) -> web.Response:
        _require_admin(self)
        if self.svc(config_key).mode != "ha":
            return error_response(
                501, "NOT_IMPLEMENTED", "HA user sync only available in HA mode"
            )
        username = self.match("username")
        user_service = self.svc(user_service_key)
        try:
            await user_service.deprovision_ha_user(username)
        except KeyError as exc:
            return error_response(404, "NOT_FOUND", str(exc).strip("'\""))
        return web.json_response({"username": username, "synced": False})
