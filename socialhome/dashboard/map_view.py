"""Home's Map tab: who is where, as the wall draws it.

The read model (:class:`~socialhome.domain.household_dashboard.HomeSnapshot`)
carries each shown member's presence, shared GPS point (4 dp, as
stored) and when it last changed, plus the household's own zones. This
module turns that into what the Map tab renders — the side list ("Who's
where", the accessible equivalent of the map), one bubble per occupied
zone or per person outside every zone, and the zone pins — in Home
Assistant 2026.10's people/zone style:

* **In a zone → at the zone centre.** Everyone a presence row puts in a
  household zone (``home`` → the Home zone, ``zone`` → the zone of that
  name) shares that zone's bubble; their own point is never drawn.
* **Outside every zone → their own point.** A one-face bubble over a dot
  at the 4-dp point. A zone name the household has no zone for (Home
  Assistant knows more zones than were synced) is such a point too,
  named after that zone.
* **Fresh only.** A location older than :data:`MAP_STALE_AFTER` is
  *stale* (faded, "last seen 2 h ago"); older than :data:`MAP_DROP_AFTER`
  the person leaves the map. Someone who shares neither a point nor a
  known zone is not on it at all. The tab exists while anyone is
  (:func:`has_map`).
* **Order.** Home first, then the other occupied zones A→Z, then
  everyone else; by name within each — the list, the bubbles and the
  faces in a bubble all follow it.

Screen geometry (framing, merging bubbles that overlap, the selection)
is ``map.js``'s job; this module is pure: no I/O, no pixels.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from ..domain.household_dashboard import (
    MAP_DROP_AFTER,
    MAP_STALE_AFTER,
    SECTION_PRESENCE,
    DashboardPerson,
    DashboardZone,
    HomeSnapshot,
)
from .clock import ago
from .presenter import Fmt

__all__ = [
    "AWAY_ICON",
    "BUBBLE_FACES",
    "HOME_ICON",
    "PLACE_ICON",
    "MapBubble",
    "MapPin",
    "MapSpot",
    "MapView",
    "has_map",
    "map_view",
    "pin_icon",
    "zone_for",
]

#: Faces a bubble shows before the rest fold into "+N" (tapping the
#: bubble shows them all).
BUBBLE_FACES: Final = 3
#: Away — the same glyph as Overview's who's-home "away" group.
AWAY_ICON: Final = "🚗"
HOME_ICON: Final = "🏠"
#: A named place the wall has no zone for, and a zone with no letter.
PLACE_ICON: Final = "📍"


@dataclass(slots=True, frozen=True)
class MapSpot:
    """One person on the map and their row in the list.

    ``zone`` is the household zone they are in (drawn at its centre),
    else ``latitude`` / ``longitude`` is their own 4-dp point. ``icon`` +
    ``place`` say where ("🏫 School", "🚗 Away"); ``when`` how long ago
    the location changed ("5 min ago", "2 h ago"), ``iso`` that instant.
    """

    person: DashboardPerson
    zone: DashboardZone | None
    latitude: float | None
    longitude: float | None
    icon: str
    place: str
    when: str
    iso: str
    stale: bool


@dataclass(slots=True, frozen=True)
class MapBubble:
    """One marker bubble: everyone in a zone (``key`` ``zone:<id>``, at
    the zone centre) or one person outside every zone (``person:<id>``,
    at their point). ``caption`` names the place in a merged bubble,
    ``label`` names who and where on an edge chip; ``age`` is the chip on
    a bubble whose faces are all stale (the newest of them)."""

    key: str
    latitude: float
    longitude: float
    zone: DashboardZone | None
    spots: tuple[MapSpot, ...]
    caption: str
    label: str
    stale: bool
    age: str | None

    @property
    def more(self) -> int:
        """Faces folded into the "+N" chip."""
        return max(0, len(self.spots) - BUBBLE_FACES)

    @property
    def away(self) -> bool:
        """One person away (outside every zone and named place) — a merged
        bubble's caption says "🚗 Away" once, after its places."""
        return self.zone is None and self.spots[0].icon == AWAY_ICON


@dataclass(slots=True, frozen=True)
class MapPin:
    """A household zone on the map: its emoji (or first letter) pin and
    radius; an empty zone is drawn muted, as context."""

    zone: DashboardZone
    icon: str
    occupied: bool


@dataclass(slots=True, frozen=True)
class MapView:
    """The Map tab: the list in order, the bubbles, every zone pin and
    the map's spoken summary ("Map: Mia and Pascal at Home, …")."""

    spots: tuple[MapSpot, ...]
    bubbles: tuple[MapBubble, ...]
    pins: tuple[MapPin, ...]
    label: str


def zone_for(
    person: DashboardPerson, zones: Sequence[DashboardZone]
) -> DashboardZone | None:
    """The household zone ``person``'s presence puts them in: the Home
    zone for ``home``, the zone named ``zone_name`` (any case) for
    ``zone`` — ``None`` otherwise. Away is away: a point that happens to
    lie in a zone circle doesn't move them in (the list says "Away")."""
    if person.presence == "home":
        return next((z for z in zones if z.is_home), None)
    if person.presence == "zone" and person.zone_name:
        name = person.zone_name.casefold()
        return next((z for z in zones if z.name.casefold() == name), None)
    return None


def pin_icon(zone: DashboardZone) -> str:
    """A zone pin's glyph: its emoji, else the first letter of its name."""
    if zone.icon:
        return zone.icon
    return zone.name[:1].upper() or PLACE_ICON


def _fresh(person: DashboardPerson, now: datetime) -> bool:
    return person.located_at is not None and now - person.located_at < MAP_DROP_AFTER


def _has_point(person: DashboardPerson) -> bool:
    return person.latitude is not None and person.longitude is not None


def _on_map(
    person: DashboardPerson, zones: Sequence[DashboardZone], now: datetime
) -> bool:
    return _fresh(person, now) and (
        zone_for(person, zones) is not None or _has_point(person)
    )


def has_map(snapshot: HomeSnapshot) -> bool:
    """Does Home show the Map tab? While the presence section is on and
    at least one shown member has a fresh location (a point or a known
    zone, under :data:`MAP_DROP_AFTER` old)."""
    if SECTION_PRESENCE not in snapshot.sections:
        return False
    return any(_on_map(p, snapshot.zones, snapshot.now) for p in snapshot.people)


def _spot(
    person: DashboardPerson,
    zone: DashboardZone | None,
    fmt: Fmt,
) -> MapSpot:
    located_at = person.located_at
    assert located_at is not None
    if zone is not None:
        icon, place = pin_icon(zone), zone.name
    elif person.presence == "home":
        icon, place = HOME_ICON, fmt.t("dashboard.presence.home")
    elif person.presence == "zone" and person.zone_name:
        icon, place = PLACE_ICON, person.zone_name
    else:
        icon, place = AWAY_ICON, fmt.t("dashboard.presence.away")
    return MapSpot(
        person=person,
        zone=zone,
        latitude=None if zone is not None else person.latitude,
        longitude=None if zone is not None else person.longitude,
        icon=icon,
        place=place,
        when=ago(fmt.now, located_at, fmt.t, hours=True),
        iso=located_at.isoformat(),
        stale=fmt.now - located_at >= MAP_STALE_AFTER,
    )


def _age(spot: MapSpot, fmt: Fmt) -> float:
    """Seconds since ``spot``'s location changed."""
    located_at = spot.person.located_at
    assert located_at is not None
    return (fmt.now - located_at).total_seconds()


def _names(names: Sequence[str], fmt: Fmt) -> str:
    """ "Mia", "Mia and Pascal", "Anna, Mia and Pascal"."""
    if len(names) == 1:
        return names[0]
    return fmt.t("dashboard.map.and", rest=", ".join(names[:-1]), last=names[-1])


def _bubble(key: str, spots: Sequence[MapSpot], fmt: Fmt) -> MapBubble:
    first = spots[0]
    if first.zone is not None:
        lat, lon = first.zone.latitude, first.zone.longitude
    else:
        assert first.latitude is not None and first.longitude is not None
        lat, lon = first.latitude, first.longitude
    stale = all(s.stale for s in spots)
    newest = min(spots, key=lambda s: _age(s, fmt))
    caption = f"{first.icon} {first.place}"
    names = ", ".join(s.person.display_name for s in spots)
    return MapBubble(
        key=key,
        latitude=lat,
        longitude=lon,
        zone=first.zone,
        spots=tuple(spots),
        caption=caption,
        label=f"{names} · {caption}",
        stale=stale,
        age=newest.when if stale else None,
    )


def _summary(groups: Iterable[tuple[str, list[MapSpot]]], fmt: Fmt) -> str:
    parts = []
    for kind, spots in groups:
        names = _names([s.person.display_name for s in spots], fmt)
        if kind == "away":
            parts.append(fmt.t("dashboard.map.away", names=names))
        else:
            parts.append(fmt.t("dashboard.map.at", names=names, place=spots[0].place))
    return fmt.t("dashboard.map.label", summary=", ".join(parts))


def map_view(snapshot: HomeSnapshot, fmt: Fmt) -> MapView | None:
    """The Map tab for ``snapshot`` — ``None`` when it doesn't show
    (:func:`has_map`)."""
    if not has_map(snapshot):
        return None
    zones = snapshot.zones
    rank = {z.id: i for i, z in enumerate(sorted(zones, key=_zone_order))}
    spots: list[MapSpot] = []
    for person in snapshot.people:
        if not _on_map(person, zones, snapshot.now):
            continue
        spots.append(_spot(person, zone_for(person, zones), fmt))

    def order(s: MapSpot) -> tuple[int, int, str]:
        group = 1 if s.zone is None else 0
        return (
            group,
            rank[s.zone.id] if s.zone else 0,
            s.person.display_name.casefold(),
        )

    spots.sort(key=order)
    bubbles: list[MapBubble] = []
    by_zone: dict[str, list[MapSpot]] = {}
    for s in spots:
        if s.zone is not None:
            by_zone.setdefault(s.zone.id, []).append(s)
    for zone_id, members in by_zone.items():
        bubbles.append(_bubble(f"zone:{zone_id}", members, fmt))
    for s in spots:
        if s.zone is None:
            bubbles.append(_bubble(f"person:{s.person.user_id}", [s], fmt))
    # Spoken: one phrase per zone, then each other place, everyone away
    # together at the end.
    groups: dict[str, list[MapSpot]] = {}
    for s in spots:
        if s.zone is not None:
            key = f"zone:{s.zone.id}"
        elif s.icon == AWAY_ICON:
            key = "away"
        else:
            key = f"place:{s.icon}{s.place}"
        groups.setdefault(key, []).append(s)
    away = groups.pop("away", None)
    ordered = [("at", g) for g in groups.values()]
    if away:
        ordered.append(("away", away))
    occupied = set(by_zone)
    pins = tuple(
        MapPin(zone=z, icon=pin_icon(z), occupied=z.id in occupied)
        for z in sorted(zones, key=_zone_order)
    )
    return MapView(
        spots=tuple(spots),
        bubbles=tuple(bubbles),
        pins=pins,
        label=_summary(ordered, fmt),
    )


def _zone_order(zone: DashboardZone) -> tuple[bool, str]:
    """Home first, then A→Z."""
    return (not zone.is_home, zone.name.casefold())
