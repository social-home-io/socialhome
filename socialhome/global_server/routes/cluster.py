"""Cluster sync + health routes (``/cluster/*``, spec §24.10)."""

from __future__ import annotations

import json
import logging
import time

from aiohttp import web

from .. import app_keys as K
from ..admin_service import verify_report_signature
from ..cluster import (
    CLUSTER_RATE_LIMIT_PER_MIN,
    NODE_HEARTBEAT,
    NODE_HELLO,
    NODE_PARTITION_CATCHUP,
    NODE_PARTITION_GAP,
    NODE_POLICY_PUSH,
    NODE_RELAY,
    NODE_SYNC_CLIENT,
    NODE_SYNC_REPORT,
    NODE_SYNC_SPACE,
    verify_node_signature,
)
from .base import GfsBaseView

log = logging.getLogger(__name__)

#: Per-node inbound message window for NODE_* (spec §24.10.4).
_CLUSTER_SYNC_HITS: dict[str, list[float]] = {}


class ClusterHealthView(GfsBaseView):
    """``GET /cluster/health`` — public node + peer status."""

    async def get(self) -> web.Response:
        svc = self.svc(K.gfs_cluster_key)
        return web.json_response(await svc.health())


class ClusterSyncView(GfsBaseView):
    """``POST /cluster/sync`` — NODE_* dispatch with signature + rate limit.

    Body is the raw canonical JSON ``{type, from, ts, payload}``; the
    ``X-Node-Signature`` header carries the Ed25519 signature. Unknown
    peers → 403 without verifying the sig (CPU-burn DoS guard).
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_cluster_key)
        raw = await self.request.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise web.HTTPBadRequest(reason="Invalid JSON body") from exc

        from_node = str(
            body.get("from") or self.request.headers.get("X-Node-Id") or "",
        )
        msg_type = str(body.get("type") or "")
        payload = body.get("payload") or {}
        if not from_node or not msg_type:
            raise web.HTTPBadRequest(reason="Missing 'type' or 'from'")

        # Rate-limit per node (60 msgs / minute).
        now = time.monotonic()
        hits = [t for t in _CLUSTER_SYNC_HITS.get(from_node, []) if now - t < 60.0]
        if len(hits) >= CLUSTER_RATE_LIMIT_PER_MIN:
            return web.json_response({"error": "rate_limited"}, status=429)
        hits.append(now)
        _CLUSTER_SYNC_HITS[from_node] = hits

        # Look up the peer's public key. NODE_HELLO is special-cased: the
        # sender isn't yet in the DB, so we trust the payload (TOFU).
        cluster_repo = self.svc(K.gfs_cluster_repo_key)
        if msg_type == NODE_HELLO:
            pk_hex = str(payload.get("public_key") or "")
        else:
            nodes = await cluster_repo.list_nodes()
            match = next((n for n in nodes if n.node_id == from_node), None)
            if match is None:
                return web.json_response(
                    {"error": "unknown_node"},
                    status=403,
                )
            pk_hex = match.public_key

        signature = self.request.headers.get("X-Node-Signature", "")
        if not verify_node_signature(raw, signature, pk_hex):
            return web.json_response(
                {"error": "invalid_signature"},
                status=401,
            )

        # Dispatch by message type.
        if msg_type == NODE_HELLO:
            await svc.handle_hello(
                from_node_id=from_node,
                url=str(payload.get("url") or ""),
                public_key_hex=pk_hex,
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


#: Per-instance window for ``/cluster/signaling-session*`` calls.
#: Same shape as ``_CLUSTER_SYNC_HITS`` but keyed by paired client
#: instance_id (not cluster node_id).
_SIGNALING_HITS: dict[str, list[float]] = {}
_SIGNALING_LIMIT_PER_MIN: int = 60


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
    now = time.monotonic()
    hits = [t for t in _SIGNALING_HITS.get(instance_id, []) if now - t < 60.0]
    if len(hits) >= _SIGNALING_LIMIT_PER_MIN:
        raise web.HTTPTooManyRequests(reason="rate_limited")
    hits.append(now)
    _SIGNALING_HITS[instance_id] = hits


class ClusterSignalingBeginView(GfsBaseView):
    """``POST /cluster/signaling-session`` — pick a signaling node.

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
