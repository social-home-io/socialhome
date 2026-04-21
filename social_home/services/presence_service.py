"""Presence service — household member presence + location (§23.21).

Manages the ``presence`` table. In HA mode, location updates are pushed
by the HA integration via ``POST /api/presence/location``. In standalone
mode the user's browser shares location via the Geolocation API.

GPS coordinates are **always** truncated to 4 decimal places before
storage (§25 / CLAUDE.md rule). Updates with ``gps_accuracy > 500m``
have their coordinates nulled (zone name is still stored).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

from ..domain.events import PresenceUpdated
from ..domain.presence import (
    PRESENCE_STATES,
    LocationUpdate,
    PersonPresence,
    truncate_coord,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.presence_repo import AbstractPresenceRepo

log = logging.getLogger(__name__)

#: GPS updates with accuracy worse than this are downgraded to zone-only.
MAX_GPS_ACCURACY_M = 500.0


class PresenceService:
    """Read and update household member presence."""

    __slots__ = ("_repo", "_bus")

    def __init__(
        self,
        repo: AbstractPresenceRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = repo
        # The bus is optional so existing call sites that don't pass
        # one (route handlers, tests) keep working — only the WS
        # publish path requires it.
        self._bus = bus

    def attach_event_bus(self, bus: EventBus) -> None:
        """Attach an :class:`EventBus` after construction."""
        self._bus = bus

    async def list_presence(self) -> list[PersonPresence]:
        """Return the current presence snapshot for all household members."""
        return await self._repo.list_active()

    async def list_presence_for_members(
        self,
        user_ids: set[str],
        *,
        location_mode: str = "gps",
    ) -> list[PersonPresence]:
        """§23.80 — presence snapshot filtered to a specific member set
        + per-space privacy mode.

        ``user_ids`` = the local user_ids allowed to surface in the
        result (i.e. the space's member list).

        ``location_mode`` governs what location detail leaks:

        * ``"off"``       — returns an empty list (caller should hide
                            the map entirely).
        * ``"zone_only"`` — keeps ``zone_name`` + ``state`` but nulls
                            lat/lon/accuracy.
        * ``"gps"``       — full data (subject to the existing
                            §25 4-decimal truncation applied at ingest).
        """
        if location_mode == "off":
            return []
        all_entries = await self._repo.list_active()
        out: list[PersonPresence] = []
        for p in all_entries:
            if p.user_id not in user_ids:
                continue
            if location_mode == "zone_only":
                out.append(
                    replace(
                        p,
                        latitude=None,
                        longitude=None,
                        gps_accuracy_m=None,
                    )
                )
            else:
                out.append(p)
        return out

    async def update_location(self, update: LocationUpdate) -> None:
        """Persist a location update from the HA integration or browser.

        GPS coordinates are truncated to 4dp. If ``gps_accuracy_m``
        exceeds :data:`MAX_GPS_ACCURACY_M`, coordinates are nulled but
        the zone name is still stored.
        """
        if update.state not in PRESENCE_STATES:
            raise ValueError(f"invalid presence state {update.state!r}")

        lat = update.latitude
        lon = update.longitude
        acc = update.gps_accuracy_m

        # Accuracy gate: drop coordinates when too imprecise
        if acc is not None and acc > MAX_GPS_ACCURACY_M:
            lat = None
            lon = None
            acc = None

        # 4dp truncation (§25)
        if lat is not None:
            lat = truncate_coord(lat)
        if lon is not None:
            lon = truncate_coord(lon)

        await self._repo.upsert_local(
            username=update.username,
            entity_id=update.username,
            state=update.state,
            zone_name=update.zone_name,
            latitude=lat,
            longitude=lon,
            gps_accuracy_m=acc,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        # Real-time fan-out so other household members see the change
        # without polling. The event carries the post-truncation
        # coordinates (§25 GPS rule).
        if self._bus is not None:
            await self._bus.publish(
                PresenceUpdated(
                    username=update.username,
                    state=update.state,
                    zone_name=update.zone_name,
                    latitude=lat,
                    longitude=lon,
                )
            )

    async def apply_remote(self, *, from_instance: str, payload: dict) -> None:
        """Apply a ``PRESENCE_UPDATED`` federation event from a remote peer.

        Remote presence lands in :table:`remote_presence`, keyed on
        ``(from_instance, remote_username)``. The local
        :table:`presence` table remains household-only.
        """
        try:
            state = str(payload.get("state") or "away")
            username = str(payload.get("username") or "").strip()
        except Exception as exc:
            log.debug("PRESENCE_UPDATED: malformed payload — %s", exc)
            return
        if not username:
            return
        if state not in PRESENCE_STATES:
            log.debug(
                "PRESENCE_UPDATED: invalid state %r from %s",
                state,
                from_instance,
            )
            return

        lat = payload.get("latitude")
        lon = payload.get("longitude")
        acc = payload.get("gps_accuracy_m")

        if acc is not None and float(acc) > MAX_GPS_ACCURACY_M:
            lat = lon = acc = None
        if lat is not None:
            lat = truncate_coord(float(lat))
        if lon is not None:
            lon = truncate_coord(float(lon))

        await self._repo.upsert_remote(
            from_instance=from_instance,
            remote_username=username,
            state=state,
            zone_name=payload.get("zone_name"),
            latitude=lat,
            longitude=lon,
            gps_accuracy_m=acc,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    async def get_presence(self, username: str) -> PersonPresence | None:
        """Get a single member's presence."""
        return await self._repo.get_by_username(username)
