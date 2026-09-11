"""Admin diagnostic bundle — ``GET /api/admin/diagnostics``.

A single download an operator can attach to a bug report, so remote
debugging doesn't depend on them pasting log fragments and guessing which
parts matter. Reading one real production log to answer "why am I not
federated any more?" needed: the peer table with reachability timestamps,
the outbox backlog per peer, the effective federation base URL, the ICE
server shape, and the build version. All of that is assembled here.

**Nothing secret goes in.** The bundle is built from an explicit
allow-list of fields, never by serialising rows and subtracting
:data:`~socialhome.security.SENSITIVE_FIELDS` — with a deny-list, a
column added later leaks by default; with an allow-list it is simply
absent until somebody deliberately adds it. On top of that the assembled
payload is passed through :func:`~socialhome.security.sanitise_for_api`
as a belt-and-braces second pass, and a test asserts the serialised
bundle contains no key from that frozenset.

Deliberately excluded, beyond the obvious keys and tokens:

* **User content and names.** Counts only — a bundle that carries display
  names or post bodies is one an operator cannot safely share.
* **Home coordinates.** Even 4dp-truncated, they locate a household.
* **TURN credentials.** The ICE section reuses the same redaction as
  ``/api/admin/federation/ice-servers`` (urls + a has-credentials flag).
* **Inbox URLs of peers.** These embed a per-pair secret path segment;
  the host is what matters for debugging, so only that is kept.
"""

from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

from aiohttp import web

from .._version import __version__
from ..app_keys import (
    config_key,
    db_key,
    federation_repo_key,
    federation_service_key,
    federation_transport_key,
    platform_adapter_key,
)
from ..domain.federation_capabilities import OURS
from ..security import error_response, sanitise_for_api
from .admin_federation import _redact_ice_server
from .base import BaseView


def _host_only(url: str | None) -> str | None:
    """Reduce a peer inbox URL to scheme+host.

    The path carries a per-pair secret inbox id — the thing an attacker
    needs in order to post to that peer — so it never goes in a bundle
    that is meant to be shareable. The host is what actually matters when
    debugging ("is this a Nabu Casa Remote UI address? does it resolve?").
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


class AdminDiagnosticsView(BaseView):
    """``GET /api/admin/diagnostics`` (admin-only).

    Returns the bundle as JSON. ``?download=1`` adds a
    ``Content-Disposition`` header so the browser saves it as a file with
    a timestamped name, which is what the Federation page's button uses.
    """

    async def get(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")

        now = datetime.now(timezone.utc)
        config = self.svc(config_key)
        db = self.svc(db_key)

        bundle: dict = {
            "generated_at": now.isoformat(),
            "schema": 1,
            "build": {
                "version": __version__,
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "proto_version": OURS,
            },
            "deployment": {
                "mode": config.mode,
                # Whether a federation base is resolvable at all is the
                # first thing to check — pairing hard-fails without one.
                "federation_base_configured": False,
                "federation_base_host": None,
            },
        }

        try:
            adapter = self.svc(platform_adapter_key)
            base = await adapter.get_federation_base()
            bundle["deployment"]["federation_base_configured"] = bool(base)
            bundle["deployment"]["federation_base_host"] = _host_only(base)
        except Exception as exc:  # pragma: no cover — never fail the bundle
            bundle["deployment"]["error"] = type(exc).__name__

        # ── Peers ────────────────────────────────────────────────────
        # The reachability timestamps are the point: "not connected" is
        # unanswerable from a status chip alone.
        peers: list[dict] = []
        try:
            for inst in await self.svc(federation_repo_key).list_instances():
                status = getattr(inst.status, "value", inst.status)
                peers.append(
                    {
                        "instance_id": inst.id,
                        "status": status,
                        "source": getattr(inst.source, "value", inst.source),
                        "proto_version": getattr(inst, "proto_version", None),
                        "paired_at": getattr(inst, "paired_at", None),
                        "last_reachable_at": getattr(inst, "last_reachable_at", None),
                        "unreachable_since": getattr(inst, "unreachable_since", None),
                        "capabilities_seen_at": getattr(
                            inst, "capabilities_seen_at", None
                        ),
                        "relay_via": getattr(inst, "relay_via", None),
                        # Host only — the path is a per-pair secret.
                        "inbox_host": _host_only(
                            getattr(inst, "remote_inbox_url", None)
                        ),
                        # No display_name: it is user-authored text, and a
                        # bundle carrying household names is one an
                        # operator cannot freely attach to an issue.
                    }
                )
        except Exception as exc:  # pragma: no cover
            bundle["peers_error"] = type(exc).__name__
        bundle["peers"] = peers

        # ── Outbox backlog ───────────────────────────────────────────
        # A stuck backlog is invisible in the UI and was the whole story
        # in the log that prompted this: 54 envelopes cycling the retry
        # ladder while the household believed it was federated.
        try:
            rows = await db.fetchall(
                "SELECT instance_id, status, count(*) AS n,"
                " min(created_at) AS oldest, max(attempts) AS max_attempts"
                " FROM federation_outbox GROUP BY instance_id, status",
            )
            bundle["outbox"] = [
                {
                    "instance_id": r["instance_id"],
                    "status": r["status"],
                    "count": r["n"],
                    "oldest_created_at": r["oldest"],
                    "max_attempts": r["max_attempts"],
                }
                for r in rows
            ]
        except Exception as exc:  # pragma: no cover
            bundle["outbox_error"] = type(exc).__name__

        # ── WebRTC ───────────────────────────────────────────────────
        try:
            fed = self.svc(federation_service_key)
            raw = list(getattr(fed, "_ice_servers", []) or [])  # noqa: SLF001
            servers = [_redact_ice_server(s) for s in raw if isinstance(s, dict)]
            bundle["webrtc"] = {
                "ice_servers": servers,
                "has_turn": any(
                    u.startswith(("turn:", "turns:"))
                    for s in servers
                    for u in s["urls"]
                ),
                "turn_usable": any(
                    s["has_credentials"]
                    and any(u.startswith(("turn:", "turns:")) for u in s["urls"])
                    for s in servers
                ),
            }
        except Exception as exc:  # pragma: no cover
            bundle["webrtc_error"] = type(exc).__name__

        try:
            transport = self.request.app.get(federation_transport_key)
            if transport is not None:
                bundle["webrtc"]["rtc_ready_peers"] = sum(
                    1 for p in peers if transport.is_ready(p["instance_id"])
                )
        except Exception:  # pragma: no cover
            pass

        # ── Schema ───────────────────────────────────────────────────
        try:
            row = await db.fetchone("SELECT max(version) AS v FROM schema_version")
            bundle["database"] = {"migration_version": row["v"] if row else None}
        except Exception as exc:  # pragma: no cover
            bundle["database_error"] = type(exc).__name__

        # Belt-and-braces: the bundle is allow-listed above, so this is a
        # second line of defence rather than the mechanism.
        safe = sanitise_for_api(bundle)

        resp = self._json(safe)
        if self.request.query.get("download"):
            stamp = now.strftime("%Y%m%dT%H%M%SZ")
            resp.headers["Content-Disposition"] = (
                f'attachment; filename="socialhome-diagnostics-{stamp}.json"'
            )
        return resp
