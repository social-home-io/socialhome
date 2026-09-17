"""First-boot setup routes — the wizard's three mode-specific endpoints.

* ``POST /api/setup/standalone`` — operator submits ``{username, password}``;
  we seed ``platform_users`` + ``users`` and mark setup complete.
* ``GET  /api/setup/ha/persons`` — list HA persons so the operator can
  pick which one becomes the SH owner.
* ``POST /api/setup/ha/owner`` — operator submits ``{username}`` for the
  picked HA person; we mirror them into ``users`` as admin and mark
  setup complete. ha-mode auth runs through HA (X-Remote-User-Name or HA
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

import asyncio
import base64
import logging
import os
import signal

from aiohttp import web

from ..app_keys import (
    config_key,
    db_key,
    federation_repo_key,
    preferences_service_key,
    platform_adapter_key,
    recovery_kit_service_key,
    setup_service_key,
)
from ..identity_bootstrap import derive_local_user_id
from ..platform.adapter import Capability, ExternalUser
from ..security import error_response
from ..services.recovery_crypto import (
    RecoveryKitError,
    UnsupportedRecoverySuite,
    unseal_kit,
)
from ..services.recovery_kit_service import RecoveryKitService, RecoveryRestoreError
from .base import BaseView

log = logging.getLogger(__name__)


def request_process_restart(delay: float = 1.0) -> None:
    """Schedule a graceful self-SIGTERM so a supervisor/container restarts us
    with the restored identity. Standalone-without-restart-policy operators
    restart manually (the response says restart_required)."""
    loop = asyncio.get_running_loop()
    loop.call_later(delay, lambda: os.kill(os.getpid(), signal.SIGTERM))


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
    :meth:`PreferencesService.update_household` so we fail-fast before
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
    await view.svc(preferences_service_key).update_household(
        actor_is_admin=True,
        household_name=name,
    )
    # Also set the FEDERATED display name — the QR + peer display read
    # instance_identity.display_name, not the local-only preference above.
    await view.svc(federation_repo_key).set_instance_display_name(name)


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
    ``POST /api/auth/token`` (in addition to X-Remote-User-Name and HA
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
    """``POST /api/setup/haos/complete`` — read the owner from HA Core.

    Idempotent. The SPA POSTs this silently on first load when
    ``mode == 'haos'`` and redirects to the app afterwards. The owner
    record (``username``, display ``name``) comes from HA Core's WS
    ``config/auth/list``; the Supervisor's REST ``/auth/list`` is
    deliberately *not* a second source (#297 / #298 — same data,
    smaller payload, kept the slug-vs-username confusion alive).
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
        owner = await adapter.users.get_owner()
        if owner is None:
            return error_response(
                422,
                "NO_OWNER",
                "Home Assistant reported no owner user.",
            )
        # ``owner`` is already an ``ExternalUser`` with the correct
        # auth-provider username + display name + ``external_id`` (the
        # HA ``user_id`` from ``config/auth/list``). Pass it through
        # verbatim — ``_mirror_admin_user`` hardcodes ``is_admin=1`` in
        # the INSERT so the dataclass flag is ignored anyway, and we
        # must not drop ``external_id`` on the floor here: that's what
        # ``_mirror_admin_user`` keys on to stamp ``source='ha'``, and
        # a row with ``source='manual'`` makes the HA Users admin
        # panel show the "Not added" toggle for the household owner.
        await _mirror_admin_user(self.svc(db_key), owner)
        await _apply_household_name(self, household_name)
        await self.svc(setup_service_key).mark_complete()
        return web.json_response({"username": owner.username})


class RecoverySetupRestoreView(BaseView):
    """``POST /api/setup/recovery/restore`` — reconstitute identity from a Kit.

    Setup-gated, no bearer (no admin exists on a fresh box yet — same threat
    model as the other fresh-box setup endpoints; possession of a valid Kit +
    passphrase is the gate). Wipes the auto-minted trust state, restores the
    Kit, marks setup complete, and schedules a restart so the restored
    identity/KEK load on reboot.
    """

    async def post(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        body = await self.body()
        kit_b64 = body.get("kit_b64")
        passphrase = body.get("passphrase")
        if not isinstance(kit_b64, str) or not isinstance(passphrase, str):
            return error_response(
                422, "UNPROCESSABLE", "kit_b64 and passphrase are required."
            )
        try:
            kit_bytes = base64.b64decode(kit_b64, validate=True)
        except Exception:
            return error_response(422, "UNPROCESSABLE", "kit_b64 is not valid base64.")
        svc: RecoveryKitService = self.svc(recovery_kit_service_key)
        # Validate the kit (passphrase + integrity) BEFORE the destructive wipe.
        # reset_trust_layer() is a wide ON DELETE CASCADE; running it only after
        # the kit proves valid means a wrong passphrase can never strand a fresh
        # box, and the cascade cannot fire without a usable kit (defense in depth
        # atop the setup gate).
        try:
            unseal_kit(kit_bytes, passphrase)
        except RecoveryKitError, UnsupportedRecoverySuite:
            return error_response(
                422,
                "BAD_KIT",
                "Recovery kit could not be opened (wrong passphrase or corrupt file).",
            )
        try:
            await svc.reset_trust_layer()
            instance_id = await svc.restore_kit(kit_bytes, passphrase)
        except RecoveryKitError, UnsupportedRecoverySuite:
            # Generic message only — never echo the underlying exception text,
            # so a wrong passphrase isn't a guessing oracle.
            return error_response(
                422,
                "BAD_KIT",
                "Recovery kit could not be opened (wrong passphrase or corrupt file).",
            )
        except RecoveryRestoreError as exc:
            return error_response(422, "RESTORE_FAILED", str(exc))
        await self.svc(setup_service_key).mark_complete()
        request_process_restart()
        return web.json_response({"instance_id": instance_id, "restart_required": True})


async def _mirror_admin_user(db, external: ExternalUser) -> None:
    """Insert the picked HA user into ``users`` as admin (idempotent).

    Stamps ``source='ha'`` and persists the HA-side ``external_id``
    when present so the picture lifter / future presence bridge can
    walk to provider-side resources without re-running the
    username→id lookup. Re-runs of the wizard refresh the
    ``external_id`` to track HA-side rotations.
    """
    # Derived, never synthetic: a ``uid-<username>`` id can't be
    # self-certified by a remote household against our identity key, so every
    # post this admin writes is dropped by the public-space relay.
    user_id = await derive_local_user_id(db, external.username)
    has_external_id = external.external_id is not None
    source = "ha" if has_external_id else "manual"
    await db.enqueue(
        """
        INSERT INTO users(username, user_id, display_name, is_admin,
                          source, external_id, identity_anchor, handle)
        VALUES(?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            is_admin=1,
            source=excluded.source,
            external_id=excluded.external_id
        """,
        (
            external.username,
            user_id,
            external.display_name or external.username,
            source,
            external.external_id,
            # Username-anchored (see ``derive_local_user_id``) so the row keeps
            # ``user_id == derive_user_id(pk, identity_anchor)`` and the column
            # is never NULL. Insert-only — a re-run must not re-anchor a row
            # whose user_id already derives from the stored value.
            external.username,
            # Seed the public ``@handle`` from the username on first insert so
            # the row is never NULL-handle. Left untouched on conflict — a
            # re-run of the wizard must not clobber a handle the user has since
            # customised via the §public-handle editor.
            external.username,
        ),
    )
