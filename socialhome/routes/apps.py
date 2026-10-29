"""App-registry routes — install, uninstall, enable/disable, catalog, and per-user store.

Four resource groups:

* ``AppCollectionView``      — ``GET /api/apps`` (list) + ``POST /api/apps`` (install)
* ``AppCatalogView``         — ``GET /api/apps/catalog`` (browse remote catalog)
* ``AppDetailView``          — ``GET /api/apps/{app_id}`` + ``PATCH`` (toggle) + ``DELETE``
* ``AppStoreCollectionView`` — ``GET /api/apps/{app_id}/store`` (list all KV pairs)
* ``AppStoreItemView``       — ``GET/PUT/DELETE /api/apps/{app_id}/store/{key}``
"""

from __future__ import annotations

import orjson
from aiohttp import web

from ..app_keys import app_federation_service_key, app_service_key
from ..domain.apps import AppAgeRestrictedError, AppCatalogStatus, InstalledApp
from ..security import error_response
from .base import BaseView

# Maximum encoded payload size for app federation messages (256 KiB).
_MAX_APP_PAYLOAD_BYTES = 256 * 1024


def _catalog_freshness(status: AppCatalogStatus) -> dict:
    """``checked_at`` (when the catalog last answered, UTC ISO 8601 or
    ``None``) and ``stale`` (the latest try failed: this is that earlier
    answer) — so the SPA only says "up to date" after the catalog answered."""
    return {"checked_at": status.checked_at, "stale": status.stale}


def _serialize(app: InstalledApp) -> dict:
    """Return a safe, wire-shape dict for an :class:`InstalledApp`."""
    return {
        "app_id": app.app_id,
        "name": app.name,
        "version": app.version,
        "enabled": app.enabled,
        "capabilities": list(app.manifest.capabilities),
        "icon": app.manifest.icon,
        "min_age": app.min_age,
    }


class AppCollectionView(BaseView):
    """``GET /api/apps`` + ``POST /api/apps``."""

    async def get(self) -> web.Response:
        user = self.user
        svc = self.svc(app_service_key)
        apps = await svc.list_visible(user_id=user.user_id, is_admin=user.is_admin)
        return self._json({"apps": [_serialize(a) for a in apps]})

    async def post(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        body = await self.body()
        app_id = body.get("app_id")
        if not isinstance(app_id, str) or not app_id:
            return error_response(
                400, "UNPROCESSABLE", "app_id must be a non-empty string."
            )

        svc = self.svc(app_service_key)
        app = await svc.install(
            app_id,
            actor_is_admin=True,
            actor_user_id=user.user_id,
        )
        return self._json(_serialize(app), status=201)


class AppCatalogView(BaseView):
    """``GET /api/apps/catalog[?refresh=1]`` — admin-only; ``refresh``
    re-fetches past the cache (and past the retry pause after a failure)."""

    async def get(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        svc = self.svc(app_service_key)
        entries = await svc.browse_catalog(
            force=bool(self.request.query.get("refresh"))
        )
        return self._json(
            {
                # ``False`` when the operator turned the catalog off
                # (``apps_catalog_url = ""``) — the SPA says so instead of
                # "no apps available".
                "enabled": svc.catalog_enabled,
                "apps": [
                    {
                        "app_id": e.app_id,
                        "name": e.name,
                        "latest_version": e.latest_version,
                        "description": e.description,
                        "icon_url": e.icon_url,
                        "capabilities": list(e.capabilities),
                    }
                    for e in entries
                ],
                **_catalog_freshness(svc.catalog_status),
            }
        )


class AppDetailView(BaseView):
    """``GET /api/apps/{app_id}`` + ``PATCH`` + ``DELETE``."""

    async def get(self) -> web.Response:
        user = self.user
        app_id = self.match("app_id")
        svc = self.svc(app_service_key)
        app = await svc.get(app_id)
        if app is None or (not user.is_admin and not app.enabled):
            return error_response(404, "NOT_FOUND", "App not found.")
        # Age gate (non-admins only): return 404 rather than 403 so a minor
        # cannot confirm that the app exists by probing the endpoint.
        if not user.is_admin:
            try:
                await svc.assert_age_allowed(app, user.user_id)
            except AppAgeRestrictedError:
                return error_response(404, "NOT_FOUND", "App not found.")
        return self._json(_serialize(app))

    async def patch(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        app_id = self.match("app_id")
        body = await self.body()

        # Must provide at least one recognised field.
        if "enabled" not in body and "min_age" not in body:
            return error_response(
                400,
                "UNPROCESSABLE",
                "At least one of 'enabled' or 'min_age' is required.",
            )

        svc = self.svc(app_service_key)
        app: InstalledApp | None = None

        if "enabled" in body:
            enabled = body["enabled"]
            if not isinstance(enabled, bool):
                return error_response(
                    400, "UNPROCESSABLE", "enabled must be a boolean."
                )
            app = await svc.set_enabled(app_id, enabled=enabled, actor_is_admin=True)

        if "min_age" in body:
            raw_min_age = body["min_age"]
            if not isinstance(raw_min_age, int):
                return error_response(
                    400, "UNPROCESSABLE", "min_age must be an integer."
                )
            # Delegate validation (allowed set + not found) to the service;
            # ValueError maps to 422 via BaseView._iter.
            app = await svc.set_min_age(
                app_id, min_age=raw_min_age, actor_is_admin=True
            )

        # app is guaranteed non-None — at least one branch ran.
        assert app is not None
        return self._json(_serialize(app))

    async def delete(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        app_id = self.match("app_id")
        svc = self.svc(app_service_key)
        await svc.uninstall(app_id, actor_is_admin=True)
        return self._json({"status": "ok"})


class AppStoreCollectionView(BaseView):
    """``GET /api/apps/{app_id}/store`` — list all KV pairs for the caller."""

    async def get(self) -> web.Response:
        app_id = self.match("app_id")
        svc = self.svc(app_service_key)
        items = await svc.store_list(app_id, self.user.user_id)
        # Do NOT use self._json here: sanitise_for_api strips keys in
        # SENSITIVE_FIELDS (e.g. "signature", "endpoint") from nested dicts,
        # which would silently corrupt opaque user-supplied KV values.
        return web.json_response({"items": items})


class AppStoreItemView(BaseView):
    """``GET/PUT/DELETE /api/apps/{app_id}/store/{key}`` — single KV entry."""

    async def get(self) -> web.Response:
        app_id, key = self.match("app_id"), self.match("key")
        svc = self.svc(app_service_key)
        try:
            value = await svc.store_get(app_id, self.user.user_id, key)
        except KeyError:
            return error_response(404, "NOT_FOUND", "Key not found.")
        # Do NOT use self._json here: sanitise_for_api strips keys in
        # SENSITIVE_FIELDS (e.g. "signature", "endpoint") from nested dicts,
        # which would silently corrupt opaque user-supplied KV values.
        return web.json_response({"key": key, "value": value})

    async def put(self) -> web.Response:
        app_id, key = self.match("app_id"), self.match("key")
        body = await self.body()
        if "value" not in body:
            return error_response(400, "UNPROCESSABLE", "value is required.")
        svc = self.svc(app_service_key)
        await svc.store_set(app_id, self.user.user_id, key, body["value"])
        # Do NOT use self._json here: sanitise_for_api strips keys in
        # SENSITIVE_FIELDS (e.g. "signature", "endpoint") from nested dicts,
        # which would silently corrupt opaque user-supplied KV values.
        return web.json_response({"key": key, "value": body["value"]})

    async def delete(self) -> web.Response:
        app_id, key = self.match("app_id"), self.match("key")
        svc = self.svc(app_service_key)
        await svc.store_delete(app_id, self.user.user_id, key)
        return web.json_response({"status": "ok"})


class AppUpdatesView(BaseView):
    """``GET /api/apps/updates`` — list installed apps with a newer catalog version.

    Any authenticated member may call this endpoint.  Forcing a re-fetch of
    the remote catalog (``?refresh=1`` or ``?refresh=true``) is restricted to
    admins because it hits the external network; non-admin callers silently
    fall back to the ≤24 h cache.
    """

    async def get(self) -> web.Response:
        user = self.user
        refresh = self.request.query.get("refresh")
        # Force-refresh hits the external catalog — admins only.
        force = bool(refresh) and user.is_admin
        svc = self.svc(app_service_key)
        updates = await svc.list_updates(force=force)
        return self._json(
            {"updates": updates, **_catalog_freshness(svc.catalog_status)}
        )


class AppUpdateView(BaseView):
    """``POST /api/apps/{app_id}/update`` — upgrade an app to its latest catalog version.

    Admin-only.  Raises :class:`~socialhome.domain.apps.AppNotFoundError` if
    the app is not installed; :class:`~socialhome.domain.apps.AppIntegrityError`
    on bundle-integrity failure (both are mapped centrally by
    :meth:`~socialhome.routes.base.BaseView._iter`).
    """

    async def post(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        svc = self.svc(app_service_key)
        app = await svc.update_app(self.match("app_id"), actor_is_admin=True)
        return self._json(_serialize(app))


class AppPeersView(BaseView):
    """``GET /api/apps/{app_id}/peers`` — list confirmed federation peers."""

    async def get(self) -> web.Response:
        svc = self.svc(app_federation_service_key)
        peers = await svc.list_peers()
        return self._json({"peers": peers})


class AppContactsView(BaseView):
    """``GET /api/apps/{app_id}/contacts`` — people this user can challenge."""

    async def get(self) -> web.Response:
        svc = self.svc(app_federation_service_key)
        contacts = await svc.list_contacts(self_user_id=self.user.user_id)
        return self._json({"contacts": contacts})


class AppSessionsView(BaseView):
    """``POST /api/apps/{app_id}/sessions`` — open a cross-household app session."""

    async def post(self) -> web.Response:
        app_id = self.match("app_id")
        body = await self.body()
        target = body.get("target")

        if target is None:
            # Back-compat: a legacy household-addressed body carries
            # ``peer_instance_id`` instead of a ``target`` object.  Map it to
            # a remote target with an empty user_ref (household fan-out).
            peer_instance_id = body.get("peer_instance_id")
            if not isinstance(peer_instance_id, str) or not peer_instance_id:
                return error_response(
                    400,
                    "UNPROCESSABLE",
                    "target object or peer_instance_id is required.",
                )
            target = {
                "instance_id": peer_instance_id,
                "user_ref": "",
                "is_local": False,
            }
        elif (
            not isinstance(target, dict)
            or not isinstance(target.get("instance_id"), str)
            or not target["instance_id"]
            or not isinstance(target.get("user_ref"), str)
            or not target["user_ref"]
            or not isinstance(target.get("is_local"), bool)
        ):
            return error_response(
                400,
                "UNPROCESSABLE",
                "target must have non-empty string instance_id, non-empty "
                "string user_ref, and a bool is_local.",
            )

        svc = self.svc(app_federation_service_key)
        session_id = await svc.open_session(
            app_id=app_id,
            target=target,
            actor_user_id=self.user.user_id,
        )
        return self._json({"session_id": session_id}, status=201)


def _target(body: dict) -> dict | None:
    """A ``{instance_id, user_ref, is_local}`` person target, or ``None``."""
    target = body.get("target")
    if (
        not isinstance(target, dict)
        or not isinstance(target.get("instance_id"), str)
        or not target["instance_id"]
        or not isinstance(target.get("user_ref"), str)
        or not target["user_ref"]
        or not isinstance(target.get("is_local"), bool)
    ):
        return None
    return target


class AppSessionDeclineView(BaseView):
    """``POST /api/apps/{app_id}/sessions/{session_id}/decline`` — the
    invitee declines a game invite that waits for them; the challenger's
    app hears ``{verb: "close", reason: "declined"}``. ``409
    APP_CHALLENGE_HANDLED`` when it no longer waits."""

    async def post(self) -> web.Response:
        await self.svc(app_federation_service_key).decline_session(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"ok": True})


class AppSessionCancelView(BaseView):
    """``POST /api/apps/{app_id}/sessions/{session_id}/cancel`` — the
    challenger withdraws an invite. Body ``{target}`` — the person the
    session was opened with."""

    async def post(self) -> web.Response:
        target = _target(await self.body())
        if target is None:
            return error_response(
                400,
                "UNPROCESSABLE",
                "target must have non-empty string instance_id, non-empty "
                "string user_ref, and a bool is_local.",
            )
        await self.svc(app_federation_service_key).cancel_session(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            target=target,
            actor_user_id=self.user.user_id,
        )
        return self._json({"ok": True})


class AppSessionAcceptView(BaseView):
    """``POST /api/apps/{app_id}/sessions/{session_id}/accept`` — the
    invitee plays one game invite ("Play" in Needs you). Only that invite
    is taken in (every other one keeps waiting) and returned, for the app
    host to hand to the app as its launch context. ``409
    APP_CHALLENGE_HANDLED`` when it no longer waits."""

    async def post(self) -> web.Response:
        invite = await self.svc(app_federation_service_key).accept_session(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json(
            {
                "session": {
                    "session_id": invite.session_id,
                    "from_instance": invite.from_instance,
                    "from_user": invite.from_user,
                    "payload": invite.payload,
                }
            }
        )


class AppMessagesView(BaseView):
    """``POST /api/apps/{app_id}/messages`` — send an app-layer message to a peer."""

    async def post(self) -> web.Response:
        app_id = self.match("app_id")
        body = await self.body()
        session_id = body.get("session_id")
        target = body.get("target")
        payload = body.get("payload")

        if not isinstance(session_id, str) or not session_id:
            return error_response(
                400, "UNPROCESSABLE", "session_id must be a non-empty string."
            )

        if target is None:
            # Back-compat: a legacy household-addressed body carries
            # ``peer_instance_id`` instead of a ``target`` object.  Map it to
            # a remote target with an empty user_ref (household fan-out).
            peer_instance_id = body.get("peer_instance_id")
            if not isinstance(peer_instance_id, str) or not peer_instance_id:
                return error_response(
                    400,
                    "UNPROCESSABLE",
                    "target object or peer_instance_id is required.",
                )
            target = {
                "instance_id": peer_instance_id,
                "user_ref": "",
                "is_local": False,
            }
        elif (
            not isinstance(target, dict)
            or not isinstance(target.get("instance_id"), str)
            or not target["instance_id"]
            or not isinstance(target.get("user_ref"), str)
            or not target["user_ref"]
            or not isinstance(target.get("is_local"), bool)
        ):
            return error_response(
                400,
                "UNPROCESSABLE",
                "target must have non-empty string instance_id, non-empty "
                "string user_ref, and a bool is_local.",
            )

        if payload is None:
            return error_response(400, "UNPROCESSABLE", "payload is required.")

        # Guard against excessively large payloads before forwarding to
        # the federation layer.  JSON-encode to get a byte-accurate count.
        try:
            encoded_size = len(orjson.dumps(payload))
        except TypeError, ValueError:
            return error_response(
                400, "UNPROCESSABLE", "payload must be JSON-serialisable."
            )
        if encoded_size > _MAX_APP_PAYLOAD_BYTES:
            return error_response(
                413,
                "PAYLOAD_TOO_LARGE",
                f"payload exceeds the {_MAX_APP_PAYLOAD_BYTES // 1024} KiB limit.",
            )

        svc = self.svc(app_federation_service_key)
        await svc.send_message(
            app_id=app_id,
            target=target,
            session_id=session_id,
            payload=payload,
            actor_user_id=self.user.user_id,
        )
        return web.json_response({"ok": True})
