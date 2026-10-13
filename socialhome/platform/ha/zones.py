"""Home Assistant zones → household zones (the HA :class:`ZoneSource`).

Shared by :class:`~socialhome.platform.ha.HaAdapter` and
:class:`~socialhome.platform.haos.HaosAdapter` (both talk to HA Core
through the same :class:`~socialhome.platform.ha.client.HaClient`).

HA contract — verified, not inferred:

* ``GET /api/states`` (REST, bearer token;
  https://developers.home-assistant.io/docs/api/rest) answers a JSON
  array of state objects ``{entity_id, state, attributes, last_changed,
  last_updated}``.
* A zone is a ``zone.*`` entity; its ``state`` is the number of persons
  in it. Its attributes come from home-assistant/core
  ``homeassistant/components/zone/__init__.py`` (``Zone._generate_attrs``)
  and ``homeassistant/const.py`` / ``zone/const.py``
  (``ZoneEntityStateAttribute``): ``latitude``, ``longitude``, ``radius``
  (metres, float), ``passive`` (bool), ``persons``, ``device_trackers``,
  ``editable``; plus the standard entity attributes ``friendly_name`` and
  ``icon`` (an ``mdi:*`` name, e.g. ``mdi:home``).
* ``zone.home`` is built from the core config (location name, latitude,
  longitude, radius) and carries ``mdi:home``. The zone integration's
  ``async_setup`` always adds it (unless a user zone already took that
  entity id), and ``zone`` is one of HA's default integrations
  (``bootstrap.DEFAULT_INTEGRATIONS``) — so an ``/api/states`` answer
  without ``zone.home`` means HA hasn't loaded its zones yet (startup,
  recovery mode), never "HA has no zones". :meth:`HaZoneSource.list_zones`
  reports it as unavailable so the sync keeps the synced zones.
* HA core stores no zone colour (the HA frontend picks one) — synced
  zones get ``color=None`` and Social Home's deterministic palette.

Passive zones (``passive: true`` — used only for automations, never as a
person's location) are left out. Field validation (name, centre, radius
clamp, emoji icon) is :meth:`HouseholdZoneService.mirror_external`'s job;
this module only maps shapes.
"""

from __future__ import annotations

import logging
from typing import Any

from ...domain.household_zone import ExternalHouseholdZone, ZoneSourceUnavailableError

__all__ = ["HA_HOME_ZONE", "HaZoneSource", "icon_for_mdi", "zones_from_states"]

log = logging.getLogger(__name__)

#: HA's home zone entity — Social Home's Home zone.
HA_HOME_ZONE = "zone.home"

#: Fallback for an ``mdi:*`` icon with no emoji here (or no icon at all).
DEFAULT_ZONE_EMOJI = "📍"

#: ``mdi:<name>`` → one emoji. HA zone icons are Material Design Icons;
#: Social Home zones carry a single emoji. The common place-like icons
#: map; anything else falls back to :data:`DEFAULT_ZONE_EMOJI`.
MDI_ZONE_EMOJI: dict[str, str] = {
    "home": "🏠",
    "home-outline": "🏠",
    "home-variant": "🏠",
    "home-city": "🏠",
    "house": "🏠",
    "briefcase": "💼",
    "briefcase-outline": "💼",
    "office-building": "🏢",
    "office-building-outline": "🏢",
    "domain": "🏢",
    "school": "🏫",
    "school-outline": "🏫",
    "town-hall": "🏫",
    "cart": "🛒",
    "cart-outline": "🛒",
    "shopping": "🛒",
    "shopping-outline": "🛒",
    "store": "🛒",
    "dumbbell": "🏋️",
    "weight-lifter": "🏋️",
    "tree": "🌳",
    "tree-outline": "🌳",
    "pine-tree": "🌳",
    "forest": "🌳",
    "nature": "🌳",
    "pine-tree-box": "🌳",
    "hospital": "🏥",
    "hospital-building": "🏥",
    "hospital-box": "🏥",
    "medical-bag": "🏥",
    "doctor": "🏥",
    "soccer": "⚽",
    "soccer-field": "⚽",
    "basketball": "🏀",
    "tennis": "🎾",
    "music": "🎵",
    "music-note": "🎵",
    "silverware-fork-knife": "🍽️",
    "silverware": "🍽️",
    "food": "🍽️",
    "coffee": "☕",
    "airplane": "✈️",
    "airport": "✈️",
    "train": "🚉",
    "subway": "🚉",
    "bus": "🚌",
    "map-marker": "📍",
    "map-marker-outline": "📍",
    "church": "⛪",
    "bank": "🏦",
    "gas-station": "⛽",
    "car": "🚗",
    "garage": "🚗",
    "parking": "🅿️",
    "beach": "🏖️",
    "pool": "🏊",
    "swim": "🏊",
    "baby-face": "🧒",
    "baby-face-outline": "🧒",
    "human-child": "🧒",
    "human-male-child": "🧒",
    "human-female-girl": "🧒",
    "dog": "🐕",
    "dog-side": "🐕",
    "paw": "🐕",
    "cat": "🐈",
    "horse": "🐎",
    "account": "👤",
    "account-group": "👥",
    "library": "📚",
    "book-open-variant": "📚",
    "theater": "🎭",
    "palette": "🎨",
    "factory": "🏭",
    "warehouse": "🏭",
    "tent": "⛺",
    "campfire": "⛺",
    "image-filter-hdr": "⛰️",
    "ski": "⛷️",
    "bike": "🚲",
    "sail-boat": "⛵",
    "anchor": "⚓",
    "island": "🏝️",
}


def icon_for_mdi(icon: object) -> str:
    """The emoji for an HA ``mdi:*`` icon; :data:`DEFAULT_ZONE_EMOJI`
    when it is missing, not an ``mdi:`` name, or not in the map."""
    if not isinstance(icon, str) or not icon.startswith("mdi:"):
        return DEFAULT_ZONE_EMOJI
    return MDI_ZONE_EMOJI.get(icon[len("mdi:") :].strip().lower(), DEFAULT_ZONE_EMOJI)


def _zone_name(entity_id: str, attrs: dict[str, Any]) -> str:
    """``friendly_name``, else the entity id's object id (``zone.my_work``
    → ``my work``)."""
    friendly = attrs.get("friendly_name")
    if isinstance(friendly, str) and friendly.strip():
        return friendly.strip()
    return entity_id.split(".", 1)[1].replace("_", " ").strip()


def _raw(attrs: dict[str, Any], key: str) -> Any:
    """An attribute exactly as HA sent it (``None`` when absent) — the
    mirror validates, repairs or skips it, so no coercion here."""
    return attrs.get(key)


def zones_from_states(states: list[Any]) -> list[ExternalHouseholdZone]:
    """The non-passive ``zone.*`` entities in an ``/api/states`` answer,
    as :class:`ExternalHouseholdZone` values (``zone.home`` is the Home
    zone). Entries that aren't state objects are ignored; field values
    go through unvalidated — the mirror repairs or skips them."""
    zones: list[ExternalHouseholdZone] = []
    for state in states:
        if not isinstance(state, dict):
            continue
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("zone."):
            continue
        raw_attrs = state.get("attributes")
        # ``dict[str, Any]``: values go to the mirror as HA sent them.
        attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
        if attrs.get("passive") is True:
            continue
        zones.append(
            ExternalHouseholdZone(
                external_id=entity_id,
                name=_zone_name(entity_id, attrs),
                latitude=_raw(attrs, "latitude"),
                longitude=_raw(attrs, "longitude"),
                radius_m=_raw(attrs, "radius"),
                is_home=entity_id == HA_HOME_ZONE,
                icon=icon_for_mdi(attrs.get("icon")),
                color=None,
            )
        )
    return zones


class HaZoneSource:
    """:class:`~socialhome.platform.adapter.ZoneSource` over HA's
    ``zone.*`` entities.

    Holds the adapter (like the other HA providers) and reaches its
    ``_client`` lazily — the client exists only after ``on_startup``.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def list_zones(self) -> list[ExternalHouseholdZone]:
        """HA's zones now. Raises :class:`ZoneSourceUnavailableError` when
        ``/api/states`` failed (the client logs the WARNING) or the client
        isn't wired yet — never an empty list for a failed call."""
        try:
            client = self._adapter._client
        except RuntimeError as exc:
            raise ZoneSourceUnavailableError(str(exc)) from exc
        states = await client.fetch_states()
        if states is None:
            raise ZoneSourceUnavailableError("Home Assistant didn't answer /api/states")
        # Checked before the passive filter: a (user-defined) passive
        # ``zone.home`` still proves the zones are loaded.
        if not any(
            isinstance(s, dict) and s.get("entity_id") == HA_HOME_ZONE for s in states
        ):
            log.warning(
                "household zones: Home Assistant has no %s yet (still starting?);"
                " keeping the synced zones",
                HA_HOME_ZONE,
            )
            raise ZoneSourceUnavailableError(
                f"Home Assistant hasn't loaded its zones yet (no {HA_HOME_ZONE})"
            )
        return zones_from_states(states)
