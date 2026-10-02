"""Routes for user-filed content reports.

* ``POST /api/reports`` — any member files one (space-scoped when the
  target lives in a space, or ``space_id`` names one for a member report).
* ``GET /api/admin/reports`` / ``POST /api/admin/reports/{id}/resolve`` —
  household admins triage HOUSEHOLD-LEVEL reports only.
* ``GET /api/spaces/{id}/reports`` / ``POST
  /api/spaces/{id}/reports/{report_id}/resolve`` — the space's content
  authority (owner / admin / moderator) triages that space's reports.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import report_service_key
from ..domain.report import (
    DuplicateReportError,
    ReportRateLimitedError,
)
from ..services.report_service import MAX_NOTES
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
        space_id = body.get("space_id")
        if space_id is not None and not isinstance(space_id, str):
            return error_response(422, "UNPROCESSABLE", "space_id must be a string")
        if notes is not None and not isinstance(notes, str):
            return error_response(422, "UNPROCESSABLE", "notes must be a string")
        if notes and len(notes.strip()) > MAX_NOTES:
            return error_response(
                422, "UNPROCESSABLE", f"notes are limited to {MAX_NOTES} characters"
            )
        try:
            report, federated = await svc.create_report(
                reporter_user_id=ctx.user_id,
                target_type=target_type,
                target_id=target_id,
                category=category,
                notes=notes or None,
                forward_gfs=False,
                space_id=space_id or None,
            )
        except DuplicateReportError as exc:
            return error_response(409, "DUPLICATE_REPORT", str(exc))
        except ReportRateLimitedError as exc:
            return error_response(429, "REPORT_RATE_LIMIT", str(exc))
        # What actually goes to a GFS — not merely what was asked for.
        forwarded = await svc.forward_to_gfs(report) if forward_flag else False
        return web.json_response(
            {
                "id": report.id,
                "status": report.status.value,
                "federated": federated,
                "forwarded_to_gfs": forwarded,
                # Set when the space's moderators (not the household
                # admins) triage the report.
                "space_id": report.space_id,
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


class SpaceReportCollectionView(BaseView):
    """``GET /api/spaces/{id}/reports`` — the space's pending reports, for
    its owner / admins / moderators. Anyone else (a plain member, a
    household admin without a seat) gets 403; an unknown space 404."""

    async def get(self) -> web.Response:
        svc = self.svc(report_service_key)
        space_id = self.match("id")
        views = await svc.review_space(space_id, actor_user_id=self.user.user_id)
        names = await svc.display_names(
            space_id,
            {v.report.reporter_user_id for v in views if not v.anonymous}
            | {
                v.report.target_id
                for v in views
                if v.report.target_type.value == "user"
            },
        )
        out = []
        for v in views:
            r = v.report
            row = _report_dict(r)
            if v.anonymous:
                # The reviewer is the report's subject (sole authority):
                # nothing that could name the reporter — who, or their
                # own words.
                row["reporter_user_id"] = None
                row["reporter_instance_id"] = None
                row["reporter_name"] = None
                row["notes"] = None
            else:
                row["reporter_name"] = names.get(r.reporter_user_id)
            row["anonymous"] = v.anonymous
            row["dismiss_only"] = v.dismiss_only
            # ``target_gone``: the item was deleted / moved (or the member left).
            row["target_gone"] = v.gone
            row["target_preview"] = v.preview
            if r.target_type.value == "user":
                row["target_name"] = names.get(r.target_id)
            out.append(row)
        return web.json_response(out)


class SpaceReportResolveView(BaseView):
    """``POST /api/spaces/{id}/reports/{report_id}/resolve`` ``{dismissed?}``
    — content authority only; a report of another space is 404."""

    async def post(self) -> web.Response:
        body = await self.body() if self.request.can_read_body else {}
        dismissed = bool(body.get("dismissed")) if isinstance(body, dict) else False
        report_id = self.match("report_id")
        await self.svc(report_service_key).resolve_in_space(
            self.match("id"),
            report_id,
            actor_user_id=self.user.user_id,
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
        "space_id": r.space_id,
    }
