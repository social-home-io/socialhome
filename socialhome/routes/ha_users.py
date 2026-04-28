"""Admin routes for mirroring Home Assistant users into Social Home.

In ha and haos modes the household owner opts each HA ``person.*``
into Social Home explicitly — no silent auto-provision. The endpoints
here list the HA users alongside their provisioning state and let
admins flip them on / off.

* ``ha`` mode requires a ``password`` on provision so the user can sign
  in via ``POST /api/auth/token`` (X-Ingress-User isn't available
  outside the Supervisor proxy).
* ``haos`` mode rejects ``password`` — Ingress is the only entry point,
  the Supervisor proxy authenticates the HA user before forwarding.

Standalone mode returns ``501 Not Implemented`` so the frontend can
hide the tab.
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

_HA_MODES = ("ha", "haos")


def _require_admin(view: BaseView) -> None:
    if not getattr(view.user, "is_admin", False):
        raise PermissionError("household admin required")


def _require_ha_mode(view: BaseView) -> web.Response | None:
    """Return a 501 response when not in ha/haos mode, else ``None``."""
    if view.svc(config_key).mode not in _HA_MODES:
        return error_response(
            501,
            "NOT_IMPLEMENTED",
            "HA user sync is only available in ha/haos modes",
        )
    return None


class HaUsersCollectionView(BaseView):
    """GET /api/admin/ha-users — admin-only list of HA users + sync state."""

    async def get(self) -> web.Response:
        _require_admin(self)
        if (resp := _require_ha_mode(self)) is not None:
            return resp
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
    """POST + DELETE /api/admin/ha-users/{username}/provision.

    The POST body is ``{password?}``:

    * ``ha`` mode — ``password`` is required. We hash and stash it in
      ``platform_users`` so the provisioned user can ``POST
      /api/auth/token``.
    * ``haos`` mode — ``password`` MUST be omitted. Ingress signs the
      user in; a local password would be a misleading second auth
      surface.
    """

    async def post(self) -> web.Response:
        _require_admin(self)
        if (resp := _require_ha_mode(self)) is not None:
            return resp
        config = self.svc(config_key)
        adapter = self.svc(platform_adapter_key)
        username = self.match("username")
        ext = await adapter.get_external_user(username)
        if ext is None:
            return error_response(404, "NOT_FOUND", f"HA user {username!r} not found")

        body: dict = {}
        if self.request.can_read_body:
            try:
                body = await self.request.json()
            except Exception:
                body = {}
        password = str(body.get("password") or "")

        if config.mode == "ha":
            if not password:
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    "password is required when provisioning in ha mode.",
                )
        else:  # haos
            if password:
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    "haos mode authenticates via Ingress; do not set a password.",
                )

        user_service = self.svc(user_service_key)
        user = await user_service.provision(
            username=ext.username,
            display_name=ext.display_name,
            is_admin=False,  # promote later via member tools
            email=ext.email,
            picture_url=ext.picture_url,
            source="ha",
        )
        if password:
            await adapter.set_local_password(
                ext.username,
                password,
                display_name=ext.display_name,
                is_admin=False,
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
        if (resp := _require_ha_mode(self)) is not None:
            return resp
        username = self.match("username")
        user_service = self.svc(user_service_key)
        try:
            await user_service.deprovision_ha_user(username)
        except KeyError as exc:
            return error_response(404, "NOT_FOUND", str(exc).strip("'\""))
        return web.json_response({"username": username, "synced": False})
