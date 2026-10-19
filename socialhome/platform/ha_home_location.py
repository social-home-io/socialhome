"""The household's home location from Home Assistant.

Both :class:`socialhome.platform.ha.HaAdapter` and
:class:`socialhome.platform.haos.HaosAdapter` learn the household's
GPS coordinates from HA Core's ``/api/config``:

* :func:`persist_home_location_from_ha` — the startup write-side: it
  compares the value HA reports to what's already on the local
  ``instance_identity`` row, persists the new value (4dp-truncated per
  §25) if different, and publishes :class:`LocalHomeLocationUpdated` on
  change so the federation service can fan it out to peers.
* :class:`HaHomeLocationSource` — the adapter's
  :class:`~socialhome.platform.adapter.HomeLocationSource`, read again
  whenever Admin › Settings shows the home (so a change made in HA shows
  up without a restart).

HA contract — verified, not inferred: ``GET /api/config`` (REST, bearer
token; https://developers.home-assistant.io/docs/api/rest) answers the
core config, including ``"latitude"`` and ``"longitude"`` as numbers
(home-assistant/core ``homeassistant/core_config.py`` ``Config.as_dict``)
— the location set under Settings → System → General, and the centre of
``zone.home``. HA reports ``0, 0`` when no location was ever set. A failed
call is logged at WARNING by :meth:`HaClient.get_config`.

Read-only against HA: we never write back. HA Core stays the source
of truth.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import AsyncDatabase
from ..domain.events import LocalHomeLocationUpdated
from ..domain.home_location import InvalidHomeLocationError, validate_home_location
from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)


class HaHomeLocationSource:
    """:class:`~socialhome.platform.adapter.HomeLocationSource` over HA's
    ``/api/config``.

    Holds the adapter (like the other HA providers) and reaches its
    ``_client`` lazily — the client exists only after ``on_startup``.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def get_home_location(self) -> tuple[float, float] | None:
        """HA's home now, at 4 dp — ``None`` when HA has none (``0, 0``),
        answered nothing usable, or isn't wired yet."""
        try:
            client = self._adapter._client
        except RuntimeError:
            return None
        cfg = await client.get_config()
        if not isinstance(cfg, dict):
            return None
        try:
            return validate_home_location(cfg.get("latitude"), cfg.get("longitude"))
        except InvalidHomeLocationError:
            if cfg.get("latitude") or cfg.get("longitude"):
                log.warning(
                    "home location: Home Assistant's /api/config has an unusable "
                    "latitude/longitude — keeping the stored home",
                )
            return None


async def persist_home_location_from_ha(
    *,
    db: AsyncDatabase,
    bus: EventBus,
    latitude: float,
    longitude: float,
) -> None:
    """Persist HA's home GPS to ``instance_identity`` if it changed.

    Args:
        db: The application database.
        bus: The in-process event bus. Published-to on change.
        latitude: The lat HA Core reports (raw — we truncate here).
        longitude: The lon HA Core reports.

    Skips entirely when HA reports the placeholder ``0.0/0.0`` (HA
    Core returns that when the operator hasn't set a location). On a
    real value, truncates to 4dp, compares to the existing
    ``instance_identity.home_lat``/``home_lon``, and writes +
    publishes only when different.
    """
    if latitude == 0.0 and longitude == 0.0:
        return  # operator hasn't set a location in HA — nothing to do
    new_lat = round(float(latitude), 4)
    new_lon = round(float(longitude), 4)

    row = await db.fetchone(
        "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'",
    )
    current_lat = row["home_lat"] if row is not None else None
    current_lon = row["home_lon"] if row is not None else None

    if current_lat == new_lat and current_lon == new_lon:
        return  # unchanged — nothing to do

    await db.enqueue(
        "UPDATE instance_identity SET home_lat = ?, home_lon = ? WHERE id = 'self'",
        (new_lat, new_lon),
    )
    log.info(
        "home location updated from HA: lat=%s lon=%s (was lat=%s lon=%s)",
        new_lat,
        new_lon,
        current_lat,
        current_lon,
    )
    await bus.publish(
        LocalHomeLocationUpdated(latitude=new_lat, longitude=new_lon),
    )
