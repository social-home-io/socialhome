"""Routes for user-filed content reports."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import report_service_key
from ..domain.report import (
    DuplicateReportError,
    ReportRateLimitedError,
)
from .base import BaseView, error_response


class ReportCollectionView(BaseView):
    """POST /api/reports — any authenticated member."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(report_service_key)
        body = await self.body()
        target_type = str(body.get("target_type") or "")
        target_id = str(body.get("target_id") or "")
        category = str(body.get("category") or "")
        notes = body.get("notes")
        if not target_type or not target_id or not category:
            return error_response(
                422,
                "UNPROCESSABLE",
                "target_type, target_id, category are required",
            )
        forward_gfs = body.get("forward_gfs")
        forward_flag = True if forward_gfs is None else bool(forward_gfs)
        try:
            report, federated = await svc.create_report(
                reporter_user_id=ctx.user_id,
                target_type=target_type,
                target_id=target_id,
                category=category,
                notes=str(notes) if notes else None,
                forward_gfs=forward_flag,
            )
        except DuplicateReportError as exc:
            return error_response(409, "DUPLICATE_REPORT", str(exc))
        except ReportRateLimitedError as exc:
            return error_response(429, "REPORT_RATE_LIMIT", str(exc))
        return web.json_response(
            {
                "id": report.id,
                "status": report.status.value,
                "federated": federated,
                "forwarded_to_gfs": forward_flag,
            },
            status=201,
        )


class AdminReportCollectionView(BaseView):
    """GET /api/admin/reports?status=pending — admin-only."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(report_service_key)
        reports = await svc.list_pending(actor_username=ctx.username)
        return web.json_response([_report_dict(r) for r in reports])


class AdminReportResolveView(BaseView):
    """POST /api/admin/reports/{id}/resolve — admin-only."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(report_service_key)
        report_id = self.match("id")
        # Body is optional — `{"dismissed": true}` distinguishes dismissal
        # from resolution, but the default is "resolve".
        dismissed = False
        try:
            body = await self.request.json()
            if isinstance(body, dict):
                dismissed = bool(body.get("dismissed"))
        except Exception:
            pass
        await svc.resolve(
            report_id,
            actor_username=ctx.username,
            dismissed=dismissed,
        )
        return web.json_response(
            {"id": report_id, "status": "dismissed" if dismissed else "resolved"}
        )


def _report_dict(r) -> dict:
    return {
        "id": r.id,
        "target_type": r.target_type.value,
        "target_id": r.target_id,
        "reporter_user_id": r.reporter_user_id,
        "reporter_instance_id": r.reporter_instance_id,
        "category": r.category.value,
        "notes": r.notes,
        "status": r.status.value,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "resolved_by": r.resolved_by,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
    }
