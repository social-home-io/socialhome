"""§25.6 sync of a space's zones (§23.8.7) and their tombstones."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ....domain.federation import FederationEvent
from ....domain.space import (
    SpaceZone,
    validate_zone_color,
    validate_zone_coord,
    validate_zone_icon,
    validate_zone_name,
    validate_zone_radius,
)
from .receiver_base import ReceiverBase, _log_tombstone_refusals
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)


class ZonesMixin(ReceiverBase):
    """The zone resources of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_zones(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        # §23.8.7: per-space zone catalogue. Receiver may be
        # configured without a zone repo (older deployments) — in
        # that case skip rather than error.
        if self._zone_repo is None:
            log.debug(
                "received %d zone records — no zone_repo wired, skipping",
                len(records),
            )
            return
        for r in records:
            zone = _zone_from_record(r, space_id, provider=provider)
            if zone is None:
                continue
            if await self._zone_repo.is_deleted(zone.id, space_id=space_id):
                log.debug("sync: zone %s was deleted here — skipped", zone.id)
                continue
            if "icon" not in r:
                # An older provider (pre-icon) streams no ``icon`` key:
                # keep the stored icon rather than wiping it.
                stored_zone = await self._zone_repo.get(zone.id)
                if stored_zone is not None and stored_zone.space_id == space_id:
                    zone = replace(zone, icon=stored_zone.icon)
            if not await self._zone_repo.upsert(zone, space_id=space_id):
                log.warning(
                    "space sync: zone %s already exists in another space "
                    "— refusing the write for %s",
                    zone.id,
                    space_id,
                )

    async def _authored_zone_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_ZONE_DELETED rule: an admin household
        # (zones are admin-only) removes a zone held live here IN
        # THIS SPACE.
        held_zone = (
            await self._zone_repo.get(rid)
            if rid and self._zone_repo is not None
            else None
        )
        if held_zone is None or held_zone.space_id != space_id:
            return False
        return await auth.is_admin_household(event, space_id)

    async def _authored_zone(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        if (
            rid
            and self._zone_repo is not None
            and await self._zone_repo.is_deleted(rid, space_id=space_id)
        ):
            return False  # deleted here; the tombstone wins
        return await auth.is_admin_household(event, space_id)

    async def _persist_zone_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed zone deletes (``space_zones_deleted``, migration
        0085): a zone held live here in this space is tombstoned, as a live
        ``SPACE_ZONE_DELETED`` does. A member household's records were
        admitted only from an admin household (the live rule).

        No stub for a zone never held here: zone ids are not owner-bound
        (``z_<random>``), so nothing proves an id is this space's — a stub
        could squat another space's zone. A stale copy a household streams
        later lands, and the host's next tombstone stream removes it.
        """
        if self._zone_repo is None:
            return
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        for r in records:
            zone_id = str(r.get("id") or r.get("zone_id") or "")
            if not zone_id:
                continue
            held = await self._zone_repo.get(zone_id)
            if held is None:
                continue
            if held.space_id != space_id:
                cross_space.append(zone_id)
                continue
            await self._zone_repo.delete(
                zone_id,
                space_id=space_id,
                deleted_by=await self._vetted_actor(r, space_id, provider, from_host),
            )
        _log_tombstone_refusals("zone", provider, space_id, cross_space, 0)


def _zone_from_record(
    r: dict[str, Any], space_id: str, *, provider: str
) -> SpaceZone | None:
    """Reconstruct a :class:`SpaceZone` from an exporter chunk record.

    Lenient: skip the row rather than raising if a malformed record
    leaks into the chunk. The federation layer has already verified
    the envelope signature, so the worst case is a peer with a buggy
    catalogue — log and drop the offending row, keep the others.

    Name, colour and icon pass the same validation as the local API
    (§23.8.7) — a refused row is a WARNING naming the space and the
    provider, never the name.
    """
    zone_id = r.get("id")
    if not zone_id or not r.get("name"):
        return None
    try:
        name = validate_zone_name(r.get("name"))
        color = validate_zone_color(r.get("color"))
        # Same geometry rules as the local API: finite, in range, 4 dp
        # (CLAUDE.md GPS rule), radius 25 m – 50 km.
        latitude = validate_zone_coord(r.get("latitude"), name="latitude", limit=90)
        longitude = validate_zone_coord(r.get("longitude"), name="longitude", limit=180)
        radius_m = validate_zone_radius(r.get("radius_m"))
        icon = validate_zone_icon(r.get("icon"))
    except ValueError as exc:
        log.warning(
            "space sync from %s: zone %s in space %s refused — %s",
            provider,
            str(zone_id)[:64],
            space_id,
            exc,
        )
        return None
    return SpaceZone(
        id=str(zone_id),
        space_id=space_id or str(r.get("space_id") or ""),
        name=name,
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        color=color,
        icon=icon,
        created_by=str(r.get("created_by") or ""),
        created_at=str(r.get("created_at") or ""),
        updated_at=str(r.get("updated_at") or ""),
    )
