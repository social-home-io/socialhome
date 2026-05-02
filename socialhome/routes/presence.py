"""Presence routes — /api/presence/* (§23.21, §23.8.5)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    online_status_service_key,
    presence_service_key,
    user_repo_key,
)
from ..domain.presence import LocationUpdate, PresenceState
from ..security import error_response
from .base import BaseView


def _derive_state(zone_name: str | None) -> PresenceState:
    """Map the HA-sourced ``zone_name`` to a :class:`PresenceState` value.

    §7.3 / §23.8.5 — the HA integration forwards HA's aggregated person
    entity state as a zone name, then nulls it when HA reports
    ``not_home`` / ``unknown`` / ``unavailable``. The core derives the
    four-value :class:`PresenceState` from that:

    * ``"home"`` → ``"home"`` (the member is at the household's home zone).
    * Any other non-empty zone → ``"zone"`` (named away-zone like "Work").
    * ``None`` or empty → ``"away"`` (not in any tracked zone).
    """
    if not zone_name:
        return "away"
    if zone_name == "home":
        return "home"
    return "zone"


class PresenceCollectionView(BaseView):
    """``GET /api/presence`` — return household presence list.

    Each row carries both *physical* presence (``state`` / ``zone_name``
    / GPS) and *session* presence (``is_online`` / ``is_idle`` /
    ``last_seen_at``). The two signals are orthogonal and the frontend
    surfaces them independently.
    """

    async def get(self) -> web.Response:
        self.user
        entries = await self.svc(presence_service_key).list_presence()
        online_svc = self.request.app.get(online_status_service_key)
        # ``user_repo.list_by_ids`` is the source for offline users'
        # ``last_seen_at`` (online users get the live timestamp from the
        # service, which is more recent than the persisted column).
        user_repo = self.request.app.get(user_repo_key)
        persisted: dict[str, str | None] = {}
        if user_repo is not None and entries:
            users = await user_repo.list_by_ids({p.user_id for p in entries})
            persisted = {u.user_id: u.last_seen_at for u in users}
        rows: list[dict] = []
        for p in entries:
            is_online = bool(online_svc and online_svc.is_online(p.user_id))
            is_idle = bool(online_svc and online_svc.is_idle(p.user_id))
            if is_online and online_svc is not None:
                last_dt = online_svc.last_seen(p.user_id)
                last_seen = last_dt.isoformat() if last_dt is not None else None
            else:
                last_seen = persisted.get(p.user_id)
            rows.append(
                {
                    "username": p.username,
                    "user_id": p.user_id,
                    "display_name": p.display_name,
                    "state": p.state,
                    "zone_name": p.zone_name,
                    "latitude": p.latitude,
                    "longitude": p.longitude,
                    "gps_accuracy_m": p.gps_accuracy_m,
                    "is_online": is_online,
                    "is_idle": is_idle,
                    "last_seen_at": last_seen,
                }
            )
        return self._json(rows)


class PresenceLocationView(BaseView):
    """``POST /api/presence/location`` — upsert a presence row.

    Wire contract (§23.8.5): the HA integration (separate
    ``ha-integration/`` repo) subscribes to HA's ``state_changed`` bus
    and POSTs canonical ``{username, latitude, longitude, accuracy_m,
    zone_name}`` bodies here. The core derives :class:`PresenceState`
    from ``zone_name`` (``home`` / ``zone`` / ``away``) and returns
    204 No Content.
    """

    async def post(self) -> web.Response:
        self.user
        body = await self.body()

        username = body.get("username")
        if not username:
            return error_response(422, "UNPROCESSABLE", "username is required.")

        zone_name = body.get("zone_name")
        state: PresenceState = body.get("state") or _derive_state(zone_name)
        lat = body.get("latitude")
        lon = body.get("longitude")
        accuracy = body.get("accuracy_m")

        await self.svc(presence_service_key).update_location(
            LocationUpdate(
                username=username,
                state=state,
                zone_name=zone_name,
                latitude=float(lat) if lat is not None else None,
                longitude=float(lon) if lon is not None else None,
                gps_accuracy_m=float(accuracy) if accuracy is not None else None,
            )
        )
        return web.Response(status=204)
