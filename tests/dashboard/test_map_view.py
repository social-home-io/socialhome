"""Tests for :mod:`socialhome.dashboard.map_view` — Home's Map tab model."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from socialhome import i18n as i18n_pkg
from socialhome.dashboard.map_view import (
    AWAY_ICON,
    BUBBLE_FACES,
    PLACE_ICON,
    has_map,
    map_view,
    pin_icon,
    zone_for,
)
from socialhome.dashboard.presenter import ColorBook, Fmt
from socialhome.domain.household_dashboard import SECTION_PRESENCE
from socialhome.i18n import Catalog

from .factories import (
    ANNA,
    LEO,
    MIA,
    NOW,
    PASCAL,
    TODAY,
    ZONE_HOME,
    ZONE_SCHOOL,
    ZONE_WORK,
    ZONES,
    located,
    map_home,
)

CATALOG = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")


def _fmt(snap, locale: str = "en") -> Fmt:
    def t(key: str, **kw: object) -> str:
        return CATALOG.gettext(key, locale=locale, **kw)

    return Fmt(now=snap.now, today=TODAY, t=t, people=snap.people, colors=ColorBook())


def _view(**kw):
    snap = map_home(**kw)
    return map_view(snap, _fmt(snap))


# ─── Who is where ────────────────────────────────────────────────────────


def test_zone_for_follows_the_presence_state():
    assert zone_for(located(PASCAL, presence="home"), ZONES) is ZONE_HOME
    school = located(LEO, presence="zone", zone_name="school")  # any case
    assert zone_for(school, ZONES) is ZONE_SCHOOL
    unknown = located(LEO, presence="zone", zone_name="Gym")
    assert zone_for(unknown, ZONES) is None
    # Away is away — a point inside a zone circle doesn't move them in.
    away = located(ANNA, presence="away", lat=52.52, lon=13.405)
    assert zone_for(away, ZONES) is None
    assert zone_for(located(PASCAL, presence="home"), (ZONE_SCHOOL,)) is None


def test_the_list_home_first_then_zones_a_to_z_then_away():
    view = _view()
    assert view is not None
    names = [(s.person.display_name, s.icon, s.place) for s in view.spots]
    assert names == [
        ("Mia", "🏠", "Home"),
        ("Pascal", "🏠", "Home"),
        ("Leo", "🏫", "School"),
        ("Anna", AWAY_ICON, "Away"),
    ]
    assert [s.when for s in view.spots] == [
        "5 min ago",
        "2 min ago",
        "8 min ago",
        "5 min ago",
    ]
    assert not any(s.stale for s in view.spots)


def test_zone_members_sit_at_the_zone_centre_never_their_own_point():
    view = _view()
    assert view is not None
    home, school, anna = view.bubbles
    assert home.key == "zone:hz_home" and home.zone is ZONE_HOME
    assert (home.latitude, home.longitude) == (52.52, 13.405)
    assert [s.person.user_id for s in home.spots] == ["u-mia", "u-pascal"]
    # Pascal shared a point, but inside Home only the zone is drawn.
    assert all(s.latitude is None for s in home.spots)
    assert school.key == "zone:hz_school" and len(school.spots) == 1
    assert anna.key == "person:u-anna" and anna.zone is None
    assert (anna.latitude, anna.longitude) == (52.5232, 13.3961)
    assert anna.spots[0].latitude == 52.5232
    assert home.caption == "🏠 Home" and anna.caption == "🚗 Away"
    assert anna.away and not home.away and not school.away
    assert anna.label == "Anna · 🚗 Away"
    assert home.label == "Mia, Pascal · 🏠 Home"


def test_every_zone_is_a_pin_occupied_or_not():
    view = _view()
    assert view is not None
    assert [(p.zone.id, p.icon, p.occupied) for p in view.pins] == [
        ("hz_home", "🏠", True),
        ("hz_school", "🏫", True),
        ("hz_work", "W", False),  # no icon: its first letter
    ]


def test_pin_icon_falls_back_to_the_first_letter():
    assert pin_icon(ZONE_HOME) == "🏠"
    assert pin_icon(replace(ZONE_WORK, name="élan")) == "É"
    assert pin_icon(replace(ZONE_WORK, name="")) == PLACE_ICON


def test_a_zone_name_without_a_zone_on_the_wall_is_a_free_point():
    """HA put Leo in a zone the household doesn't have (not synced): he is
    drawn at his own point, named after that zone."""
    leo = located(LEO, presence="zone", zone_name="Gym", lat=52.51, lon=13.4)
    view = _view(people=(leo,))
    assert view is not None
    (spot,) = view.spots
    assert (spot.icon, spot.place) == (PLACE_ICON, "Gym")
    assert view.bubbles[0].key == "person:u-leo"
    assert not view.bubbles[0].away  # a named place, not "away"


def test_home_without_a_home_zone_is_a_free_point_called_home():
    pascal = located(PASCAL, presence="home", lat=52.5201, lon=13.4049)
    view = _view(people=(pascal,), zones=(ZONE_SCHOOL,))
    assert view is not None
    assert (view.spots[0].icon, view.spots[0].place) == ("🏠", "Home")
    assert view.bubbles[0].zone is None


def test_people_who_share_nothing_are_not_on_the_map():
    nothing = located(PASCAL, presence="away")  # no point, no zone
    unknown = replace(MIA, located_at=None, latitude=52.5, longitude=13.4)
    leo_gym = located(LEO, presence="zone", zone_name="Gym")  # no point
    view = _view(people=(nothing, unknown, leo_gym, located(ANNA, presence="home")))
    assert view is not None
    assert [s.person.user_id for s in view.spots] == ["u-anna"]


# ─── Freshness ───────────────────────────────────────────────────────────


def test_stale_after_an_hour_gone_after_a_day():
    people = (
        located(PASCAL, 59, presence="home"),
        located(MIA, 125, presence="home"),
        located(ANNA, 24 * 60 + 1, presence="away", lat=52.5, lon=13.4),
    )
    view = _view(people=people)
    assert view is not None
    assert [(s.person.display_name, s.stale, s.when) for s in view.spots] == [
        ("Mia", True, "2 h ago"),
        ("Pascal", False, "59 min ago"),
    ]
    (home,) = view.bubbles
    assert home.stale is False and home.age is None  # Pascal is fresh


def test_a_bubble_of_only_stale_faces_says_its_newest_age():
    people = (located(PASCAL, 70, presence="home"), located(MIA, 190, presence="home"))
    view = _view(people=people)
    assert view is not None
    (home,) = view.bubbles
    assert home.stale is True and home.age == "1 h ago"


def test_a_location_from_the_future_is_just_now():
    view = _view(people=(located(PASCAL, -3, presence="home"),))
    assert view is not None
    assert view.spots[0].when == "just now" and not view.spots[0].stale


def test_has_map_needs_one_fresh_located_person():
    assert has_map(map_home())
    assert not has_map(map_home(people=(located(PASCAL, 24 * 60, presence="home"),)))
    assert not has_map(map_home(people=(PASCAL, MIA)))  # no location at all
    assert has_map(map_home(people=(located(ANNA, presence="away", lat=1, lon=2),)))
    assert not has_map(map_home(sections=frozenset({"calendar"})))
    assert _view(people=()) is None


def test_presence_off_means_no_map_even_with_rows():
    snap = map_home(sections=map_home().sections - {SECTION_PRESENCE})
    assert map_view(snap, _fmt(snap)) is None


# ─── Bubbles ─────────────────────────────────────────────────────────────


def test_a_crowded_zone_keeps_every_face_in_list_order():
    people = tuple(
        located(replace(p, user_id=f"u{i}", display_name=n), presence="home")
        for i, (p, n) in enumerate(
            [
                (PASCAL, "Pascal"),
                (MIA, "Mia"),
                (ANNA, "Ella"),
                (LEO, "Noah"),
                (MIA, "Leo"),
            ]
        )
    )
    view = _view(people=people)
    assert view is not None
    (home,) = view.bubbles
    assert [s.person.display_name for s in home.spots] == [
        "Ella",
        "Leo",
        "Mia",
        "Noah",
        "Pascal",
    ]
    assert BUBBLE_FACES == 3 and home.more == 2


# ─── Spoken summary ──────────────────────────────────────────────────────


def test_the_map_label_says_who_is_where():
    view = _view()
    assert view is not None
    assert view.label == ("Map: Mia and Pascal at Home, Leo at School, Anna away")


def test_the_label_groups_everyone_away_and_lists_three_names():
    people = (
        located(PASCAL, presence="away", lat=52.5, lon=13.4),
        located(MIA, presence="away", lat=52.6, lon=13.4),
        located(ANNA, presence="away", lat=52.7, lon=13.4),
    )
    view = _view(people=people)
    assert view is not None
    assert view.label == "Map: Anna, Mia and Pascal away"


def test_the_label_is_localised():
    snap = map_home()
    view = map_view(snap, _fmt(snap, "de"))
    assert view is not None
    assert view.label == (
        # Zone names are the household's own words; "away" is ours.
        "Karte: Mia und Pascal: Home, Leo: School, Anna unterwegs"
    )
    assert view.spots[-1].place == "Unterwegs"


@pytest.mark.parametrize("minutes", [0, 30])
def test_located_now_is_relative_to_the_snapshot(minutes):
    snap = map_home(now=NOW + timedelta(minutes=minutes))
    view = map_view(snap, _fmt(snap))
    assert view is not None
    assert view.spots[1].when == f"{2 + minutes} min ago"
