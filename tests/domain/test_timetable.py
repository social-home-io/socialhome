"""Tests for the pure timetable domain module (socialhome.domain.timetable)."""

from __future__ import annotations

import copy
import itertools
from datetime import date, datetime, time, timedelta, timezone

import orjson
import pytest

from socialhome.domain import timetable as tt_mod
from socialhome.domain.timetable import (
    MAX_WIRE_BYTES,
    OVERRIDE_RETENTION_DAYS,
    TIMETABLE_COLORS,
    WEEK_START_MONDAY,
    WEEK_START_SUNDAY,
    WIRE_SCHEMA,
    EntryKind,
    LessonStatus,
    OverrideKind,
    Timetable,
    TimetableConflictError,
    TimetableDefaults,
    TimetableEntry,
    TimetableLimitError,
    TimetableOrphanError,
    TimetableOverride,
    TimetableValidationError,
    TimetableValidity,
    clear_week,
    copy_day,
    defaults_from_dict,
    defaults_to_dict,
    entry_from_dict,
    entry_patch,
    entry_to_dict,
    from_wire_dict,
    generate_day,
    normalize_name,
    override_from_dict,
    override_patch,
    override_to_dict,
    prune_overrides,
    resolve_day,
    resolve_week,
    shift_after,
    to_wire_dict,
    validate,
    week_anchor,
    with_entries,
    with_entry,
    with_header,
    with_override,
    with_validity,
    without_entry,
    without_override,
)

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

# 2026-09-28 is a Monday.
MON = date(2026, 9, 28)
TUE = MON + timedelta(days=1)
WED = MON + timedelta(days=2)
FRI = MON + timedelta(days=4)
SAT = MON + timedelta(days=5)
SUN = MON + timedelta(days=6)


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def ent(
    eid: str,
    weekday: int,
    start: str,
    end: str,
    **kw,
) -> TimetableEntry:
    return TimetableEntry(id=eid, weekday=weekday, start=hm(start), end=hm(end), **kw)


def tt_(**kw) -> Timetable:
    base = dict(
        id="tt-1",
        name="Anna 5b",
        created_by="uid-alice",
        created_at=T0,
        updated_at=T0,
    )
    base.update(kw)
    return Timetable(**base)


def std() -> Timetable:
    """Mon: 1.+2. lesson and a break; Tue: one lesson."""
    return tt_(
        entries=(
            ent("m1", 0, "08:00", "08:45", label="1.", title="Mathe"),
            ent("m2", 0, "08:50", "09:35", label="2.", title="Deutsch"),
            ent("mb", 0, "09:35", "09:55", kind=EntryKind.BREAK, title="Pause"),
            ent("t1", 1, "08:00", "08:45", title="Sport"),
        )
    )


# ─── week_anchor ─────────────────────────────────────────────────────────


class TestWeekAnchor:
    def test_constants(self):
        assert WEEK_START_MONDAY == 0
        assert WEEK_START_SUNDAY == 6

    @pytest.mark.parametrize("offset", range(7))
    def test_monday_start_every_day_maps_to_monday(self, offset):
        assert week_anchor(MON + timedelta(days=offset), 0) == MON

    def test_sunday_under_monday_start_belongs_to_previous_monday(self):
        assert week_anchor(SUN, 0) == MON

    def test_sunday_under_sunday_start_is_its_own_anchor(self):
        assert week_anchor(SUN, 6) == SUN

    def test_saturday_under_sunday_start_goes_back_to_previous_sunday(self):
        assert week_anchor(SAT, 6) == MON - timedelta(days=1)

    def test_monday_under_sunday_start(self):
        assert week_anchor(MON, 6) == MON - timedelta(days=1)

    def test_anchor_weekday_matches_week_start(self):
        for d in (MON + timedelta(days=i) for i in range(14)):
            assert week_anchor(d, 0).weekday() == 0
            assert week_anchor(d, 6).weekday() == 6

    def test_invalid_week_start_rejected(self):
        with pytest.raises(TimetableValidationError):
            week_anchor(MON, 3)


# ─── normalize_name ──────────────────────────────────────────────────────


class TestNormalizeName:
    def test_trims(self):
        assert normalize_name("  Anna 5b \n") == "Anna 5b"

    def test_empty_rejected(self):
        with pytest.raises(TimetableValidationError):
            normalize_name("   ")

    def test_max_length(self):
        assert normalize_name("x" * 60) == "x" * 60
        with pytest.raises(TimetableValidationError):
            normalize_name("x" * 61)

    def test_non_string_rejected(self):
        with pytest.raises(TimetableValidationError):
            normalize_name(None)  # type: ignore[arg-type]


# ─── validate ────────────────────────────────────────────────────────────


def _bad(tt: Timetable, match: str | None = None) -> None:
    with pytest.raises(TimetableValidationError, match=match):
        validate(tt)


class TestValidateHeader:
    def test_default_and_standard_are_valid(self):
        validate(tt_())
        validate(std())

    def test_name_untrimmed_rejected(self):
        _bad(tt_(name=" Anna "), "name")

    def test_name_empty_rejected(self):
        _bad(tt_(name=""), "name")

    def test_week_start(self):
        validate(tt_(week_start=6))
        _bad(tt_(week_start=1), "week_start")

    def test_tz(self):
        validate(tt_(tz="Europe/Berlin"))
        _bad(tt_(tz="Foo/Bar"), "tz")
        _bad(tt_(tz="../etc/passwd"), "tz")

    def test_color(self):
        for c in TIMETABLE_COLORS:
            validate(tt_(color=c))
        _bad(tt_(color="#ff0000"), "color")
        _bad(tt_(color="red"), "color")

    def test_colors_are_theme_tokens(self):
        assert len(TIMETABLE_COLORS) == 12
        assert not any(c.startswith("#") for c in TIMETABLE_COLORS)

    @pytest.mark.parametrize(
        "days",
        [(), (0, 0), (0, 7), (-1, 0), (2, 1)],
        ids=["empty", "dup", "high", "negative", "unsorted"],
    )
    def test_days_rejected(self, days):
        _bad(tt_(days=days), "days")

    def test_days_full_week_ok(self):
        validate(tt_(days=tuple(range(7))))

    @pytest.mark.parametrize(
        "defaults",
        [
            TimetableDefaults(lesson_minutes=4),
            TimetableDefaults(lesson_minutes=241),
            TimetableDefaults(gap_minutes=-1),
            TimetableDefaults(gap_minutes=121),
            TimetableDefaults(day_start=time(8, 0, 30)),
        ],
    )
    def test_defaults_out_of_range(self, defaults):
        _bad(tt_(defaults=defaults), "defaults")

    def test_defaults_edges_ok(self):
        validate(tt_(defaults=TimetableDefaults(lesson_minutes=5, gap_minutes=0)))
        validate(tt_(defaults=TimetableDefaults(lesson_minutes=240, gap_minutes=120)))

    def test_assignees(self):
        validate(tt_(assignees=tuple(f"u{i}" for i in range(20))))
        _bad(tt_(assignees=tuple(f"u{i}" for i in range(21))), "assignees")
        _bad(tt_(assignees=("u1", "u1")), "assignees")

    def test_version_positive(self):
        _bad(tt_(version=0), "version")


class TestValidateEntries:
    def test_duplicate_ids(self):
        _bad(
            tt_(entries=(ent("a", 0, "08:00", "08:45"), ent("a", 1, "08:00", "08:45"))),
            "duplicate",
        )

    def test_weekday_not_in_days(self):
        _bad(tt_(entries=(ent("a", 5, "08:00", "08:45"),)), "weekday")

    def test_start_after_end(self):
        _bad(tt_(entries=(ent("a", 0, "09:00", "08:00"),)), "start")

    def test_shorter_than_five_minutes(self):
        _bad(tt_(entries=(ent("a", 0, "08:00", "08:04"),)), "5")

    def test_exactly_five_minutes_ok(self):
        validate(tt_(entries=(ent("a", 0, "08:00", "08:05"),)))

    def test_end_2359_ok(self):
        validate(tt_(entries=(ent("a", 0, "23:00", "23:59"),)))

    def test_seconds_rejected(self):
        e = TimetableEntry(id="a", weekday=0, start=time(8, 0, 1), end=time(9, 0))
        _bad(tt_(entries=(e,)), "HH:MM")

    def test_overlap_rejected(self):
        _bad(
            tt_(
                entries=(
                    ent("a", 0, "08:00", "08:45"),
                    ent("b", 0, "08:44", "09:30"),
                )
            ),
            "overlap",
        )

    def test_contained_overlap_rejected(self):
        _bad(
            tt_(
                entries=(
                    ent("a", 0, "08:00", "10:00"),
                    ent("b", 0, "08:30", "09:00"),
                )
            ),
            "overlap",
        )

    def test_touching_intervals_ok(self):
        validate(
            tt_(
                entries=(
                    ent("a", 0, "08:00", "08:45"),
                    ent("b", 0, "08:45", "09:30"),
                )
            )
        )

    def test_same_time_different_day_ok(self):
        validate(
            tt_(
                entries=(
                    ent("a", 0, "08:00", "08:45"),
                    ent("b", 1, "08:00", "08:45"),
                )
            )
        )

    def test_per_day_limit(self):
        many = tuple(
            TimetableEntry(
                id=f"e{i}",
                weekday=0,
                start=time(i // 2, (i % 2) * 30),
                end=time(i // 2, (i % 2) * 30 + 29),
            )
            for i in range(25)
        )
        _bad(tt_(entries=many), "per day")
        validate(tt_(entries=many[:24]))

    def test_total_limit(self, monkeypatch):
        # 7 days x 24/day = 168 < 200, so the per-day cap binds first on
        # real constants; shrink the total cap to exercise its branch.
        monkeypatch.setattr(tt_mod, "MAX_ENTRIES", 10)
        many = tuple(
            TimetableEntry(
                id=f"e{wd}-{i}",
                weekday=wd,
                start=time(i, 0),
                end=time(i, 45),
            )
            for wd in range(5)
            for i in range(3)
        )
        _bad(tt_(entries=many), "entries")
        validate(tt_(entries=many[:10]))

    @pytest.mark.parametrize(
        ("field", "limit"),
        [("label", 8), ("title", 60), ("room", 30), ("teacher", 60), ("note", 200)],
    )
    def test_string_limits(self, field, limit):
        ok = ent("a", 0, "08:00", "08:45", **{field: "x" * limit})
        validate(tt_(entries=(ok,)))
        bad = ent("a", 0, "08:00", "08:45", **{field: "x" * (limit + 1)})
        _bad(tt_(entries=(bad,)), field)

    def test_entry_color(self):
        validate(tt_(entries=(ent("a", 0, "08:00", "08:45", color="teal"),)))
        _bad(tt_(entries=(ent("a", 0, "08:00", "08:45", color="#123456"),)), "color")


class TestValidateOverrides:
    def test_cancel_ok(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
        )
        validate(copy.replace(std(), overrides=(ov,)))

    def test_override_date_not_in_days(self):
        ov = TimetableOverride(
            id="o1",
            date=SAT,
            kind=OverrideKind.ADD,
            start=hm("08:00"),
            end=hm("09:00"),
        )
        _bad(copy.replace(std(), overrides=(ov,)), "days")

    def test_cancel_unknown_entry(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="x"
        )
        _bad(copy.replace(std(), overrides=(ov,)), "entry")

    def test_cancel_missing_entry_id(self):
        ov = TimetableOverride(id="o1", date=MON, kind=OverrideKind.CANCEL)
        _bad(copy.replace(std(), overrides=(ov,)), "entry_id")

    def test_weekday_mismatch(self):
        # m1 is a Monday lesson; a Tuesday cancel of it is nonsense.
        ov = TimetableOverride(
            id="o1", date=TUE, kind=OverrideKind.CANCEL, entry_id="m1"
        )
        _bad(copy.replace(std(), overrides=(ov,)), "weekday")

    def test_duplicate_cancel_same_date_entry(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o2", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
        )
        _bad(copy.replace(std(), overrides=ovs), "one")

    def test_cancel_and_replace_same_date_entry(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o2", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", room="B1"
            ),
        )
        _bad(copy.replace(std(), overrides=ovs), "one")

    def test_same_entry_different_dates_ok(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o2",
                date=MON + timedelta(days=7),
                kind=OverrideKind.CANCEL,
                entry_id="m1",
            ),
        )
        validate(copy.replace(std(), overrides=ovs))

    def test_duplicate_override_ids(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m2"
            ),
        )
        _bad(copy.replace(std(), overrides=ovs), "duplicate")

    def test_override_limit(self):
        ovs = tuple(
            TimetableOverride(
                id=f"o{i}",
                date=MON + timedelta(days=7 * i),
                kind=OverrideKind.CANCEL,
                entry_id="m1",
            )
            for i in range(201)
        )
        _bad(copy.replace(std(), overrides=ovs), "overrides")
        validate(copy.replace(std(), overrides=ovs[:200]))

    def test_add_requires_times(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.ADD, start=hm("12:00")
        )
        _bad(copy.replace(std(), overrides=(ov,)), "start")

    def test_add_too_short(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.ADD, start=hm("12:00"), end=hm("12:04")
        )
        _bad(copy.replace(std(), overrides=(ov,)), "5")

    def test_add_with_entry_id_rejected(self):
        ov = TimetableOverride(
            id="o1",
            date=MON,
            kind=OverrideKind.ADD,
            entry_id="m1",
            start=hm("12:00"),
            end=hm("12:45"),
        )
        _bad(copy.replace(std(), overrides=(ov,)), "entry_id")

    def test_replace_start_after_end(self):
        ov = TimetableOverride(
            id="o1",
            date=MON,
            kind=OverrideKind.REPLACE,
            entry_id="m1",
            start=hm("09:00"),
            end=hm("08:30"),
        )
        _bad(copy.replace(std(), overrides=(ov,)), "start")

    def test_replace_start_only_past_entry_end(self):
        # Only start is given; merged with m1's 08:45 end it is inverted.
        ov = TimetableOverride(
            id="o1",
            date=MON,
            kind=OverrideKind.REPLACE,
            entry_id="m1",
            start=hm("08:50"),
        )
        _bad(copy.replace(std(), overrides=(ov,)), "start")

    def test_add_overlapping_effective_day(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.ADD, start=hm("08:30"), end=hm("09:00")
        )
        _bad(copy.replace(std(), overrides=(ov,)), "overlap")

    def test_add_over_cancelled_lesson_ok(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o2",
                date=MON,
                kind=OverrideKind.ADD,
                start=hm("08:00"),
                end=hm("08:45"),
                title="Vertretung",
            ),
        )
        validate(copy.replace(std(), overrides=ovs))

    def test_add_touching_ok(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.ADD, start=hm("09:55"), end=hm("10:40")
        )
        validate(copy.replace(std(), overrides=(ov,)))

    def test_replace_moving_time_into_overlap(self):
        ov = TimetableOverride(
            id="o1",
            date=MON,
            kind=OverrideKind.REPLACE,
            entry_id="m1",
            start=hm("08:30"),
            end=hm("09:00"),
        )
        _bad(copy.replace(std(), overrides=(ov,)), "overlap")

    def test_replace_moving_time_into_free_slot_ok(self):
        ov = TimetableOverride(
            id="o1",
            date=MON,
            kind=OverrideKind.REPLACE,
            entry_id="m1",
            start=hm("10:00"),
            end=hm("10:45"),
        )
        validate(copy.replace(std(), overrides=(ov,)))

    def test_override_color_and_strings(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", color="pink"
        )
        _bad(copy.replace(std(), overrides=(ov,)), "color")
        ov2 = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", room="x" * 31
        )
        _bad(copy.replace(std(), overrides=(ov2,)), "room")


class TestValidateValidity:
    def test_from_after_until(self):
        _bad(
            tt_(validity=TimetableValidity(valid_from=FRI, valid_until=MON)),
            "valid_from",
        )

    def test_from_equals_until_ok(self):
        validate(tt_(validity=TimetableValidity(valid_from=MON, valid_until=MON)))

    def test_excluded_not_aligned(self):
        _bad(tt_(validity=TimetableValidity(excluded_weeks=(TUE,))), "anchor")

    def test_excluded_aligned_to_other_week_start(self):
        # A Sunday anchor is not valid for a Monday-start timetable.
        _bad(
            tt_(validity=TimetableValidity(excluded_weeks=(SUN,))),
            "anchor",
        )

    def test_excluded_anchor_outside_days_ok(self):
        # Sunday-start timetable teaching Mon–Fri: the anchor (a Sunday)
        # is not in ``days``, yet it's the right key for the week.
        validate(
            tt_(
                week_start=6,
                validity=TimetableValidity(excluded_weeks=(SUN,)),
            )
        )

    def test_excluded_unsorted_or_duplicate(self):
        nxt = MON + timedelta(days=7)
        _bad(tt_(validity=TimetableValidity(excluded_weeks=(nxt, MON))), "sorted")
        _bad(tt_(validity=TimetableValidity(excluded_weeks=(MON, MON))), "sorted")

    def test_excluded_limit(self):
        weeks = tuple(MON + timedelta(days=7 * i) for i in range(111))
        _bad(tt_(validity=TimetableValidity(excluded_weeks=weeks)), "excluded")
        validate(tt_(validity=TimetableValidity(excluded_weeks=weeks[:110])))


class TestWireSizeCap:
    def test_huge_note_filled_timetable_rejected(self):
        days = tuple(range(7))
        entries = tuple(
            TimetableEntry(
                id=f"e{wd}-{i}",
                weekday=wd,
                start=time(i // 2, (i % 2) * 30),
                end=time(i // 2, (i % 2) * 30 + 29),
                title="ü" * 60,
                room="ü" * 30,
                teacher="ü" * 60,
                note="ü" * 200,
                label="ü" * 8,
            )
            for wd in days
            for i in range(24)
        )
        tt = tt_(days=days, entries=entries)
        assert len(orjson.dumps(to_wire_dict(tt))) > MAX_WIRE_BYTES
        _bad(tt, "bytes")

    def test_normal_timetable_under_cap(self):
        assert len(orjson.dumps(to_wire_dict(std()))) < MAX_WIRE_BYTES


# ─── resolve_day / resolve_week ──────────────────────────────────────────


class TestResolveDay:
    def test_normal_day_sorted(self):
        tt = tt_(
            entries=(
                ent("late", 0, "10:00", "10:45"),
                ent("early", 0, "07:15", "07:45", title="Musik"),
                ent("mid", 0, "08:00", "08:45"),
            )
        )
        day = resolve_day(tt, MON)
        assert day.valid
        assert day.date == MON
        assert [ls.source_id for ls in day.lessons] == ["early", "mid", "late"]
        assert all(ls.status is LessonStatus.NORMAL for ls in day.lessons)
        assert all(ls.original is None for ls in day.lessons)
        assert all(ls.date == MON for ls in day.lessons)

    def test_only_that_weekday(self):
        day = resolve_day(std(), TUE)
        assert [ls.source_id for ls in day.lessons] == ["t1"]

    def test_cancel_keeps_entry_with_status(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
        )
        day = resolve_day(copy.replace(std(), overrides=(ov,)), MON)
        m1 = next(ls for ls in day.lessons if ls.source_id == "m1")
        assert m1.status is LessonStatus.CANCELLED
        assert m1.override_id == "o1"
        assert m1.original is not None and m1.original.id == "m1"
        assert m1.title == "Mathe"

    def test_replace_merges_and_sets_original(self):
        ov = TimetableOverride(
            id="o1",
            date=MON,
            kind=OverrideKind.REPLACE,
            entry_id="m2",
            room="B12",
            teacher="Hr. X",
            start=hm("10:00"),
            end=hm("10:45"),
        )
        day = resolve_day(copy.replace(std(), overrides=(ov,)), MON)
        ids = [ls.source_id for ls in day.lessons]
        assert ids == ["m1", "mb", "m2"]  # moved after the break
        m2 = day.lessons[-1]
        assert m2.status is LessonStatus.CHANGED
        assert (m2.start, m2.end) == (hm("10:00"), hm("10:45"))
        assert m2.room == "B12" and m2.teacher == "Hr. X"
        assert m2.title == "Deutsch"  # untouched field kept
        assert m2.label == "2."
        assert m2.original is not None
        assert m2.original.start == hm("08:50")

    def test_add(self):
        ov = TimetableOverride(
            id="o-add",
            date=MON,
            kind=OverrideKind.ADD,
            start=hm("07:00"),
            end=hm("07:45"),
            title="AG",
            entry_kind=EntryKind.LESSON,
            color="sky",
        )
        day = resolve_day(copy.replace(std(), overrides=(ov,)), MON)
        first = day.lessons[0]
        assert first.source_id == "o-add"
        assert first.override_id == "o-add"
        assert first.status is LessonStatus.ADDED
        assert first.original is None
        assert first.title == "AG" and first.color == "sky"

    def test_overrides_for_other_date_ignored(self):
        ov = TimetableOverride(
            id="o1",
            date=MON + timedelta(days=7),
            kind=OverrideKind.CANCEL,
            entry_id="m1",
        )
        day = resolve_day(copy.replace(std(), overrides=(ov,)), MON)
        assert all(ls.status is LessonStatus.NORMAL for ls in day.lessons)

    def test_day_not_in_days(self):
        day = resolve_day(std(), SAT)
        assert not day.valid and day.lessons == ()

    def test_excluded_week(self):
        tt = copy.replace(std(), validity=TimetableValidity(excluded_weeks=(MON,)))
        day = resolve_day(tt, TUE)
        assert not day.valid and day.lessons == ()
        assert resolve_day(tt, TUE + timedelta(days=7)).valid

    def test_outside_valid_range(self):
        tt = copy.replace(
            std(),
            validity=TimetableValidity(valid_from=TUE, valid_until=WED),
        )
        assert not resolve_day(tt, MON).valid
        assert resolve_day(tt, TUE).valid
        assert resolve_day(tt, WED).valid
        assert not resolve_day(tt, WED + timedelta(days=7)).valid

    def test_open_ended_validity(self):
        tt = copy.replace(std(), validity=TimetableValidity(valid_from=TUE))
        assert not resolve_day(tt, MON).valid
        assert resolve_day(tt, MON + timedelta(days=700)).valid

    def test_is_valid_on(self):
        # SUN (2026-10-04) anchors the Sunday-start week Oct 4-10.
        v = TimetableValidity(excluded_weeks=(SUN,))
        next_tue = SUN + timedelta(days=2)
        assert not v.is_valid_on(next_tue, 6)
        assert not v.is_valid_on(SUN, 6)
        # Under Monday start, SUN belongs to the week anchored on MON.
        assert v.is_valid_on(next_tue, 0)
        assert v.is_valid_on(SAT, 6)


class TestResolveWeek:
    def test_monday_start(self):
        wk = resolve_week(std(), WED)
        assert wk.anchor == MON
        assert wk.valid
        assert [d.date for d in wk.days] == [MON + timedelta(days=i) for i in range(5)]

    def test_sunday_start_calendar_order(self):
        tt = copy.replace(std(), week_start=6, days=(0, 1, 2, 3, 4, 6))
        wk = resolve_week(tt, WED)
        anchor = MON - timedelta(days=1)
        assert wk.anchor == anchor
        assert [d.date for d in wk.days] == [
            anchor,
            MON,
            TUE,
            WED,
            MON + timedelta(days=3),
            FRI,
        ]

    def test_excluded_week_invalid_and_empty(self):
        tt = copy.replace(std(), validity=TimetableValidity(excluded_weeks=(MON,)))
        wk = resolve_week(tt, FRI)
        assert not wk.valid
        assert all(not d.valid and d.lessons == () for d in wk.days)

    def test_partially_in_range_week_is_valid(self):
        tt = copy.replace(std(), validity=TimetableValidity(valid_from=WED))
        wk = resolve_week(tt, MON)
        assert wk.valid
        assert [d.valid for d in wk.days] == [False, False, True, True, True]

    def test_week_outside_range_invalid(self):
        tt = copy.replace(
            std(), validity=TimetableValidity(valid_until=MON - timedelta(1))
        )
        assert not resolve_week(tt, MON).valid


# ─── prune_overrides ─────────────────────────────────────────────────────


def test_prune_overrides_drops_old():
    today = MON + timedelta(days=28)
    keep_edge = today - timedelta(days=OVERRIDE_RETENTION_DAYS)
    ovs = (
        TimetableOverride(id="old", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"),
        TimetableOverride(
            id="edge",
            date=keep_edge,
            kind=OverrideKind.CANCEL,
            entry_id="m1",
        ),
        TimetableOverride(
            id="future",
            date=today + timedelta(days=7),
            kind=OverrideKind.CANCEL,
            entry_id="m1",
        ),
    )
    assert keep_edge.weekday() == 0
    tt = copy.replace(std(), overrides=ovs)
    pruned = prune_overrides(tt, today)
    assert [o.id for o in pruned.overrides] == ["edge", "future"]
    # Pruning is housekeeping, not an edit: the version is not bumped.
    assert pruned.version == tt.version


def test_prune_overrides_noop_returns_same():
    tt = std()
    assert prune_overrides(tt, MON) is tt


# ─── mutators ────────────────────────────────────────────────────────────


class TestWithHeader:
    def test_bumps_version_and_stamps(self):
        tt = std()
        out = with_header(tt, name="  Ben 3a ", color="teal", now=NOW, by="uid-bob")
        assert out.version == tt.version + 1
        assert out.updated_at == NOW
        assert out.updated_by == "uid-bob"
        assert out.name == "Ben 3a"
        assert out.color == "teal"
        assert out.entries == tt.entries

    def test_unset_leaves_fields(self):
        tt = copy.replace(std(), color="olive", tz="Europe/Berlin")
        out = with_header(tt, now=NOW, by=None)
        assert out.color == "olive" and out.tz == "Europe/Berlin"
        assert out.version == tt.version + 1

    def test_color_can_be_cleared(self):
        tt = copy.replace(std(), color="olive")
        assert with_header(tt, color=None, now=NOW).color is None
        assert with_header(tt, color="", now=NOW).color is None

    def test_invalid_values_rejected(self):
        with pytest.raises(TimetableValidationError):
            with_header(std(), color="#fff", now=NOW)
        with pytest.raises(TimetableValidationError):
            with_header(std(), tz="Mars/Base", now=NOW)
        with pytest.raises(TimetableValidationError):
            with_header(std(), name="", now=NOW)

    def test_days_orphan_error(self):
        with pytest.raises(TimetableOrphanError) as ei:
            with_header(std(), days=(1, 2, 3, 4), now=NOW)
        assert ei.value.code == "DAYS_ORPHAN_ENTRIES"
        assert ei.value.count == 3

    def test_days_drop_orphans(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o2",
                date=MON,
                kind=OverrideKind.ADD,
                start=hm("12:00"),
                end=hm("12:45"),
            ),
            TimetableOverride(
                id="o3", date=TUE, kind=OverrideKind.CANCEL, entry_id="t1"
            ),
        )
        tt = copy.replace(std(), overrides=ovs)
        out = with_header(tt, days=(1, 2, 3, 4), drop_orphans=True, now=NOW)
        assert out.days == (1, 2, 3, 4)
        assert [e.id for e in out.entries] == ["t1"]
        assert [o.id for o in out.overrides] == ["o3"]

    def test_adding_days_no_orphans(self):
        out = with_header(std(), days=(0, 1, 2, 3, 4, 5), now=NOW)
        assert out.days == (0, 1, 2, 3, 4, 5)

    def test_days_normalised(self):
        out = with_header(std(), days=[4, 0, 1, 1, 2, 3], now=NOW)
        assert out.days == (0, 1, 2, 3, 4)

    def test_removed_empty_day_drops_its_overrides(self):
        ov = TimetableOverride(
            id="o-fri",
            date=FRI,
            kind=OverrideKind.ADD,
            start=hm("12:00"),
            end=hm("12:45"),
        )
        tt = copy.replace(std(), overrides=(ov,))
        out = with_header(tt, days=(0, 1, 2, 3), now=NOW)
        assert out.overrides == ()

    def test_week_start_mon_to_sun_remaps(self):
        nxt = MON + timedelta(days=7)
        tt = copy.replace(std(), validity=TimetableValidity(excluded_weeks=(MON, nxt)))
        out = with_header(tt, week_start=6, now=NOW)
        assert out.week_start == 6
        assert out.validity.excluded_weeks == (
            MON - timedelta(days=1),
            nxt - timedelta(days=1),
        )

    def test_week_start_sun_to_mon_remaps(self):
        sun_anchor = MON - timedelta(days=1)
        tt = copy.replace(
            std(),
            week_start=6,
            validity=TimetableValidity(excluded_weeks=(sun_anchor,)),
        )
        out = with_header(tt, week_start=0, now=NOW)
        assert out.validity.excluded_weeks == (MON,)

    def test_same_week_start_no_remap(self):
        tt = copy.replace(std(), validity=TimetableValidity(excluded_weeks=(MON,)))
        out = with_header(tt, week_start=0, now=NOW)
        assert out.validity.excluded_weeks == (MON,)

    def test_invalid_week_start_rejected(self):
        with pytest.raises(TimetableValidationError, match="week_start"):
            with_header(std(), week_start=2, now=NOW)

    def test_unset_sentinel_repr(self):
        assert repr(tt_mod.UNSET) == "UNSET"

    def test_defaults_and_assignees(self):
        d = TimetableDefaults(lesson_minutes=50, gap_minutes=10, day_start=hm("07:30"))
        out = with_header(std(), defaults=d, assignees=("u1", "u2", "u1"), now=NOW)
        assert out.defaults == d
        assert out.assignees == ("u1", "u2")


class TestEntryMutators:
    def test_with_entry_adds(self):
        tt = std()
        out = with_entry(tt, ent("w1", 2, "08:00", "08:45", title=" Kunst "), now=NOW)
        assert out.version == tt.version + 1
        assert out.updated_at == NOW
        w1 = next(e for e in out.entries if e.id == "w1")
        assert w1.title == "Kunst"

    def test_with_entry_empty_strings_to_none(self):
        out = with_entry(
            std(),
            ent("w1", 2, "08:00", "08:45", title="", room="  ", color=""),
            now=NOW,
        )
        w1 = next(e for e in out.entries if e.id == "w1")
        assert w1.title is None and w1.room is None and w1.color is None

    def test_with_entry_replaces_by_id(self):
        out = with_entry(std(), ent("m1", 0, "08:00", "08:45", title="Bio"), now=NOW)
        assert len(out.entries) == 4
        assert next(e for e in out.entries if e.id == "m1").title == "Bio"

    def test_with_entry_overlap_rejected(self):
        with pytest.raises(TimetableValidationError):
            with_entry(std(), ent("x", 0, "08:10", "08:30"), now=NOW)

    def test_with_entry_weekday_move_drops_its_overrides(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
        )
        tt = copy.replace(std(), overrides=(ov,))
        out = with_entry(tt, ent("m1", 2, "08:00", "08:45"), now=NOW)
        assert out.overrides == ()

    def test_without_entry_drops_referencing_overrides(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o2", date=MON, kind=OverrideKind.CANCEL, entry_id="m2"
            ),
        )
        tt = copy.replace(std(), overrides=ovs)
        out = without_entry(tt, "m1", now=NOW, by="uid-bob")
        assert "m1" not in {e.id for e in out.entries}
        assert [o.id for o in out.overrides] == ["o2"]
        assert out.version == tt.version + 1
        assert out.updated_by == "uid-bob"

    def test_without_entry_unknown(self):
        with pytest.raises(TimetableValidationError):
            without_entry(std(), "nope", now=NOW)

    def test_with_entries_replaces_and_drops_dangling(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o2", date=TUE, kind=OverrideKind.CANCEL, entry_id="t1"
            ),
            TimetableOverride(
                id="o3",
                date=MON,
                kind=OverrideKind.ADD,
                start=hm("12:00"),
                end=hm("12:45"),
            ),
        )
        tt = copy.replace(std(), overrides=ovs)
        out = with_entries(tt, [ent("t1", 1, "08:00", "08:45")], now=NOW)
        assert [e.id for e in out.entries] == ["t1"]
        assert [o.id for o in out.overrides] == ["o2", "o3"]


class TestGenerateDay:
    def test_empty_day(self):
        slots = [ent("w1", 2, "08:00", "08:45"), ent("w2", 2, "08:50", "09:35")]
        out = generate_day(std(), 2, slots, now=NOW)
        assert [e.id for e in out.entries if e.weekday == 2] == ["w1", "w2"]
        assert out.version == 2

    def test_non_empty_day_without_replace(self):
        with pytest.raises(TimetableOrphanError) as ei:
            generate_day(std(), 0, [ent("x", 0, "08:00", "08:45")], now=NOW)
        assert ei.value.code == "DAY_HAS_ENTRIES"
        assert ei.value.count == 3

    def test_replace_drops_old_and_their_overrides(self):
        ovs = (
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="o2", date=TUE, kind=OverrideKind.CANCEL, entry_id="t1"
            ),
        )
        tt = copy.replace(std(), overrides=ovs)
        out = generate_day(
            tt, 0, [ent("x", 0, "08:00", "08:45")], replace=True, now=NOW
        )
        assert sorted(e.id for e in out.entries) == ["t1", "x"]
        assert [o.id for o in out.overrides] == ["o2"]

    def test_slot_weekday_mismatch_rejected(self):
        with pytest.raises(TimetableValidationError):
            generate_day(std(), 2, [ent("x", 3, "08:00", "08:45")], now=NOW)


class TestCopyDay:
    @staticmethod
    def _ids():
        counter = itertools.count(1)
        return lambda: f"new-{next(counter)}"

    def test_copies_with_new_ids(self):
        out = copy_day(std(), 0, [2, 3], id_factory=self._ids(), now=NOW)
        wed = [e for e in out.entries if e.weekday == 2]
        thu = [e for e in out.entries if e.weekday == 3]
        assert len(wed) == 3 and len(thu) == 3
        assert all(e.id.startswith("new-") for e in wed + thu)
        assert len({e.id for e in out.entries}) == len(out.entries)
        assert [e.title for e in wed] == ["Mathe", "Deutsch", "Pause"]
        assert out.version == 2

    def test_without_subjects_keeps_break_titles(self):
        src = copy.replace(
            std(),
            entries=(
                ent(
                    "m1",
                    0,
                    "08:00",
                    "08:45",
                    label="1.",
                    title="Mathe",
                    room="A1",
                    teacher="T",
                    note="n",
                    color="teal",
                ),
                ent("mb", 0, "08:45", "09:00", kind=EntryKind.BREAK, title="Pause"),
            ),
        )
        out = copy_day(
            src, 0, [2], with_subjects=False, id_factory=self._ids(), now=NOW
        )
        wed = sorted((e for e in out.entries if e.weekday == 2), key=lambda e: e.start)
        lesson, brk = wed
        assert lesson.label == "1."
        assert (
            lesson.title,
            lesson.room,
            lesson.teacher,
            lesson.note,
            lesson.color,
        ) == (
            None,
            None,
            None,
            None,
            None,
        )
        assert brk.title == "Pause"
        assert brk.kind is EntryKind.BREAK

    def test_non_empty_target_orphan_count_sums(self):
        tt = with_entry(std(), ent("w1", 2, "08:00", "08:45"), now=NOW)
        with pytest.raises(TimetableOrphanError) as ei:
            copy_day(tt, 0, [1, 2], id_factory=self._ids(), now=NOW)
        assert ei.value.code == "DAY_HAS_ENTRIES"
        assert ei.value.count == 2

    def test_replace_overwrites_targets_and_drops_their_overrides(self):
        ov = TimetableOverride(
            id="o2", date=TUE, kind=OverrideKind.CANCEL, entry_id="t1"
        )
        tt = copy.replace(std(), overrides=(ov,))
        out = copy_day(tt, 0, [1], replace=True, id_factory=self._ids(), now=NOW)
        tue = [e for e in out.entries if e.weekday == 1]
        assert len(tue) == 3 and "t1" not in {e.id for e in tue}
        assert out.overrides == ()

    def test_target_not_in_days(self):
        with pytest.raises(TimetableValidationError):
            copy_day(std(), 0, [5], id_factory=self._ids(), now=NOW)

    def test_source_not_in_days(self):
        with pytest.raises(TimetableValidationError, match="source"):
            copy_day(std(), 5, [1], id_factory=self._ids(), now=NOW)

    def test_target_same_as_source(self):
        with pytest.raises(TimetableValidationError):
            copy_day(std(), 0, [0], replace=True, id_factory=self._ids(), now=NOW)


class TestShiftAfter:
    def test_positive_shift(self):
        out = shift_after(std(), 0, hm("08:50"), 10, now=NOW)
        by_id = {e.id: e for e in out.entries}
        assert by_id["m1"].start == hm("08:00")  # before from_time, untouched
        assert (by_id["m2"].start, by_id["m2"].end) == (hm("09:00"), hm("09:45"))
        assert (by_id["mb"].start, by_id["mb"].end) == (hm("09:45"), hm("10:05"))
        assert by_id["t1"].start == hm("08:00")  # other weekday untouched
        assert out.version == 2

    def test_negative_shift(self):
        out = shift_after(std(), 0, hm("08:50"), -5, now=NOW)
        by_id = {e.id: e for e in out.entries}
        assert by_id["m2"].start == hm("08:45")

    def test_negative_shift_into_overlap_rejected(self):
        with pytest.raises(TimetableValidationError):
            shift_after(std(), 0, hm("08:50"), -30, now=NOW)

    def test_crossing_midnight_forward(self):
        tt = tt_(entries=(ent("a", 0, "23:00", "23:50"),))
        with pytest.raises(TimetableValidationError, match="midnight"):
            shift_after(tt, 0, hm("00:00"), 10, now=NOW)
        # Landing exactly on 23:59 is allowed.
        out = shift_after(tt, 0, hm("00:00"), 9, now=NOW)
        assert out.entries[0].end == hm("23:59")

    def test_crossing_midnight_backward(self):
        tt = tt_(entries=(ent("a", 0, "00:05", "00:50"),))
        with pytest.raises(TimetableValidationError, match="midnight"):
            shift_after(tt, 0, hm("00:00"), -10, now=NOW)


class TestValidityMutator:
    def test_normalises_excluded(self):
        v = TimetableValidity(
            valid_from=MON,
            excluded_weeks=(WED + timedelta(days=7), TUE, MON, FRI),
        )
        out = with_validity(std(), v, now=NOW)
        assert out.validity.excluded_weeks == (MON, MON + timedelta(days=7))
        assert out.validity.valid_from == MON
        assert out.version == 2

    def test_snaps_to_sunday_anchor(self):
        tt = copy.replace(std(), week_start=6)
        out = with_validity(tt, TimetableValidity(excluded_weeks=(WED,)), now=NOW)
        assert out.validity.excluded_weeks == (MON - timedelta(days=1),)

    def test_from_after_until_rejected(self):
        with pytest.raises(TimetableValidationError):
            with_validity(
                std(), TimetableValidity(valid_from=FRI, valid_until=MON), now=NOW
            )


class TestOverrideMutators:
    def test_with_override_adds_and_replaces(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", room=" B1 "
        )
        out = with_override(std(), ov, now=NOW, by="uid-x")
        assert out.overrides[0].room == "B1"
        assert out.version == 2 and out.updated_by == "uid-x"
        out2 = with_override(out, copy.replace(ov, room="C3"), now=NOW)
        assert len(out2.overrides) == 1 and out2.overrides[0].room == "C3"
        assert out2.version == 3

    def test_with_override_empty_strings_to_none(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", room=""
        )
        assert with_override(std(), ov, now=NOW).overrides[0].room is None

    def test_with_override_invalid(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="t1"
        )
        with pytest.raises(TimetableValidationError):
            with_override(std(), ov, now=NOW)

    def test_without_override(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
        )
        tt = copy.replace(std(), overrides=(ov,))
        out = without_override(tt, "o1", now=NOW)
        assert out.overrides == ()
        assert out.version == 2
        with pytest.raises(TimetableValidationError):
            without_override(out, "o1", now=NOW)

    def test_clear_week(self):
        nxt = MON + timedelta(days=7)
        ovs = (
            TimetableOverride(
                id="a", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
            TimetableOverride(
                id="b", date=TUE, kind=OverrideKind.CANCEL, entry_id="t1"
            ),
            TimetableOverride(
                id="c", date=nxt, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
        )
        tt = copy.replace(std(), overrides=ovs)
        out = clear_week(tt, FRI, now=NOW)
        assert [o.id for o in out.overrides] == ["c"]
        assert out.version == 2

    def test_clear_week_sunday_start(self):
        tt = copy.replace(std(), week_start=6, days=(0, 1, 2, 3, 4, 6))
        ovs = (
            TimetableOverride(
                id="sun",
                date=SUN,  # belongs to the NEXT Sunday-start week
                kind=OverrideKind.ADD,
                start=hm("10:00"),
                end=hm("10:45"),
            ),
            TimetableOverride(
                id="mon", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
            ),
        )
        tt = copy.replace(tt, overrides=ovs)
        out = clear_week(tt, WED, now=NOW)
        assert [o.id for o in out.overrides] == ["sun"]


# ─── wire format ─────────────────────────────────────────────────────────


def _rich() -> Timetable:
    ovs = (
        TimetableOverride(id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"),
        TimetableOverride(
            id="o2",
            date=MON,
            kind=OverrideKind.REPLACE,
            entry_id="m2",
            room="B1",
            start=hm("10:00"),
            end=hm("10:45"),
        ),
        TimetableOverride(
            id="o3",
            date=TUE,
            kind=OverrideKind.ADD,
            start=hm("12:00"),
            end=hm("12:20"),
            entry_kind=EntryKind.BREAK,
            title="Extra",
            color="rose",
            label="x",
            teacher="t",
            note="n",
        ),
    )
    return copy.replace(
        std(),
        color="moss",
        tz="Europe/Berlin",
        defaults=TimetableDefaults(
            lesson_minutes=50, gap_minutes=10, day_start=hm("07:45")
        ),
        overrides=ovs,
        validity=TimetableValidity(
            valid_from=MON, valid_until=MON + timedelta(days=300), excluded_weeks=(MON,)
        ),
        assignees=("uid-a", "uid-b"),
        version=7,
        updated_by="uid-bob",
    )


class TestWire:
    def test_round_trip(self):
        tt = _rich()
        validate(tt)
        d = to_wire_dict(tt)
        assert d["schema"] == WIRE_SCHEMA == 1
        # Plain JSON types only.
        assert orjson.loads(orjson.dumps(d)) == d
        assert from_wire_dict(orjson.loads(orjson.dumps(d))) == tt

    def test_round_trip_minimal(self):
        tt = tt_()
        assert from_wire_dict(to_wire_dict(tt)) == tt

    def test_shape(self):
        d = to_wire_dict(_rich())
        assert d["days"] == [0, 1, 2, 3, 4]
        assert d["defaults"] == {
            "lesson_minutes": 50,
            "gap_minutes": 10,
            "day_start": "07:45",
        }
        assert d["entries"][0]["start"] == "08:00"
        assert d["validity"]["excluded_weeks"] == [MON.isoformat()]
        assert d["created_at"] == T0.isoformat()
        assert d["overrides"][0]["date"] == MON.isoformat()

    def test_schema_mismatch(self):
        d = to_wire_dict(std())
        d["schema"] = 2
        with pytest.raises(TimetableValidationError, match="schema"):
            from_wire_dict(d)
        del d["schema"]
        with pytest.raises(TimetableValidationError, match="schema"):
            from_wire_dict(d)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.pop("id"),
            lambda d: d.pop("entries"),
            lambda d: d.update(entries="nope"),
            lambda d: d.update(entries=[{"id": "x"}]),
            lambda d: d.update(entries=[1]),
            lambda d: d["entries"][0].update(start="8 o'clock"),
            lambda d: d["entries"][0].update(start="25:00"),
            lambda d: d["entries"][0].update(start="08:00:30"),
            lambda d: d["entries"][0].update(kind="lunch"),
            lambda d: d["entries"][0].update(weekday="0"),
            lambda d: d["entries"][0].update(weekday=True),
            lambda d: d["entries"][0].update(title=5),
            lambda d: d.update(days="01234"),
            lambda d: d.update(days=[0, "1"]),
            lambda d: d.update(version="1"),
            lambda d: d.update(created_at="yesterday"),
            lambda d: d.update(created_at=None),
            lambda d: d.update(defaults=[]),
            lambda d: d.update(validity=None),
            lambda d: d["validity"].update(valid_from="2026-13-01"),
            lambda d: d["validity"].update(excluded_weeks=[1]),
            lambda d: d.update(overrides=[{"id": "o", "date": "x", "kind": "cancel"}]),
            lambda d: d.update(
                overrides=[{"id": "o", "date": "2026-09-28", "kind": "zap"}]
            ),
            lambda d: d.update(assignees=[None]),
            lambda d: d.update(name=None),
            lambda d: d.update(tz=3),
        ],
    )
    def test_malformed_raises_validation_error(self, mutate):
        d = to_wire_dict(_rich())
        mutate(d)
        with pytest.raises(TimetableValidationError):
            from_wire_dict(d)

    def test_not_a_dict(self):
        with pytest.raises(TimetableValidationError):
            from_wire_dict([])  # type: ignore[arg-type]

    def test_optional_fields_default(self):
        d = to_wire_dict(tt_())
        for k in ("color", "updated_by", "assignees", "overrides"):
            d.pop(k)
        tt = from_wire_dict(d)
        assert tt.color is None and tt.updated_by is None
        assert tt.assignees == () and tt.overrides == ()

    def test_element_helpers_round_trip(self):
        tt = _rich()
        for e in tt.entries:
            assert entry_from_dict(entry_to_dict(e)) == e
        for o in tt.overrides:
            assert override_from_dict(override_to_dict(o)) == o
        assert defaults_from_dict(defaults_to_dict(tt.defaults)) == tt.defaults

    def test_defaults_from_empty_dict(self):
        assert defaults_from_dict({}) == TimetableDefaults()


# ─── exceptions ──────────────────────────────────────────────────────────


def test_exceptions_carry_fields():
    assert issubclass(TimetableValidationError, ValueError)
    c = TimetableConflictError(current_version=4)
    assert c.current_version == 4
    o = TimetableOrphanError(code="DAY_HAS_ENTRIES", count=3)
    assert o.code == "DAY_HAS_ENTRIES" and o.count == 3
    assert "3" in str(o)
    assert isinstance(TimetableLimitError("too many"), Exception)


# ─── review hardening ────────────────────────────────────────────────────


class TestHardening:
    def test_tz_overlong_rejected_without_lookup(self):
        _bad(tt_(tz="a" * 5000), "tz")
        _bad(tt_(tz="Europe/" + "x" * 60), "tz")

    def test_version_bounds(self):
        assert tt_mod.MAX_VERSION == 2**31 - 1
        validate(tt_(version=tt_mod.MAX_VERSION))
        _bad(tt_(version=tt_mod.MAX_VERSION + 1), "version")
        _bad(tt_(version=2**70), "version")

    @pytest.mark.parametrize("v", [0, -1, 2**31, 2**63, 2**70])
    def test_wire_version_out_of_range(self, v):
        d = to_wire_dict(std())
        d["version"] = v
        with pytest.raises(TimetableValidationError, match="version"):
            from_wire_dict(d)

    @pytest.mark.parametrize("bad_id", ["", "x" * 65])
    @pytest.mark.parametrize(
        "build",
        [
            lambda i: tt_(id=i),
            lambda i: tt_(created_by=i),
            lambda i: tt_(updated_by=i),
            lambda i: tt_(assignees=("ok", i)),
            lambda i: tt_(entries=(ent(i, 0, "08:00", "08:45"),)),
            lambda i: copy.replace(
                std(),
                overrides=(
                    TimetableOverride(
                        id=i, date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
                    ),
                ),
            ),
        ],
        ids=["id", "created_by", "updated_by", "assignee", "entry", "override"],
    )
    def test_id_caps(self, build, bad_id):
        assert tt_mod.MAX_ID == 64
        _bad(build(bad_id))

    def test_override_entry_id_cap(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="x" * 65
        )
        _bad(copy.replace(std(), overrides=(ov,)), "entry_id")

    def test_id_at_cap_ok(self):
        validate(tt_(id="x" * 64, created_by="u" * 64, updated_by="u" * 64))

    @pytest.mark.parametrize("schema", [True, 1.0, "1"])
    def test_schema_strict_type(self, schema):
        d = to_wire_dict(std())
        d["schema"] = schema
        with pytest.raises(TimetableValidationError, match="schema"):
            from_wire_dict(d)

    def test_naive_datetime_rejected_on_wire(self):
        d = to_wire_dict(std())
        d["updated_at"] = "2026-09-01T08:00:00"
        with pytest.raises(TimetableValidationError, match="timezone"):
            from_wire_dict(d)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("entries", [0] * 201),
            ("overrides", [0] * 201),
            ("days", [0] * 8),
            ("assignees", [0] * 21),
        ],
    )
    def test_wire_list_caps_checked_before_elements(self, key, value):
        d = to_wire_dict(std())
        d[key] = value
        with pytest.raises(TimetableValidationError, match="at most"):
            from_wire_dict(d)

    def test_wire_excluded_weeks_cap(self):
        d = to_wire_dict(std())
        d["validity"]["excluded_weeks"] = [0] * 111
        with pytest.raises(TimetableValidationError, match="at most"):
            from_wire_dict(d)

    def test_error_messages_truncate_raw_values(self):
        d = to_wire_dict(std())
        d["entries"][0]["kind"] = "x" * 5000
        with pytest.raises(TimetableValidationError) as ei:
            from_wire_dict(d)
        assert len(str(ei.value)) < 120
        with pytest.raises(TimetableValidationError) as ei:
            validate(tt_(color="c" * 5000))
        assert len(str(ei.value)) < 120

    def test_replace_inverted_interval_message(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", end=hm("07:00")
        )
        with pytest.raises(TimetableValidationError, match="start must be before end"):
            validate(copy.replace(std(), overrides=(ov,)))

    def test_replace_short_interval_allowed(self):
        # Only the entry/add minimum applies to base slots; a replace may
        # shorten a lesson below it (spec: start < end only).
        ov = TimetableOverride(
            id="o1",
            date=MON,
            kind=OverrideKind.REPLACE,
            entry_id="m1",
            start=hm("08:00"),
            end=hm("08:02"),
        )
        validate(copy.replace(std(), overrides=(ov,)))

    def test_resolve_skips_malformed_add_on_unvalidated_timetable(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.ADD, start=hm("12:00")
        )
        day = resolve_day(copy.replace(std(), overrides=(ov,)), MON)
        assert "o1" not in {ls.source_id for ls in day.lessons}

    @pytest.mark.parametrize(
        "extra",
        [
            {"title": "x"},
            {"room": "x"},
            {"teacher": "x"},
            {"note": "x"},
            {"color": "teal"},
            {"label": "1."},
            {"start": time(8, 0)},
            {"end": time(8, 45)},
            {"entry_kind": EntryKind.BREAK},
        ],
    )
    def test_cancel_rejects_payload_fields(self, extra):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1", **extra
        )
        _bad(copy.replace(std(), overrides=(ov,)), "cancel")

    def test_replace_rejects_entry_kind(self):
        ov = TimetableOverride(
            id="o1",
            date=MON,
            kind=OverrideKind.REPLACE,
            entry_id="m1",
            entry_kind=EntryKind.BREAK,
        )
        _bad(copy.replace(std(), overrides=(ov,)), "entry_kind")

    def test_replace_label_is_replaceable(self):
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", label="1a"
        )
        tt = copy.replace(std(), overrides=(ov,))
        validate(tt)
        m1 = next(ls for ls in resolve_day(tt, MON).lessons if ls.source_id == "m1")
        assert m1.label == "1a" and m1.original.label == "1."

    def test_copy_day_no_targets_is_noop(self):
        tt = std()
        assert copy_day(tt, 0, [], id_factory=lambda: "x", now=NOW) is tt

    def test_shift_after_noop(self):
        tt = std()
        assert shift_after(tt, 0, hm("08:00"), 0, now=NOW) is tt
        assert shift_after(tt, 0, hm("20:00"), 10, now=NOW) is tt
        assert shift_after(tt, 2, hm("00:00"), 10, now=NOW) is tt


class TestIdInjectionAndPatch:
    def test_entry_from_dict_injects_id(self):
        d = entry_to_dict(ent("client-id", 0, "08:00", "08:45"))
        assert entry_from_dict(d, id="srv-1").id == "srv-1"
        d.pop("id")
        assert entry_from_dict(d, id="srv-2").id == "srv-2"
        with pytest.raises(TimetableValidationError, match="id"):
            entry_from_dict(d)

    def test_override_from_dict_injects_id(self):
        o = TimetableOverride(
            id="client", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
        )
        d = override_to_dict(o)
        assert override_from_dict(d, id="srv-1") == copy.replace(o, id="srv-1")
        d.pop("id")
        assert override_from_dict(d, id="srv-2").id == "srv-2"

    def test_entry_patch_merges(self):
        e = ent("m1", 0, "08:00", "08:45", title="Mathe", room="A1")
        out = entry_patch(e, {"room": "B2", "end": "08:50", "note": None})
        assert out == copy.replace(e, room="B2", end=hm("08:50"))
        assert entry_patch(e, {}) == e

    def test_entry_patch_can_clear_a_field(self):
        e = ent("m1", 0, "08:00", "08:45", title="Mathe")
        assert entry_patch(e, {"title": None}).title is None

    def test_entry_patch_never_changes_id(self):
        e = ent("m1", 0, "08:00", "08:45")
        assert entry_patch(e, {"id": "evil", "title": "x"}).id == "m1"

    def test_entry_patch_malformed(self):
        e = ent("m1", 0, "08:00", "08:45")
        with pytest.raises(TimetableValidationError):
            entry_patch(e, {"start": "soon"})
        with pytest.raises(TimetableValidationError, match="unknown"):
            entry_patch(e, {"colour": "teal"})
        with pytest.raises(TimetableValidationError):
            entry_patch(e, ["title"])  # type: ignore[arg-type]

    def test_override_patch(self):
        o = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", room="B1"
        )
        out = override_patch(o, {"room": "C3", "start": "10:00", "id": "evil"})
        assert out == copy.replace(o, room="C3", start=hm("10:00"))
        with pytest.raises(TimetableValidationError, match="kind"):
            override_patch(o, {"kind": "nope"})
        with pytest.raises(TimetableValidationError, match="unknown"):
            override_patch(o, {"bogus": 1})


# ─── School template + resolved serializers + public parsers (PR2 part 2) ─


def _counter_ids():
    n = itertools.count(1)
    return lambda: f"id{next(n)}"


class TestSchoolTemplateDay:
    def test_default_school_grid(self):
        slots = tt_mod.school_template_day(2, TimetableDefaults(), _counter_ids())
        got = [
            (
                s.kind,
                s.label,
                s.title,
                s.start.strftime("%H:%M"),
                s.end.strftime("%H:%M"),
            )
            for s in slots
        ]
        assert got == [
            (EntryKind.LESSON, "1.", None, "08:00", "08:45"),
            (EntryKind.LESSON, "2.", None, "08:50", "09:35"),
            (EntryKind.BREAK, None, "Pause", "09:35", "09:55"),
            (EntryKind.LESSON, "3.", None, "09:55", "10:40"),
            (EntryKind.LESSON, "4.", None, "10:45", "11:30"),
            (EntryKind.BREAK, None, "Pause", "11:30", "11:45"),
            (EntryKind.LESSON, "5.", None, "11:45", "12:30"),
            (EntryKind.LESSON, "6.", None, "12:35", "13:20"),
        ]
        assert all(s.weekday == 2 for s in slots)
        assert [s.id for s in slots] == [f"id{i}" for i in range(1, 9)]

    def test_follows_defaults(self):
        dft = TimetableDefaults(lesson_minutes=50, gap_minutes=0, day_start=hm("07:30"))
        slots = tt_mod.school_template_day(0, dft, _counter_ids())
        assert slots[0].start == hm("07:30") and slots[0].end == hm("08:20")
        assert slots[1].start == hm("08:20")  # zero gap
        assert slots[2].kind is EntryKind.BREAK
        assert slots[2].start == slots[1].end

    def test_template_passes_validate(self):
        slots = tt_mod.school_template_day(0, TimetableDefaults(), _counter_ids())
        validate(tt_(entries=slots))

    def test_crossing_midnight_is_rejected(self):
        dft = TimetableDefaults(lesson_minutes=240, day_start=hm("20:00"))
        with pytest.raises(TimetableValidationError, match="midnight"):
            tt_mod.school_template_day(0, dft, _counter_ids())


class TestResolvedSerializers:
    def test_week_dict_shape(self):
        base = std()
        tt = copy.replace(
            base,
            overrides=(
                TimetableOverride(
                    id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
                ),
                TimetableOverride(
                    id="o2",
                    date=MON,
                    kind=OverrideKind.REPLACE,
                    entry_id="m2",
                    room="B7",
                ),
            ),
        )
        d = tt_mod.resolved_week_to_dict(resolve_week(tt, WED))
        assert d["anchor"] == MON.isoformat()
        assert d["valid"] is True
        assert [day["date"] for day in d["days"]] == [
            (MON + timedelta(days=i)).isoformat() for i in range(5)
        ]
        mon = d["days"][0]
        assert mon["valid"] is True
        first = mon["lessons"][0]
        assert first == {
            "source_id": "m1",
            "date": MON.isoformat(),
            "start": "08:00",
            "end": "08:45",
            "kind": "lesson",
            "label": "1.",
            "title": "Mathe",
            "room": None,
            "teacher": None,
            "note": None,
            "color": None,
            "icon": None,
            "status": "cancelled",
            "override_id": "o1",
            "original": entry_to_dict(base.entries[0]),
        }
        changed = mon["lessons"][1]
        assert changed["status"] == "changed" and changed["room"] == "B7"
        assert mon["lessons"][2]["original"] is None
        orjson.dumps(d)  # plain JSON types only

    def test_day_dict_invalid(self):
        d = tt_mod.resolved_day_to_dict(resolve_day(std(), SAT))
        assert d == {"date": SAT.isoformat(), "valid": False, "lessons": []}

    def test_lesson_dict_added(self):
        tt = copy.replace(
            std(),
            overrides=(
                TimetableOverride(
                    id="a1",
                    date=TUE,
                    kind=OverrideKind.ADD,
                    start=hm("10:00"),
                    end=hm("10:45"),
                    title="AG",
                ),
            ),
        )
        lessons = resolve_day(tt, TUE).lessons
        d = tt_mod.effective_lesson_to_dict(lessons[-1])
        assert (
            d["status"] == "added" and d["source_id"] == "a1" and d["original"] is None
        )


class TestPublicParsers:
    def test_parse_date(self):
        assert tt_mod.parse_date("2026-09-28", "date") == MON
        for bad in ("2026-13-01", 5, None, "x"):
            with pytest.raises(TimetableValidationError):
                tt_mod.parse_date(bad, "date")

    def test_parse_hhmm(self):
        assert tt_mod.parse_hhmm("07:15", "from") == hm("07:15")
        for bad in ("7:15", "24:00", 715, None):
            with pytest.raises(TimetableValidationError):
                tt_mod.parse_hhmm(bad, "from")

    def test_parse_int(self):
        assert tt_mod.parse_int(3, "n") == 3
        for bad in (True, "3", 3.0, None):
            with pytest.raises(TimetableValidationError):
                tt_mod.parse_int(bad, "n")

    def test_parse_weekdays(self):
        assert tt_mod.parse_weekdays([4, 0, 2, 0], "days") == (0, 2, 4)
        for bad in ([], [7], [-1], ["1"], [True], "0,1", None, list(range(8)) * 2):
            with pytest.raises(TimetableValidationError):
                tt_mod.parse_weekdays(bad, "days")

    def test_parse_user_ids(self):
        assert tt_mod.parse_user_ids(["b", "a", "b"], "assignees") == ("b", "a")
        assert tt_mod.parse_user_ids([], "assignees") == ()
        for bad in ("a", [1], [""], ["x" * 65], [f"u{i}" for i in range(21)], None):
            with pytest.raises(TimetableValidationError):
                tt_mod.parse_user_ids(bad, "assignees")


class TestEntriesFromList:
    def test_mints_missing_ids_and_keeps_given(self):
        got = tt_mod.entries_from_list(
            [
                {"id": "keep", "weekday": 0, "start": "08:00", "end": "08:45"},
                {"weekday": 0, "start": "09:00", "end": "09:45"},
                {"id": None, "weekday": 1, "start": "09:00", "end": "09:45"},
            ],
            id_factory=_counter_ids(),
        )
        assert [e.id for e in got] == ["keep", "id1", "id2"]

    def test_fresh_ids_and_forced_weekday(self):
        got = tt_mod.entries_from_list(
            [{"start": "08:00", "end": "08:45", "weekday": 4}],
            id_factory=_counter_ids(),
            weekday=2,
            allow_id=False,
        )
        assert got[0].id == "id1" and got[0].weekday == 2

    def test_rejects_non_list_and_too_long(self):
        for bad in (None, {"a": 1}, "x"):
            with pytest.raises(TimetableValidationError):
                tt_mod.entries_from_list(bad, id_factory=_counter_ids())
        with pytest.raises(TimetableValidationError, match="at most"):
            tt_mod.entries_from_list([{}] * 3, id_factory=_counter_ids(), max_len=2)

    def test_bad_element(self):
        with pytest.raises(TimetableValidationError):
            tt_mod.entries_from_list(["nope"], id_factory=_counter_ids())


def test_timetable_view_dict_adds_computed_flags():
    tt = std()
    view = tt_mod.timetable_view_dict(tt, WED)
    assert view == {**to_wire_dict(tt), "active_this_week": True, "valid_today": True}
    weekend = tt_mod.timetable_view_dict(tt, SAT)
    assert weekend["valid_today"] is False and weekend["active_this_week"] is True
    held = copy.replace(tt, validity=TimetableValidity(excluded_weeks=(MON,)))
    off = tt_mod.timetable_view_dict(held, WED)
    assert off["valid_today"] is False and off["active_this_week"] is False


# ─── Lesson icons (emoji for kids who can't read yet) ─────────────────────


class TestIcon:
    @pytest.mark.parametrize(
        "icon",
        ["🔢", "✏️", "👩‍🔬", "👍🏽", "🇬🇧", "⚽", "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "1️⃣", "#️⃣", "*\u20e3", "★"],
    )
    def test_valid_emoji(self, icon):
        validate(tt_(entries=(ent("e1", 0, "08:00", "08:45", icon=icon),)))
        validate(
            tt_(
                overrides=(
                    TimetableOverride(
                        id="o1",
                        date=MON,
                        kind=OverrideKind.ADD,
                        start=hm("10:00"),
                        end=hm("10:45"),
                        icon=icon,
                    ),
                )
            )
        )

    @pytest.mark.parametrize(
        "icon",
        [
            "abc",
            "<b>",
            "🔢" * 17,
            " 🔢",
            "🔢 ",
            "\u00a0🔢",  # non-ASCII whitespace
            "",
            "日本",  # a word in another script
            "\u202e🔢",  # bidi override
            "a\u20e3",  # a keycap on a letter is not a keycap
            "\u200d",  # a bare ZWJ
            "\u0301\ufe0f",  # marks only
            "\U0001f3fd",  # a skin tone on its own
            "★★",
            "★" * 16,
            "🔢🔢",
            5,
        ],
    )
    def test_rejected(self, icon):
        with pytest.raises(TimetableValidationError, match="icon"):
            validate(tt_(entries=(ent("e1", 0, "08:00", "08:45", icon=icon),)))

    def test_sixteen_code_points_is_the_cap(self):
        # One pictograph (a tag-sequence flag) padded with tag characters.
        at_cap = "🏴" + "\U000e0067" * 15
        validate(tt_(entries=(ent("e1", 0, "08:00", "08:45", icon=at_cap),)))
        with pytest.raises(TimetableValidationError, match="icon"):
            validate(
                tt_(
                    entries=(
                        ent("e1", 0, "08:00", "08:45", icon=at_cap + "\U000e0067"),
                    )
                )
            )

    def test_mutator_turns_empty_into_none(self):
        out = with_entry(tt_(), ent("e1", 0, "08:00", "08:45", icon=""), now=NOW)
        assert out.entries[0].icon is None

    def test_mutator_does_not_strip_icon(self):
        with pytest.raises(TimetableValidationError, match="icon"):
            with_entry(tt_(), ent("e1", 0, "08:00", "08:45", icon=" 🔢"), now=NOW)

    def test_cancel_cannot_carry_icon(self):
        with pytest.raises(TimetableValidationError, match="cancel"):
            validate(
                copy.replace(
                    std(),
                    overrides=(
                        TimetableOverride(
                            id="o1",
                            date=MON,
                            kind=OverrideKind.CANCEL,
                            entry_id="m1",
                            icon="🔢",
                        ),
                    ),
                )
            )

    def test_replace_changes_icon_and_original_keeps_old(self):
        base = copy.replace(
            std(),
            entries=tuple(
                copy.replace(e, icon="🔢") if e.id == "m1" else e for e in std().entries
            ),
        )
        tt = with_override(
            base,
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="m1", icon="🎨"
            ),
            now=NOW,
        )
        lesson = resolve_day(tt, MON).lessons[0]
        assert lesson.status is LessonStatus.CHANGED
        assert lesson.icon == "🎨"
        assert lesson.original is not None and lesson.original.icon == "🔢"
        d = tt_mod.effective_lesson_to_dict(lesson)
        assert d["icon"] == "🎨" and d["original"]["icon"] == "🔢"

    def test_normal_and_added_lessons_carry_icon(self):
        tt = copy.replace(
            tt_(entries=(ent("e1", 0, "08:00", "08:45", icon="🔢"),)),
            overrides=(
                TimetableOverride(
                    id="a1",
                    date=MON,
                    kind=OverrideKind.ADD,
                    start=hm("09:00"),
                    end=hm("09:45"),
                    icon="⚽",
                ),
            ),
        )
        lessons = resolve_day(tt, MON).lessons
        assert [ls.icon for ls in lessons] == ["🔢", "⚽"]

    def test_wire_round_trip(self):
        tt = copy.replace(
            tt_(entries=(ent("e1", 0, "08:00", "08:45", icon="👩‍🔬"),)),
            overrides=(
                TimetableOverride(
                    id="o1",
                    date=MON,
                    kind=OverrideKind.REPLACE,
                    entry_id="e1",
                    icon="🇬🇧",
                ),
            ),
        )
        wire = orjson.loads(orjson.dumps(to_wire_dict(tt)))
        assert wire["entries"][0]["icon"] == "👩‍🔬"
        assert wire["overrides"][0]["icon"] == "🇬🇧"
        assert from_wire_dict(wire) == tt
        # A pre-icon row / payload reads back as no icon.
        del wire["entries"][0]["icon"]
        assert from_wire_dict(wire).entries[0].icon is None

    def test_patch_sets_and_clears_icon(self):
        e = ent("e1", 0, "08:00", "08:45")
        assert entry_patch(e, {"icon": "⚽"}).icon == "⚽"
        assert entry_patch(copy.replace(e, icon="⚽"), {"icon": None}).icon is None
        ov = TimetableOverride(
            id="o1", date=MON, kind=OverrideKind.REPLACE, entry_id="e1"
        )
        assert override_patch(ov, {"icon": "🎨"}).icon == "🎨"
        with pytest.raises(TimetableValidationError):
            entry_patch(e, {"icon": 7})

    def test_school_template_has_no_icons(self):
        slots = tt_mod.school_template_day(0, TimetableDefaults(), _counter_ids())
        assert all(s.icon is None for s in slots)

    def test_copy_day_without_subjects_clears_lesson_icon(self):
        tt = tt_(
            entries=(
                ent("e1", 0, "08:00", "08:45", icon="🔢", title="Mathe"),
                ent("b1", 0, "08:45", "09:00", kind=EntryKind.BREAK, icon="🍎"),
            )
        )
        out = copy_day(
            tt, 0, [1], with_subjects=False, id_factory=_counter_ids(), now=NOW
        )
        tue = sorted((e for e in out.entries if e.weekday == 1), key=lambda e: e.start)
        assert [e.icon for e in tue] == [None, "🍎"]


# ─── Review hardening: date range, surrogates, unknown keys, ids ─────────


class TestDateRange:
    @pytest.mark.parametrize(
        "bad", ["0001-01-01", "1899-12-31", "2200-01-01", "9999-12-31"]
    )
    def test_parse_date_bounded(self, bad):
        with pytest.raises(TimetableValidationError, match="1900"):
            tt_mod.parse_date(bad, "date")

    def test_parse_date_edges_ok(self):
        assert tt_mod.parse_date("1900-01-01", "d") == date(1900, 1, 1)
        assert tt_mod.parse_date("2199-12-31", "d") == date(2199, 12, 31)

    @pytest.mark.parametrize(
        "d", [date(1, 1, 1), date(9999, 12, 31), date(1899, 12, 31)]
    )
    def test_week_anchor_and_resolve_refuse_out_of_range(self, d):
        for ws in (0, 6):
            with pytest.raises(TimetableValidationError):
                week_anchor(d, ws)
        with pytest.raises(TimetableValidationError):
            resolve_week(std(), d)
        with pytest.raises(TimetableValidationError):
            resolve_day(std(), d)

    def test_validate_checks_validity_and_override_dates(self):
        with pytest.raises(TimetableValidationError, match="1900"):
            validate(tt_(validity=TimetableValidity(valid_from=date(1, 1, 1))))
        with pytest.raises(TimetableValidationError, match="1900"):
            validate(tt_(validity=TimetableValidity(valid_until=date(9999, 12, 31))))
        with pytest.raises(TimetableValidationError, match="1900"):
            validate(tt_(validity=TimetableValidity(excluded_weeks=(date(1, 1, 1),))))
        with pytest.raises(TimetableValidationError, match="1900"):
            validate(
                copy.replace(
                    std(),
                    overrides=(
                        TimetableOverride(
                            id="o1",
                            date=date(9999, 12, 27),  # a Monday
                            kind=OverrideKind.CANCEL,
                            entry_id="m1",
                        ),
                    ),
                )
            )

    def test_week_start_change_at_the_edge_is_a_validation_error(self):
        # 1900-01-01 is a Monday; under Sunday-start its anchor is 1899-12-31.
        tt = tt_(validity=TimetableValidity(excluded_weeks=(date(1900, 1, 1),)))
        validate(tt)
        with pytest.raises(TimetableValidationError):
            with_header(tt, now=NOW, week_start=6)

    def test_clear_week_out_of_range(self):
        with pytest.raises(TimetableValidationError):
            clear_week(std(), date(9999, 12, 31), now=NOW)


class TestSurrogates:
    def test_lone_surrogate_title(self):
        with pytest.raises(TimetableValidationError, match="UTF-8"):
            validate(tt_(entries=(ent("e1", 0, "08:00", "08:45", title="\ud800"),)))

    def test_lone_surrogate_name(self):
        with pytest.raises(TimetableValidationError, match="UTF-8"):
            with_header(tt_(), now=NOW, name="Anna \udfff")


class TestRejectUnknown:
    def test_allows_known_rejects_unknown(self):
        tt_mod.reject_unknown({"a": 1}, frozenset({"a", "b"}), "thing")
        with pytest.raises(TimetableValidationError, match="unknown field"):
            tt_mod.reject_unknown({"a": 1, "zz": 2}, frozenset({"a"}), "thing")
        with pytest.raises(TimetableValidationError):
            tt_mod.reject_unknown([1], frozenset({"a"}), "thing")

    def test_field_sets(self):
        assert tt_mod.ENTRY_FIELDS == frozenset(
            entry_to_dict(ent("e", 0, "08:00", "08:45"))
        )
        o = TimetableOverride(id="o", date=MON, kind=OverrideKind.CANCEL, entry_id="e")
        assert tt_mod.OVERRIDE_FIELDS == frozenset(override_to_dict(o))

    def test_entries_from_list_rejects_unknown(self):
        with pytest.raises(TimetableValidationError, match="unknown field"):
            tt_mod.entries_from_list(
                [{"weekday": 0, "start": "08:00", "end": "08:45", "bogus": 1}],
                id_factory=_counter_ids(),
            )

    def test_entries_from_list_id_disallowed_when_minting(self):
        with pytest.raises(TimetableValidationError, match="unknown field"):
            tt_mod.entries_from_list(
                [{"id": "x", "start": "08:00", "end": "08:45"}],
                id_factory=_counter_ids(),
                weekday=0,
                allow_id=False,
            )

    def test_entries_from_list_keeps_only_known_ids(self):
        got = tt_mod.entries_from_list(
            [
                {"id": "keep", "weekday": 0, "start": "08:00", "end": "08:45"},
                {"id": "stranger", "weekday": 0, "start": "09:00", "end": "09:45"},
            ],
            id_factory=_counter_ids(),
            keep_ids=frozenset({"keep"}),
        )
        assert [e.id for e in got] == ["keep", "id1"]

    def test_from_wire_dict_stays_lenient(self):
        wire = to_wire_dict(std())
        wire["future_field"] = 1
        wire["entries"][0]["future_field"] = 1
        assert from_wire_dict(wire) == std()


class TestIdCharset:
    @pytest.mark.parametrize("bad", ["a b", "a/b", "ä", "a.b", "<x>", ""])
    def test_entry_id(self, bad):
        with pytest.raises(TimetableValidationError, match="id"):
            validate(tt_(entries=(ent(bad, 0, "08:00", "08:45"),)))

    @pytest.mark.parametrize("field", ["id", "created_by", "updated_by"])
    def test_header_ids(self, field):
        with pytest.raises(TimetableValidationError):
            validate(tt_(**{field: "x y"}))

    def test_assignees_and_override_ids(self):
        with pytest.raises(TimetableValidationError):
            validate(tt_(assignees=("ok", "not ok")))
        with pytest.raises(TimetableValidationError):
            validate(
                copy.replace(
                    std(),
                    overrides=(
                        TimetableOverride(
                            id="o 1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"
                        ),
                    ),
                )
            )
        with pytest.raises(TimetableValidationError):
            tt_mod.parse_user_ids(["bad id"], "assignees")

    def test_real_ids_pass(self):
        # uuid4().hex, base32 derived user ids, fixtures with dashes.
        validate(
            tt_(
                id="9f1c2e3d4b5a69788796a5b4c3d2e1f0",
                created_by="ftsmmf26uygffxkyblbo3rgj336pst4y",
                assignees=("uid-alice", "u_bob"),
            )
        )


# ─── Today on the absolute time line (home screen) ───────────────────────


class TestTodayTimetable:
    def test_lessons_carry_utc_instants(self):
        tt = copy.replace(std(), tz="Europe/Berlin", color="sky")
        today = tt_mod.today_timetable(tt, MON)
        assert today is not None
        assert (today.timetable_id, today.name, today.color, today.tz) == (
            "tt-1",
            "Anna 5b",
            "sky",
            "Europe/Berlin",
        )
        assert today.date == MON
        first = today.lessons[0]
        assert first.lesson.title == "Mathe"
        assert first.start_at == datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)
        assert first.end_at == datetime(2026, 9, 28, 6, 45, tzinfo=timezone.utc)
        assert [ls.lesson.source_id for ls in today.lessons] == ["m1", "m2", "mb"]

    def test_invalid_day_is_none(self):
        tt = copy.replace(std(), validity=TimetableValidity(excluded_weeks=(MON,)))
        assert tt_mod.today_timetable(tt, MON) is None
        assert tt_mod.today_timetable(std(), SAT) is None

    def test_cancelled_lessons_are_kept(self):
        tt = copy.replace(
            std(),
            overrides=(
                TimetableOverride(
                    id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m2"
                ),
            ),
        )
        today = tt_mod.today_timetable(tt, MON)
        assert today is not None
        status = {ls.lesson.source_id: ls.lesson.status for ls in today.lessons}
        assert status["m2"] is LessonStatus.CANCELLED

    def test_untitled_slots_are_dropped_before_the_cap(self):
        tt = tt_(
            entries=(
                ent("u1", 0, "07:00", "07:45", label="0."),  # untitled slot
                ent("u2", 0, "07:50", "08:35", label="1."),
                ent("i1", 0, "08:40", "09:25", icon="🔢"),  # icon only
                ent("m1", 0, "09:30", "10:15", title="Mathe"),
                ent("b1", 0, "10:15", "10:30", kind=EntryKind.BREAK),
            )
        )
        today = tt_mod.today_timetable(tt, MON, max_lessons=2)
        assert today is not None
        assert [ls.lesson.source_id for ls in today.lessons] == ["i1", "m1"]
        full = tt_mod.today_timetable(tt, MON)
        assert full is not None
        assert [ls.lesson.source_id for ls in full.lessons] == ["i1", "m1", "b1"]

    def test_capped(self):
        tt = copy.replace(std(), tz="UTC")
        today = tt_mod.today_timetable(tt, MON, max_lessons=2)
        assert today is not None
        assert [ls.lesson.source_id for ls in today.lessons] == ["m1", "m2"]

    def test_dst_day(self):
        # A Sunday lesson across the spring-forward gap stays ordered.
        tt = tt_(
            tz="Europe/Berlin",
            days=(6,),
            entries=(ent("s1", 6, "01:30", "02:30", title="Nacht"),),
        )
        today = tt_mod.today_timetable(tt, date(2026, 3, 29))
        assert today is not None
        ls = today.lessons[0]
        assert ls.start_at == datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)
        assert ls.end_at == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
        assert ls.end_at > ls.start_at

    def test_to_dict_flattens_the_lesson(self):
        today = tt_mod.today_timetable(copy.replace(std(), tz="Europe/Berlin"), MON)
        assert today is not None
        d = tt_mod.today_timetable_to_dict(today)
        assert set(d) == {"timetable_id", "name", "color", "tz", "date", "lessons"}
        assert d["date"] == "2026-09-28"
        first = d["lessons"][0]
        assert first["title"] == "Mathe"
        assert first["start"] == "08:00"
        assert first["status"] == "normal"
        assert first["start_at"] == "2026-09-28T06:00:00+00:00"
        assert first["end_at"] == "2026-09-28T06:45:00+00:00"


class TestWireTimestampsAndVersions:
    """Hostile wire timestamps and version jumps (security review M1 / M2)."""

    def test_the_wire_cap_fits_the_double_sealed_relay_path(self):
        # See tests/federation/sync/space/exporters/test_timetables.py.
        assert MAX_WIRE_BYTES == 96 * 1024

    @pytest.mark.parametrize(
        "value",
        [
            "0001-01-01T00:00:00+14:00",  # astimezone(UTC) overflows
            "9999-12-31T23:59:59-14:00",
            "1969-12-31T23:59:59+00:00",
            "2200-01-01T00:00:00+00:00",
            "2026-06-01T10:00:00",  # naive
            "yesterday",
            7,
        ],
    )
    def test_an_unusable_timestamp_is_a_validation_error(self, value):
        with pytest.raises(TimetableValidationError):
            tt_mod.parse_datetime(value, "at")
        wire = to_wire_dict(std())
        wire["updated_at"] = value
        with pytest.raises(TimetableValidationError):
            from_wire_dict(wire)

    def test_timestamps_are_normalised_to_utc(self):
        got = tt_mod.parse_datetime("2026-06-01T12:00:00+02:00", "at")
        assert got == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        assert got.utcoffset() == timedelta(0)

    def test_remote_version_refusal(self):
        jump = tt_mod.MAX_REMOTE_VERSION_JUMP
        cap = tt_mod.MAX_VERSION
        assert tt_mod.remote_version_refusal(1, None) is None
        assert tt_mod.remote_version_refusal(1 + jump, 1) is None
        assert tt_mod.remote_version_refusal(2 + jump, 1) is not None
        assert tt_mod.remote_version_refusal(cap - jump, None) is not None
        assert tt_mod.remote_version_refusal(cap - jump - 1, None) is None
