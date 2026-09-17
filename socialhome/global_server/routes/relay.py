"""Relay + public GFS wire routes (``/gfs/*`` + ``/healthz``)."""

from __future__ import annotations

import logging
from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from ..admin_service import verify_report_signature
from .base import GfsBaseView

log = logging.getLogger(__name__)

#: The only actions ``POST /gfs/subscribe`` accepts. Anything else is a 400 —
#: a typo'd action must never silently fall through to a subscribe.
_SUBSCRIBE_ACTIONS = frozenset({"subscribe", "unsubscribe"})


class GfsInfoView(GfsBaseView):
    """``GET /gfs/info`` — public GFS identity descriptor.

    Returns the GFS's instance id, Ed25519 public key, and display
    metadata so an HFS client that scanned the pairing QR (which only
    carries ``{base_url, token}``) can fetch the public key it needs to
    pin before sending its registration. Unauthenticated by design —
    the public key is, well, public.
    """

    async def get(self) -> web.Response:
        cfg = self.svc(K.gfs_config_key)
        cluster = self.svc(K.gfs_cluster_key)
        admin_repo = self.svc(K.gfs_admin_repo_key)
        server_name = (await admin_repo.get_config("server_name")) or cfg.server_name
        return web.json_response(
            {
                "gfs_instance_id": cfg.instance_id,
                "public_key": cluster.own_public_key_hex,
                "server_name": server_name,
                "base_url": cfg.base_url,
            }
        )


class RegisterView(GfsBaseView):
    """``POST /gfs/register`` — register or update a client instance.

    Body shape: ``{token, instance_id, public_key, inbox_url,
    display_name?, keywrap_public_key?, kem_suite?, keywrap_sig?}``. The
    ``token`` is the single-use pairing token from the QR
    (``PairingTokenService.consume``); the rest is the HFS's own identity.
    ``keywrap_public_key`` + ``kem_suite`` publish the household's X25519
    key-wrap pubkey (Phase 5b foundation); ``keywrap_sig`` is the household's
    self-signature over that pubkey so a remote sealer can bind it to the
    household identity end-to-end and never trust the GFS-served value — all
    omitted by older HFS, in which case that household can't be sealed-to yet.
    Requests without a valid token are rejected with ``401`` so a stale QR
    can't be replayed.
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        admin_repo = self.svc(K.gfs_admin_repo_key)
        token_svc = self.request.app["gfs_token_service"]
        body = await self.body_or_400()
        try:
            instance_id = body["instance_id"]
            public_key = body["public_key"]
            inbox_url = body["inbox_url"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        token = str(body.get("token") or "")
        if not token:
            raise web.HTTPBadRequest(reason="Missing field: token")
        if not await token_svc.consume(token):
            return web.json_response(
                {"error": "invalid_or_expired_token"},
                status=401,
            )
        display_name = str(body.get("display_name") or "")
        keywrap_public_key = str(body.get("keywrap_public_key") or "")
        kem_suite = str(body.get("kem_suite") or "")
        keywrap_sig = str(body.get("keywrap_sig") or "")
        auto_accept = (await admin_repo.get_config("auto_accept_clients")) == "1"
        await svc.register_instance(
            instance_id,
            public_key,
            inbox_url,
            display_name=display_name,
            auto_accept=auto_accept,
            keywrap_public_key=keywrap_public_key,
            kem_suite=kem_suite,
            keywrap_sig=keywrap_sig,
        )
        return web.json_response(
            {
                "status": "registered" if auto_accept else "pending",
                "instance_id": instance_id,
            }
        )


class InstanceUpdateView(GfsBaseView):
    """``POST /gfs/instance`` — a registered HFS updates its own
    ``display_name``.

    Body shape: ``{instance_id, display_name, ts, signature}``. The
    pairing-registration token is single-use, so an already-registered
    instance can't re-register to change its name; this signed update is
    the supported path. The Ed25519 signature is verified against the
    registered ``ClientInstance.public_key`` (same trust model as
    :class:`SpacePublishView` — a peer can't rename another household).
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        body = await self.body_or_400()
        try:
            instance_id = body["instance_id"]
            display_name = body["display_name"]
            ts = body["ts"]
            signature = body["signature"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        try:
            await svc.update_instance(
                str(instance_id),
                str(display_name),
                str(ts),
                str(signature),
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=422)
        return web.json_response({"status": "ok", "instance_id": instance_id})


class PublishView(GfsBaseView):
    """``POST /gfs/publish`` — relay an event to a space's subscribers.

    The Ed25519 signature is mandatory and verified against the
    ``from_instance``'s registered ``public_key``; unknown instance,
    missing / malformed / invalid signature all map to ``403``.
    """

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
        try:
            delivered = await svc.publish_event(
                space_id,
                event_type,
                payload,
                from_instance,
                signature,
                session=session,
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        return web.json_response(
            {"status": "published", "delivered_to": delivered},
        )


class SubscribeView(GfsBaseView):
    """``POST /gfs/subscribe`` — subscribe or unsubscribe an instance.

    BOTH actions are Ed25519-signed (mandatory): body
    ``{instance_id, space_id, ts, signature, action?}``, signed over the
    canonical JSON of ``{action, instance_id, space_id, ts}`` — the
    ``action`` rides inside the signed bytes (domain separation), so a
    signature for one action can never be replayed as the other — and
    verified against
    the registered ``ClientInstance.public_key`` (replay-guarded ±300 s on
    ``ts``). The signature binds the request to *instance_id* so a caller
    can only subscribe — or unsubscribe — **itself**; an unsigned
    unsubscribe would otherwise let anyone evict any household from any
    space's relay fan-out. A missing ``ts`` / ``signature`` — or an
    ``action`` outside ``{"subscribe", "unsubscribe"}`` — is a ``400``;
    auth failures map to ``403``.
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        body = await self.body_or_400()
        try:
            instance_id = body["instance_id"]
            space_id = body["space_id"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        action = str(body.get("action", "subscribe"))
        if action not in _SUBSCRIBE_ACTIONS:
            raise web.HTTPBadRequest(reason=f"Unknown action: {action}")
        try:
            ts = body["ts"]
            signature = body["signature"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        if action == "unsubscribe":
            try:
                await svc.unsubscribe(
                    str(instance_id),
                    str(space_id),
                    str(ts),
                    str(signature),
                )
            except PermissionError as exc:
                return web.json_response({"error": str(exc)}, status=403)
            return web.json_response({"status": "unsubscribed"})
        try:
            await svc.subscribe(
                str(instance_id),
                str(space_id),
                str(ts),
                str(signature),
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        return web.json_response({"status": "subscribed"})


class SpacesListView(GfsBaseView):
    """``GET /gfs/spaces`` — list active global spaces for discovery."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        spaces = await svc.list_spaces(status="active")
        return web.json_response(
            {"spaces": [asdict(s) for s in spaces]},
        )


class SpaceDetailView(GfsBaseView):
    """``GET /gfs/spaces/{space_id}`` — single space metadata.

    SH clients hit this after picking a row from
    :class:`SpacesListView` so they can mirror name / description /
    cover onto a local ``spaces`` stub before subscribing — without
    that mirror, the SH-side ``subscribe_to_space`` route would refuse
    the join (no local row to attach a member to).
    """

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        space_id = self.request.match_info["space_id"]
        space = await svc.get_space(space_id)
        # ``withdrawn`` is the owner's own retraction — hidden from discovery
        # just like a non-active status (see ``GfsFederationService.hide_space``).
        if space is None or space.status != "active" or space.withdrawn:
            raise web.HTTPNotFound(reason="Space not found or not published")
        return web.json_response(asdict(space))


class SpaceSubscribersView(GfsBaseView):
    """``GET /gfs/spaces/{space_id}/subscribers`` — release the subscriber
    list to a verified SEED-HOLDER (Phase-5b-c reconcile).

    SECURITY: the subscriber list is sensitive, so the read is gated on a
    SPACE-AUTHORITY signature proving the caller holds the space seed (the
    owner OR a delegated admin) — the SAME pinned-pubkey trust model as the
    authority relay path, no new roster/key. The caller passes
    ``?ts=&authority_sig=&authority_sig_suite=`` where the signature is over
    ``{space_id, ts}`` under ``space_subscribers_query`` and the ``ts`` is
    replay-guarded (±300 s). Any auth failure (no/forged/stale sig, unknown
    space, no pinned pubkey) maps to ``403`` — fail-closed.

    The response exposes only each subscriber's already-GFS-registered public
    material — its instance id, Ed25519 identity pubkey, and X25519 key-wrap
    pubkey + self-signature — so the seed-holder can re-seal the per-space
    content key to each (``space_subscriber_reconcile``). No inbox URL, no
    private data.
    """

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        space_id = self.request.match_info["space_id"]
        q = self.request.query
        try:
            subscribers = await svc.list_subscribers_with_keys(
                space_id,
                ts=str(q.get("ts") or ""),
                authority_sig=str(q.get("authority_sig") or ""),
                authority_sig_suite=str(q.get("authority_sig_suite") or ""),
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        return web.json_response(
            {
                "subscribers": [
                    {
                        "instance_id": s.instance_id,
                        "identity_public_key": s.identity_public_key,
                        "keywrap_public_key": s.keywrap_public_key,
                        "keywrap_sig": s.keywrap_sig,
                    }
                    for s in subscribers
                ]
            }
        )


class SpacePublishView(GfsBaseView):
    """``POST /gfs/spaces/{space_id}/publish`` — owning HFS pushes
    space metadata so this GFS can list it on ``/gfs/spaces``.

    Body: ``{owning_instance, name, description?, about_markdown?,
    cover_url?, min_age?, category?, accent_color?, ts?, signature}``.
    The Ed25519 signature is verified against the registered
    ``ClientInstance.public_key`` (so a paired-but-malicious peer
    can't masquerade as another household's space owner). ``ts``, when
    present, is inside the signed bytes and replay-guarded (±300 s); only a
    publish carrying one can restore an owner-withdrawn listing.
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        space_id = self.request.match_info["space_id"]
        body = await self.body_or_400()
        try:
            owning_instance = body["owning_instance"]
            name = body["name"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        try:
            space = await svc.publish_space(
                space_id=space_id,
                owning_instance=str(owning_instance),
                name=str(name),
                description=body.get("description"),
                about_markdown=body.get("about_markdown"),
                cover_url=body.get("cover_url"),
                icon_url=body.get("icon_url"),
                min_age=int(body.get("min_age") or 0),
                category=str(body.get("category") or "general"),
                accent_color=str(body.get("accent_color") or "#D2542A"),
                primary_color=str(body.get("primary_color") or "#D2542A"),
                identity_public_key=str(body.get("identity_public_key") or ""),
                signature=str(body.get("signature") or ""),
                ts=str(body.get("ts") or ""),
            )
        except PermissionError as exc:
            return web.json_response(
                {"error": str(exc)},
                status=403,
            )
        return web.json_response(
            {"status": space.status, "space_id": space.space_id},
        )


class SpaceUnpublishView(GfsBaseView):
    """``POST|DELETE /gfs/spaces/{space_id}/unpublish`` — the OWNING HFS
    withdraws its global-space listing.

    Body: ``{owning_instance, ts, signature}`` — the Ed25519 signature covers
    the canonical ``{action: "unpublish", owning_instance, space_id, ts}``
    JSON and is verified against the registered ``ClientInstance.public_key``,
    with the usual ±300 s replay guard. SECURITY: this endpoint used to take
    no authentication at all, so any internet caller could permanently delist
    any space. A missing body field is a ``400``; anything the service refuses
    (unknown instance, bad/stale signature, a caller that isn't the owner) is
    a ``403``.

    The row is kept and only flagged ``withdrawn`` (so the GFS admin's audit
    trail, the subscriber list and the pinned authority key survive) — the
    owner's next publish restores the listing. Both verbs are accepted since
    some HTTP clients struggle with DELETE bodies.
    """

    async def post(self) -> web.Response:
        return await self._handle()

    async def delete(self) -> web.Response:
        return await self._handle()

    async def _handle(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        space_id = self.request.match_info["space_id"]
        body = await self.body_or_400()
        try:
            owning_instance = body["owning_instance"]
            ts = body["ts"]
            signature = body["signature"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        try:
            await svc.hide_space(
                space_id,
                str(owning_instance),
                str(ts),
                str(signature),
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        return web.json_response({"status": "unpublished"})


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
