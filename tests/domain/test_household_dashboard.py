"""Tests for the household-dashboard domain types."""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, time, timezone

import pytest

from socialhome.domain.errors import CodedError
from socialhome.domain.household_dashboard import (
    normalize_mac,
    MAX_DISPLAYS,
    MAX_PENDING_PAIRINGS,
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_TTL_SECONDS,
    DashboardDisabledError,
    DashboardDisplay,
    DashboardDisplayLimitError,
    DashboardDisplayNotFoundError,
    DashboardPairingCodeInvalidError,
    DashboardPairingThrottledError,
    DisplayPairingStatus,
    MENTION_HERE,
    MENTION_PERSON,
    DashboardMention,
    here_alerts,
    mentioned_ids,
    mentions_for,
    is_pure_emoji,
    leading_emoji,
    DEFAULT_DASHBOARD_PORT,
    IDLE_RETURN_CHOICES,
    MAP_DROP_AFTER,
    MAP_STALE_AFTER,
    DashboardZone,
    MIN_PASSWORD_LENGTH,
    PERSON_PALETTE,
    SETUP_TOKEN_TTL_SECONDS,
    SUPPORTED_DASHBOARD_LOCALES,
    TIMETABLE_COLOR_HEX,
    DashboardEvent,
    DashboardLesson,
    DashboardNotRunningError,
    DashboardPasswordRequiredError,
    DashboardPasswordTooShortError,
    DashboardPerson,
    DashboardPersonSettings,
    DashboardPinInvalidError,
    DashboardPortFixedError,
    DashboardPortInUseError,
    DashboardSchoolDay,
    DashboardSettings,
    DashboardSettingsInvalidError,
    DashboardShoppingItem,
    DashboardSticky,
    DashboardTask,
    HomeSnapshot,
    PersonSnapshot,
    hash_color,
    in_night_window,
    initials,
    lesson_color_token,
    normalize_token_key,
    picture_effective,
    subject_color_tokens,
    token_hex,
)
from socialhome.domain.timetable import (
    TIMETABLE_COLORS,
    EntryKind,
    Timetable,
    TimetableEntry,
)
from socialhome.domain.user import User


def test_constants():
    assert DEFAULT_DASHBOARD_PORT == 8100
    assert IDLE_RETURN_CHOICES == frozenset({0, 1, 3, 10})
    assert SUPPORTED_DASHBOARD_LOCALES == frozenset({"en", "de", "es", "fr", "nl"})
    assert MIN_PASSWORD_LENGTH == 8
    assert SETUP_TOKEN_TTL_SECONDS == 600


def test_settings_defaults():
    s = DashboardSettings()
    assert s.enabled is False
    assert s.port == DEFAULT_DASHBOARD_PORT
    assert s.locale == "en"
    assert s.idle_return_minutes == 3
    assert s.night_start == "22:00"
    assert s.night_end == "06:00"
    assert s.keep_screen_on is True
    assert s.show_status_text is False
    assert s.show_household_messages is False
    # Mentions play a tone by default (admin can turn it off).
    assert s.mention_sound is True
    assert s.address is None
    assert s.people == ()
    assert s.password_set_at is None


def test_settings_are_frozen_and_hashable():
    s = DashboardSettings(people=(("u1", DashboardPersonSettings(show=False)),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.enabled = True  # type: ignore[misc]
    assert hash(s) == hash(
        DashboardSettings(people=(("u1", DashboardPersonSettings(show=False)),))
    )


def test_person_defaults():
    p = DashboardPersonSettings()
    assert p.show is True
    assert p.picture is None


def test_person_lookup_returns_stored_entry():
    stored = DashboardPersonSettings(show=False, picture=True)
    s = DashboardSettings(people=(("u1", stored),))
    assert s.person("u1") == stored


def test_person_lookup_returns_default_when_absent():
    s = DashboardSettings(people=(("u1", DashboardPersonSettings(show=False)),))
    assert s.person("u2") == DashboardPersonSettings()


def test_people_map_view():
    stored = DashboardPersonSettings(show=False)
    s = DashboardSettings(people=(("u1", stored),))
    assert s.people_map() == {"u1": stored}


def test_password_required_error():
    e = DashboardPasswordRequiredError()
    assert isinstance(e, CodedError)
    assert e.status == 422
    assert e.code == "DASHBOARD_PASSWORD_REQUIRED"
    assert e.detail == "Set a dashboard password before enabling the dashboard."


def test_port_fixed_error():
    e = DashboardPortFixedError()
    assert isinstance(e, CodedError)
    assert e.status == 422
    assert e.code == "DASHBOARD_PORT_FIXED"


def test_port_in_use_error():
    e = DashboardPortInUseError(8100)
    assert isinstance(e, CodedError)
    assert e.status == 409
    assert e.code == "DASHBOARD_PORT_IN_USE"
    assert e.params == {"port": 8100}


def test_settings_invalid_error_names_field():
    e = DashboardSettingsInvalidError("port")
    assert isinstance(e, CodedError)
    assert isinstance(e, ValueError)
    assert e.status == 422
    assert e.code == "DASHBOARD_SETTINGS_INVALID"
    assert e.params == {"field": "port"}
    assert "port" in e.detail


def test_password_too_short_error():
    e = DashboardPasswordTooShortError()
    assert isinstance(e, CodedError)
    assert e.status == 422
    assert e.code == "DASHBOARD_PASSWORD_TOO_SHORT"
    assert e.params == {"min": MIN_PASSWORD_LENGTH}


def test_not_running_error():
    exc = DashboardNotRunningError()
    assert isinstance(exc, CodedError)
    assert exc.status == 409
    assert exc.code == "DASHBOARD_NOT_RUNNING"
    assert exc.detail == "Turn the dashboard on before creating a sign-in link."


# ── Snapshot helpers (the dashboard's read model) ────────────────────────


def _user(**kw) -> User:
    base: dict = {"user_id": "u1", "username": "anna", "display_name": "Anna"}
    base.update(kw)
    return User(**base)


@pytest.mark.parametrize(
    ("stored", "minor", "protected", "expected"),
    [
        (None, True, True, True),  # default: child-protected minor → on
        (None, True, False, False),
        (None, False, True, False),
        (None, False, False, False),
        (True, False, False, True),  # an explicit choice always wins
        (False, True, True, False),
    ],
)
def test_picture_effective(stored, minor, protected, expected):
    user = _user(is_minor=minor, child_protection_enabled=protected)
    got = picture_effective(DashboardPersonSettings(picture=stored), user)
    assert got is expected


def test_timetable_color_hex_covers_every_token():
    assert set(TIMETABLE_COLOR_HEX) == set(TIMETABLE_COLORS)
    for value in TIMETABLE_COLOR_HEX.values():
        assert re.fullmatch(r"#[0-9A-F]{6}", value)
    # Mirrors the light-theme ``--sh-tt-<token>-edge`` in app.css.
    assert TIMETABLE_COLOR_HEX["terracotta"] == "#C8562E"
    assert TIMETABLE_COLOR_HEX["coral"] == "#CF5439"


def test_token_hex():
    assert token_hex("sky") == "#357BBB"
    assert token_hex(None) is None
    assert token_hex("not-a-token") is None


def test_person_palette_follows_the_token_order():
    assert PERSON_PALETTE[0] == TIMETABLE_COLOR_HEX["terracotta"]
    assert PERSON_PALETTE[-1] == TIMETABLE_COLOR_HEX["coral"]
    assert len(set(PERSON_PALETTE)) == len(TIMETABLE_COLORS)


def test_normalize_token_key():
    assert normalize_token_key("  Música   Theorie ") == "musica theorie"
    assert normalize_token_key("GÄRDEN") == "garden"


@pytest.mark.parametrize(
    ("name", "token"),
    # Reference values from client/src/utils/tokenColor.ts ``hashColor``.
    [
        ("Mathe", "sand"),
        ("Deutsch", "moss"),
        ("Música", "violet"),
        ("Sport 🏃", "sand"),
        ("", "amber"),
    ],
)
def test_hash_color_matches_the_spa(name, token):
    assert hash_color(name) == token


def _tt(*entries: TimetableEntry) -> Timetable:
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Timetable(
        id="tt1",
        name="5b",
        created_by="u1",
        created_at=at,
        updated_at=at,
        entries=entries,
    )


def _entry(eid: str, title: str | None, color: str | None = None, **kw):
    return TimetableEntry(
        id=eid,
        weekday=0,
        start=time(8, 0),
        end=time(8, 45),
        title=title,
        color=color,
        **kw,
    )


def test_subject_color_tokens_mirror_the_spa():
    tt = _tt(
        _entry("a", "Mathe"),
        _entry("b", "Deutsch", color="terracotta"),  # explicit — not handed out
        _entry("c", "  mathe "),  # same subject
        _entry("d", "Biologie"),
        _entry("e", None),  # untitled slot
        _entry("f", "Pause", kind=EntryKind.BREAK),  # breaks never coloured
    )
    # Sorted distinct titles get the free tokens in palette order,
    # skipping the explicitly used "terracotta".
    assert subject_color_tokens(tt) == {"biologie": "amber", "mathe": "olive"}


def test_subject_color_tokens_fall_back_to_the_hash_past_the_palette():
    titles = [f"subject {i:02d}" for i in range(13)]
    tt = _tt(*(_entry(str(i), t) for i, t in enumerate(titles)))
    colors = subject_color_tokens(tt)
    assert len(colors) == 13
    assert colors["subject 12"] == hash_color("subject 12")


def test_lesson_color_token():
    tt = _tt(_entry("a", "Mathe"))
    subjects = subject_color_tokens(tt)
    assert lesson_color_token("sky", "Mathe", subjects) == "sky"
    assert lesson_color_token(None, "MATHE", subjects) == subjects["mathe"]
    assert lesson_color_token(None, "Kunst", subjects) == hash_color("Kunst")
    assert lesson_color_token(None, "  ", subjects) is None
    assert lesson_color_token(None, None, subjects) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Anna Maria Schmidt", "AM"),
        ("anna", "A"),
        ("  ", "?"),
        ("Émile", "É"),
    ],
)
def test_initials(name, expected):
    assert initials(name) == expected


@pytest.mark.parametrize(
    ("now", "start", "end", "expected"),
    [
        (time(23, 0), "22:00", "06:00", True),  # window wraps midnight
        (time(2, 0), "22:00", "06:00", True),
        (time(6, 0), "22:00", "06:00", False),  # end is exclusive
        (time(22, 0), "22:00", "06:00", True),  # start is inclusive
        (time(12, 0), "22:00", "06:00", False),
        (time(13, 0), "12:00", "14:00", True),  # same-day window
        (time(14, 0), "12:00", "14:00", False),
        (time(11, 59), "12:00", "14:00", False),
        (time(3, 0), "05:00", "05:00", False),  # empty window — never night
        (time(3, 0), "bad", "06:00", False),  # unparsable → never night
    ],
)
def test_in_night_window(now, start, end, expected):
    assert in_night_window(now, start, end) is expected


def test_snapshot_types_are_frozen_and_slotted():
    for cls in (
        DashboardPerson,
        DashboardEvent,
        DashboardTask,
        DashboardLesson,
        DashboardSchoolDay,
        DashboardShoppingItem,
        DashboardSticky,
        DashboardZone,
        HomeSnapshot,
        PersonSnapshot,
    ):
        params = cls.__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen, cls
        assert hasattr(cls, "__slots__"), cls


def test_item_types_tell_events_and_lessons_apart():
    assert DashboardEvent.item_type == "event"
    assert DashboardLesson.item_type == "lesson"


def test_pin_invalid_error():
    e = DashboardPinInvalidError()
    assert isinstance(e, CodedError)
    assert isinstance(e, ValueError)
    assert e.status == 422
    assert e.code == "DASHBOARD_PIN_INVALID"
    assert e.params == {"min": 4, "max": 6}


def test_settings_pin_set_defaults_off():
    assert DashboardSettings().pin_set is False


@pytest.mark.parametrize(
    ("text", "pure"),
    [
        ("🎉", True),
        ("👍🏽", True),
        ("🇩🇪", True),
        ("❤️", True),
        ("👩‍👧", True),
        ("", False),
        ("Ärzt·Termin", False),
        ("🎉🎉", False),
        ("🎉 x", False),
        # A zero-width joiner may only join another emoji — never letters:
        # "🏥‍K‍l‍i‍n‍i‍k" would read "🏥Klinik" on the wall.
        ("🏥\u200dK\u200dl\u200di\u200dn\u200di\u200dk", False),
        ("🏥\u200dK", False),
        ("👨\u200d👩\u200d👧\u200d👦", True),
        ("🧑\u200d🚒", True),
        ("⚽", True),
        ("☀️", True),
        ("⭐", True),
        ("⌚", True),
        ("🀄", True),
        # Letterlike / enclosed alphanumerics show letters or digits — on
        # a page with no words or digits they are not pictures.
        ("©", False),
        ("®", False),
        ("™", False),
        ("Ⓜ️", False),
        ("🅰️", False),
        ("🅿️", False),
        ("🔟", False),
        ("🔠", False),
        ("🔡", False),
        ("🔤", False),
        ("🈁", False),
        ("㊗️", False),
        ("#️⃣", False),
        ("1️⃣", False),
        # Half a flag is a letter in a box.
        ("🇩", False),
        # A joiner may only join a picture.
        ("👩\u200d©", False),
    ],
)
def test_is_pure_emoji(text, pure):
    assert is_pure_emoji(text) is pure


@pytest.mark.parametrize(
    ("text", "lead"),
    [
        ("© 2026 Concert", None),
        ("🅰️ Team", None),
        ("🔟 Uhr", None),
        ("🇩 x", None),
        ("🇩🇪 Deutsch", "🇩🇪"),
        ("⚽️ Training", "⚽️"),
    ],
)
def test_leading_emoji_only_takes_pictures(text, lead):
    assert leading_emoji(text) == lead


def _mention(mid: str, kind: str, person: str | None = None) -> DashboardMention:
    return DashboardMention(
        message_id=mid,
        sender_id="u-anna",
        text="hi",
        sent_at=datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
        kind=kind,
        person_id=person,
    )


def test_mention_helpers_split_here_from_people():
    ms = (
        _mention("a", MENTION_HERE),
        _mention("b", MENTION_PERSON, "u-ben"),
        _mention("c", MENTION_PERSON, "u-dora"),
        _mention("d", MENTION_PERSON, "u-ben"),
    )
    assert [m.message_id for m in here_alerts(ms)] == ["a"]
    assert [m.message_id for m in mentions_for(ms, "u-ben")] == ["b", "d"]
    assert mentions_for(ms, "u-anna") == ()
    assert mentioned_ids(ms) == frozenset({"u-ben", "u-dora"})
    assert mentioned_ids(()) == frozenset()


def test_map_freshness_thresholds():
    """Stale (faded, "last seen …") after an hour; gone after a day."""
    assert MAP_STALE_AFTER.total_seconds() == 3600
    assert MAP_DROP_AFTER.total_seconds() == 24 * 3600


def test_people_and_home_carry_no_location_by_default():
    p = DashboardPerson(
        user_id="u",
        display_name="U",
        initials="U",
        color="#000000",
        picture_hash=None,
        picture_view=False,
    )
    assert (p.latitude, p.longitude, p.located_at) == (None, None, None)
    zone = DashboardZone(
        id="hz",
        name="Home",
        latitude=1.0,
        longitude=2.0,
        radius_m=100,
        color="#3b82f6",
    )
    assert zone.icon is None and zone.is_home is False


# ─── Wall panels ("displays") ────────────────────────────────────────────


def test_pairing_code_alphabet_has_no_lookalikes():
    for confusable in "01ILOU":
        assert confusable not in PAIRING_CODE_ALPHABET
    assert len(set(PAIRING_CODE_ALPHABET)) == len(PAIRING_CODE_ALPHABET)
    assert PAIRING_CODE_ALPHABET == PAIRING_CODE_ALPHABET.upper()
    assert PAIRING_CODE_LENGTH == 6


def test_pairing_limits():
    assert PAIRING_TTL_SECONDS == 600
    assert MAX_PENDING_PAIRINGS == 32
    assert MAX_DISPLAYS == 20


def test_display_is_frozen_and_defaults():
    d = DashboardDisplay(
        id="d1",
        name="Kitchen",
        token_hash="ab" * 32,
        created_at="2026-10-13T08:00:00+00:00",
    )
    assert d.last_seen_at is None
    assert d.firmware is None
    assert d.model is None
    assert d.locked is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.name = "x"  # type: ignore[misc]


def test_display_repr_hides_the_token_hash():
    d = DashboardDisplay(
        id="d1",
        name="Kitchen",
        token_hash="ab" * 32,
        created_at="2026-10-13T08:00:00+00:00",
    )
    assert "ab" * 32 not in repr(d)


@pytest.mark.parametrize(
    ("err", "status", "code", "params"),
    [
        (DashboardDisabledError(), 409, "DASHBOARD_DISABLED", {}),
        (DashboardPairingCodeInvalidError(), 404, "DASHBOARD_PAIRING_CODE_INVALID", {}),
        (DashboardDisplayNotFoundError(), 404, "DASHBOARD_DISPLAY_NOT_FOUND", {}),
        (DashboardDisplayLimitError(), 409, "DASHBOARD_DISPLAY_LIMIT", {"max": 20}),
        (
            DashboardPairingThrottledError(90),
            429,
            "DASHBOARD_PAIRING_THROTTLED",
            {"retry_after": 90},
        ),
    ],
)
def test_display_errors_are_coded(err, status, code, params):
    assert isinstance(err, CodedError)
    assert err.status == status
    assert err.code == code
    assert err.params == params


def test_pairing_status_repr_hides_the_token():
    st = DisplayPairingStatus(paired=True, token="s3cret-token", display_id="d1")
    assert "s3cret-token" not in repr(st)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff"),
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
        ("aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee:ff"),
        (" AABBCCDDEEFF ", "aa:bb:cc:dd:ee:ff"),
        ("aa:bb:cc:dd:ee", None),
        ("aa:bb:cc:dd:ee:fg", None),
        ("aab:bc:cd:de:ef:f0", None),
        ("aa:bb-cc:dd:ee:ff", None),
        ("", None),
        (None, None),
        (0xAABBCCDDEEFF, None),
    ],
)
def test_normalize_mac(raw, expected):
    """Home Assistant's device registry spells a MAC as ``format_mac`` does
    (``homeassistant/helpers/device_registry.py``): lower case, ``:``."""
    assert normalize_mac(raw) == expected
