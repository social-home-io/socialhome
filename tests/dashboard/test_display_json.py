"""Tests for the wall-panel JSON spelling (:mod:`socialhome.dashboard.display_json`).

Pure: built from the factories' snapshots with the real catalog (so the
wording is the tablet's own).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from string import Formatter
from zoneinfo import ZoneInfo

import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.boards import board_base, home_board, person_board
from socialhome.dashboard.display_json import (
    STRING_KEYS,
    avatar_path,
    board_header,
    css_palettes,
    display_clock,
    emoji,
    home_payload,
    lock_payload,
    map_payload,
    messages_payload,
    notes_payload,
    person_payload,
    theme_payload,
)
from socialhome.dashboard.presenter import home_colors, person_colors
from socialhome.dashboard.render import STATIC_DIR, DashboardRenderer
from socialhome.dashboard.tabs import TAB_MAP, TAB_MESSAGES, TAB_NOTES
from socialhome.domain.calendar import DaySpan
from socialhome.domain.household_dashboard import (
    SUPPORTED_DASHBOARD_LOCALES,
    DashboardSettings,
    DashboardShoppingItem,
    DashboardTask,
)
from socialhome.domain.theme import HouseholdTheme
from socialhome.i18n import Catalog

from .factories import (
    ANNA,
    LEO,
    MENTIONS,
    MESSAGES,
    PASCAL,
    TOMORROW,
    WORK,
    at,
    event,
    home,
    leo_page,
    map_home,
    mia_page,
    person,
)

MESSAGES_DIR = Path(i18n_pkg.__file__).parent / "messages"
RENDERER = DashboardRenderer(Catalog.from_directory(MESSAGES_DIR))
T = RENDERER.translator("en")
SETTINGS = DashboardSettings()


def _home(snap, tab="overview", t=T):
    base = board_base(snap, people=snap.people, colors=home_colors(snap), t=t)
    clock = display_clock(snap.now.timestamp(), snap.now.tzinfo, t)
    header = board_header(
        view=tab,
        base=base,
        sections=snap.sections,
        settings=SETTINGS,
        household="H",
        clock=clock,
        view_key="k",
    )
    board = home_board(snap, base.fmt, tab)
    return header, board, base, clock


def _overview(snap):
    return home_payload(*_home(snap))["overview"]


def _person(page):
    base = board_base(
        page,
        people=page.people or (page.person,),
        colors=person_colors(page),
        t=T,
        own_color=page.person.color,
    )
    clock = display_clock(page.now.timestamp(), page.now.tzinfo, T)
    header = board_header(
        view="person",
        base=base,
        sections=page.sections,
        settings=SETTINGS,
        household="H",
        clock=clock,
        view_key="k",
        current=page.person.user_id,
    )
    return person_payload(header, person_board(page, base.fmt), base, clock)


# ── Small spellings ──────────────────────────────────────────────────────


def test_emoji_ref():
    assert emoji("🏊") == {"text": "🏊", "code": "1f3ca"}
    assert emoji("W") == {"text": "W", "code": None}
    assert emoji("") is None and emoji(None) is None


def test_display_clock_truncates_to_the_minute_with_offset():
    now = at(14, 32).timestamp() + 41
    clock = display_clock(now, ZoneInfo("Europe/Berlin"), T)
    assert clock["time"] == "14:32"
    assert clock["iso"] == "2026-10-12T14:32:00+02:00"
    assert clock["unix"] % 60 == 0
    assert clock["utc_offset_minutes"] == 120
    assert clock["date"] == "Monday, October 12"
    west = display_clock(now, ZoneInfo("America/St_Johns"), T)
    assert west["utc_offset_minutes"] == -150
    assert display_clock(now, UTC, T)["utc_offset_minutes"] == 0


def test_avatar_path_quotes_ids():
    assert avatar_path(ANNA) == "/api/display/v1/avatar/u-anna?v=pic1"
    assert avatar_path(PASCAL) is None
    odd = replace(ANNA, user_id="a/b c", picture_hash="h?x")
    assert avatar_path(odd) == "/api/display/v1/avatar/a%2Fb%20c?v=h%3Fx"


# ── Home ─────────────────────────────────────────────────────────────────


def test_header_people_and_badges():
    header, *_ = _home(home(mentions=MENTIONS))
    people = {p["id"]: p for p in header["people"]}
    assert people["u-leo"]["badge"] == 1
    assert people["u-leo"]["badge_label"] == "New message for Leo"
    assert people["u-anna"]["where"] == {
        "icon": {"text": "📍", "code": "1f4cd"},
        "label": "Work",
    }
    assert people["u-anna"]["avatar"].endswith("?v=pic1")
    no_presence = home(sections=frozenset({"calendar"}))
    header, *_ = _home(no_presence)
    assert all(p["where"] is None for p in header["people"])


def test_overview_sections():
    ov = _overview(home())
    assert ov["next"]["kind"] == "next" and ov["next"]["rel"] == "in 28 min"
    assert ov["shopping"]["label"] == "Shopping · 11"
    assert ov["shopping"]["more"] == "+ 5 more"
    assert len(ov["shopping"]["items"]) == 6
    assert ov["notes"]["label"] == "Notes · 2"
    bad = next(n for n in ov["notes"]["items"] if n["text"] == "Bad colour")
    assert bad["color"] is None and bad["light"] is None
    good = next(n for n in ov["notes"]["items"] if n["text"] == "Plumber Tue 9am")
    assert good["light"]["bg"].startswith("#") and good["dark"]["ink"].startswith("#")
    assert ov["today"]["mode"] == "timeline"
    changes = ov["today"]["changes"]["items"]
    cancelled = next(c for c in changes if c["status"] == "cancelled")
    assert cancelled["meta"] is None and cancelled["who"] == "Mia"
    late = [c for c in ov["chores"]["items"] if c["late"]]
    assert late and all(c["due"] is None for c in ov["chores"]["items"])


def test_overview_tomorrow_band_and_empty_day():
    ov = _overview(home(next_up=None, events_today=(), school=()))
    assert ov["next"]["kind"] == "tomorrow"
    assert ov["next"]["title"] == "School" and ov["next"]["icon"]["code"] == "1f392"
    assert ov["today"]["mode"] == "empty"
    assert ov["today"]["empty"] == "Nothing planned today"


def test_overview_list_mode_for_a_crowd():
    crowd = tuple(
        replace(PASCAL, user_id=f"u-{i}", display_name=f"P{i}") for i in range(7)
    )
    ov = _overview(home(people=crowd))
    assert ov["today"]["mode"] == "list" and ov["today"]["timeline"] is None
    assert ov["today"]["list"][0]["time"] == "All day"


def test_overview_hides_off_sections():
    snap = home(
        sections=frozenset({"shopping"}),
        shopping=(DashboardShoppingItem(id="s", text="Milk", store="Secret"),),
        shopping_total=1,
    )
    ov = _overview(snap)
    assert ov["today"] is None and ov["chores"] is None and ov["notes"] is None
    assert ov["presence"] is None
    assert ov["shopping"] == {"label": "Shopping · 1", "items": ["Milk"], "more": None}


def test_tabs_badge_and_paths():
    snap = map_home(messages=MESSAGES, messages_on=True)
    payload = home_payload(*_home(snap))
    tabs = {t["key"]: t for t in payload["tabs"]}
    assert tabs["overview"]["path"] == "/api/display/v1/home"
    assert tabs["map"]["path"] == "/api/display/v1/map"
    assert tabs["messages"]["badge"] == 1 and tabs["messages"]["badge_label"] == "1 new"
    on_messages = messages_payload(*_home(snap, TAB_MESSAGES))
    assert {t["key"]: t for t in on_messages["tabs"]}["messages"]["badge"] == 0


def test_messages_tab_rows_and_empty():
    snap = home(messages=MESSAGES, messages_on=True)
    days = messages_payload(*_home(snap, TAB_MESSAGES))["messages"]
    assert days["empty"] is None
    row = days["days"][0]["rows"][0]
    assert row["time"] == "14:28" and row["rest"] is False
    assert row["playing"] == "Reading Anna's message aloud"
    empty = messages_payload(*_home(home(messages_on=True), TAB_MESSAGES))
    assert empty["messages"]["empty"]["title"] == "No messages in the last day"
    # The only message is on an @here banner: no "no messages" claim.
    bannered = home(messages_on=True, mentions=MENTIONS[:1])
    assert messages_payload(*_home(bannered, TAB_MESSAGES))["messages"]["empty"] is None


def test_notes_tab_more_and_empty():
    full = notes_payload(*_home(home(stickies_total=9), TAB_NOTES))["notes"]
    assert full["more"] == "+ 7 more" and full["empty"] is None
    empty = notes_payload(*_home(home(stickies=(), stickies_total=0), TAB_NOTES))
    assert empty["notes"]["empty"]["icon"]["code"] == "1f4dd"
    assert empty["notes"]["more"] is None


def test_map_payload():
    mp = map_payload(*_home(map_home(), TAB_MAP), "© OSM")["map"]
    assert mp["attribution"] == "© OSM" and mp["title"] == "Who's where"
    spots = {s["person"]: s for s in mp["spots"]}
    # In a zone: drawn at the zone, never their own point.
    assert spots["u-pascal"]["zone"] == "hz_home" and spots["u-pascal"]["lat"] is None
    assert spots["u-anna"]["zone"] is None and spots["u-anna"]["lat"] == 52.5232
    assert spots["u-anna"]["age_label"] == "updated 5 min ago"
    pins = {p["id"]: p for p in mp["pins"]}
    assert pins["hz_work"]["icon"] == {"text": "W", "code": None}
    gone = replace(
        LEO, located_at=at(10), presence="away", latitude=52.5, longitude=13.4
    )
    stale = map_home(people=(gone,))
    spot = map_payload(*_home(stale, TAB_MAP), "x")["map"]["spots"][0]
    assert spot["stale"] and spot["age_label"] == "last seen 4 h ago"


def test_night_home():
    payload = home_payload(*_home(home(is_night=True)))
    night = payload["night"]
    assert night["item"]["when"] == "tomorrow" and night["item"]["title"] == "School"
    assert night["wake_hint"] == "Tap to show the dashboard"


# ── A person ─────────────────────────────────────────────────────────────


def test_reader_page():
    p = _person(person(mentions=(replace(MENTIONS[1], person_id="u-anna"),)))
    assert p["kind"] == "reader" and p["picture"] is None
    assert p["person"]["status_emoji"] == {"text": "😴", "code": "1f634"}
    assert [m["label"] for m in p["mentions"]] == ["Message for you"]
    assert p["mentions"][0]["from_label"] == "Message from Pascal for you"
    r = p["reader"]
    assert r["now"]["until"] == "until 17:00"
    assert r["next"]["rel"].startswith("in ")
    assert r["day"]["tomorrow"]["rows"][0]["time"] == "All day"
    assert r["chores"]["label"] == "Chores"


def test_agenda_rows_add_the_days_of_a_multi_day_event():
    """Additive for the panel (v1 stays compatible): ``days`` spells a
    multi-day event as one line and ``day_count`` says how many; ``time``
    keeps the clock span it always carried."""
    trip = event(
        "t",
        "Camp",
        at(18),
        at(10, day=TOMORROW + timedelta(days=1)),
        "u-anna",
        days=DaySpan(TOMORROW - timedelta(days=1), TOMORROW + timedelta(days=1)),
    )
    r = _person(person(today_items=(trip,), now_item=None, next_item=None))["reader"]
    row = r["day"]["rows"][0]
    assert row["days"] == "Mon 12 – Wed 14 · 3 days"
    assert row["day_count"] == 3
    assert row["time"] == "18:00–10:00"
    plain = _person(person())["reader"]["day"]["tomorrow"]["rows"][0]
    assert plain["days"] is None and plain["day_count"] == 1
    # Running now, but standing context: "now" nowhere, past nowhere.
    running = _person(
        person(now=at(20), today_items=(trip,), tomorrow_items=(trip,), next_item=None)
    )["reader"]["day"]
    assert running["rows"][0]["now"] is False
    assert running["rows"][0]["past"] is False
    assert running["tomorrow"]["rows"][0]["now"] is False
    # A one-day event running now still is "now" today — never tomorrow.
    work_now = _person(person(tomorrow_items=(WORK,)))["reader"]["day"]
    assert any(r["now"] for r in work_now["rows"])
    assert work_now["tomorrow"]["rows"][0]["now"] is False


def test_reader_due_label():
    chore = DashboardTask(
        id="c", title="Bins", due=TOMORROW, overdue_days=0, done=False, person_ids=()
    )
    r = _person(person(chores=(chore,)))["reader"]
    assert r["chores"]["items"][0]["due"] == "tomorrow"


def test_school_day_reader():
    r = _person(mia_page())["reader"]
    assert r["school"]["label"] == "School today"
    statuses = {row["status"] for row in r["school"]["rows"]}
    assert {"cancelled", "changed", "added"} <= statuses
    breaks = [row for row in r["school"]["rows"] if row["is_break"]]
    assert breaks and breaks[0]["title"] == "Break"
    first = r["day"]["tomorrow"]["rows"][-1]
    assert first["kind"] == "school" and first["title"] == "First lesson"


def test_reader_empty_day():
    r = _person(person(today_items=(), now_item=None, next_item=None))["reader"]
    assert r["day"]["empty"] == "Nothing planned today" and r["now"] is None


def test_reader_night():
    late = event("x", "Run", at(7, day=TOMORROW), at(8, day=TOMORROW), "u-anna")
    p = _person(person(is_night=True, now=at(22), tomorrow_items=(late,)))
    assert p["night"]["item"]["title"] == "Run"
    assert p["night"]["item"]["people"] == []  # the page is theirs


def test_picture_page():
    p = _person(leo_page(messages=MESSAGES, mentions=MENTIONS))
    assert p["kind"] == "picture" and p["reader"] is None
    pv = p["picture"]
    assert pv["clock"]["label"] == "It's 10:30"
    assert pv["clock"]["hour_deg"] == 315.0 and pv["clock"]["minute_deg"] == 180.0
    assert pv["clock"]["wedge"]["full"] is True
    # Events are painted in the page owner's colour.
    assert pv["clock"]["wedge"]["color"] == "#ad780c"
    assert pv["now"]["caption"] == "Kunst · 10:00"
    cancelled = [
        x for part in pv["day"]["parts"] for x in part["tiles"] if x["cancelled"]
    ]
    assert cancelled and cancelled[0]["label"].endswith(", cancelled")
    assert pv["jobs"]["items"][1] == {
        "icon": {"text": "🦷", "code": "1f9b7"},
        "label": "Done: Zähneputzen",
        "caption": "Zähneputzen",
        "done": True,
    }
    assert pv["where"]["home"]["people"] == ["u-pascal", "u-mia"]
    assert len(pv["messages"]) == 3 and pv["night"] is None


def test_picture_night_and_free_time():
    night = _person(leo_page(is_night=True, now=at(21)))["picture"]["night"]
    assert night["when"] == "tomorrow" and night["moon"]["code"] == "1f319"
    free = _person(leo_page(now_item=None))["picture"]["now"]
    assert free["caption"] == "Free time"


def test_picture_empty_day():
    pv = _person(leo_page(today_items=(), now_item=None, next_item=None))["picture"]
    assert pv["day"]["empty"] is True and pv["day"]["parts"] == []
    assert pv["day"]["empty_label"] == "Nothing planned today"


def test_german_wording():
    t = RENDERER.translator("de")
    ov = home_payload(*_home(home(), t=t))["overview"]
    assert ov["next"]["rel"] == t("dashboard.rel.in_minutes", n=28)
    assert ov["chores"]["label"].startswith(t("dashboard.home.chores"))


# ── Lock + theme ─────────────────────────────────────────────────────────


def test_lock_payload_is_minimal():
    clock = display_clock(at(9).timestamp(), ZoneInfo("Europe/Berlin"), T)
    payload = lock_payload(
        household="H", locale="en", clock=clock, is_night=False, ha_unlock=False, t=T
    )
    assert set(payload) == {
        "api_version",
        "locked",
        "household",
        "locale",
        "dir",
        "clock",
        "is_night",
        "ha_unlock",
        "strings",
    }
    assert payload["is_night"] is False
    assert payload["ha_unlock"] is False
    assert payload["strings"] == {
        "locked": "Screen locked",
        "pin_button": "PIN",
        "enter_pin": "Enter PIN",
        "unlock": "Unlock",
        "delete": "Delete last digit",
        "wrong_pin": "Wrong PIN",
        "tries_left": "Tries left: {n}",
        "last_try": "Last try — one more wrong PIN signs this screen out.",
        "forgot_pin": (
            "Forgot the PIN? An admin can change it in Social Home under "
            "Admin › Dashboard."
        ),
    }


def test_lock_payload_at_night():
    clock = display_clock(at(9).timestamp(), ZoneInfo("Europe/Berlin"), T)
    payload = lock_payload(
        household="H", locale="en", clock=clock, is_night=True, ha_unlock=False, t=T
    )
    assert payload["is_night"] is True


def test_lock_payload_says_whether_home_assistant_may_unlock():
    clock = display_clock(at(9).timestamp(), ZoneInfo("Europe/Berlin"), T)
    payload = lock_payload(
        household="H", locale="en", clock=clock, is_night=False, ha_unlock=True, t=T
    )
    assert payload["ha_unlock"] is True


@pytest.mark.parametrize("allowed", [False, True])
def test_board_settings_say_whether_home_assistant_may_unlock(allowed):
    snap = home()
    base = board_base(snap, people=snap.people, colors=home_colors(snap), t=T)
    header = board_header(
        view="overview",
        base=base,
        sections=snap.sections,
        settings=DashboardSettings(panel_unlock_from_ha=allowed),
        household="H",
        clock=display_clock(snap.now.timestamp(), snap.now.tzinfo, T),
        view_key="k",
    )
    assert header["settings"]["ha_unlock"] is allowed


def _css() -> str:
    return (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")


def test_css_palettes_read_dashboard_css():
    palettes = css_palettes(_css())
    for name in ("light", "dark", "night"):
        assert len(palettes[name]) == 10, name
    assert palettes["light"]["bg"] == "#f4f2ee"
    assert palettes["dark"]["accent"] == "#6cc3a8"
    assert palettes["night"]["bg"] == "#000000"
    assert css_palettes("") == {"light": {}, "dark": {}, "night": {}}


def test_theme_payload():
    payload = theme_payload(css=_css(), theme=None, locale="en", t=T)
    assert payload["household_colors"] == {"primary": None, "accent": None}
    assert payload["mode"] == "auto" and payload["font"]["household"] is None
    assert set(payload["strings"]) == set(STRING_KEYS)
    assert payload["strings"]["offline"] == (
        "Can't reach Social Home — showing info from {time}"
    )
    light = theme_payload(
        css=_css(), theme=HouseholdTheme(primary_color="#f0e68c"), locale="en", t=T
    )
    # Pale khaki reads on dark surfaces only.
    assert light["colors"]["dark"]["accent"] == "#f0e68c"
    assert light["colors"]["light"]["accent"] == "#2f6f5e"


#: The firmware's setup / settings / PIN-pad strings (its English
#: built-ins until the server's arrive) and the placeholders each keeps.
PANEL_STRINGS = {
    "wifi_title": set(),
    "wifi_text": set(),
    "wifi_waiting": set(),
    "improv_title": set(),
    "improv_allow": set(),
    "server_title": set(),
    "server_text": set(),
    "server_text_found": set(),
    "found_title": set(),
    "found_looking": set(),
    "found_none": set(),
    "server_placeholder": set(),
    "server_invalid": set(),
    "server_ha": set(),
    "connect": set(),
    "pair_unreachable": set(),
    "pair_busy": set(),
    "claim_title": set(),
    "claim_expired": set(),
    "claim_disabled": set(),
    "claim_busy": set(),
    "claim_unreachable": {"host"},
    "claim_sealed": set(),
    "ok": set(),
    "change_server": set(),
    "update_title": set(),
    "update_text": set(),
    "map_placeholder": set(),
    "map_none": set(),
    "settings_title": set(),
    "settings_server": set(),
    "settings_panel": set(),
    "settings_firmware": set(),
    "forget": set(),
    "forget_confirm": set(),
    "cancel": set(),
    "close": set(),
    "credits": set(),
    "pin_remaining": {"n"},
    "pin_wait": {"n"},
    "pin_offline": set(),
}


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in Formatter().parse(text) if f}


def test_every_string_key_is_in_every_catalog():
    """No theme string falls back to English (or to the raw key)."""
    for locale in SUPPORTED_DASHBOARD_LOCALES:
        path = MESSAGES_DIR / f"{locale}.json"
        messages = json.loads(path.read_text(encoding="utf-8"))
        for name, key in STRING_KEYS.items():
            assert messages.get(key, "").strip(), (locale, name, key)


def test_theme_carries_the_panel_strings_in_every_locale():
    assert set(PANEL_STRINGS) <= set(STRING_KEYS)
    for locale in SUPPORTED_DASHBOARD_LOCALES:
        t = RENDERER.translator(locale)
        strings = theme_payload(css=_css(), theme=None, locale=locale, t=t)["strings"]
        for name, fields in PANEL_STRINGS.items():
            text = strings[name]
            assert text.strip() and text != STRING_KEYS[name], (locale, name)
            # The placeholders reach the panel literally, once each.
            assert _fields(text) == fields, (locale, name, text)
            for field in fields:
                assert text.count("{" + field + "}") == 1, (locale, name, text)
        # "Social Home" is a name, never translated.
        assert strings["settings_server"] == "Social Home", locale
        assert "Social Home" in strings["pin_offline"], locale
        # The Wi-Fi screen tells people to tap the Improv prompt's button
        # by the name the panel draws on it.
        assert strings["improv_allow"] in strings["wifi_text"], locale
        assert "Home Assistant App" in strings["found_none"], locale


#: The SPA's own catalogs — the pair hint names the admin's labels.
SPA_LOCALES_DIR = Path(__file__).parents[2] / "client" / "src" / "i18n" / "locales"


def test_credits_name_roboto_under_the_sil_ofl_in_every_locale():
    """Roboto (bundled under ``dashboard/fonts/`` with ``OFL.txt``, and
    embedded in the panel firmware) is SIL OFL 1.1 — not Apache 2.0."""
    for locale in SUPPORTED_DASHBOARD_LOCALES:
        t = RENDERER.translator(locale)
        credits = theme_payload(css=_css(), theme=None, locale=locale, t=t)["strings"][
            "credits"
        ]
        assert "Roboto, SIL OFL 1.1" in credits, (locale, credits)
        assert "Apache" not in credits, (locale, credits)
        for part in ("Twemoji", "CC-BY 4.0", "MPL-2.0", "GPLv3", "LVGL (MIT)"):
            assert part in credits, (locale, part, credits)


def test_credits_english_matches_the_firmware_built_in():
    strings = theme_payload(css=_css(), theme=None, locale="en", t=T)["strings"]
    assert strings["credits"] == (
        "Emoji: Twemoji © Twitter, Inc and other contributors, "
        "© jdecked and other contributors, CC-BY 4.0. Font: Roboto, SIL OFL 1.1. "
        "Firmware: MPL-2.0, built on ESPHome (GPLv3) and LVGL (MIT)."
    )


def test_bundled_roboto_ships_its_ofl_licence():
    fonts = STATIC_DIR.parent / "fonts"
    assert "SIL Open Font License, Version 1.1" in (fonts / "OFL.txt").read_text(
        encoding="utf-8"
    )


def test_pair_hint_walks_to_the_add_panel_button_in_every_locale():
    """The hint names the exact Admin › Dashboard › Wall panels › Add
    labels the SPA shows in the admin's language."""
    for locale in SUPPORTED_DASHBOARD_LOCALES:
        spa = json.loads(
            (SPA_LOCALES_DIR / f"{locale}.json").read_text(encoding="utf-8")
        )
        t = RENDERER.translator(locale)
        hint = theme_payload(css=_css(), theme=None, locale=locale, t=t)["strings"][
            "pair_hint"
        ]
        path = " › ".join(
            (
                spa["admin.tab.dashboard"],
                spa["admin.dashboard.panels.heading"],
                spa["admin.dashboard.panels.add"],
            )
        )
        assert path in hint, (locale, hint, path)


def test_theme_panel_strings_are_the_firmware_english():
    strings = theme_payload(css=_css(), theme=None, locale="en", t=T)["strings"]
    # Worded without a plural (the panel fills one number into one
    # string), unlike the firmware's built-in "{n} tries left".
    assert strings["pin_remaining"] == "Tries left: {n}"
    assert strings["pin_wait"] == "Too many tries. Try again in {n} s."
    assert strings["pin_offline"] == "Can't reach Social Home. Try again."
    assert strings["claim_unreachable"] == "Can't reach Social Home at {host}."
    assert strings["wifi_waiting"] == "Waiting for Wi-Fi…"
    assert strings["map_none"] == (
        "No map picture on this Social Home — the list shows who's where."
    )
    assert strings["improv_title"] == "A phone wants to set up Wi-Fi"
    assert strings["improv_allow"] == "Allow"
    assert strings["server_text_found"] == (
        "Tap your Social Home, or type its address. "
        "The dashboard port 8100 is added for you."
    )
    assert strings["found_title"] == "Found on this network"
    assert strings["found_looking"] == "Looking for Social Home on this network…"
    assert strings["found_none"] == (
        "None found yet. Using the Home Assistant App? Type its address below."
    )
    assert strings["claim_sealed"] == (
        "This connect request can't be opened on this panel. "
        "Press Connect in Social Home again."
    )


def test_picture_page_shows_no_status_text():
    """The picture view shows the status emoji only — like its template."""
    shown = replace(LEO, status_emoji="🦖", status_text="Dinosaur day")
    p = _person(leo_page(person=shown))
    assert p["person"]["status_emoji"]["text"] == "🦖"
    assert p["person"]["status_text"] is None
    reader = _person(person(replace(ANNA, status_text="Busy")))
    assert reader["person"]["status_text"] == "Busy"
