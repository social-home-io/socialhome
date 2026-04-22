"""Admin-portal routes (``/admin/api/*``, spec §24.6-§24.9).

Every view subclass is gated by ``admin_auth_middleware`` — an
unauthenticated request to any admin route is rejected by the
middleware before the view runs, so views can assume they're
authenticated. They still record the client IP for the audit log via
``self.client_ip()``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import BodyPartReader, web

from .. import app_keys as K
from ..config import GfsConfig
from ...domain.space import ModerationAlreadyDecidedError
from ...media.image_processor import ImageProcessor
from .base import GfsBaseView

log = logging.getLogger(__name__)

_ADMIN_UI_DIR = Path(__file__).resolve().parent.parent / "admin_ui"


class AdminUiIndexView(GfsBaseView):
    """``GET /admin`` — serves the single-page dashboard HTML."""

    async def get(self) -> web.StreamResponse:
        index = _ADMIN_UI_DIR / "index.html"
        if not index.is_file():
            return web.Response(
                text="GFS admin UI not bundled",
                status=500,
                content_type="text/plain",
            )
        return web.FileResponse(index)


# ─── Overview / clients / spaces ─────────────────────────────────────


class AdminOverviewView(GfsBaseView):
    """``GET /admin/api/overview`` — dashboard counts."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        return web.json_response(await svc.overview())


class AdminClientCollectionView(GfsBaseView):
    """``GET /admin/api/clients`` — list clients filtered by status."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        status = self.request.query.get("status")
        return web.json_response(await svc.list_clients(status=status))


class AdminClientActionView(GfsBaseView):
    """``POST /admin/api/clients/{instance_id}/{action}`` — accept /
    reject / ban / unban."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        instance_id = self.match("instance_id")
        action = self.match("action")
        ip = self.client_ip()
        if action == "accept":
            await svc.accept_client(instance_id, admin_ip=ip)
            return web.json_response(
                {"instance_id": instance_id, "status": "active"},
            )
        if action == "reject":
            await svc.reject_client(instance_id, admin_ip=ip)
            return web.json_response(
                {"instance_id": instance_id, "status": "removed"},
            )
        if action == "ban":
            body = await self.body()
            reason = body.get("reason") if isinstance(body, dict) else None
            await svc.ban_client(instance_id, reason=reason, admin_ip=ip)
            return web.json_response(
                {"instance_id": instance_id, "status": "banned"},
            )
        if action == "unban":
            await svc.unban_client(instance_id, admin_ip=ip)
            return web.json_response(
                {"instance_id": instance_id, "status": "pending"},
            )
        return web.json_response({"error": "unknown_action"}, status=404)


class AdminSpaceCollectionView(GfsBaseView):
    """``GET /admin/api/spaces`` — list spaces filtered by status."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        status = self.request.query.get("status")
        return web.json_response(await svc.list_spaces(status=status))


class AdminSpaceActionView(GfsBaseView):
    """``POST /admin/api/spaces/{space_id}/{action}`` — accept / reject /
    ban / unban."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        space_id = self.match("space_id")
        action = self.match("action")
        ip = self.client_ip()
        if action == "accept":
            await svc.accept_space(space_id, admin_ip=ip)
            return web.json_response(
                {"space_id": space_id, "status": "active"},
            )
        if action == "reject":
            await svc.reject_space(space_id, admin_ip=ip)
            return web.json_response(
                {"space_id": space_id, "status": "removed"},
            )
        if action == "ban":
            body = await self.body()
            reason = body.get("reason") if isinstance(body, dict) else None
            await svc.ban_space(space_id, reason=reason, admin_ip=ip)
            return web.json_response(
                {"space_id": space_id, "status": "banned"},
            )
        if action == "unban":
            await svc.unban_space(space_id, admin_ip=ip)
            return web.json_response(
                {"space_id": space_id, "status": "pending"},
            )
        return web.json_response({"error": "unknown_action"}, status=404)


# ─── Policy + branding ──────────────────────────────────────────────


class AdminPolicyView(GfsBaseView):
    """``GET /admin/api/policy`` + ``PATCH /admin/api/policy``."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        return web.json_response(await svc.get_policy())

    async def patch(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        body = await self.body()
        return web.json_response(
            await svc.set_policy(
                auto_accept_clients=body.get("auto_accept_clients"),
                auto_accept_spaces=body.get("auto_accept_spaces"),
                fraud_threshold=body.get("fraud_threshold"),
                admin_ip=self.client_ip(),
            )
        )


class AdminBrandingView(GfsBaseView):
    """``GET /admin/api/branding`` + ``PATCH /admin/api/branding``."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        return web.json_response(await svc.get_branding())

    async def patch(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        body = await self.body()
        return web.json_response(
            await svc.set_branding(
                server_name=body.get("server_name"),
                landing_markdown=body.get("landing_markdown"),
                header_image_file=body.get("header_image_file"),
                admin_ip=self.client_ip(),
            )
        )


class AdminBrandingHeaderImageView(GfsBaseView):
    """``POST /admin/api/branding/header-image`` — multipart upload.

    Writes the WebP-processed file into ``{data_dir}/media/`` and updates
    ``server_config.header_image_file`` so the landing page picks it up.
    """

    async def post(self) -> web.Response:
        cfg: GfsConfig = self.svc(K.gfs_config_key)
        admin_repo = self.svc(K.gfs_admin_repo_key)
        svc = self.svc(K.gfs_admin_service_key)
        max_bytes = 2 * 1024 * 1024
        try:
            reader = await self.request.multipart()
        except Exception:
            return web.json_response(
                {"error": "invalid_multipart"},
                status=400,
            )
        file_bytes: bytes | None = None
        filename = "header.webp"
        while True:
            part = await reader.next()
            if part is None:
                break
            if not isinstance(part, BodyPartReader):
                continue
            if part.name == "file":
                filename = part.filename or "upload"
                file_bytes = await part.read(decode=False)
                if len(file_bytes) > max_bytes:
                    return web.json_response(
                        {"error": "too_large", "limit_bytes": max_bytes},
                        status=413,
                    )
        if not file_bytes:
            return web.json_response({"error": "missing_file"}, status=400)
        proc = ImageProcessor()
        try:
            webp_bytes, new_name = await proc.process(file_bytes, filename)
        except ValueError:
            return web.json_response(
                {"error": "unsupported_image"},
                status=415,
            )
        media_path = Path(cfg.media_dir)
        try:
            media_path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return web.json_response(
                {"error": "media_dir_not_writable"},
                status=500,
            )
        (media_path / new_name).write_bytes(webp_bytes)
        await admin_repo.set_config("header_image_file", new_name)
        await svc._log(
            "set_header_image",
            None,
            None,
            self.client_ip(),
            metadata={"filename": new_name, "bytes": len(webp_bytes)},
        )
        return web.json_response(
            {
                "header_image_file": new_name,
                "url": f"{cfg.base_url}/media/{new_name}",
            }
        )


# ─── Reports + appeals + audit ──────────────────────────────────────


class AdminReportCollectionView(GfsBaseView):
    """``GET /admin/api/reports`` — list fraud reports by status."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        status = self.request.query.get("status")
        return web.json_response(
            await svc.list_fraud_reports(status=status),
        )


class AdminReportReviewView(GfsBaseView):
    """``POST /admin/api/reports/{report_id}/review`` — admin decision."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        report_id = self.match("report_id")
        body = await self.body()
        action = str(body.get("action") or "")
        try:
            result = await svc.review_fraud_report(
                report_id,
                action=action,
                admin_ip=self.client_ip(),
            )
        except KeyError as exc:
            return web.json_response(
                {"error": "not_found", "detail": str(exc).strip("'\"")},
                status=404,
            )
        except ModerationAlreadyDecidedError as exc:
            return web.json_response(
                {"error": "already_decided", "detail": str(exc)},
                status=409,
            )
        except ValueError as exc:
            return web.json_response(
                {"error": "invalid_action", "detail": str(exc)},
                status=422,
            )
        return web.json_response(result)


class AdminAppealCollectionView(GfsBaseView):
    """``GET /admin/api/appeals`` — list appeals by status."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        status = self.request.query.get("status")
        return web.json_response(await svc.list_appeals(status=status))


class AdminAppealDecideView(GfsBaseView):
    """``POST /admin/api/appeals/{appeal_id}/decide`` — lift / dismiss."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        appeal_id = self.match("appeal_id")
        body = await self.body()
        action = str(body.get("action") or "")
        try:
            result = await svc.decide_appeal(
                appeal_id,
                action=action,
                admin_ip=self.client_ip(),
            )
        except KeyError as exc:
            return web.json_response(
                {"error": "not_found", "detail": str(exc).strip("'\"")},
                status=404,
            )
        except ValueError as exc:
            return web.json_response(
                {"error": "invalid_action", "detail": str(exc)},
                status=422,
            )
        return web.json_response(result)


class AdminAuditView(GfsBaseView):
    """``GET /admin/api/audit`` — paginated audit log."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_admin_service_key)
        action = self.request.query.get("action")
        since = self.request.query.get("since")
        since_int = int(since) if since else None
        try:
            limit = int(self.request.query.get("limit", "200"))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 500))
        return web.json_response(
            await svc.list_audit_log(
                action=action,
                since=since_int,
                limit=limit,
            )
        )


# ─── Cluster tab ────────────────────────────────────────────────────


class AdminClusterCollectionView(GfsBaseView):
    """``GET /admin/api/cluster`` — node + peer list."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_cluster_key)
        return web.json_response(await svc.health())


class AdminClusterPeerCollectionView(GfsBaseView):
    """``POST /admin/api/cluster/peers`` — add a peer."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_cluster_key)
        body = await self.body()
        peer_url = str(body.get("url") or "").rstrip("/")
        if not peer_url:
            return web.json_response(
                {"error": "missing_url"},
                status=422,
            )
        node = await svc.add_peer(peer_url)
        return web.json_response(
            {"node_id": node.node_id, "url": node.url},
            status=201,
        )


class AdminClusterPeerDetailView(GfsBaseView):
    """``DELETE /admin/api/cluster/peers/{node_id}`` — remove a peer."""

    async def delete(self) -> web.Response:
        svc = self.svc(K.gfs_cluster_key)
        await svc.remove_peer(self.match("node_id"))
        return web.json_response({"ok": True})


class AdminClusterPeerPingView(GfsBaseView):
    """``POST /admin/api/cluster/peers/{node_id}/ping`` — retry ping."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_cluster_key)
        node_id = self.match("node_id")
        nodes = await self.svc(K.gfs_cluster_repo_key).list_nodes()
        match = next((n for n in nodes if n.node_id == node_id), None)
        if match is None:
            return web.json_response(
                {"error": "not_found"},
                status=404,
            )
        ok = await svc.ping_peer(match.url)
        return web.json_response({"node_id": node_id, "online": ok})
