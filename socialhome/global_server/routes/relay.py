"""Relay + public GFS wire routes (``/gfs/*`` + ``/healthz``)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from ..admin_service import verify_report_signature
from ..public import PUBLISH_MAX_BODY_BYTES
from .base import GfsBaseView

log = logging.getLogger(__name__)

#: The only actions ``POST /gfs/subscribe`` accepts. Anything else is a 400 —
#: a typo'd action must never silently fall through to a subscribe.
_SUBSCRIBE_ACTIONS = frozenset({"subscribe", "unsubscribe"})

#: Upper bound on the identifier-shaped wire fields ``/gfs/publish`` binds into
#: SQL (``space_id``) or matches against the allow-set (``event_type``). Mirrors
#: the household-side ``_SAFE_SPACE_ID`` guard. Without it a JSON list/dict
#: reached the SQLite bind and produced a 500 + ERROR traceback on every
#: unauthenticated hit — a log-volume DoS.
_MAX_WIRE_ID_CHARS = 128

#: Read granularity for the bounded ``/gfs/publish`` body read. Large enough
#: that a legitimate payload is a handful of chunks, small enough that an
#: oversized body is refused within one chunk of the cap.
_BODY_CHUNK_BYTES = 64 * 1024


def _require_short_str(value: object, field: str) -> str:
    """Return *value* as a non-empty, bounded ``str`` or raise ``400``."""
    if not isinstance(value, str) or not value or len(value) > _MAX_WIRE_ID_CHARS:
        raise web.HTTPBadRequest(reason=f"Invalid field: {field}")
    return value


class GfsInfoView(GfsBaseView):
    """``GET /gfs/info`` — public GFS identity descriptor.

    Returns the GFS's instance id, Ed25519 public key, and display
    metadata so an HFS client that scanned the pairing QR (which only
    carries ``{base_url, token}``) can fetch the public key it needs to
    pin before sending its registration. Unauthenticated by design —
    the public key is, well, public.

    Also the capability channel for the GFS↔HFS leg, which has no
    ``proto_version`` negotiation: ``anonymous_publish`` tells a household
    that ``POST /gfs/publish`` authorizes on the space-authority signature
    alone, so it can stop sending ``from_instance``.

    That capability ships SIGNED (``capabilities`` + ``capabilities_sig`` +
    ``capabilities_sig_suite``, see :mod:`socialhome.capabilities_sig`) with the same
    identity key this response publishes as ``public_key`` and every paired
    household pinned at pair time. The signature is what a household trusts —
    an unauthenticated flag on an unauthenticated endpoint could be stripped
    on-path, forcing every relay back to the identified legacy body, which is
    precisely the third-party-provable artefact the anonymous relay exists to
    avoid. The top-level ``anonymous_publish`` stays for readability and for
    older households, but it is INFORMATIONAL only.
    """

    async def get(self) -> web.Response:
        cfg = self.svc(K.gfs_config_key)
        cluster = self.svc(K.gfs_cluster_key)
        admin_repo = self.svc(K.gfs_admin_repo_key)
        server_name = (await admin_repo.get_config("server_name")) or cfg.server_name
        # ``envelope_relay``: this GFS carries ``POST /gfs/envelope``, the
        # opaque household-to-household relay the §D2b invite bootstrap needs.
        # It ships inside the SIGNED block for the same reason
        # ``anonymous_publish`` does — an on-path stripper must not be able to
        # push a household back onto a path that reveals more.
        capabilities = {"anonymous_publish": True, "envelope_relay": True}
        sig, suite = cluster.sign_capabilities_block(cfg.instance_id, capabilities)
        body = {
            "gfs_instance_id": cfg.instance_id,
            "public_key": cluster.own_public_key_hex,
            "server_name": server_name,
            "base_url": cfg.base_url,
            "anonymous_publish": True,
            "capabilities": capabilities,
        }
        if sig:
            body["capabilities_sig"] = sig
            body["capabilities_sig_suite"] = suite
        else:
            log.warning(
                "GET /gfs/info: no cluster signing key wired — serving an "
                "UNSIGNED capability block; paired households will keep "
                "sending the identified (legacy) relay body",
            )
        return web.json_response(body)


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
    """``POST /gfs/publish`` — relay an event to a space's subscribers
    **without the relaying household's identity**.

    Canonical body: ``{space_id, event_type, payload}``. Authorization is the
    space-authority signature inside the opaque ``payload`` alone (verified
    against the space's TOFU-pinned key), so the GFS does not REQUIRE, STORE,
    LOG or FORWARD which household relayed the event. That is the guarantee —
    not that the operator *cannot* learn it: the same household usually holds
    an authenticated ``/gfs/ws`` socket from the same IP, so network-level
    correlation (source address, timing, body size) remains available to
    whoever runs the server. See :meth:`GfsFederationService.publish_event`.

    Because the caller is anonymous, instance-level moderation
    (``client_instances.status = 'banned'``) CANNOT gate this path — a banned
    household simply omits the legacy fields. The **space**-level ban is the
    only moderation lever on the relay.

    ``from_instance`` / ``signature`` (+ ``ts``) from an older household are
    accepted but never trusted: the transport signature is still verified when
    present (a bogus legacy field is a ``403``), then discarded.

    The response reports only the NUMBER of subscribers reached. The authority
    signature carries no nonce or timestamp, so a captured relay frame stays
    valid forever and anyone who saw one can re-POST it. What bounds that is a
    content-blind replay dedupe: this node remembers the digest of each
    accepted payload for ``PUBLISH_REPLAY_TTL_S`` (5 minutes) and answers an
    identical body with ``delivered_to: 0`` and no fan-out. The cache is
    in-memory and PER NODE — a restart, or a sibling node in a cluster, forgets
    — so it bounds the burst one captured frame can drive, and SUBSCRIBER-side
    dedupe by the post id inside the payload stays the standing backstop (see
    ``_authorize_authority_relay``). Returning the roster would hand an
    anonymous replayer exactly the data ``GET /gfs/spaces/{id}/subscribers``
    gates behind a replay-guarded authority query.

    Every authorization failure returns ONE uniform ``403`` body — distinct
    messages would let an unauthenticated caller enumerate space existence,
    moderation status and pin status. The precise reason is logged at DEBUG.
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_federation_key)
        session = self.request.app.get(K.gfs_http_session_key)
        body = await self._bounded_body()
        try:
            space_id = body["space_id"]
            event_type = body["event_type"]
            payload = body["payload"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        space_id = _require_short_str(space_id, "space_id")
        event_type = _require_short_str(event_type, "event_type")
        # Legacy fields — tolerated, verified, never trusted (see the service).
        from_instance = str(body.get("from_instance") or "")
        signature = str(body.get("signature") or "")
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
            # DEBUG only, and never the legacy ``from_instance``: the caller
            # gets one uniform body so the endpoint is not an oracle.
            log.debug("GFS publish refused for space %s: %s", space_id, exc)
            return web.json_response(
                {"error": "not authorized to relay for this space"},
                status=403,
            )
        return web.json_response(
            {"status": "published", "delivered_to": len(delivered)},
        )

    async def _bounded_body(self) -> dict:
        """Read + parse the JSON body under :data:`PUBLISH_MAX_BODY_BYTES`.

        The endpoint is unauthenticated until the authority signature inside
        the payload verifies, so the bytes are bounded BEFORE they are buffered
        or parsed: a declared ``Content-Length`` over the cap is refused
        outright, and the read itself is capped so a chunked body (which
        declares no length at all) cannot exceed it either.
        """
        declared = self.request.content_length
        if declared is not None and declared > PUBLISH_MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=PUBLISH_MAX_BODY_BYTES,
                actual_size=declared,
            )
        raw = bytearray()
        # ``StreamReader.read(n)`` returns only what is buffered, so the cap is
        # enforced by accumulating chunk by chunk and bailing the moment the
        # total crosses it — the rest of the body is never buffered.
        async for chunk in self.request.content.iter_chunked(_BODY_CHUNK_BYTES):
            raw += chunk
            if len(raw) > PUBLISH_MAX_BODY_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=PUBLISH_MAX_BODY_BYTES,
                    actual_size=len(raw),
                )
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=f"Invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise web.HTTPBadRequest(reason="Invalid JSON body: expected an object")
        return parsed


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
    auth failures map to ``403``. A subscribe for a space whose stored
    ``allow_subscribers`` is false is also a ``403``: such a space is listed
    for discovery but is not publicly readable, so there is no readership to
    join. That is the OWNER's explicit opt-in, NOT ``join_mode`` — an
    ``invite_only`` space that allows subscribers seats them normally.
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
    cover_url?, min_age?, category?, join_mode?, allow_subscribers?,
    accent_color?, ts?, signature}``.
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
                # Optional on the wire (older households send none) and only
                # folded into the signed bytes when present — see
                # ``GfsFederationService.publish_space``. Absent/unknown ⇒
                # stored as the fail-closed ``invite_only``.
                join_mode=str(body.get("join_mode") or ""),
                # Likewise optional, and tri-state on purpose: ``None`` means
                # "the household sent no such key" (so it signed a body
                # without it), which is distinct from an explicit ``false``.
                # Absent ⇒ stored as the fail-closed "not publicly readable".
                allow_subscribers=(
                    bool(body["allow_subscribers"])
                    if "allow_subscribers" in body
                    else None
                ),
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
