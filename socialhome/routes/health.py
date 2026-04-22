"""Health-check route — GET /healthz.

Returns 200 with subsystem status when everything is healthy, 503 when
a subsystem fails. Public — no auth required (listed in
``_DEFAULT_PUBLIC_PATHS``). Used by the HA Supervisor + container
orchestrators for liveness probes.

Subsystem checks:

* ``db``         — runs ``SELECT 1`` against the SQLite handle.
* ``ws``         — reports the connected WebSocket-client count.
* ``outbox``     — reports the federation outbox depth (informational
                   only — a deep outbox doesn't fail health, but the
                   number is useful for dashboards).
"""

from __future__ import annotations

import logging

from aiohttp import web

from .. import app_keys as K
from .base import BaseView

log = logging.getLogger(__name__)


class HealthView(BaseView):
    """``GET /healthz`` — public liveness probe (no auth)."""

    async def get(self) -> web.Response:
        body: dict = {"status": "ok", "subsystems": {}}
        overall_ok = True

        # ── DB probe ────────────────────────────────────────────────────
        db = self.request.app.get(K.db_key)
        if db is not None:
            try:
                row = await db.fetchone("SELECT 1 AS one")
                body["subsystems"]["db"] = (
                    "ok" if row and row["one"] == 1 else "degraded"
                )
            except Exception as exc:  # pragma: no cover
                log.warning("healthz: db probe failed: %s", exc)
                body["subsystems"]["db"] = "fail"
                overall_ok = False
        else:
            body["subsystems"]["db"] = "uninitialised"

        # ── WS-manager probe ────────────────────────────────────────────
        ws_mgr = self.request.app.get(K.ws_manager_key)
        if ws_mgr is not None:
            try:
                count = ws_mgr.connection_count()
            except AttributeError:
                count = None
            body["subsystems"]["ws_clients"] = (
                int(count) if isinstance(count, int) else "ok"
            )
        else:
            body["subsystems"]["ws_clients"] = "uninitialised"

        # ── Outbox depth (informational) ────────────────────────────────
        outbox = self.request.app.get(K.outbox_repo_key)
        if outbox is not None:
            try:
                depth = await outbox.queue_depth()
                body["subsystems"]["outbox_depth"] = int(depth)
            except AttributeError:
                body["subsystems"]["outbox_depth"] = "ok"
            except Exception as exc:  # pragma: no cover
                log.debug("healthz: outbox probe failed: %s", exc)
                body["subsystems"]["outbox_depth"] = "unknown"

        if not overall_ok:
            body["status"] = "fail"
            return web.json_response(body, status=503)
        return web.json_response(body)
