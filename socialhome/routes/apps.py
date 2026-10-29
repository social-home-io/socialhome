"""App-registry routes — install, uninstall, enable/disable, catalog, and per-user store.

Four resource groups:

* ``AppCollectionView``      — ``GET /api/apps`` (list) + ``POST /api/apps`` (install)
* ``AppCatalogView``         — ``GET /api/apps/catalog`` (browse remote catalog)
* ``AppRequestsView``        — ``POST/DELETE /api/apps/{app_id}/requests``
  ("Ask an admin" / "Not now")
* ``AppDetailView``          — ``GET /api/apps/{app_id}`` + ``PATCH`` (toggle) + ``DELETE``
* ``AppUninstallPreviewView`` — ``GET /api/apps/{app_id}/uninstall-preview``
  (what an uninstall loses, admins)
* ``AppStoreCollectionView`` — ``GET /api/apps/{app_id}/store`` (list all KV pairs)
* ``AppStoreItemView``       — ``GET/PUT/DELETE /api/apps/{app_id}/store/{key}``

Permissions (owner decision D2). Every route an app reaches through the
SPA's bridge checks the permission it needs against the app's *installed*
manifest (:func:`~socialhome.domain.apps.app_permissions`) and answers
``403 APP_PERMISSION_DENIED`` (``params.permission``) without it:

=================================================  =========================
Route                                              Permission
=================================================  =========================
``GET /store``, ``GET/PUT/DELETE /store/{key}``    ``storage``
``GET /contacts``, ``GET /invitable``              ``people``
``GET /peers``                                     ``federation``
``POST /sessions``, ``POST /messages``,            ``people`` — and
``POST /sessions/{sid}/cancel``,                   ``federation`` when the
``POST /sessions/{sid}/continue``                  target is in another
                                                   household (service)
``POST /sessions/{sid}/accept``,                   ``people``
``POST /sessions/{sid}/decline``, ``POST /held``
=================================================  =========================

``/runtime`` (it hands the permissions to the bridge), ``/bundle`` and the
admin routes (install, update, toggle, catalog) need none.
"""

from __future__ import annotations

import re

import orjson
from aiohttp import web

from ..app_keys import (
    app_federation_service_key,
    app_service_key,
    notification_service_key,
)
from ..domain.apps import (
    PERMISSION_FEDERATION,
    PERMISSION_PEOPLE,
    AppAgeRestrictedError,
    AppCatalogEntry,
    AppCatalogStatus,
    InstalledApp,
)
from ..security import error_response
from ..services.app_service import AppService
from .base import BaseView

# Maximum encoded payload size for app federation messages (256 KiB).
_MAX_APP_PAYLOAD_BYTES = 256 * 1024


def _catalog_freshness(status: AppCatalogStatus) -> dict:
    """``checked_at`` (when the catalog last answered, UTC ISO 8601 or
    ``None``) and ``stale`` (the latest try failed: this is that earlier
    answer) — so the SPA only says "up to date" after the catalog answered."""
    return {"checked_at": status.checked_at, "stale": status.stale}


def _serialize(app: InstalledApp, names: dict[str, str] | None = None) -> dict:
    """Return a safe, wire-shape dict for an :class:`InstalledApp`.

    ``permissions`` are the plain-word ones the household agreed to (the
    installed manifest; ``people`` implied by ``federation``);
    ``installed_by_name`` says who added it ("Added by Anna") when
    *names* holds them."""
    return {
        "app_id": app.app_id,
        "name": app.name,
        "version": app.version,
        "enabled": app.enabled,
        "capabilities": list(app.manifest.capabilities),
        "permissions": list(app.permissions),
        "icon": app.manifest.icon,
        "min_age": app.min_age,
        "installed_at": app.installed_at,
        "installed_by_name": (names or {}).get(app.installed_by or ""),
    }


async def _app_json(svc: AppService, app: InstalledApp) -> dict:
    """:func:`_serialize` with who added it named."""
    names = await svc.user_names({app.installed_by} if app.installed_by else set())
    return _serialize(app, names)


def _serialize_entry(
    entry: AppCatalogEntry, *, compatible: bool, requested: bool
) -> dict:
    """A catalog entry on the wire: the v1 fields plus the schema-2 ones
    (``None`` / empty when the catalog doesn't say). Texts per locale ride
    as ``i18n`` and the SPA picks the viewer's language (switching
    language needs no refetch). ``compatible`` — this Social Home can
    install it; ``requested`` — the caller asked for it and nobody has
    answered yet."""
    return {
        "app_id": entry.app_id,
        "name": entry.name,
        "latest_version": entry.latest_version,
        "description": entry.description,
        "icon_url": entry.icon_url,
        "capabilities": list(entry.capabilities),
        "permissions": list(entry.permissions),
        "i18n": {
            t.locale: {"name": t.name, "description": t.description} for t in entry.i18n
        },
        "bundle_size": entry.bundle_size,
        "min_age": entry.min_age,
        "languages": list(entry.languages),
        "publisher": entry.publisher,
        "homepage": entry.homepage,
        "min_core_version": entry.min_core_version,
        "changelog": [
            {"version": c.version, "date": c.date, "notes": dict(c.notes)}
            for c in entry.changelog
        ],
        "compatible": compatible,
        "requested": requested,
    }


class AppPermissionView(BaseView):
    """A route an app reaches through the bridge: it needs a permission."""

    async def require_permission(self, permission: str) -> None:
        """The ``{app_id}`` app must be installed, enabled, allowed for the
        caller (age gate) and granted *permission* — else the domain error
        (404 / 403 ``APP_NOT_ENABLED`` / ``APP_AGE_RESTRICTED`` /
        ``APP_PERMISSION_DENIED``), mapped by ``BaseView._iter``."""
        await self.svc(app_service_key).require_app_permission(
            self.match("app_id"), self.user.user_id, permission
        )


class AppCollectionView(BaseView):
    """``GET /api/apps`` + ``POST /api/apps``."""

    async def get(self) -> web.Response:
        user = self.user
        svc = self.svc(app_service_key)
        apps = await svc.list_visible(user_id=user.user_id, is_admin=user.is_admin)
        names = await svc.user_names({a.installed_by for a in apps if a.installed_by})
        return self._json({"apps": [_serialize(a, names) for a in apps]})

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
        names = await svc.user_names({user.user_id})
        return self._json(_serialize(app, names), status=201)


class AppCatalogView(BaseView):
    """``GET /api/apps/catalog[?refresh=1]`` — every member (D4: members
    see "More apps" and can ask for one); a protected account doesn't see
    apps rated above its age (D5). ``refresh`` (admins only — it reaches
    the network) re-fetches past the cache and the retry pause."""

    async def get(self) -> web.Response:
        user = self.user
        svc = self.svc(app_service_key)
        entries = await svc.browse_catalog_for(
            user_id=user.user_id,
            is_admin=user.is_admin,
            force=bool(self.request.query.get("refresh")),
        )
        asked: frozenset[str] = frozenset()
        if not user.is_admin and entries:
            asked = await self.svc(notification_service_key).pending_app_requests(
                user.user_id
            )
        return self._json(
            {
                # ``False`` when the operator turned the catalog off
                # (``apps_catalog_url = ""``) — the SPA says so instead of
                # "no apps available".
                "enabled": svc.catalog_enabled,
                # D6: ``None`` = Social Home's own app list ("From Social
                # Home"); else the host an operator set.
                "source_host": svc.catalog_source_host,
                "apps": [
                    _serialize_entry(
                        e,
                        compatible=svc.is_compatible(e),
                        requested=e.app_id in asked,
                    )
                    for e in entries
                ],
                **_catalog_freshness(svc.catalog_status),
            }
        )


class AppRequestsView(BaseView):
    """``POST /api/apps/{app_id}/requests`` — a member asks for a catalog
    app ("Ask an admin"; a protected account's request goes to its
    guardians). ``DELETE`` — an admin answers every open request for it
    with "Not now". The request is the deciders' "Needs you" row; adding
    the app resolves it too."""

    async def post(self) -> web.Response:
        user = self.user
        await self.svc(app_service_key).request_app(
            self.match("app_id"), user_id=user.user_id, is_admin=user.is_admin
        )
        return self._json({"requested": True}, status=202)

    async def delete(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        await self.svc(app_service_key).dismiss_requests(
            self.match("app_id"), actor_is_admin=True, actor_user_id=user.user_id
        )
        return self._json({"ok": True})


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
        return self._json(await _app_json(svc, app))

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
        return self._json(await _app_json(svc, app))

    async def delete(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        app_id = self.match("app_id")
        svc = self.svc(app_service_key)
        await svc.uninstall(app_id, actor_is_admin=True)
        return self._json({"status": "ok"})


class AppUninstallPreviewView(BaseView):
    """``GET /api/apps/{app_id}/uninstall-preview`` (admins) — what an
    uninstall loses (D9): ``{people, games, invites, told, told_more,
    untold}``. Counts and the names of people in other households who will
    be told — never what the games hold, nor who here plays whom."""

    async def get(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        preview = await self.svc(app_service_key).uninstall_preview(
            self.match("app_id"), actor_is_admin=True
        )
        return self._json(
            {
                "people": preview.people,
                "games": preview.games,
                "invites": preview.invites,
                "told": list(preview.told),
                "told_more": preview.told_more,
                "untold": preview.untold,
            }
        )


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

    Admin-only. Body (optional): ``{"accept_permissions": [...]}`` — the
    new permissions the admin said yes to. A version asking for more than
    the household agreed to is refused with 409 ``APP_NEW_PERMISSIONS``
    (``params.permissions``: only the new ones) until they are accepted
    (D7). Raises :class:`~socialhome.domain.apps.AppNotFoundError` if the
    app is not installed; :class:`~socialhome.domain.apps.AppIntegrityError`
    on bundle-integrity failure (all mapped centrally by
    :meth:`~socialhome.routes.base.BaseView._iter`).
    """

    async def post(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body() if self.request.can_read_body else {}
        accept = body.get("accept_permissions", []) if isinstance(body, dict) else None
        if not isinstance(accept, list) or not all(isinstance(p, str) for p in accept):
            return error_response(
                400, "UNPROCESSABLE", "accept_permissions must be a list of strings."
            )
        svc = self.svc(app_service_key)
        app = await svc.update_app(
            self.match("app_id"),
            actor_is_admin=True,
            actor_user_id=user.user_id,
            accept_permissions=frozenset(accept),
        )
        return self._json(await _app_json(svc, app))


class AppPeersView(AppPermissionView):
    """``GET /api/apps/{app_id}/peers`` — list confirmed federation peers
    (``federation``)."""

    async def get(self) -> web.Response:
        await self.require_permission(PERMISSION_FEDERATION)
        svc = self.svc(app_federation_service_key)
        peers = await svc.list_peers()
        return self._json({"peers": peers})


class AppContactsView(AppPermissionView):
    """``GET /api/apps/{app_id}/contacts`` — the people the app may know
    about (``people``): who the caller picked in the invite sheet and who
    they play with in this app (D3) — never the roster."""

    async def get(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        svc = self.svc(app_federation_service_key)
        contacts = await svc.list_contacts(
            app_id=self.match("app_id"), self_user_id=self.user.user_id
        )
        return self._json({"contacts": contacts})


class AppInvitableView(AppPermissionView):
    """``GET /api/apps/{app_id}/invitable`` — the people Social Home's
    invite sheet lists (``people``) and what the invite caps leave
    (``invites: {open, max_open, today, max_per_day}``). The sheet is
    drawn by Social Home outside the app's frame; the bridge never offers
    this list to an app (D3)."""

    async def get(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        app_id = self.match("app_id")
        uid = self.user.user_id
        svc = self.svc(app_federation_service_key)
        people = await svc.list_invitable(app_id=app_id, self_user_id=uid)
        invites = await svc.invite_counts(app_id=app_id, user_id=uid)
        return self._json({"people": people, "invites": invites})


class AppSessionsView(AppPermissionView):
    """``POST /api/apps/{app_id}/sessions`` — send a game invite to one
    person picked in Social Home's invite sheet (``people``; ``federation``
    too for another household — checked by the service, which knows where
    the target lives). The app's bridge never calls this (D3). ``429
    APP_INVITES_OPEN_LIMIT`` / ``APP_INVITES_DAILY_LIMIT`` past the invite
    caps."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        app_id = self.match("app_id")
        target = _target(await self.body())
        if target is None:
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


class AppSessionDeclineView(AppPermissionView):
    """``POST /api/apps/{app_id}/sessions/{session_id}/decline`` — the
    invitee declines a game invite that waits for them; the challenger's
    app hears ``{verb: "close", reason: "declined"}``. ``409
    APP_CHALLENGE_HANDLED`` when it no longer waits.

    An invite that waits for its app (D10: not in the household, or
    turned off) is declined here too — without the app, and without
    anything sent to the challenger."""

    async def post(self) -> web.Response:
        svc = self.svc(app_federation_service_key)
        if await svc.decline_waiting(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        ):
            return self._json({"ok": True})
        await self.require_permission(PERMISSION_PEOPLE)
        await svc.decline_session(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"ok": True})


#: An app's own session id: what the SPA's ``?session=`` accepts too.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class AppSessionContinueView(AppPermissionView):
    """``POST /api/apps/{app_id}/sessions/{session_id}/continue`` — pick a
    game this household no longer knows up again with one person, from
    Social Home's sheet ("Continue game with Ben"; ``people``, and
    ``federation`` for another household). Body ``{target}``. Returns
    ``{session_id}``. ``409 APP_SESSION_TAKEN`` when the id is known with
    someone else; the invite caps apply (``429``)."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        session_id = self.match("session_id")
        if not _SESSION_ID_RE.fullmatch(session_id):
            return error_response(400, "UNPROCESSABLE", "Not a session id.")
        target = _target(await self.body())
        if target is None:
            return error_response(
                400,
                "UNPROCESSABLE",
                "target must have non-empty string instance_id, non-empty "
                "string user_ref, and a bool is_local.",
            )
        sid = await self.svc(app_federation_service_key).continue_session(
            app_id=self.match("app_id"),
            session_id=session_id,
            target=target,
            actor_user_id=self.user.user_id,
        )
        return self._json({"session_id": sid})


class AppSessionCancelView(AppPermissionView):
    """``POST /api/apps/{app_id}/sessions/{session_id}/cancel`` — the
    challenger withdraws an invite. Body ``{target}`` — the person the
    session was opened with."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
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


class AppSessionAcceptView(AppPermissionView):
    """``POST /api/apps/{app_id}/sessions/{session_id}/accept`` — the
    invitee plays one game invite ("Play" in Needs you). Only that invite
    is taken in (every other one keeps waiting) and returned, for the app
    host to hand to the app as its launch context. ``409
    APP_CHALLENGE_HANDLED`` when it no longer waits."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
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


class AppHeldView(AppPermissionView):
    """``POST /api/apps/{app_id}/held`` — the app opened: hand over what
    the caller's mailbox kept for it (D11) — the latest undelivered
    message of each game, oldest first — and clear it. ``kind`` is
    ``"message"`` (a move) or ``"session"`` (a close: a decline or a
    withdrawal). A POST: taking them empties the mailbox."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        held = await self.svc(app_federation_service_key).take_held(
            app_id=self.match("app_id"), actor_user_id=self.user.user_id
        )
        return self._json(
            {
                "messages": [
                    {
                        "session_id": h.session_id,
                        "from_instance": h.from_instance,
                        "from_user": h.from_user,
                        "kind": (h.held or {}).get("kind", "message"),
                        "payload": (h.held or {}).get("payload"),
                    }
                    for h in held
                ]
            }
        )


class AppMessagesView(AppPermissionView):
    """``POST /api/apps/{app_id}/messages`` — send an app-layer message to a
    person (``people``; ``federation`` too for another household — checked
    by the service), only in a game this household knows the caller plays
    with them (``403 APP_SESSION_UNKNOWN`` otherwise, D3)."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        app_id = self.match("app_id")
        body = await self.body()
        session_id = body.get("session_id")
        target = _target(body)
        payload = body.get("payload")

        if not isinstance(session_id, str) or not session_id:
            return error_response(
                400, "UNPROCESSABLE", "session_id must be a non-empty string."
            )

        if target is None:
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
