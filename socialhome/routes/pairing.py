"""Pairing + connections HTTP routes (§11, §23.71).

The pairing flow:

1. ``POST /api/pairing/initiate`` — generate a QR payload for the peer to scan.
2. ``POST /api/pairing/accept``   — scan-side: consume the QR payload and
   return a six-digit verification code the admins compare out of band.
3. ``POST /api/pairing/confirm``  — admins match codes -> instance becomes
   ``CONFIRMED``.

Paired-instance management:

* ``GET /api/pairing/connections`` — list every remote instance (any status).
* ``DELETE /api/pairing/connections/{instance_id}`` — unpair.

``GET /api/connections`` is a read-only alias for the list endpoint that the
frontend NetworkMap component consumes.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    auto_pair_coordinator_key,
    auto_pair_inbox_key,
    federation_repo_key,
    federation_service_key,
    pairing_relay_queue_key,
    platform_adapter_key,
)
from ..domain.federation import FederationEventType, PairingStatus
from ..security import error_response
from .base import BaseView


def _instance_dict(inst) -> dict:
    """Public-shape view of a :class:`RemoteInstance`.

    Omits the KEK-encrypted session keys (``key_self_to_remote`` /
    ``key_remote_to_self``) and the ``routing_secret`` — those are stored
    fields, never exposed over HTTP (§27.9 SENSITIVE_FIELDS).
    """
    status = (
        inst.status.value if isinstance(inst.status, PairingStatus) else inst.status
    )
    reachable = inst.is_reachable() if hasattr(inst, "is_reachable") else True
    return {
        "instance_id": inst.id,
        "display_name": inst.display_name,
        "status": status,
        "reachable": reachable,
        "paired_at": getattr(inst, "paired_at", None),
        "source": (inst.source.value if hasattr(inst.source, "value") else inst.source),
    }


class PairingInitiateView(BaseView):
    """``POST /api/pairing/initiate`` — generate QR pairing payload.

    The inbox URL is sourced from the platform adapter, not from the
    request body. In standalone mode this comes from
    ``[standalone].external_url``; in HA mode it's the value the HA
    integration has pushed into ``instance_config`` (Nabu Casa Remote
    UI or admin-set ``external_url``).

    Returns 422 ``NOT_CONFIGURED`` if the adapter has no base to offer —
    admin must set the URL before they can issue a QR.
    """

    async def post(self) -> web.Response:
        self.user  # auth check
        adapter = self.svc(platform_adapter_key)
        base = await adapter.get_federation_base()
        if not base:
            return error_response(
                422,
                "NOT_CONFIGURED",
                (
                    "Set external URL in Home Assistant (Settings ▸ System ▸ "
                    "Network / Nabu Casa) or config.toml [standalone].external_url "
                    "before pairing."
                ),
            )
        qr = await self.svc(federation_service_key).initiate_pairing(base)
        return web.json_response(qr, status=201)


class PairingAcceptView(BaseView):
    """``POST /api/pairing/accept`` — consume QR payload (public endpoint)."""

    async def post(self) -> web.Response:
        # /api/pairing/accept is on the auth middleware's public-path list
        # (it IS the auth/handshake entry point) — no current_user check.
        body = await self.body()
        result = await self.svc(federation_service_key).accept_pairing(body)
        return web.json_response(result)


class PairingConfirmView(BaseView):
    """``POST /api/pairing/confirm`` — finalise pairing with verification code."""

    async def post(self) -> web.Response:
        self.user  # auth check
        body = await self.body()
        token = str(body.get("token") or "")
        code = str(body.get("verification_code") or "")
        if not token or not code:
            return error_response(
                422,
                "UNPROCESSABLE",
                "token and verification_code are required.",
            )
        instance = await self.svc(federation_service_key).confirm_pairing(token, code)
        return web.json_response(_instance_dict(instance))


class PairingIntroduceView(BaseView):
    """``POST /api/pairing/introduce`` (§11.9 friend-of-friend).

    Body: ``{"target_instance_id": str, "via_instance_id": str,
             "message"?: str}``. Sends a ``PAIRING_INTRO_RELAY``
    event to the intermediary (``via_instance_id``), which notifies
    its admin and — if accepted — forwards the intro to the target.
    """

    async def post(self) -> web.Response:
        self.user  # auth check
        body = await self.body()
        target = str(body.get("target_instance_id") or "")
        via = str(body.get("via_instance_id") or "")
        if not target or not via:
            return error_response(
                422,
                "UNPROCESSABLE",
                "target_instance_id and via_instance_id are required.",
            )
        if target == via:
            return error_response(
                422,
                "UNPROCESSABLE",
                "target_instance_id and via_instance_id must differ.",
            )
        fed_repo = self.svc(federation_repo_key)
        if await fed_repo.get_instance(via) is None:
            return error_response(
                404,
                "NOT_FOUND",
                f"Relay peer {via!r} not found.",
            )
        message = str(body.get("message") or "")[:500]
        result = await self.svc(federation_service_key).send_event(
            to_instance_id=via,
            event_type=FederationEventType.PAIRING_INTRO_RELAY,
            payload={
                "target_instance_id": target,
                "message": message,
            },
        )
        if not result.ok:
            return error_response(
                502,
                "UPSTREAM_UNREACHABLE",
                "Could not reach the relay instance — retry later.",
            )
        return web.Response(status=204)


class PairingConnectionCollectionView(BaseView):
    """``GET /api/pairing/connections`` — list paired instances.

    Also mounted on ``GET /api/connections`` for the NetworkMap frontend.
    """

    async def get(self) -> web.Response:
        self.user  # auth check
        instances = await self.svc(federation_repo_key).list_instances()
        return web.json_response([_instance_dict(i) for i in instances])


class AutoPairViaView(BaseView):
    """``POST /api/pairing/auto-pair-via`` — transitive auto-pair via
    a trusted paired peer (§11 "simple pairing").

    Body: ``{via_instance_id, target_instance_id, target_display_name?}``.
    The routing through the vouching peer happens without admin
    approval on either side of the relay; the target household's
    admin still reviews + approves the incoming request (one click,
    no QR scan) before the pair is established.
    """

    async def post(self) -> web.Response:
        self.user  # auth check
        body = await self.body()
        via = str(body.get("via_instance_id") or "")
        target = str(body.get("target_instance_id") or "")
        display_name = str(body.get("target_display_name") or "")
        if not via or not target:
            return error_response(
                422,
                "UNPROCESSABLE",
                "via_instance_id and target_instance_id are required.",
            )
        coord = self.svc(auto_pair_coordinator_key)
        try:
            result = await coord.request_via(
                via_instance_id=via,
                target_instance_id=target,
                target_display_name=display_name,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(result, status=202)


class AutoPairInboxCollectionView(BaseView):
    """``GET /api/pairing/auto-pair-requests`` — admin inbox of
    incoming transitive auto-pair requests awaiting approval.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        inbox = self.svc(auto_pair_inbox_key)
        return web.json_response(
            [
                {
                    "request_id": r.request_id,
                    "from_a_id": r.from_a_id,
                    "from_a_display": r.from_a_display,
                    "via_b_id": r.via_b_id,
                    "via_b_display": r.via_b_display,
                    "ts": r.ts,
                    "received_at": r.received_at,
                }
                for r in inbox.list_pending()
            ]
        )


class AutoPairInboxApproveView(BaseView):
    """``POST /api/pairing/auto-pair-requests/{request_id}/approve``."""

    async def post(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        request_id = self.match("request_id")
        coord = self.svc(auto_pair_coordinator_key)
        try:
            inst = await coord.finalize_pending(request_id)
        except KeyError:
            return error_response(
                404,
                "NOT_FOUND",
                "Request not found (already handled?).",
            )
        return web.json_response(
            {
                "ok": True,
                "instance_id": inst.id,
                "display_name": inst.display_name,
            }
        )


class AutoPairInboxDeclineView(BaseView):
    """``POST /api/pairing/auto-pair-requests/{request_id}/decline``."""

    async def post(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        request_id = self.match("request_id")
        coord = self.svc(auto_pair_coordinator_key)
        body = await self.body()
        try:
            await coord.decline_pending(
                request_id,
                reason=str(body.get("reason") or ""),
            )
        except KeyError:
            return error_response(
                404,
                "NOT_FOUND",
                "Request not found (already handled?).",
            )
        return web.json_response({"ok": True})


class PairingConnectionDetailView(BaseView):
    """``DELETE /api/pairing/connections/{instance_id}`` — unpair."""

    async def delete(self) -> web.Response:
        self.user  # auth check
        instance_id = self.match("instance_id")
        repo = self.svc(federation_repo_key)
        inst = await repo.get_instance(instance_id)
        if inst is None:
            return error_response(404, "NOT_FOUND", "Instance not found.")
        await repo.delete_instance(instance_id)
        return web.json_response({"ok": True})


def _relay_request_dict(req) -> dict:
    return {
        "id": req.id,
        "from_instance": req.from_instance,
        "target_instance_id": req.target_instance_id,
        "message": req.message,
        "received_at": req.received_at.isoformat(),
    }


class PairingRelayRequestCollectionView(BaseView):
    """``GET /api/pairing/relay-requests`` — list pending intro relays (§11.9).

    Admin-only. A paired peer asks us to introduce them to a third
    instance; the request sits here until the admin acts on it.
    """

    async def get(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        queue = self.svc(pairing_relay_queue_key)
        return web.json_response(
            [_relay_request_dict(r) for r in await queue.list_pending()],
        )


class PairingRelayApproveView(BaseView):
    """``POST /api/pairing/relay-requests/{id}/approve`` — forward the intro."""

    async def post(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        request_id = self.match("id")
        queue = self.svc(pairing_relay_queue_key)
        pending = await queue.approve(request_id)
        if pending is None:
            return error_response(404, "NOT_FOUND", "Relay request not found.")
        return web.json_response(_relay_request_dict(pending))


class PairingRelayDeclineView(BaseView):
    """``POST /api/pairing/relay-requests/{id}/decline`` — drop the request."""

    async def post(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        request_id = self.match("id")
        queue = self.svc(pairing_relay_queue_key)
        dropped = await queue.decline(request_id)
        if dropped is None:
            return error_response(404, "NOT_FOUND", "Relay request not found.")
        return web.Response(status=204)
