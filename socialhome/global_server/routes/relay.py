"""Relay + public GFS wire routes (``/gfs/*`` + ``/healthz``)."""

from __future__ import annotations

import logging
from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from ..admin_service import verify_report_signature
from .base import GfsBaseView

log = logging.getLogger(__name__)


class RegisterView(GfsBaseView):
    """``POST /gfs/register`` — register or update a client instance."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        admin_repo = self.svc(K.gfs_admin_repo_key)
        body = await self.body_or_400()
        try:
            instance_id = body["instance_id"]
            public_key = body["public_key"]
            inbox_url = body["inbox_url"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        display_name = str(body.get("display_name") or "")
        auto_accept = (await admin_repo.get_config("auto_accept_clients")) == "1"
        await svc.register_instance(
            instance_id,
            public_key,
            inbox_url,
            display_name=display_name,
            auto_accept=auto_accept,
        )
        return web.json_response(
            {
                "status": "registered" if auto_accept else "pending",
                "instance_id": instance_id,
            }
        )


class PublishView(GfsBaseView):
    """``POST /gfs/publish`` — relay an event to a space's subscribers."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        session = self.request.app.get(K.gfs_http_session_key)
        body = await self.body_or_400()
        try:
            space_id = body["space_id"]
            event_type = body["event_type"]
            payload = body["payload"]
            from_instance = body["from_instance"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        signature = body.get("signature", "")
        delivered = await svc.publish_event(
            space_id,
            event_type,
            payload,
            from_instance,
            signature,
            session=session,
        )
        return web.json_response(
            {"status": "published", "delivered_to": delivered},
        )


class SubscribeView(GfsBaseView):
    """``POST /gfs/subscribe`` — subscribe or unsubscribe an instance."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        body = await self.body_or_400()
        try:
            instance_id = body["instance_id"]
            space_id = body["space_id"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        action = body.get("action", "subscribe")
        if action == "unsubscribe":
            await svc.unsubscribe(instance_id, space_id)
            return web.json_response({"status": "unsubscribed"})
        await svc.subscribe(instance_id, space_id)
        return web.json_response({"status": "subscribed"})


class SpacesListView(GfsBaseView):
    """``GET /gfs/spaces`` — list active global spaces for discovery."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        spaces = await svc.list_spaces(status="active")
        return web.json_response(
            {"spaces": [asdict(s) for s in spaces]},
        )


class HealthzView(GfsBaseView):
    """``GET /healthz`` — liveness probe."""

    async def get(self) -> web.Response:
        return web.json_response({"status": "ok"})


class ReportView(GfsBaseView):
    """``POST /gfs/report`` — household-admin fraud report.

    Signature-verified against the reporter's registered public_key.
    Unknown / banned reporters → 403. Duplicates (UNIQUE index on
    reporter+target) → 200 ``{"status": "duplicate"}``.
    """

    async def post(self) -> web.Response:
        admin_svc = self.svc(K.gfs_admin_service_key)
        fed_repo = self.svc(K.gfs_fed_repo_key)
        body = await self.body_or_400()
        required = {
            "target_type",
            "target_id",
            "category",
            "reporter_instance_id",
        }
        if not required.issubset(body):
            return web.json_response(
                {"error": "missing_fields", "required": sorted(required)},
                status=422,
            )
        reporter = await fed_repo.get_instance(body["reporter_instance_id"])
        if reporter is None or reporter.status == "banned":
            return web.json_response({"error": "forbidden"}, status=403)
        signature = body.pop("signature", "")
        if not verify_report_signature(body, signature, reporter.public_key):
            return web.json_response(
                {"error": "invalid_signature"},
                status=401,
            )
        was_new, auto_banned = await admin_svc.record_fraud_report(
            target_type=body["target_type"],
            target_id=body["target_id"],
            category=body["category"],
            notes=body.get("notes"),
            reporter_instance_id=body["reporter_instance_id"],
            reporter_user_id=body.get("reporter_user_id"),
            signed_body=b"",  # already verified above
            signature=signature,
        )
        return web.json_response(
            {
                "status": "recorded" if was_new else "duplicate",
                "quarantined": auto_banned,
            }
        )


class AppealView(GfsBaseView):
    """``POST /gfs/appeal`` — a banned household asks the admin to review."""

    async def post(self) -> web.Response:
        admin_svc = self.svc(K.gfs_admin_service_key)
        fed_repo = self.svc(K.gfs_fed_repo_key)
        body = await self.body_or_400()
        required = {"target_type", "target_id"}
        if not required.issubset(body):
            return web.json_response(
                {"error": "missing_fields", "required": sorted(required)},
                status=422,
            )
        sender_id = body.get("from_instance") or body.get("target_id")
        sender = await fed_repo.get_instance(str(sender_id))
        if sender is None:
            return web.json_response({"error": "forbidden"}, status=403)
        signature = body.pop("signature", "")
        if not verify_report_signature(body, signature, sender.public_key):
            return web.json_response(
                {"error": "invalid_signature"},
                status=401,
            )
        appeal = await admin_svc.record_appeal(
            target_type=str(body["target_type"]),
            target_id=str(body["target_id"]),
            message=str(body.get("message") or ""),
        )
        return web.json_response(
            {"id": appeal.id, "status": "pending"},
            status=201,
        )
