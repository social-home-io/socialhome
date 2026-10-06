"""Cluster sync + health routes (``/cluster/*``, spec §24.10)."""

from __future__ import annotations

import logging

import orjson

from aiohttp import web

from .. import app_keys as K
from ..admin_service import verify_report_signature
from ..cluster import (
    NODE_HEARTBEAT,
    NODE_HELLO,
    NODE_PARTITION_CATCHUP,
    NODE_PARTITION_GAP,
    NODE_POLICY_PUSH,
    NODE_RELAY,
    NODE_SYNC_CLIENT,
    NODE_SYNC_REPORT,
    NODE_SYNC_SPACE,
    UnsupportedClusterSigSuite,
    authorize_frame,
    parse_cluster_sig_suite,
    verify_node_signature,
)
from ..public import SlidingWindowCounter
from .base import GfsBaseView

log = logging.getLogger(__name__)

#: Marks a frame without a ``to`` field (an older sender) — distinct from an
#: explicit ``"to": null``, which is malformed.
_NO_RECIPIENT = object()


class ClusterHealthView(GfsBaseView):
    """``GET /cluster/health`` — public node + peer status."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_cluster_key)
        return web.json_response(await svc.health())


def _rate_limited() -> web.Response:
    resp = web.json_response({"error": "rate_limited"}, status=429)
    resp.headers["Retry-After"] = "60"
    return resp


class ClusterSyncView(GfsBaseView):
    """``POST /cluster/sync`` — NODE_* dispatch with signature + rate limit.

    Body is the raw canonical JSON ``{type, from, to, ts, nonce, sig_suite,
    payload}`` (an older sender omits ``to``, ``nonce`` + ``sig_suite``); the
    ``X-Node-Signature`` header carries the signature over those exact
    bytes. The sender id is the signed ``from`` — there is no header
    fallback.

    Membership (spec §24.10): a node is a member if and only if its frames
    verify under a key this GFS already holds — its own identity key (the
    shared seed) or a key pinned on the node's ``cluster_nodes`` row by an
    operator. See :func:`~socialhome.global_server.cluster.authorize_frame`.

    Order matters (spec §24.10.4):

    1. Note whether the source address has spent its unverified budget
       ("shed"). A shed address's frames still go through the cheap checks
       below, but every rejection is answered 429.
    2. Not a JSON object → 400 ``invalid_json``; ``type`` / ``from`` /
       ``payload`` / ``to`` missing or mistyped → 400 ``invalid_message``.
    3. Unknown ``sig_suite`` → 400 ``unsupported_sig_suite``.
    4. ``ts`` not an int → 400 ``invalid_timestamp``; more than ±300 s off
       our wall clock, or before this process started → 401
       ``stale_timestamp``; ``to`` present and not our node id → 409
       ``wrong_recipient`` (a frame for another node, replayed to us).
    5. Already-accepted bytes → 409 ``replay`` (in-memory, no crypto).
    6. Membership → 403 ``unknown_node`` (non-HELLO, no row),
       ``unapproved_node`` (HELLO under a key we don't hold; nothing is
       written) or ``key_mismatch`` (HELLO for a known node under a key
       other than its pin; WARNING).
    7. Signature under the key step 6 chose → 401 ``invalid_signature``.
       From a shed address the verify runs only while the named node's
       failed-verify budget lasts (else 429, no verify), and a failure
       spends it.
    8. The verified node's budget → 429.
    9. Record the frame digest, then dispatch.

    Every failure in 2–7 spends the source address's budget, never a
    node's verified budget: forged or replayed traffic naming a real peer
    cannot lock it out, and junk from a member's own address cannot either —
    a frame that verifies under the member's pin is accepted even from a
    shed address. Verify CPU on forgeries stays bounded: per address until
    it is shed, then per approved node
    (:data:`~socialhome.global_server.cluster.CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN`).
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_cluster_key)
        client_ip = self.client_ip()
        # A shed address is not dropped outright: a member syncing from an
        # address someone filled with junk must still get through. Its
        # frames take the cheap checks; anything that does not verify as an
        # approved node is answered 429.
        shed = svc.sync_source_exhausted(client_ip)

        def _reject(response: web.Response) -> web.Response:
            svc.charge_unverified_sync(client_ip)
            return _rate_limited() if shed else response

        raw = await self.request.read()
        try:
            body = orjson.loads(raw)
        except orjson.JSONDecodeError:
            body = None
        if not isinstance(body, dict):
            return _reject(
                web.json_response({"error": "invalid_json"}, status=400),
            )

        # The sender id comes from the SIGNED body only — never from an
        # unsigned header a forger fully controls.
        from_node = body.get("from")
        msg_type = body.get("type")
        payload = body.get("payload") or {}
        # ``to`` (the recipient's node id) is optional — a sender older
        # than the field, or a HELLO to a configured URL, omits it — but
        # when present it must be a string.
        to = body.get("to", _NO_RECIPIENT)
        if (
            not isinstance(from_node, str)
            or not from_node
            or not isinstance(msg_type, str)
            or not msg_type
            or not isinstance(payload, dict)
            or (to is not _NO_RECIPIENT and not isinstance(to, str))
        ):
            return _reject(
                web.json_response({"error": "invalid_message"}, status=400),
            )

        # The signature suite — unknown is refused, never defaulted (a
        # missing field is an older sender and means Ed25519).
        try:
            parse_cluster_sig_suite(body.get("sig_suite"))
        except UnsupportedClusterSigSuite:
            return _reject(
                web.json_response({"error": "unsupported_sig_suite"}, status=400),
            )

        # Freshness of the signed ``ts`` — cheap, so before any key lookup
        # or signature work.
        ts_error = svc.frame_ts_error(body.get("ts"))
        if ts_error:
            return _reject(
                web.json_response(
                    {"error": ts_error},
                    status=400 if ts_error == "invalid_timestamp" else 401,
                ),
            )

        # Recipient binding: a frame signed for another node is a replay
        # across the cluster. Refused before any key lookup or signature
        # work, and charged to the address like any other replay.
        if to is not _NO_RECIPIENT and to != svc.node_id:
            return _reject(
                web.json_response({"error": "wrong_recipient"}, status=409),
            )

        # Replay: these exact signed bytes were already accepted. In-memory
        # and crypto-free, so before any key lookup or verify. Charged to
        # the address — a replay proves nothing about who sent it.
        if svc.frame_seen(raw):
            return _reject(web.json_response({"error": "replay"}, status=409))

        # Membership (spec §24.10): which key, if any, this frame must
        # verify under — our own identity key (shared seed) or the key
        # pinned on the sender's row. Decided before any signature work, so
        # an outsider cannot make us burn verify CPU; nothing is written for
        # a refused sender.
        cluster_repo = self.svc(K.gfs_cluster_repo_key)
        nodes = await cluster_repo.list_nodes()
        pinned = next((n for n in nodes if n.node_id == from_node), None)
        carried = payload.get("public_key") if msg_type == NODE_HELLO else ""
        verdict = authorize_frame(
            msg_type=msg_type,
            from_node=from_node,
            carried_key=carried if isinstance(carried, str) else "",
            pinned=pinned,
            own_key=svc.own_public_key_hex,
        )
        if verdict.error:
            if verdict.error == "key_mismatch" and not shed:
                log.warning(
                    "cluster: key_mismatch — NODE_HELLO for known node %r from "
                    "%s carries a key other than its pin; refused. If the node "
                    "really rotated its key, remove the peer and re-add it "
                    "with the new key.",
                    from_node,
                    client_ip,
                )
            return _reject(
                web.json_response({"error": verdict.error}, status=403),
            )

        # From a shed address, verify only while the named node's
        # failed-verify budget lasts: that bounds the CPU forgeries can burn
        # without ever spending the node's own (verified) budget.
        if shed and svc.failed_verify_exhausted(from_node):
            return _rate_limited()
        signature = self.request.headers.get("X-Node-Signature", "")
        if not verify_node_signature(raw, signature, verdict.verify_key):
            svc.charge_unverified_sync(client_ip)
            if shed:
                svc.charge_failed_verify(from_node)
            return web.json_response({"error": "invalid_signature"}, status=401)

        # The frame verified under a key we already held, so it is the
        # member's own traffic: spend that node's budget.
        if not svc.charge_verified_sync(from_node):
            return _rate_limited()
        svc.record_frame(raw)

        # Dispatch by message type.
        if msg_type == NODE_HELLO:
            await svc.handle_hello(
                from_node_id=from_node,
                url=str(payload.get("url") or ""),
                public_key_hex=verdict.verify_key,
            )
        elif msg_type == NODE_HEARTBEAT:
            await svc.handle_heartbeat(from_node, payload)
        elif msg_type == NODE_SYNC_CLIENT:
            await svc.apply_sync_client(
                action=str(payload.get("action") or "upsert"),
                client_instance=payload.get("client_instance") or {},
            )
        elif msg_type == NODE_SYNC_SPACE:
            await svc.apply_sync_space(
                action=str(payload.get("action") or "upsert"),
                global_space=payload.get("global_space") or {},
            )
        elif msg_type == NODE_SYNC_REPORT:
            await svc.apply_sync_report(payload.get("report") or {})
        elif msg_type == NODE_POLICY_PUSH:
            await svc.apply_policy_push(payload)
        elif msg_type == NODE_RELAY:
            await svc.apply_relay(
                str(payload.get("space_id") or ""),
                payload.get("envelope") or {},
            )
        elif msg_type == NODE_PARTITION_CATCHUP:
            session = self.request.app.get(K.gfs_http_session_key)
            await svc.apply_partition_catchup(
                from_node,
                payload.get("last_relay_ts") or {},
                session=session,
            )
        elif msg_type == NODE_PARTITION_GAP:
            await svc.apply_partition_gap(payload)
        else:
            log.debug(
                "cluster: unknown NODE_* type %s from %s",
                msg_type,
                from_node,
            )
        return web.json_response({"status": "ok"})


# ─── Sync-signaling round-robin (spec §24.10.7) ──────────────────────────


_SIGNALING_LIMIT_PER_MIN: int = 60
#: Per-instance window for ``/cluster/signaling-session*`` calls, keyed by
#: the paired client instance_id whose signature already verified. The
#: shared capped-LRU counter, so idle keys don't accumulate.
_SIGNALING_LIMITER = SlidingWindowCounter(_SIGNALING_LIMIT_PER_MIN)


async def _verify_caller(view: GfsBaseView, body: dict) -> tuple[str, dict]:
    """Authenticate a paired client instance via Ed25519 over canonical body.

    Returns ``(instance_id, body_without_signature)``. Raises HTTP errors
    on missing fields, unknown / banned instance, or invalid signature.

    Used by both signaling-session views — keeps the verification path
    identical to ``/gfs/report`` so we don't ship two flavours of GFS
    inbound auth.
    """
    fed_repo = view.svc(K.gfs_fed_repo_key)
    instance_id = str(body.get("from_instance") or "")
    if not instance_id:
        raise web.HTTPBadRequest(reason="Missing 'from_instance'")
    inst = await fed_repo.get_instance(instance_id)
    if inst is None or inst.status == "banned":
        raise web.HTTPForbidden(reason="forbidden")
    sig = str(body.pop("signature", ""))
    if not verify_report_signature(body, sig, inst.public_key):
        raise web.HTTPUnauthorized(reason="invalid_signature")
    return instance_id, body


def _check_signaling_rate(instance_id: str) -> None:
    if not _SIGNALING_LIMITER.allow(instance_id):
        raise web.HTTPTooManyRequests(reason="rate_limited")


class ClusterSignalingBeginView(GfsBaseView):
    """``POST /cluster/signaling-session`` — pick a signaling node.

    **Legacy — older households only.** Current households never call
    this: the identified body told the GFS which household began a direct
    space sync and when, and the returned URL was never used (ICE rides
    the household-to-household path). It stays so an older household's
    sync keeps working; see ``tests/protocol/test_gfs_no_sync_signaling.py``.

    Body: ``{from_instance, sync_id, signature}``. Returns
    ``{signaling_node, session_id}`` where ``signaling_node`` is the URL
    chosen by :meth:`ClusterService.pick_signaling_node`. Single-node
    deployments return ``{signaling_node: null}`` so the SH provider
    omits the field from ``SPACE_SYNC_OFFER`` (spec §24.10.7
    "Non-cluster GFS"). Returns ``503 {reason: "node_capacity"}`` when
    every active peer is at :data:`MAX_SIGNALING_SESSIONS`.

    The picked node's local sync count is incremented before the response
    so the next caller's selector sees the new load. Provider must call
    ``/release`` on ``SPACE_SYNC_DIRECT_READY`` /
    ``SPACE_SYNC_DIRECT_FAILED`` to decrement.
    """

    async def post(self) -> web.Response:
        body = await self.body_or_400()
        instance_id, signed_body = await _verify_caller(self, body)
        sync_id = str(signed_body.get("sync_id") or "")
        if not sync_id:
            raise web.HTTPBadRequest(reason="Missing 'sync_id'")
        _check_signaling_rate(instance_id)

        cluster = self.svc(K.gfs_cluster_key)
        chosen_url = await cluster.pick_signaling_node()
        # Distinguish single-node (None and not enabled) from cap-hit
        # (None and enabled).
        if chosen_url is None:
            cluster_repo = self.svc(K.gfs_cluster_repo_key)
            nodes = await cluster_repo.list_nodes()
            online_peers = [n for n in nodes if n.status != "offline"]
            if online_peers:
                # Cluster mode + every peer at cap → S-8 capacity reject.
                return web.json_response(
                    {"reason": "node_capacity"},
                    status=503,
                )
            # Single-node — caller should omit ``signaling_node``.
            return web.json_response(
                {"signaling_node": None, "session_id": sync_id},
            )

        # Map URL back to node_id so we can bump the right counter.
        cluster_repo = self.svc(K.gfs_cluster_repo_key)
        chosen_node_id = await _node_id_for_url(cluster_repo, chosen_url)
        await cluster.note_signaling_started(chosen_node_id)
        return web.json_response(
            {"signaling_node": chosen_url, "session_id": sync_id},
        )


class ClusterSignalingEndView(GfsBaseView):
    """``POST /cluster/signaling-session/release`` — decrement load.

    **Legacy — older households only** (see :class:`ClusterSignalingBeginView`).

    Body: ``{from_instance, sync_id, signaling_node, signature}``.
    Idempotent: duplicate releases (e.g. both ``DIRECT_READY`` and
    ``DIRECT_FAILED``) floor at zero rather than going negative. Unknown
    ``signaling_node`` URLs are accepted silently to keep the
    SH-provider's release path simple.
    """

    async def post(self) -> web.Response:
        body = await self.body_or_400()
        instance_id, signed_body = await _verify_caller(self, body)
        signaling_node = str(signed_body.get("signaling_node") or "")
        if not signaling_node:
            raise web.HTTPBadRequest(reason="Missing 'signaling_node'")
        _check_signaling_rate(instance_id)

        cluster = self.svc(K.gfs_cluster_key)
        cluster_repo = self.svc(K.gfs_cluster_repo_key)
        node_id = await _node_id_for_url(cluster_repo, signaling_node)
        if node_id:
            await cluster.note_signaling_ended(node_id)
        return web.json_response({"status": "released"})


async def _node_id_for_url(cluster_repo, url: str) -> str:
    """Resolve a cluster-node URL back to its node_id, or empty string."""
    if not url:
        return ""
    nodes = await cluster_repo.list_nodes()
    for n in nodes:
        if n.url == url:
            return n.node_id
    return ""
