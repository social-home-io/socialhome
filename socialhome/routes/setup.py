"""First-boot setup routes — the wizard's three mode-specific endpoints.

* ``POST /api/setup/standalone`` — operator submits ``{username, password}``;
  we seed ``platform_users`` + ``users`` and mark setup complete.
* ``GET  /api/setup/ha/persons`` — list HA persons so the operator can
  pick which one becomes the SH owner.
* ``POST /api/setup/ha/owner`` — operator submits ``{username}`` for the
  picked HA person; we mirror them into ``users`` as admin and mark
  setup complete. ha-mode auth runs through HA (X-Ingress-User or HA
  bearer tokens), so no password is needed at this step.
* ``POST /api/setup/haos/complete`` — optional ``{household_name}``.
  Reads the HA owner from ``http://supervisor/auth/list``, mirrors
  them, applies the household name (if any), and marks setup complete.

All three POST endpoints accept an optional ``household_name`` to seed
the household's display name during the wizard so the operator doesn't
have to hunt for it under Settings on first login.

Every endpoint is a public path while ``setup_required`` is true; once
complete, they all return 409 ``ALREADY_COMPLETE``. The SPA consults
``GET /api/instance/config`` before showing the wizard, so it should
never hit the gate in practice — the gate is defence-in-depth.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..app_keys import (
    config_key,
    db_key,
    household_features_service_key,
    platform_adapter_key,
    setup_service_key,
)
from ..platform.adapter import Capability, ExternalUser
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)


async def _gate(view: BaseView) -> web.Response | None:
    """Return a 409 response if setup is already complete, else ``None``.

    Centralised so each handler can short-circuit with a single line:
    ``if (resp := await _gate(self)): return resp``.
    """
    setup = view.svc(setup_service_key)
    if not await setup.is_required():
        return error_response(
            409,
            "ALREADY_COMPLETE",
            "First-boot setup has already been completed.",
        )
    return None


def _validate_household_name(
    raw: object,
) -> tuple[str | None, web.Response | None]:
    """Validate the operator-supplied household name without touching the DB.

    Returns ``(name, None)`` on success (with ``name=None`` meaning
    "no override — keep the default"), or ``(None, response)`` with a
    422 error response on validation failure. Length cap mirrors
    :meth:`HouseholdFeaturesService.update` so we fail-fast before
    provisioning touches the DB.
    """
    if raw is None:
        return None, None
    name = str(raw).strip()
    if not name:
        return None, None
    if len(name) > 80:
        return None, error_response(
            422,
            "UNPROCESSABLE",
            "household_name must be 1-80 characters",
        )
    return name, None


async def _apply_household_name(view: BaseView, name: str | None) -> None:
    """Persist a pre-validated household name. No-op when ``name`` is None."""
    if name is None:
        return
    await view.svc(household_features_service_key).update(
        actor_is_admin=True,
        household_name=name,
    )


class StandaloneSetupView(BaseView):
    """``POST /api/setup/standalone`` — set the admin username + password.

    Returns ``{token}`` (status 201) so the SPA can drop straight into
    the app authenticated, with no second login round-trip.
    """

    async def post(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        config = self.svc(config_key)
        if config.mode != "standalone":
            return error_response(
                409,
                "WRONG_MODE",
                f"This endpoint is for standalone mode (current: {config.mode}).",
            )
        body = await self.body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            return error_response(
                422,
                "UNPROCESSABLE",
                "username and password are required.",
            )
        household_name, err = _validate_household_name(body.get("household_name"))
        if err is not None:
            return err
        adapter = self.svc(platform_adapter_key)
        provision = getattr(adapter, "provision_admin", None)
        if provision is None:
            return error_response(
                500,
                "INTERNAL_ERROR",
                "Standalone adapter is missing provision_admin.",
            )
        await provision(username=username, password=password)
        await _apply_household_name(self, household_name)
        await self.svc(setup_service_key).mark_complete()
        token = await adapter.issue_bearer_token(username, password)
        return web.json_response({"token": token}, status=201)


class HaPersonsSetupView(BaseView):
    """``GET /api/setup/ha/persons`` — list HA persons for the wizard."""

    async def get(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        config = self.svc(config_key)
        if config.mode not in ("ha", "haos"):
            return error_response(
                409,
                "WRONG_MODE",
                f"This endpoint is for ha/haos modes (current: {config.mode}).",
            )
        adapter = self.svc(platform_adapter_key)
        persons = await adapter.users.list_users()
        return web.json_response(
            {
                "persons": [
                    {
                        "username": p.username,
                        "display_name": p.display_name,
                        "picture_url": p.picture_url,
                    }
                    for p in persons
                ]
            }
        )


class HaOwnerSetupView(BaseView):
    """``POST /api/setup/ha/owner`` — operator picks an HA person and
    sets a local password for them.

    The picked HA person becomes the SH admin. The password is stored in
    ``platform_users`` so the operator can also log in via
    ``POST /api/auth/token`` (in addition to X-Ingress-User and HA
    long-lived access tokens). Returns ``{token}`` (status 201) so the
    SPA drops straight into the app.
    """

    async def post(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        config = self.svc(config_key)
        if config.mode != "ha":
            return error_response(
                409,
                "WRONG_MODE",
                f"This endpoint is for ha mode (current: {config.mode}).",
            )
        body = await self.body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            return error_response(
                422,
                "UNPROCESSABLE",
                "username and password are required.",
            )
        household_name, err = _validate_household_name(body.get("household_name"))
        if err is not None:
            return err
        adapter = self.svc(platform_adapter_key)
        external = await adapter.users.get(username)
        if external is None:
            return error_response(
                422,
                "UNPROCESSABLE",
                f"No Home Assistant person found with username {username!r}.",
            )
        await _mirror_admin_user(self.svc(db_key), external)
        await adapter.set_local_password(
            username,
            password,
            display_name=external.display_name,
            is_admin=True,
        )
        await _apply_household_name(self, household_name)
        await self.svc(setup_service_key).mark_complete()
        token = await adapter.issue_bearer_token(username, password)
        return web.json_response({"token": token}, status=201)


class HaosCompleteSetupView(BaseView):
    """``POST /api/setup/haos/complete`` — read the owner from Supervisor.

    Idempotent. The SPA POSTs this silently on first load when
    ``mode == 'haos'`` and redirects to the app afterwards.
    """

    async def post(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        config = self.svc(config_key)
        if config.mode != "haos":
            return error_response(
                409,
                "WRONG_MODE",
                f"This endpoint is for haos mode (current: {config.mode}).",
            )
        # haos POSTs an optional household_name. The body may be empty,
        # so parse leniently — an empty body is a valid "no name" call.
        if self.request.content_length:
            try:
                body = await self.request.json()
                if not isinstance(body, dict):
                    body = {}
            except Exception:
                return error_response(400, "BAD_REQUEST", "Invalid JSON body.")
        else:
            body = {}
        household_name, err = _validate_household_name(body.get("household_name"))
        if err is not None:
            return err
        adapter = self.svc(platform_adapter_key)
        if Capability.INGRESS not in adapter.capabilities:
            return error_response(
                500,
                "INTERNAL_ERROR",
                "haos adapter is missing the INGRESS capability.",
            )
        sv_client = getattr(adapter, "_supervisor_client", None)
        if sv_client is None:
            return error_response(
                503,
                "SUPERVISOR_UNAVAILABLE",
                "Supervisor client not yet wired — try again after startup.",
            )
        owner = await sv_client.get_owner_username()
        if not owner:
            return error_response(
                422,
                "NO_OWNER",
                "Home Assistant Supervisor reported no owner user.",
            )
        external = await adapter.users.get(owner)
        if external is None:
            return error_response(
                422,
                "NO_OWNER",
                f"Supervisor owner {owner!r} has no person.* entity in HA.",
            )
        await _mirror_admin_user(self.svc(db_key), external)
        await _apply_household_name(self, household_name)
        await self.svc(setup_service_key).mark_complete()
        return web.json_response({"username": owner})


async def _mirror_admin_user(db, external: ExternalUser) -> None:
    """Insert the picked HA person into ``users`` as admin (idempotent)."""
    user_id = f"uid-{external.username}"
    await db.enqueue(
        """
        INSERT INTO users(username, user_id, display_name, is_admin)
        VALUES(?, ?, ?, 1)
        ON CONFLICT(username) DO UPDATE SET is_admin=1
        """,
        (external.username, user_id, external.display_name or external.username),
    )
