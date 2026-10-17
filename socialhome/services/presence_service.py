"""Presence service — household member presence + location (§23.21).

Manages the ``presence`` table. Location updates arrive on
``POST /api/presence/location``: the Home Assistant integration pushes
each member's GPS together with HA's zone state as ``zone_name``; other
clients (standalone mode, companion apps) push GPS alone — no
``zone_name`` key, no state — and the server matches the point against
the household zones
(:meth:`HouseholdZoneService.match`) to fill ``zone_name`` — the Home
zone becomes ``"home"`` (state ``home``), another zone its name (state
``zone``), no zone leaves the update as sent (``away``).

GPS coordinates are **always** truncated to 4 decimal places before
storage (§25 / CLAUDE.md rule). Updates with ``gps_accuracy > 500m``
have their coordinates nulled (zone name is still stored) — and no zone
is guessed from such a point.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from ..domain.events import PresenceUpdated
from ..domain.household_zone import HouseholdZone
from ..domain.presence import (
    PRESENCE_STATES,
    LocationUpdate,
    PersonPresence,
    truncate_coord,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.presence_repo import AbstractPresenceRepo
from ..repositories.user_repo import AbstractUserRepo
from .bus_publisher import BusPublisherMixin

log = logging.getLogger(__name__)

#: GPS updates with accuracy worse than this are downgraded to zone-only.
MAX_GPS_ACCURACY_M = 500.0


class HouseholdZoneMatcher(Protocol):
    """What :class:`PresenceService` needs from the household zones —
    satisfied by :class:`~socialhome.services.household_zone_service.HouseholdZoneService`."""

    async def match(
        self, latitude: float, longitude: float
    ) -> HouseholdZone | None: ...


class UserNotFoundError(LookupError):
    """Raised when a presence update names a username that doesn't exist.

    The ``presence`` table is FK-linked to ``users(username)``, so without
    this guard the upsert would surface as a generic ``IntegrityError`` to
    the caller — opaque, and (until #278) used to also poison every other
    write in the same batch.
    """


class PresenceService(BusPublisherMixin):
    """Read and update household member presence."""

    __slots__ = ("_bus", "_repo", "_users", "_zones")

    def __init__(
        self,
        repo: AbstractPresenceRepo,
        users: AbstractUserRepo,
        bus: EventBus | None = None,
        *,
        zone_matcher: HouseholdZoneMatcher | None = None,
    ) -> None:
        self._repo = repo
        # ``_users`` is used to short-circuit ``update_location`` for a
        # username that doesn't exist locally — the ``presence`` table's
        # FK on ``users(username)`` would otherwise raise a generic
        # ``sqlite3.IntegrityError``.
        self._users = users
        # The bus is optional so existing call sites that don't pass
        # one (route handlers, tests) keep working — only the WS
        # publish path requires it.
        self._bus = bus
        # Server-side GPS → household zone match for updates that carry
        # no ``zone_name``; ``None`` = never guess a zone.
        self._zones = zone_matcher

    def attach_event_bus(self, bus: EventBus) -> None:
        """Attach an :class:`EventBus` after construction."""
        self._bus = bus

    async def list_presence(self) -> list[PersonPresence]:
        """Return the current presence snapshot for all household members."""
        return await self._repo.list_active()

    async def list_presence_for_members(
        self,
        user_ids: set[str],
    ) -> list[PersonPresence]:
        """§23.80 — presence snapshot filtered to a specific member set.

        ``user_ids`` = the local user_ids allowed to surface in the
        result (typically the space's member list, intersected with
        ``location_share_enabled`` opt-ins by the caller).

        Each member's GPS comes from the household ``presence`` table.
        Returned entries are stripped of ``zone_name`` because zones
        are a household-only concept (§7.3); space callers must not
        receive HA zone names. The 4dp truncation is already applied
        at ingest (§25 GPS rule).
        """
        all_entries = await self._repo.list_active()
        return [
            replace(p, zone_name=None) for p in all_entries if p.user_id in user_ids
        ]

    async def update_location(self, update: LocationUpdate) -> None:
        """Persist a location update (HA integration or another client).

        GPS coordinates are truncated to 4dp. If ``gps_accuracy_m``
        exceeds :data:`MAX_GPS_ACCURACY_M`, coordinates are nulled but
        the zone name is still stored.

        When the client stated no zone at all (``update.zone_asserted`` is
        ``False``: no ``zone_name`` key, no ``state``) and the GPS point is
        usable (after the accuracy gate and the 4-dp truncation), the point
        is matched against the household zones: a match sets ``zone_name``
        and the state (Home zone → ``"home"`` / ``home``, another → its
        name / ``zone``); no match keeps ``away``. A stated zone — Home
        Assistant's, including ``None`` for not_home — or state is never
        replaced.

        Raises :class:`UserNotFoundError` if ``update.username`` is not
        a local user — the FK on ``presence(username)`` would otherwise
        produce a generic ``IntegrityError``. The check happens before
        any write so the failure path doesn't share a writer batch with
        unrelated handlers.
        """
        if update.state not in PRESENCE_STATES:
            raise ValueError(f"invalid presence state {update.state!r}")

        if await self._users.get(update.username) is None:
            raise UserNotFoundError(update.username)

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

        state = update.state
        zone_name = update.zone_name
        if (
            not update.zone_asserted
            and not zone_name
            and lat is not None
            and lon is not None
            and self._zones is not None
        ):
            zone = await self._zones.match(lat, lon)
            if zone is not None:
                zone_name = zone.presence_zone_name
                state = "home" if zone.is_home else "zone"

        await self._repo.upsert_local(
            username=update.username,
            entity_id=update.username,
            state=state,
            zone_name=zone_name,
            latitude=lat,
            longitude=lon,
            gps_accuracy_m=acc,
            updated_at=datetime.now(UTC).isoformat(),
        )
        # Real-time fan-out so other household members see the change
        # without polling. The event carries the post-truncation
        # coordinates (§25 GPS rule). ``gps_accuracy_m`` and
        # ``updated_at`` are carried alongside ``zone_name`` so the
        # per-space outbound service can build a GPS-only payload
        # without a second DB hit; subscribers that target a
        # space-bound channel must drop ``zone_name`` (§23.8.6).
        await self._emit(
            PresenceUpdated(
                username=update.username,
                state=state,
                zone_name=zone_name,
                latitude=lat,
                longitude=lon,
                gps_accuracy_m=acc,
                updated_at=datetime.now(UTC).isoformat(),
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
            log.debug("PRESENCE_UPDATED: malformed payload — %s", exc, exc_info=True)
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
            updated_at=datetime.now(UTC).isoformat(),
        )

    async def get_presence(self, username: str) -> PersonPresence | None:
        """Get a single member's presence."""
        return await self._repo.get_by_username(username)
