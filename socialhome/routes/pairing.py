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

import logging
from typing import Literal

from aiohttp import web

from ..app_keys import (
    auto_pair_coordinator_key,
    auto_pair_inbox_key,
    dm_routing_service_key,
    federation_repo_key,
    federation_service_key,
    federation_transport_key,
    pairing_relay_queue_key,
    peer_home_sharing_service_key,
    peer_user_visibility_repo_key,
    platform_adapter_key,
    user_repo_key,
)
from ..domain.federation import FederationEventType, PairingStatus
from ..security import error_response
from ..services.peer_home_sharing_service import UnknownInstanceError
from .base import BaseView

log = logging.getLogger(__name__)


def _instance_dict(
    inst, *, transport_state: Literal["rtc", "https"] | None = None
) -> dict:
    """Public-shape view of a :class:`RemoteInstance`.

    Omits the KEK-encrypted session keys (``key_self_to_remote`` /
    ``key_remote_to_self``) and the ``routing_secret`` — those are stored
    fields, never exposed over HTTP (§27.9 SENSITIVE_FIELDS).

    ``transport_state`` is the current federation transport for this
    peer: ``"rtc"`` (WebRTC DataChannel open), ``"https"`` (HTTPS
    inbox fallback), or ``None`` (unreachable / pending — caller
    short-circuits when reachability is False or status is not
    confirmed).
    """
    status = (
        inst.status.value if isinstance(inst.status, PairingStatus) else inst.status
    )
    reachable = inst.is_reachable() if hasattr(inst, "is_reachable") else True
    local_alias = getattr(inst, "local_alias", None)
    effective = (
        inst.effective_display_name
        if hasattr(inst, "effective_display_name")
        else ((local_alias or "").strip() or inst.display_name)
    )
    return {
        "instance_id": inst.id,
        # ``display_name`` carries the *effective* name — alias if the
        # admin set one, else the peer's federated display_name. Most
        # SPA call sites only need the rendered string; they get the
        # post-alias value for free without checking ``local_alias``.
        "display_name": effective,
        # The raw federated display_name is preserved so the Manage
        # modal can show "Peer advertises: <federated_name>" alongside
        # the editable alias input.
        "federated_display_name": inst.display_name,
        "local_alias": local_alias,
        # Per-peer home-location sharing toggle (§share_home).  Default True.
        # Listed next to local_alias because both are per-pair local toggles.
        "share_home": getattr(inst, "share_home", True),
        "status": status,
        "reachable": reachable,
        "transport": transport_state,
        "paired_at": getattr(inst, "paired_at", None),
        # Reachability *timestamps*, not just the ``reachable`` boolean.
        # "Not connected" is the single most common federation support
        # question, and the boolean alone can't distinguish "dropped a
        # minute ago" from "hasn't worked for three weeks" — which is the
        # difference between waiting and re-pairing. Both are plain UTC
        # timestamps already on the row; the SPA renders them locally.
        "last_reachable_at": getattr(inst, "last_reachable_at", None),
        "unreachable_since": getattr(inst, "unreachable_since", None),
        "source": (inst.source.value if hasattr(inst.source, "value") else inst.source),
        # Monotonic protocol version the peer last advertised via
        # INSTANCE_CAPABILITIES_UPDATED. Useful for an admin "is this
        # peer behind?" panel and for the federation-demo harness to
        # assert that the announcement round-tripped after pairing.
        "proto_version": getattr(inst, "proto_version", 1),
        # Household coordinates are 4dp-truncated at the schema level
        # (§25). Safe to expose: they're per-household, not per-user, and
        # already shipped to/from peers via the pairing exchange.
        "home_lat": getattr(inst, "home_lat", None),
        "home_lon": getattr(inst, "home_lon", None),
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
        adapter = self.svc(platform_adapter_key)
        own_base = await adapter.get_federation_base()
        result = await self.svc(federation_service_key).accept_pairing(
            body,
            own_inbox_base_url=own_base,
        )
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
        transport = self.request.app.get(federation_transport_key)
        rows = []
        for inst in instances:
            ts: Literal["rtc", "https"] | None = None
            if inst.status is PairingStatus.CONFIRMED and inst.is_reachable():
                if transport is not None and transport.is_ready(inst.id):
                    ts = "rtc"
                else:
                    ts = "https"
            rows.append(_instance_dict(inst, transport_state=ts))
        return web.json_response(rows)


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
        adapter = self.svc(platform_adapter_key)
        own_base = await adapter.get_federation_base()
        if not own_base:
            return error_response(
                422,
                "NOT_CONFIGURED",
                (
                    "Set external URL in Home Assistant (Settings ▸ System ▸ "
                    "Network / Nabu Casa) or config.toml [standalone].external_url "
                    "before auto-pairing."
                ),
            )
        coord = self.svc(auto_pair_coordinator_key)
        try:
            result = await coord.request_via(
                via_instance_id=via,
                target_instance_id=target,
                target_display_name=display_name,
                own_inbox_base_url=own_base,
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
        adapter = self.svc(platform_adapter_key)
        own_base = await adapter.get_federation_base()
        if not own_base:
            return error_response(
                422,
                "NOT_CONFIGURED",
                (
                    "Set external URL in Home Assistant (Settings ▸ System ▸ "
                    "Network / Nabu Casa) or config.toml [standalone].external_url "
                    "before approving auto-pair requests."
                ),
            )
        coord = self.svc(auto_pair_coordinator_key)
        try:
            inst = await coord.finalize_pending(
                request_id,
                own_inbox_base_url=own_base,
            )
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
    """``PATCH /api/pairing/connections/{instance_id}`` — update per-peer toggles.
    ``DELETE /api/pairing/connections/{instance_id}`` — unpair.

    PATCH accepts one or both of:
    - ``{"share_home": bool}`` — enable / disable home-location sharing with
      this peer.  Handled by :class:`PeerHomeSharingService`.

    Both fields are optional and may be combined in a single request.
    Admin-only.
    """

    async def patch(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        instance_id = self.match("instance_id")
        body = await self.body()

        if "share_home" in body:
            value = body["share_home"]
            if not isinstance(value, bool):
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    "share_home must be a boolean.",
                )
            try:
                await self.svc(peer_home_sharing_service_key).set_share_home(
                    instance_id,
                    value=value,
                    set_by=user.user_id,
                )
            except UnknownInstanceError:
                return error_response(404, "NOT_FOUND", "Peer not found.")

        # Re-read so the response reflects the persisted state.
        inst = await self.svc(federation_repo_key).get_instance(instance_id)
        if inst is None:
            return error_response(404, "NOT_FOUND", "Peer not found.")
        return web.json_response(_instance_dict(inst))

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


class PairingConnectionVisibleUsersView(BaseView):
    """``GET / PATCH /api/pairing/connections/{instance_id}/visible-users``.

    Per-pair allow / deny list of which local users surface to a paired
    peer. Default-visible: a household member is visible unless an admin
    has explicitly hidden them. Admin-only.

    On PATCH the handler also fans the appropriate envelope to the peer
    so their ``remote_users`` mirror tracks the change immediately —
    ``USER_REMOVED`` when a row flips to hidden, ``USER_UPDATED`` when
    one flips back to visible.
    """

    async def get(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        instance_id = self.match("instance_id")
        fed_repo = self.svc(federation_repo_key)
        inst = await fed_repo.get_instance(instance_id)
        if inst is None or inst.status is not PairingStatus.CONFIRMED:
            return error_response(404, "NOT_FOUND", "Confirmed peer not found.")

        users_repo = self.svc(user_repo_key)
        vis_repo = self.svc(peer_user_visibility_repo_key)
        hidden = await vis_repo.hidden_user_ids_for_peer(instance_id)
        local_users = await users_repo.list_active()
        return web.json_response(
            {
                "users": [
                    {
                        "user_id": u.user_id,
                        "username": u.username,
                        "display_name": u.display_name,
                        "is_admin": bool(getattr(u, "is_admin", False)),
                        "visible": u.user_id not in hidden,
                    }
                    for u in local_users
                ]
            }
        )

    async def patch(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        instance_id = self.match("instance_id")
        fed_repo = self.svc(federation_repo_key)
        inst = await fed_repo.get_instance(instance_id)
        if inst is None or inst.status is not PairingStatus.CONFIRMED:
            return error_response(404, "NOT_FOUND", "Confirmed peer not found.")

        body = await self.body()
        updates = body.get("updates")
        if not isinstance(updates, list) or not updates:
            return error_response(
                422,
                "UNPROCESSABLE",
                "updates must be a non-empty list of {user_id, visible} entries.",
            )

        users_repo = self.svc(user_repo_key)
        local_users = await users_repo.list_active()
        local_by_id = {u.user_id: u for u in local_users}

        normalized: list[tuple[str, bool]] = []
        for entry in updates:
            if not isinstance(entry, dict):
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    "Each update must be a {user_id, visible} object.",
                )
            uid = entry.get("user_id")
            vis = entry.get("visible")
            if not isinstance(uid, str) or not isinstance(vis, bool):
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    "Each update needs string user_id + boolean visible.",
                )
            if uid not in local_by_id:
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    f"Unknown local user_id: {uid!r}.",
                )
            normalized.append((uid, vis))

        vis_repo = self.svc(peer_user_visibility_repo_key)
        prev_hidden = await vis_repo.hidden_user_ids_for_peer(instance_id)
        federation = self.svc(federation_service_key)

        for uid, vis in normalized:
            await vis_repo.set_visibility(
                instance_id=instance_id,
                user_id=uid,
                visible=vis,
                set_by=user.user_id,
            )
            was_hidden = uid in prev_hidden
            if vis and was_hidden:
                # Re-publish the user's profile so the peer's mirror
                # repopulates the row that ``USER_REMOVED`` previously
                # dropped. Use the current users_repo state — display
                # name / bio may have changed while the user was hidden.
                u = local_by_id[uid]
                try:
                    await federation.send_event(
                        to_instance_id=instance_id,
                        event_type=FederationEventType.USER_UPDATED,
                        payload={
                            "user_id": u.user_id,
                            "username": u.username,
                            "display_name": u.display_name,
                            "bio": getattr(u, "bio", None),
                            "picture_hash": getattr(u, "picture_hash", None),
                        },
                    )
                except Exception:  # pragma: no cover — defensive
                    log.exception(
                        "visible-users: USER_UPDATED republish to %s failed",
                        instance_id,
                    )
            elif not vis and not was_hidden:
                try:
                    await federation.send_event(
                        to_instance_id=instance_id,
                        event_type=FederationEventType.USER_REMOVED,
                        payload={"user_id": uid},
                    )
                except Exception:  # pragma: no cover — defensive
                    log.exception(
                        "visible-users: USER_REMOVED to %s failed",
                        instance_id,
                    )

        # Re-read to return the post-update state — uniform shape with GET.
        hidden = await vis_repo.hidden_user_ids_for_peer(instance_id)
        return web.json_response(
            {
                "users": [
                    {
                        "user_id": u.user_id,
                        "username": u.username,
                        "display_name": u.display_name,
                        "is_admin": bool(getattr(u, "is_admin", False)),
                        "visible": u.user_id not in hidden,
                    }
                    for u in local_users
                ]
            }
        )


_ALIAS_MAX_LEN = 80


class PairingConnectionAliasView(BaseView):
    """``PATCH /api/pairing/connections/{instance_id}/alias`` — set or
    clear the local alias for a paired peer.

    The peer's ``display_name`` comes from the federation handshake;
    when it's empty / cryptic (e.g. the truncated instance_id) the
    admin is stuck. This local alias overrides the rendered name in
    every view that surfaces the connection — Friends dashboard,
    Connections list, DM picker — without renaming the peer remotely
    or federating the choice. Pure local UX state.

    Body: ``{"alias": "Brother's house"}`` to set,
    ``{"alias": null}`` (or empty / whitespace-only string) to clear.
    Admin-only.
    """

    async def patch(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        instance_id = self.match("instance_id")
        fed_repo = self.svc(federation_repo_key)
        inst = await fed_repo.get_instance(instance_id)
        if inst is None:
            return error_response(404, "NOT_FOUND", "Peer not found.")

        body = await self.body()
        raw = body.get("alias")
        if raw is None:
            alias: str | None = None
        elif isinstance(raw, str):
            cleaned = raw.strip()
            alias = cleaned if cleaned else None
        else:
            return error_response(
                422,
                "UNPROCESSABLE",
                "alias must be a string or null.",
            )
        if alias is not None and len(alias) > _ALIAS_MAX_LEN:
            return error_response(
                422,
                "UNPROCESSABLE",
                f"alias must be {_ALIAS_MAX_LEN} characters or fewer.",
            )

        await fed_repo.update_alias(instance_id, alias)
        # Re-read so the response reflects the persisted state.
        updated = await fed_repo.get_instance(instance_id)
        assert updated is not None  # we just upserted it
        return web.json_response(
            {
                "instance_id": updated.id,
                "display_name": updated.display_name,
                "local_alias": updated.local_alias,
                "effective_display_name": updated.effective_display_name,
            }
        )


class PairingConnectionTransportDetailView(BaseView):
    """``GET /api/pairing/connections/{instance_id}/transport-detail``.

    Admin-only side panel data for the SPA's Manage detail view.
    Returns ``{"last_relay": {"via": <iid>, "ts": <iso>} | null}`` —
    the most recent DM that relayed via a third household within
    the last 24 hours. Used to surface "Last DM took the relay path"
    on the connection detail panel.
    """

    async def get(self) -> web.Response:
        user = self.user
        if not user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        instance_id = self.match("instance_id")
        svc = self.svc(dm_routing_service_key)
        entry = await svc.last_relay_for(instance_id)
        return web.json_response(
            {
                "last_relay": (
                    {"via": entry.via, "ts": entry.ts} if entry is not None else None
                ),
            },
        )
