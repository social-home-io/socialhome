"""GFS connection routes — /api/gfs/*.

Manages Global Federation Server pairing, disconnection, and
per-space publication control.
"""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from ..security import error_response
from ..services.gfs_connection_service import GfsConnectionError, GfsSignupError
from .base import BaseView


_DEFAULT_HEALTH: dict = {"connected": False, "last_error": None}


def _conn_dict(conn, health: dict | None = None) -> dict:
    """Public-shape view of a :class:`GfsConnection`.

    ``status`` stays the stored PAIRING state (active/pending/suspended).
    ``health`` carries the supervisor's LIVE WS signal — ``connected``
    (is a socket up right now) + ``last_error`` (the last auth/close
    reason when not connected) — so the SPA reflects real liveness rather
    than treating a stored ``status='active'`` as "connected".
    """
    d = asdict(conn)
    # Remove sensitive key material from the API response.
    d.pop("public_key", None)
    h = health if health is not None else _DEFAULT_HEALTH
    d["connected"] = bool(h.get("connected", False))
    d["last_error"] = h.get("last_error")
    return d


#: ``GfsSignupError.reason`` → ``(HTTP status, error code, plain message)``,
#: shared by both ways of connecting to a GFS — a scanned / pasted pairing
#: code and open sign-up — so the same cause answers the same code on each.
#: The SPA shows its own translated copy keyed on the code; the message is
#: the English fallback. Never the GFS's own words (see ``_remote_detail``).
_CONNECT_ERRORS: dict[str, tuple[int, str, str]] = {
    "invalid_url": (
        422,
        "GFS_PAIRING_FAILED",
        "The GFS address can't be used. It needs https://.",
    ),
    "already_connected": (
        409,
        "ALREADY_CONNECTED",
        "You're already connected to this GFS.",
    ),
    "unreachable": (
        502,
        "GFS_UNREACHABLE",
        "Couldn't reach the GFS. Try again later.",
    ),
    "identity_mismatch": (
        422,
        "GFS_IDENTITY_MISMATCH",
        "This doesn't look like the Social Home GFS. Check the address.",
    ),
    "closed": (
        409,
        "GFS_SIGNUP_CLOSED",
        "This GFS doesn't take sign-ups right now. Ask its operator for a "
        "pairing code instead.",
    ),
    "busy": (
        503,
        "GFS_BUSY",
        "The GFS is busy. Try again in a minute.",
    ),
    "refused": (
        422,
        "GFS_PAIRING_FAILED",
        "The GFS didn't accept this household.",
    ),
}


def _connect_error(exc: GfsSignupError) -> web.Response:
    """The classified connect failure as its error response."""
    status, code, message = _CONNECT_ERRORS.get(exc.reason, _CONNECT_ERRORS["refused"])
    return error_response(status, code, message)


def _own_registration_identity(view: BaseView) -> dict:
    """What this household tells a GFS when it pairs.

    One source for QR pairing and open sign-up, so the two can never send
    different things. Deliberately no household address: the GFS relays
    over the WebSocket the household opens, so it has no use for an
    External URL — and on the Home Assistant add-on there is none at
    onboarding time. (Household↔household pairing still needs one; see
    ``routes/pairing.py``.)
    """
    app = view.request.app
    own_pk: bytes = app[K.instance_public_key_key]
    own_keywrap_pk: bytes = app[K.instance_keywrap_public_key_key]
    return {
        "own_instance_id": app[K.instance_id_key],
        "own_public_key_hex": own_pk.hex(),
        "own_display_name": app[K.config_key].instance_name,
        "own_keywrap_public_key_hex": own_keywrap_pk.hex(),
        "own_keywrap_sig": app[K.instance_keywrap_sig_key],
    }


def _pub_dict(pub) -> dict:
    """JSON shape of a :class:`GfsSpacePublication` for the SPA."""
    return {
        "space_id": pub.space_id,
        "gfs_connection_id": pub.gfs_connection_id,
        "published_at": pub.published_at,
        "status": pub.status,
    }


class GfsConnectionCollectionView(BaseView):
    """``GET /api/gfs/connections`` — list.

    ``POST /api/gfs/connections`` — connect with a scanned / pasted GFS
    pairing code ``{gfs_url, token}``. ``201`` with the connection
    (``status`` ``active`` or ``pending``), or the same codes the open
    sign-up step answers for the same causes: ``ALREADY_CONNECTED`` (409),
    ``GFS_UNREACHABLE`` (502 — no answer, or a 5xx), ``GFS_IDENTITY_MISMATCH``
    (422 — the address answers, but not as a GFS) or ``GFS_PAIRING_FAILED``
    (422 — a missing field, an unusable URL, or the GFS refused the token).
    Details are the household's own sentences, never the GFS's words.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        svc = self.svc(K.gfs_connection_service_key)
        connections = await svc.list_connections()
        supervisor = self.request.app.get(K.gfs_ws_supervisor_key)
        return web.json_response(
            [
                _conn_dict(
                    c,
                    supervisor.connection_health(c.id)
                    if supervisor is not None
                    else None,
                )
                for c in connections
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        own = _own_registration_identity(self)
        svc = self.svc(K.gfs_connection_service_key)
        try:
            conn = await svc.pair(body, **own)
        except GfsSignupError as exc:
            return _connect_error(exc)
        except GfsConnectionError as exc:
            # Only a malformed call gets here (missing payload / identity
            # fields) — the household's own words, nothing from a GFS.
            return error_response(422, "GFS_PAIRING_FAILED", str(exc))
        return web.json_response(_conn_dict(conn), status=201)


class GfsDefaultConnectionView(BaseView):
    """The onboarding "Connect to the GFS" step (admin-only).

    ``GET /api/gfs/connections/default`` — whether to offer the step:
    ``{url, available, reason, connection}``. Answers from LOCAL facts only
    and never contacts the GFS: nothing reaches a GFS unless an admin says
    yes. ``reason`` is ``null`` when ``available``, else ``"disabled"``
    (``[gfs] default_url`` is empty) or ``"already_connected"``
    (``connection`` then carries that row, so the SPA can say "waiting for
    approval" for a pending one). No External URL is needed — registration
    sends no household address.

    ``POST /api/gfs/connections/default`` — pair with the default GFS through
    its open sign-up (:meth:`GfsConnectionService.pair_open_signup`). ``201``
    with the connection (``status`` ``active`` or ``pending``), or an error
    code the SPA turns into plain words: ``GFS_DEFAULT_DISABLED`` (404),
    ``ALREADY_CONNECTED`` (409), ``GFS_SIGNUP_CLOSED`` (409),
    ``GFS_UNREACHABLE`` (502), ``GFS_BUSY``
    (503), ``GFS_IDENTITY_MISMATCH`` (422 — ``/gfs/info`` doesn't match the
    pinned ``[gfs] default_instance_id`` / ``default_public_key``) or
    ``GFS_PAIRING_FAILED`` (422).
    """

    async def _existing(self, url: str):
        if not url:
            return None
        svc = self.svc(K.gfs_connection_service_key)
        for conn in await svc.list_connections():
            if conn.inbox_url.rstrip("/") == url.rstrip("/"):
                return conn
        return None

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        url = str(self.request.app[K.config_key].gfs_default_url or "")
        existing = await self._existing(url)
        reason: str | None = None
        if not url:
            reason = "disabled"
        elif existing is not None:
            reason = "already_connected"
        return web.json_response(
            {
                "url": url,
                "available": reason is None,
                "reason": reason,
                "connection": _conn_dict(existing) if existing is not None else None,
            }
        )

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        config = self.request.app[K.config_key]
        url = str(config.gfs_default_url or "")
        if not url:
            return error_response(
                404,
                "GFS_DEFAULT_DISABLED",
                "No default GFS is configured for this household.",
            )
        own = _own_registration_identity(self)
        svc = self.svc(K.gfs_connection_service_key)
        try:
            pin_id, pin_key = config.gfs_default_pin()
            conn = await svc.pair_open_signup(
                url, **own, expect_instance_id=pin_id, expect_public_key=pin_key
            )
        except GfsSignupError as exc:
            return _connect_error(exc)
        return web.json_response(_conn_dict(conn), status=201)


class GfsConnectionDetailView(BaseView):
    """``GET /api/gfs/connections/{id}`` — detail.
    ``DELETE /api/gfs/connections/{id}`` — disconnect.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        gfs_id = self.match("id")
        repo = self.svc(K.gfs_connection_repo_key)
        conn = await repo.get(gfs_id)
        if conn is None:
            return error_response(404, "NOT_FOUND", "GFS connection not found.")
        supervisor = self.request.app.get(K.gfs_ws_supervisor_key)
        health = (
            supervisor.connection_health(gfs_id) if supervisor is not None else None
        )
        return web.json_response(_conn_dict(conn, health))

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        gfs_id = self.match("id")
        svc = self.svc(K.gfs_connection_service_key)
        try:
            await svc.disconnect(gfs_id)
        except GfsConnectionError as exc:
            return error_response(404, "NOT_FOUND", str(exc))
        return web.Response(status=204)


class GfsSpacePublishView(BaseView):
    """``POST /api/spaces/{id}/publish/{gfs_id}`` — publish.
    ``DELETE /api/spaces/{id}/publish/{gfs_id}`` — unpublish.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        space_id = self.match("id")
        gfs_id = self.match("gfs_id")
        svc = self.svc(K.gfs_connection_service_key)
        try:
            pub = await svc.publish_space(space_id, gfs_id)
        except GfsConnectionError as exc:
            return error_response(422, "GFS_PUBLISH_FAILED", str(exc))
        return web.json_response(_pub_dict(pub))

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        space_id = self.match("id")
        gfs_id = self.match("gfs_id")
        svc = self.svc(K.gfs_connection_service_key)
        try:
            await svc.unpublish_space(space_id, gfs_id)
        except GfsConnectionError as exc:
            return error_response(422, "GFS_UNPUBLISH_FAILED", str(exc))
        return web.Response(status=204)


class GfsSpacePublicationsView(BaseView):
    """``GET /api/spaces/{id}/publications`` — list this space's GFS
    publications (with per-row ``status``).

    Drives the space's federation panel in the SPA, fetched on mount.
    Admin-only, mirroring the gating on
    :class:`GfsSpacePublishView.post`.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        space_id = self.match("id")
        repo = self.svc(K.gfs_connection_repo_key)
        pubs = await repo.list_publications_for_space(space_id)
        return web.json_response([_pub_dict(p) for p in pubs])


class GfsPublicationsView(BaseView):
    """``GET /api/gfs/publications`` — §A5 admin list every
    (space, GFS) publication currently active across all pairings.
    Used by the admin Spaces tab to render a "currently published
    to" table with per-row Unpublish button.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        repo = self.svc(K.gfs_connection_repo_key)
        rows = await repo.list_publications_all()
        return web.json_response({"publications": rows})


class GfsAppealView(BaseView):
    """``POST /api/gfs/connections/{gfs_id}/appeal`` — file an appeal.

    Body: ``{target_type: 'space'|'instance', target_id, message}``.
    Sends ``POST /gfs/appeal`` (Ed25519-signed) to the given GFS; on
    success the admin portal's Appeals tab will surface the new row.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(
                401,
                "UNAUTHENTICATED",
                "Authentication required.",
            )
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        gfs_id = self.match("gfs_id")
        body = await self.body()
        target_type = str(body.get("target_type") or "")
        target_id = str(body.get("target_id") or "")
        message = str(body.get("message") or "").strip()
        if target_type not in ("space", "instance") or not target_id:
            return error_response(
                422,
                "UNPROCESSABLE",
                "target_type must be 'space'|'instance' and target_id required",
            )
        svc = self.svc(K.gfs_connection_service_key)
        signing_key = self.request.app[K.instance_signing_key_key]
        own_instance = self.request.app[K.instance_id_key]
        ok = await svc.send_appeal(
            gfs_id,
            target_type=target_type,
            target_id=target_id,
            message=message,
            from_instance=own_instance,
            signing_key=signing_key,
        )
        if not ok:
            return error_response(502, "GFS_APPEAL_FAILED", "GFS did not accept")
        return web.json_response({"status": "submitted"}, status=201)
