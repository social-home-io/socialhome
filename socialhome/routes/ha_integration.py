"""HA integration bridge routes (§7, §11).

Endpoints the companion ``socialhome`` Home Assistant integration calls
into — it runs inside Home Assistant, knows the externally-reachable
URL (admin-set `external_url` or Nabu Casa Remote UI), and pushes it
here so the addon can stamp it into new pairing QRs and notify already-
paired peers via ``URL_UPDATED``.

The integration is not a separate HACS download: under the add-on,
:class:`~socialhome.platform.haos.bootstrap.HaBootstrap` pushes a
Supervisor discovery entry on every boot so Home Assistant offers it
for setup directly.

Auth: uses the normal bearer-token path. The integration holds the
token written to ``<data_dir>/integration_token.txt`` by
:class:`~socialhome.platform.ha.bootstrap.HaBootstrap` on first boot.
Admin-only — the integration owner is always the HA owner provisioned
as an SH admin during bootstrap.

Routes registered here:

* ``PUT /api/ha/integration/federation-base`` — upsert the base URL.
  Fans out ``URL_UPDATED`` (the adapter's effective inbox base, not the
  bare pushed URL) to every paired peer if that effective base changed.
* ``GET /api/ha/integration/federation-base`` — read-only mirror so
  the integration can verify current state on re-bind.

The previous ``PUT /api/ha/integration/ice-servers`` push endpoint
was removed: SH's HA platform adapter now pulls ``web_rtc/ice_servers``
over the HA Core WebSocket directly (see
:mod:`socialhome.platform.ha.ice_servers_sync`). One initial fetch at
boot, daily refresh thereafter. Removing the push collapsed three
moving parts (integration listener, SH endpoint, instance_config
persistence) into one — and lets Nabu Casa Cloud's runtime TURN
registration land on SH without a YAML reload.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..app_keys import db_key, platform_adapter_key, url_update_outbound_key
from ..peer_url import InvalidPeerUrlError, validate_peer_url
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)


_INSTANCE_CONFIG_KEY = "ha_federation_base"


def _validate_base(raw: str) -> str | None:
    """Normalize + validate a pushed base URL. Return the cleaned URL
    or ``None`` if it fails the household-address rules peers apply to it
    (:func:`~socialhome.peer_url.validate_peer_url`).
    """
    base = raw.strip().rstrip("/")
    if not base:
        return None
    try:
        validate_peer_url(base, field="base")
    except InvalidPeerUrlError:
        return None
    return base


class HaIntegrationFederationBaseView(BaseView):
    """``GET / PUT /api/ha/integration/federation-base``.

    The HA integration POSTs here with ``{"base": "https://..."}`` after
    resolving the externally-reachable URL (Nabu Casa Remote UI or HA
    ``external_url``). We persist the value in ``instance_config``
    where :meth:`HomeAssistantAdapter.get_federation_base` reads it,
    and — when the value differs from the last seen one — fan out
    ``URL_UPDATED`` to every confirmed peer so their
    ``remote_inbox_url`` tracks the move.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        db = self.svc(db_key)
        row = await db.fetchone(
            "SELECT value FROM instance_config WHERE key=?",
            (_INSTANCE_CONFIG_KEY,),
        )
        base = str(row["value"]) if row is not None else None
        return web.json_response({"base": base})

    async def put(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        raw = str(body.get("base") or "")
        cleaned = _validate_base(raw)
        if cleaned is None:
            return error_response(
                422,
                "UNPROCESSABLE",
                "base must be a non-empty http(s) URL.",
            )

        db = self.svc(db_key)
        adapter = self.svc(platform_adapter_key)
        previous_row = await db.fetchone(
            "SELECT value FROM instance_config WHERE key=?",
            (_INSTANCE_CONFIG_KEY,),
        )
        previous = str(previous_row["value"]) if previous_row is not None else None
        # What peers actually POST to is the adapter's EFFECTIVE base — the
        # pushed URL plus the HA-hosted forwarder path
        # (``/api/socialhome/inbox``), or an admin override that wins over
        # it. Compare and publish that, never the bare pushed value: a
        # peer that learnt the bare URL would POST to HA's frontend.
        effective_before = await self._effective_base(adapter)

        await db.enqueue(
            "INSERT INTO instance_config(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_INSTANCE_CONFIG_KEY, cleaned),
        )

        effective_after = await self._effective_base(adapter)
        notified = 0
        if effective_after and effective_after != effective_before:
            outbound = self.svc(url_update_outbound_key)
            try:
                notified = await outbound.publish(new_inbox_base_url=effective_after)
            except Exception:  # pragma: no cover — defensive
                log.exception("ha_integration: URL_UPDATED fan-out failed")

        return web.json_response(
            {
                "ok": True,
                "base": cleaned,
                "changed": previous != cleaned,
                "peers_notified": notified,
            }
        )

    @staticmethod
    async def _effective_base(adapter) -> str | None:
        """The adapter's resolved federation base, ``None`` on failure.

        A failure is logged at WARNING: a silently skipped URL_UPDATED
        leaves every peer POSTing to the old address.
        """
        try:
            return await adapter.get_federation_base()
        except Exception as exc:  # noqa: BLE001 — a read must not 500 the push
            log.warning("ha_integration: could not resolve federation base: %s", exc)
            return None
