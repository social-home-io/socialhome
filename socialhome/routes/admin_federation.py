"""Admin route for the federation-compatibility panel.

Lists confirmed federation peers with the protocol version each advertises,
the features it lacks versus this build's :data:`OURS`, its last-reachable
timestamp, and whether it has ever advertised capabilities at all
(``capabilities_known`` — a NULL stamp distinguishes a genuine v1 peer from
one that's paired but still mid-first-handshake).

Routes:

* ``GET /api/admin/federation/compat``   (admin-only)
* ``POST /api/admin/federation/resync``  (admin-only) — ask a peer to
  re-broadcast state for a named scope (§319.6).
* ``GET / PUT /api/admin/federation/external-url`` (admin-only) — the
  admin-set federation inbox base URL. Until this existed, the only
  ways to supply it were ``socialhome.toml`` (operator-owned, not
  writable from the UI) or the Home Assistant integration — while the
  pairing error told admins to "set this Social Home\'s external URL in
  Settings", a field that did not exist anywhere in the SPA.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..app_keys import (
    config_key,
    db_key,
    federation_repo_key,
    federation_service_key,
    platform_adapter_key,
    url_update_outbound_key,
)
from ..federation.peer_url import InvalidPeerUrlError, validate_peer_url
from ..platform.federation_base import (
    INBOX_PATH,
    MANUAL_BASE_KEY,
    read_manual_base,
)
from ..domain.federation import FederationEventType, PairingStatus
from ..domain.federation_capabilities import (
    OURS,
    FederationCapability,
    features_missing_below,
)
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)


class AdminFederationCompatView(BaseView):
    async def get(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        repo = self.svc(federation_repo_key)
        peers = await repo.list_instances(status=PairingStatus.CONFIRMED.value)
        return self._json(
            {
                "ours": OURS,
                "peers": [
                    {
                        "instance_id": p.id,
                        "display_name": p.effective_display_name,
                        "proto_version": p.proto_version,
                        "status": p.status.value,
                        "last_reachable_at": p.last_reachable_at,
                        "capabilities_known": p.capabilities_seen_at is not None,
                        "lacking_features": features_missing_below(p.proto_version),
                    }
                    for p in peers
                ],
            }
        )


def _valid_scope(scope: str) -> bool:
    """A resync scope is ``capabilities`` or ``space:<id>`` /
    ``calendar:<id>`` with a non-empty id."""
    if scope == "capabilities":
        return True
    for prefix in ("space:", "calendar:"):
        if scope.startswith(prefix):
            return bool(scope[len(prefix) :])
    return False


class AdminFederationResyncView(BaseView):
    """``POST /api/admin/federation/resync`` — ask a peer to re-broadcast.

    Sends :data:`FederationEventType.INSTANCE_RESYNC_REQUEST` to a
    confirmed peer for a named scope (``capabilities`` / ``space:<id>`` /
    ``calendar:<id>``). Gated on the peer advertising
    :data:`FederationCapability.MIN_FOR_INSTANCE_RESYNC` (v_19) — an older
    peer has no handler, so we 409 rather than fire into the void.
    """

    async def post(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        instance_id = str(body.get("instance_id") or "")
        scope = str(body.get("scope") or "")
        if not instance_id or not _valid_scope(scope):
            return error_response(
                400,
                "UNPROCESSABLE",
                "instance_id is required and scope must be 'capabilities', "
                "'space:<id>', or 'calendar:<id>'.",
            )
        fed = self.svc(federation_service_key)
        if not await fed.peer_supports(
            instance_id,
            min_version=FederationCapability.MIN_FOR_INSTANCE_RESYNC,
        ):
            return error_response(
                409,
                "PEER_TOO_OLD",
                "That peer is on an older protocol version and can't honor "
                "a resync request yet.",
            )
        await fed.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.INSTANCE_RESYNC_REQUEST,
            payload={"scope": scope},
        )
        return self._json({"status": "ok", "instance_id": instance_id, "scope": scope})


def _validate_base(raw: str) -> str | None:
    """Normalize + validate an admin-entered base URL.

    Returns the cleaned value, or ``None`` when it isn't a usable
    http(s) base. Same rules the HA integration's push endpoint applies,
    so both sources of this value are held to one standard. A trailing
    inbox path is tolerated and stripped, because pasting the full URL
    from a peer's pairing QR is the obvious mistake to make.
    """
    base = raw.strip().rstrip("/")
    if not base:
        return None
    if not (base.startswith("http://") or base.startswith("https://")):
        return None
    if base.endswith(INBOX_PATH):
        base = base[: -len(INBOX_PATH)].rstrip("/")
    # Same household-address rules peers apply when they receive this base
    # (host required, no credentials, no whitespace) — covers a bare
    # scheme ("https://") left over after stripping, too.
    try:
        validate_peer_url(base, field="base")
    except InvalidPeerUrlError:
        return None
    return base


def _redact_ice_server(srv: dict) -> dict:
    """Strip the secret half of an ICE-server entry for display.

    ``credential`` is a real secret — under the recommended coturn
    TURN-REST setup it is an HMAC of the shared ``webrtc_turn_secret``, so
    leaking it hands out relay access. ``username`` is only an
    ``expiry:user_id`` pair, but it is per-credential noise that tells an
    operator nothing useful, so it is reduced to a boolean too.

    What survives is what an operator actually needs in order to answer
    "is TURN in play, and is it credentialled?": the URLs and a flag.
    """
    urls = [str(u) for u in (srv.get("urls") or []) if isinstance(u, str)]
    return {
        "urls": urls,
        "kinds": sorted({u.split(":", 1)[0].lower() for u in urls if ":" in u}),
        "has_credentials": bool(srv.get("username") and srv.get("credential")),
    }


class AdminFederationIceServersView(BaseView):
    """``GET /api/admin/federation/ice-servers`` (admin-only).

    Read-only overview of the ICE servers the **federation transport** is
    currently using, with secrets removed (:func:`_redact_ice_server`).

    Purely diagnostic. Whether RTC can traverse a given network is the
    single most opaque thing about a federation deployment — a missing or
    credential-less TURN entry degrades silently to slow HTTPS, with the
    only evidence a warning in a log the operator may never read. This
    surfaces the same facts the boot diagnostics warn about.

    ``source`` reports where the list came from, because under Home
    Assistant a pulled list replaces the config-derived one and an
    operator who set ``webrtc_turn_url`` in TOML deserves to know it is
    not what federation ended up with.
    """

    async def get(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        fed = self.svc(federation_service_key)
        raw = list(getattr(fed, "_ice_servers", []) or [])  # noqa: SLF001
        servers = [_redact_ice_server(s) for s in raw if isinstance(s, dict)]
        has_turn = any(
            u.startswith(("turn:", "turns:")) for s in servers for u in s["urls"]
        )
        turn_usable = any(
            s["has_credentials"]
            and any(u.startswith(("turn:", "turns:")) for u in s["urls"])
            for s in servers
        )
        config = self.svc(config_key)
        return self._json(
            {
                "servers": servers,
                # Mirrors the boot-time diagnostics so the UI can show the
                # same conclusion without re-deriving it.
                "has_turn": has_turn,
                "turn_usable": turn_usable,
                # ``ha``/``haos`` pull from HA Core and replace this list;
                # standalone never does.
                "pulls_from_home_assistant": config.mode in ("ha", "haos"),
            }
        )


class AdminFederationExternalUrlView(BaseView):
    """``GET / PUT /api/admin/federation/external-url`` (admin-only).

    The externally-reachable base peers POST federation envelopes to.
    ``PUT {"base": null}`` (or an empty string) clears it and hands
    control back to the deployment's automatic source — ``socialhome.toml``
    under standalone, the Home Assistant integration under ha/haos.

    GET reports the stored value alongside what the adapter would
    actually resolve, so an admin can tell "I typed something" apart from
    "it is in effect" — the two differ whenever an automatic source is
    also present.
    """

    async def get(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        db = self.svc(db_key)
        manual = await read_manual_base(db)
        effective: str | None = None
        try:
            effective = await self.svc(platform_adapter_key).get_federation_base()
        except Exception:  # pragma: no cover — a read must not 500
            effective = None
        return self._json(
            {
                "base": manual,
                "effective": effective,
                # Which source the resolved value came from, so the UI can
                # say so instead of leaving the admin to guess.
                "source": ("manual" if manual else ("auto" if effective else None)),
            }
        )

    async def put(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        raw = body.get("base")
        db = self.svc(db_key)
        previous = await read_manual_base(db)

        if raw is None or not str(raw).strip():
            cleaned: str | None = None
        else:
            cleaned = _validate_base(str(raw))
            if cleaned is None:
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    "base must be an http(s) URL, e.g. https://home.example.com",
                )

        if cleaned is None:
            await db.enqueue(
                "DELETE FROM instance_config WHERE key=?",
                (MANUAL_BASE_KEY,),
            )
        else:
            await db.enqueue(
                "INSERT INTO instance_config(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (MANUAL_BASE_KEY, cleaned),
            )

        changed = previous != cleaned
        notified = 0
        if changed:
            # Peers cache our inbox URL on their side; without this they
            # keep POSTing to the old address until they happen to re-pair.
            resolved: str | None = None
            try:
                resolved = await self.svc(
                    platform_adapter_key,
                ).get_federation_base()
            except Exception:  # pragma: no cover — defensive
                resolved = None
            if resolved:
                outbound = self.svc(url_update_outbound_key)
                try:
                    notified = await outbound.publish(
                        new_inbox_base_url=resolved,
                    )
                except Exception:  # pragma: no cover — defensive
                    log.exception(
                        "admin_federation: URL_UPDATED fan-out failed",
                    )

        return self._json(
            {
                "ok": True,
                "base": cleaned,
                "changed": changed,
                "peers_notified": notified,
            }
        )
