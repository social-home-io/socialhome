"""Cluster sync + health routes (``/cluster/*``, spec §24.10)."""

from __future__ import annotations

import json
import logging
import time

from aiohttp import web

from .. import app_keys as K
from ..cluster import (
    CLUSTER_RATE_LIMIT_PER_MIN,
    NODE_HEARTBEAT,
    NODE_HELLO,
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
            await svc.handle_heartbeat(from_node)
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
        else:
            log.debug(
                "cluster: unknown NODE_* type %s from %s",
                msg_type,
                from_node,
            )
        return web.json_response({"status": "ok"})
