"""GFS connection routes — /api/gfs/*.

Manages Global Federation Server pairing, disconnection, and
per-space publication control.
"""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from ..security import error_response
from ..services.gfs_connection_service import GfsConnectionError
from .base import BaseView


def _conn_dict(conn) -> dict:
    """Public-shape view of a :class:`GfsConnection`."""
    d = asdict(conn)
    # Remove sensitive key material from the API response.
    d.pop("public_key", None)
    return d


class GfsConnectionCollectionView(BaseView):
    """``GET /api/gfs/connections`` — list.
    ``POST /api/gfs/connections`` — pair via QR payload.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        svc = self.svc(K.gfs_connection_service_key)
        connections = await svc.list_connections()
        return web.json_response([_conn_dict(c) for c in connections])

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Authentication required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        svc = self.svc(K.gfs_connection_service_key)
        try:
            conn = await svc.pair(body)
        except GfsConnectionError as exc:
            return error_response(422, "GFS_PAIRING_FAILED", str(exc))
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
        return web.json_response(_conn_dict(conn))

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
            await svc.publish_space(space_id, gfs_id)
        except GfsConnectionError as exc:
            return error_response(422, "GFS_PUBLISH_FAILED", str(exc))
        return web.Response(status=204)

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
