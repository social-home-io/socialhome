"""Tests for socialhome.services.timetable_service (household scope)."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import TimetableDeleted, TimetableSaved
from socialhome.domain.preferences import FeatureDisabledError, HouseholdPreferences
from socialhome.domain.timetable import entry_to_dict as td_entry_to_dict
from socialhome.domain.timetable import (
    EntryKind,
    LessonStatus,
    OverrideKind,
    Timetable,
    TimetableConflictError,
    TimetableLimitError,
    TimetableOrphanError,
    TimetableOverride,
    TimetableValidationError,
    week_anchor,
)
from socialhome.repositories.timetable_repo import SqliteTimetableRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import timetable_service as ts
from socialhome.services.timetable_service import (
    MAX_TIMETABLES,
    TimetableEditorMixin,
    TimetableService,
)

#: The Monday of next week, from the service's own clock — overrides are
#: pruned 14 days after their date, so a fixed date would rot in CI.
MON = week_anchor(ts._utcnow().date() + timedelta(days=7), 0)


class _Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class _Prefs:
    """Stand-in for PreferencesService: toggle + household tz."""

    def __init__(self, *, enabled=True, tz="Europe/Berlin"):
        self.enabled = enabled
        self.tz = tz

    async def get_household(self):
        return HouseholdPreferences(tz=self.tz, feat_timetable=self.enabled)

    async def require_enabled(self, section):
        prefs = await self.get_household()
        prefs.require_enabled(section)
        return prefs


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    for username, uid, state in (
        ("anna", "u-anna", "active"),
        ("ben", "u-ben", "active"),
        ("gone", "u-gone", "inactive"),
    ):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, state) VALUES(?,?,?,?)",
            (username, uid, username.title(), state),
        )

    class Env:
        pass

    e = Env()
    e.db = db
    e.repo = SqliteTimetableRepo(db)
    e.bus = _Bus()
    e.prefs = _Prefs()
    e.svc = TimetableService(e.repo, e.bus, user_repo=SqliteUserRepo(db))
    e.svc.attach_household_features(e.prefs)
    yield e
    await db.shutdown()


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


async def _empty(env, **kw) -> Timetable:
    kw.setdefault("name", "Anna 5b")
    kw.setdefault("created_by", "u-anna")
    return await env.svc.create(template="empty", **kw)


# ─── Feature gate ────────────────────────────────────────────────────────


async def test_every_public_method_is_gated(env):
    tt = await _empty(env)
    env.prefs.enabled = False
    calls = [
        env.svc.list_all(),
        env.svc.get(tt.id),
        env.svc.create(name="x", created_by="u-anna"),
        env.svc.update(tt.id, version=1, by="u-anna", name="y"),
        env.svc.delete(tt.id, by="u-anna"),
        env.svc.duplicate(tt.id, name="z", by="u-anna"),
        env.svc.resolve_week(tt.id, MON),
        env.svc.resolve_day(tt.id, MON),
        env.svc.active_for_user("u-anna", MON),
        env.svc.household_today(),
    ]
    for coro in calls:
        with pytest.raises(FeatureDisabledError):
            await coro


async def test_works_without_household_wiring(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t2.db", batch_timeout_ms=10)
    await db.startup()
    try:
        svc = TimetableService(SqliteTimetableRepo(db), None)
        tt = await svc.create(name="Solo", created_by="u-x", template="empty")
        assert tt.tz == "UTC"  # no household → UTC
        assert tt.assignees == ("u-x",)  # no user repo → unchecked
        assert (await svc.household_today()) == ts._utcnow().date()
    finally:
        await db.shutdown()


# ─── create ──────────────────────────────────────────────────────────────


async def test_create_school_template(env):
    tt = await env.svc.create(name="  Anna 5b ", created_by="u-anna")
    assert tt.name == "Anna 5b"
    assert tt.version == 1
    assert tt.tz == "Europe/Berlin"  # household tz
    assert tt.assignees == ("u-anna",)
    assert tt.days == (0, 1, 2, 3, 4)
    assert len(tt.entries) == 8 * 5
    monday = sorted((e for e in tt.entries if e.weekday == 0), key=lambda e: e.start)
    assert [(e.start, e.end) for e in monday][:3] == [
        (_hm("08:00"), _hm("08:45")),
        (_hm("08:50"), _hm("09:35")),
        (_hm("09:35"), _hm("09:55")),
    ]
    assert monday[-1].end == _hm("13:20")
    assert len({e.id for e in tt.entries}) == len(tt.entries)
    assert await env.repo.get(tt.id) == tt
    assert env.bus.events == [
        TimetableSaved(timetable=tt, occurred_at=env.bus.events[0].occurred_at)
    ]


async def test_create_empty_with_options(env):
    tt = await env.svc.create(
        name="Ben",
        created_by="u-anna",
        template="empty",
        week_start=6,
        days=(6, 0, 1),
        tz="America/New_York",
        assignees=["u-ben", "u-anna"],
        color="teal",
    )
    assert tt.entries == ()
    assert tt.days == (0, 1, 6)
    assert tt.week_start == 6
    assert tt.tz == "America/New_York"
    assert tt.assignees == ("u-ben", "u-anna")
    assert tt.color == "teal"


@pytest.mark.parametrize(
    "kw",
    [
        {"name": "   "},
        {"template": "nope"},
        {"tz": "Mars/Base"},
        {"color": "#ff0000"},
        {"week_start": 3},
        {"assignees": ["u-nobody"]},
        {"assignees": ["u-gone"]},  # inactive user
    ],
)
async def test_create_rejects(env, kw):
    kw.setdefault("name", "Anna")
    with pytest.raises(TimetableValidationError):
        await env.svc.create(created_by="u-anna", **kw)
    assert await env.repo.count() == 0


async def test_create_limit(env):
    for i in range(MAX_TIMETABLES):
        await _empty(env, name=f"T{i}")
    with pytest.raises(TimetableLimitError):
        await _empty(env, name="one too many")


# ─── reads ───────────────────────────────────────────────────────────────


async def test_get_list_and_missing(env):
    a = await _empty(env, name="B")
    b = await _empty(env, name="A")
    assert [t.id for t in await env.svc.list_all()] == [b.id, a.id]
    assert (await env.svc.get(a.id)).id == a.id
    with pytest.raises(KeyError):
        await env.svc.get("missing")


# ─── _mutate / CAS ───────────────────────────────────────────────────────


async def test_update_header_bumps_version_and_emits(env):
    tt = await _empty(env)
    env.bus.events.clear()
    new = await env.svc.update(
        tt.id, version=1, by="u-ben", name="Renamed", color="rose"
    )
    assert new.version == 2
    assert new.updated_by == "u-ben"
    assert new.name == "Renamed" and new.color == "rose"
    assert await env.repo.get(tt.id) == new
    assert [type(e) for e in env.bus.events] == [TimetableSaved]
    assert env.bus.events[0].timetable == new
    assert env.bus.events[0].space_id is None


async def test_stale_version_conflicts(env):
    tt = await _empty(env)
    await env.svc.update(tt.id, version=1, by="u-anna", name="v2")
    with pytest.raises(TimetableConflictError) as exc:
        await env.svc.update(tt.id, version=1, by="u-anna", name="lost")
    assert exc.value.current_version == 2


async def test_store_race_reports_reloaded_version(env):
    """The repo CAS failing (a concurrent writer) → conflict with the new version."""
    tt = await _empty(env)

    real_save = env.repo.save

    async def racing_save(new, *, expected_version):
        # Another writer lands first.
        await real_save(
            Timetable(**{**_fields(new), "name": "other", "version": 2}),
            expected_version=1,
        )
        return await real_save(new, expected_version=expected_version)

    env.repo.save = racing_save
    with pytest.raises(TimetableConflictError) as exc:
        await env.svc.update(tt.id, version=1, by="u-anna", name="mine")
    assert exc.value.current_version == 2


def _fields(tt: Timetable) -> dict:
    return {f: getattr(tt, f) for f in tt.__dataclass_fields__}


async def test_update_missing_is_keyerror(env):
    with pytest.raises(KeyError):
        await env.svc.update("missing", version=1, by="u-anna", name="x")


async def test_update_days_orphans(env):
    tt = await env.svc.create(name="Anna", created_by="u-anna")  # school
    with pytest.raises(TimetableOrphanError) as exc:
        await env.svc.update(tt.id, version=1, by="u-anna", days=[0, 1, 2, 3])
    assert exc.value.code == "DAYS_ORPHAN_ENTRIES" and exc.value.count == 8
    new = await env.svc.update(
        tt.id, version=1, by="u-anna", days=[0, 1, 2, 3], drop_orphans=True
    )
    assert new.days == (0, 1, 2, 3)
    assert all(e.weekday != 4 for e in new.entries)


async def test_update_defaults_merge_and_unknown_key(env):
    tt = await _empty(env)
    new = await env.svc.update(
        tt.id, version=1, by="u-anna", defaults={"lesson_minutes": 50}
    )
    assert new.defaults.lesson_minutes == 50
    assert new.defaults.gap_minutes == 5  # kept
    with pytest.raises(TimetableValidationError):
        await env.svc.update(tt.id, version=2, by="u-anna", defaults={"bogus": 1})
    with pytest.raises(TimetableValidationError):
        await env.svc.update(tt.id, version=2, by="u-anna", defaults="x")


async def test_update_assignees_checked(env):
    tt = await _empty(env)
    new = await env.svc.update(tt.id, version=1, by="u-anna", assignees=["u-ben"])
    assert new.assignees == ("u-ben",)
    with pytest.raises(TimetableValidationError):
        await env.svc.update(tt.id, version=2, by="u-anna", assignees=["u-nobody"])


async def test_mutation_prunes_old_overrides_in_same_version(env):
    tt = await _empty(env)
    tt = await env.svc.add_entry(
        tt.id,
        version=1,
        by="u-anna",
        fields={"weekday": 0, "start": "08:00", "end": "08:45"},
    )
    old = TimetableOverride(
        id="old",
        date=MON - timedelta(days=70),
        kind=OverrideKind.CANCEL,
        entry_id=tt.entries[0].id,
    )
    seeded = Timetable(**{**_fields(tt), "overrides": (old,)})
    assert await env.repo.save(seeded, expected_version=2)
    new = await env.svc.update(tt.id, version=2, by="u-anna", name="x")
    assert new.version == 3
    assert new.overrides == ()
    assert (await env.repo.get(tt.id)).overrides == ()


async def test_noop_mutation_keeps_version_and_emits_nothing(env):
    tt = await _empty(env)
    env.bus.events.clear()
    same = await env.svc.shift_after(
        tt.id, 0, from_time=_hm("08:00"), minutes=10, version=1, by="u-anna"
    )
    assert same.version == 1
    assert env.bus.events == []


# ─── delete / duplicate ──────────────────────────────────────────────────


async def test_delete(env):
    tt = await _empty(env)
    env.bus.events.clear()
    await env.svc.delete(tt.id, by="u-anna")
    assert await env.repo.get(tt.id) is None
    assert env.bus.events[0] == TimetableDeleted(
        timetable_id=tt.id, occurred_at=env.bus.events[0].occurred_at
    )
    with pytest.raises(KeyError):
        await env.svc.delete(tt.id, by="u-anna")


async def test_delete_race_is_keyerror(env):
    tt = await _empty(env)

    async def gone(_id):
        return False

    env.repo.delete = gone
    with pytest.raises(KeyError):
        await env.svc.delete(tt.id, by="u-anna")


async def test_duplicate_remaps_ids(env):
    src = await env.svc.create(name="Anna", created_by="u-anna", assignees=["u-ben"])
    first = next(e for e in src.entries if e.weekday == 0)
    src = await env.svc.add_override(
        src.id,
        version=1,
        by="u-anna",
        fields={"date": MON.isoformat(), "kind": "cancel", "entry_id": first.id},
    )
    dup = await env.svc.duplicate(src.id, name="Anna copy", by="u-ben")
    assert dup.id != src.id
    assert dup.name == "Anna copy"
    assert dup.version == 1
    assert dup.created_by == "u-ben"
    assert dup.assignees == src.assignees
    assert not {e.id for e in dup.entries} & {e.id for e in src.entries}
    assert len(dup.entries) == len(src.entries)
    (ov,) = dup.overrides
    assert ov.id != src.overrides[0].id
    mapped = next(e for e in dup.entries if e.id == ov.entry_id)
    assert (mapped.weekday, mapped.start) == (first.weekday, first.start)
    assert await env.repo.get(dup.id) == dup


async def test_duplicate_default_name_and_limit(env):
    src = await _empty(env, name="x" * 60)
    dup = await env.svc.duplicate(src.id, name=None, by="u-anna")
    assert dup.name.endswith("(copy)") and len(dup.name) <= 60
    for i in range(MAX_TIMETABLES - 2):
        await _empty(env, name=f"T{i}")
    with pytest.raises(TimetableLimitError):
        await env.svc.duplicate(src.id, name="more", by="u-anna")


# ─── entries ─────────────────────────────────────────────────────────────


async def test_entry_crud(env):
    tt = await _empty(env)
    tt = await env.svc.add_entry(
        tt.id,
        version=1,
        by="u-anna",
        fields={"weekday": 0, "start": "08:00", "end": "08:45"},
    )
    (e,) = tt.entries
    tt = await env.svc.update_entry(
        tt.id, e.id, version=2, by="u-anna", fields={"title": "Mathe", "room": "B7"}
    )
    assert tt.entries[0].title == "Mathe" and tt.entries[0].room == "B7"
    with pytest.raises(TimetableValidationError):
        await env.svc.update_entry(
            tt.id, e.id, version=3, by="u-anna", fields={"bogus": 1}
        )
    with pytest.raises(KeyError):
        await env.svc.update_entry(tt.id, "nope", version=3, by="u-anna", fields={})
    with pytest.raises(KeyError):
        await env.svc.delete_entry(tt.id, "nope", version=3, by="u-anna")
    tt = await env.svc.delete_entry(tt.id, e.id, version=3, by="u-anna")
    assert tt.entries == ()


async def test_add_entry_overlap_rejected(env):
    tt = await _empty(env)
    tt = await env.svc.add_entry(
        tt.id,
        version=1,
        by="u-anna",
        fields={"weekday": 0, "start": "08:00", "end": "08:45"},
    )
    with pytest.raises(TimetableValidationError):
        await env.svc.add_entry(
            tt.id,
            version=2,
            by="u-anna",
            fields={"weekday": 0, "start": "08:30", "end": "09:00"},
        )


async def test_replace_entries_on_empty_mints_every_id(env):
    tt = await _empty(env)
    tt = await env.svc.replace_entries(
        tt.id,
        version=1,
        by="u-anna",
        entries=[
            {"id": "keep", "weekday": 1, "start": "08:00", "end": "08:45"},
            {"weekday": 1, "start": "09:00", "end": "09:45"},
        ],
    )
    ids = {e.id for e in tt.entries}
    assert "keep" not in ids and len(ids) == 2


async def test_generate_day(env):
    tt = await env.svc.create(name="Anna", created_by="u-anna")
    slots = [
        {"start": "07:15", "end": "07:45", "title": "Musik"},
        {"start": "08:00", "end": "08:45", "weekday": 3},
    ]
    with pytest.raises(TimetableOrphanError) as exc:
        await env.svc.generate_day(tt.id, 0, version=1, by="u-anna", slots=slots)
    assert exc.value.code == "DAY_HAS_ENTRIES" and exc.value.count == 8
    tt = await env.svc.generate_day(
        tt.id, 0, version=1, by="u-anna", slots=slots, replace=True
    )
    monday = sorted((e for e in tt.entries if e.weekday == 0), key=lambda e: e.start)
    assert [e.start for e in monday] == [_hm("07:15"), _hm("08:00")]


async def test_copy_and_shift(env):
    tt = await _empty(env)
    tt = await env.svc.generate_day(
        tt.id,
        0,
        version=1,
        by="u-anna",
        slots=[
            {"start": "08:00", "end": "08:45", "title": "Mathe"},
            {"start": "08:45", "end": "09:00", "kind": "break", "title": "Pause"},
        ],
    )
    tt = await env.svc.copy_day(
        tt.id, 0, to_weekdays=[1, 2], with_subjects=False, version=2, by="u-anna"
    )
    tue = sorted((e for e in tt.entries if e.weekday == 1), key=lambda e: e.start)
    assert [e.title for e in tue] == [None, "Pause"]
    assert len(tt.entries) == 6
    tt = await env.svc.shift_after(
        tt.id, 1, from_time=_hm("08:00"), minutes=15, version=3, by="u-anna"
    )
    tue = sorted((e for e in tt.entries if e.weekday == 1), key=lambda e: e.start)
    assert tue[0].start == _hm("08:15")
    with pytest.raises(TimetableValidationError):
        await env.svc.shift_after(
            tt.id, 1, from_time=_hm("08:00"), minutes=5000, version=4, by="u-anna"
        )


# ─── validity + overrides ────────────────────────────────────────────────


async def test_validity_and_active_for_user(env):
    anna = await env.svc.create(name="Anna", created_by="u-anna")
    ben = await env.svc.create(name="Ben", created_by="u-anna", assignees=["u-ben"])
    assert [t.id for t in await env.svc.active_for_user("u-anna", MON)] == [anna.id]
    assert await env.svc.active_for_user("u-anna", MON + timedelta(days=5)) == []
    anna = await env.svc.set_validity(
        anna.id,
        version=1,
        by="u-anna",
        valid_from=None,
        valid_until=None,
        excluded_weeks=[MON + timedelta(days=2)],  # snapped to the anchor
    )
    assert anna.validity.excluded_weeks == (MON,)
    assert await env.svc.active_for_user("u-anna", MON) == []
    assert [t.id for t in await env.svc.active_for_user("u-ben", MON)] == [ben.id]
    with pytest.raises(TimetableValidationError):
        await env.svc.set_validity(
            anna.id,
            version=2,
            by="u-anna",
            valid_from=MON,
            valid_until=MON - timedelta(days=1),
            excluded_weeks=[],
        )


async def test_override_crud_and_resolve(env):
    tt = await env.svc.create(name="Anna", created_by="u-anna")
    first = min((e for e in tt.entries if e.weekday == 0), key=lambda e: e.start)
    tt = await env.svc.add_override(
        tt.id,
        version=1,
        by="u-anna",
        fields={
            "date": MON.isoformat(),
            "kind": "replace",
            "entry_id": first.id,
            "room": "Aula",
        },
    )
    (ov,) = tt.overrides
    day = await env.svc.resolve_day(tt.id, MON)
    assert day.lessons[0].status is LessonStatus.CHANGED
    assert day.lessons[0].room == "Aula"
    tt = await env.svc.update_override(
        tt.id, ov.id, version=2, by="u-anna", fields={"kind": "cancel", "room": None}
    )
    assert tt.overrides[0].kind is OverrideKind.CANCEL
    with pytest.raises(KeyError):
        await env.svc.update_override(tt.id, "nope", version=3, by="u-anna", fields={})
    with pytest.raises(KeyError):
        await env.svc.delete_override(tt.id, "nope", version=3, by="u-anna")
    tt = await env.svc.delete_override(tt.id, ov.id, version=3, by="u-anna")
    assert tt.overrides == ()
    week = await env.svc.resolve_week(tt.id, MON + timedelta(days=3))
    assert week.anchor == MON and len(week.days) == 5


async def test_add_override_on_hidden_day_rejected(env):
    tt = await env.svc.create(name="Anna", created_by="u-anna")
    with pytest.raises(TimetableValidationError):
        await env.svc.add_override(
            tt.id,
            version=1,
            by="u-anna",
            fields={
                "date": (MON + timedelta(days=5)).isoformat(),
                "kind": "add",
                "start": "10:00",
                "end": "11:00",
            },
        )


async def test_clear_week(env):
    tt = await env.svc.create(name="Anna", created_by="u-anna")
    for i, d in enumerate((MON, MON + timedelta(days=1), MON + timedelta(days=7))):
        tt = await env.svc.add_override(
            tt.id,
            version=tt.version,
            by="u-anna",
            fields={
                "date": d.isoformat(),
                "kind": "add",
                "start": "15:00",
                "end": "16:00",
                "entry_kind": EntryKind.LESSON.value,
            },
        )
    tt = await env.svc.clear_week(
        tt.id, MON + timedelta(days=4), version=tt.version, by="u-anna"
    )
    assert [o.date for o in tt.overrides] == [MON + timedelta(days=7)]


# ─── Mixin shape ─────────────────────────────────────────────────────────


def test_editor_mixin_is_behaviour_only():
    assert TimetableEditorMixin.__slots__ == ()
    assert TimetableEditorMixin.today_in("UTC") == ts._utcnow().date()
    # An unknown / hostile tz falls back to UTC rather than raising.
    assert TimetableEditorMixin.today_in("Mars/Base") == ts._utcnow().date()


async def test_household_today_uses_household_tz(env):
    env.prefs.tz = "Pacific/Kiritimati"  # UTC+14
    today = await env.svc.household_today()
    assert today == TimetableEditorMixin.today_in("Pacific/Kiritimati")


# ─── Review hardening ────────────────────────────────────────────────────


async def test_clock_seam_is_utc_aware():
    now = ts._utcnow()
    assert now.tzinfo is not None
    assert abs(now - datetime.now(timezone.utc)) < timedelta(days=366)


async def test_add_entry_and_override_are_strict_about_keys(env):
    tt = await env.svc.create(name="Anna", created_by="u-anna")
    first = min((e for e in tt.entries if e.weekday == 0), key=lambda e: e.start)
    for fields in (
        {"id": "client", "weekday": 0, "start": "14:00", "end": "14:45"},
        {"weekday": 0, "start": "14:00", "end": "14:45", "bogus": 1},
    ):
        with pytest.raises(TimetableValidationError, match="unknown field"):
            await env.svc.add_entry(tt.id, version=1, by="u-anna", fields=fields)
    for fields in (
        {"id": "x", "date": MON.isoformat(), "kind": "cancel", "entry_id": first.id},
        {"date": MON.isoformat(), "kind": "cancel", "entry_id": first.id, "zz": 1},
    ):
        with pytest.raises(TimetableValidationError, match="unknown field"):
            await env.svc.add_override(tt.id, version=1, by="u-anna", fields=fields)
    with pytest.raises(TimetableValidationError, match="unknown field"):
        await env.svc.generate_day(
            tt.id,
            0,
            version=1,
            by="u-anna",
            slots=[{"id": "x", "start": "08:00", "end": "08:45"}],
            replace=True,
        )


async def test_replace_entries_mints_ids_the_timetable_does_not_have(env):
    tt = await _empty(env)
    tt = await env.svc.add_entry(
        tt.id,
        version=1,
        by="u-anna",
        fields={"weekday": 0, "start": "08:00", "end": "08:45"},
    )
    known = tt.entries[0].id
    tt = await env.svc.replace_entries(
        tt.id,
        version=2,
        by="u-anna",
        entries=[
            {"id": known, "weekday": 0, "start": "08:00", "end": "08:45"},
            {"id": "chosen-by-client", "weekday": 1, "start": "08:00", "end": "08:45"},
        ],
    )
    ids = [e.id for e in tt.entries]
    assert known in ids and "chosen-by-client" not in ids and len(ids) == 2


async def test_override_older_than_retention_is_422_not_pruned(env):
    tt = await env.svc.create(name="Anna", created_by="u-anna")
    today = TimetableEditorMixin.today_in(tt.tz)
    old = today - timedelta(days=15)
    while old.weekday() not in tt.days:
        old -= timedelta(days=1)
    with pytest.raises(TimetableValidationError, match="past"):
        await env.svc.add_override(
            tt.id,
            version=1,
            by="u-anna",
            fields={
                "date": old.isoformat(),
                "kind": "add",
                "start": "15:00",
                "end": "16:00",
            },
        )
    tt = await env.svc.add_override(
        tt.id,
        version=1,
        by="u-anna",
        fields={
            "date": MON.isoformat(),
            "kind": "add",
            "start": "15:00",
            "end": "16:00",
        },
    )
    with pytest.raises(TimetableValidationError, match="past"):
        await env.svc.update_override(
            tt.id,
            tt.overrides[0].id,
            version=2,
            by="u-anna",
            fields={"date": old.isoformat()},
        )


async def test_assignee_soft_deleted_user_rejected(env):
    await env.db.enqueue(
        "UPDATE users SET deleted_at=datetime('now') WHERE user_id='u-ben'"
    )
    with pytest.raises(TimetableValidationError):
        await _empty(env, assignees=["u-ben"])


async def test_duplicate_drops_no_longer_active_assignees(env):
    src = await _empty(env, assignees=["u-anna", "u-ben"])
    await env.db.enqueue("UPDATE users SET state='inactive' WHERE user_id='u-ben'")
    dup = await env.svc.duplicate(src.id, name="copy", by="u-anna")
    assert dup.assignees == ("u-anna",)


# ─── today_for_user (home screen) ────────────────────────────────────────


def _at(d, hhmm="07:00"):
    """``hhmm`` UTC on date ``d`` as an aware datetime."""
    h, m = hhmm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=timezone.utc)


async def _titled(env, **kw) -> Timetable:
    """A school template whose lessons all have a title (a filled plan)."""
    tt = await env.svc.create(**kw)
    entries = [
        {**td_entry_to_dict(e), "title": "Fach"}
        if e.kind is EntryKind.LESSON
        else td_entry_to_dict(e)
        for e in tt.entries
    ]
    return await env.svc.replace_entries(
        tt.id, version=tt.version, by="u-anna", entries=entries
    )


async def test_today_for_user_assigned_and_valid(env):
    anna = await _titled(env, name="Anna", created_by="u-anna")
    await _titled(env, name="Ben", created_by="u-anna", assignees=["u-ben"])
    got = await env.svc.today_for_user("u-anna", _at(MON))
    assert [t.timetable_id for t in got] == [anna.id]
    assert got[0].date == MON
    # School template: 08:00 Berlin (CEST/CET) → 06:00Z / 07:00Z.
    first = got[0].lessons[0]
    assert first.lesson.start == time(8, 0)
    assert first.start_at.utcoffset() == timedelta(0)
    assert first.start_at.astimezone(ZoneInfo("Europe/Berlin")).time() == time(8, 0)
    # Saturday isn't a school day.
    assert await env.svc.today_for_user("u-anna", _at(MON + timedelta(days=5))) == ()


async def test_today_for_user_uses_the_timetable_zone(env):
    # 23:30 UTC on Sunday is already Monday in Berlin.
    await _titled(env, name="Anna", created_by="u-anna")
    sunday_late = _at(MON - timedelta(days=1), "23:30")
    got = await env.svc.today_for_user("u-anna", sunday_late)
    assert len(got) == 1 and got[0].date == MON


async def test_today_for_user_skips_holiday_week(env):
    anna = await _titled(env, name="Anna", created_by="u-anna")
    await env.svc.set_validity(
        anna.id,
        version=anna.version,
        by="u-anna",
        valid_from=None,
        valid_until=None,
        excluded_weeks=[MON],
    )
    assert await env.svc.today_for_user("u-anna", _at(MON)) == ()


async def test_today_for_user_is_empty_when_feature_off(env):
    await _titled(env, name="Anna", created_by="u-anna")
    env.prefs.enabled = False
    assert await env.svc.today_for_user("u-anna", _at(MON)) == ()


async def test_today_for_user_caps_timetables(env):
    for i in range(ts.MAX_TODAY_TIMETABLES + 2):
        await _titled(env, name=f"T{i}", created_by="u-anna")
    got = await env.svc.today_for_user("u-anna", _at(MON))
    assert len(got) == ts.MAX_TODAY_TIMETABLES


async def test_today_for_user_extra_timetables_dedupe(env):
    anna = await _titled(env, name="Anna", created_by="u-anna")
    pinned = await _titled(env, name="Pinned", created_by="u-anna", assignees=[])
    got = await env.svc.today_for_user("u-anna", _at(MON), extra=[pinned, anna])
    assert [t.timetable_id for t in got] == [anna.id, pinned.id]


async def test_today_for_user_skips_a_day_without_slots(env):
    await _empty(env)  # valid on Monday, but no entries
    assert await env.svc.today_for_user("u-anna", _at(MON)) == ()


async def test_today_for_user_unfilled_templates_do_not_use_up_the_cap(env):
    # Three untouched school templates (untitled slots only) …
    for i in range(ts.MAX_TODAY_TIMETABLES):
        await env.svc.create(name=f"Blank {i}", created_by="u-anna")
    # … and one real timetable, created last.
    real = await _empty(env, name="Real")
    real = await env.svc.add_entry(
        real.id,
        version=real.version,
        by="u-anna",
        fields={"weekday": 0, "start": "08:00", "end": "08:45", "title": "Mathe"},
    )
    got = await env.svc.today_for_user("u-anna", _at(MON))
    assert [t.timetable_id for t in got] == [real.id]


async def test_today_for_user_one_bad_timetable_does_not_empty_the_slice(
    env, monkeypatch, caplog
):
    bad = await _titled(env, name="Bad", created_by="u-anna")
    good = await _titled(env, name="Good", created_by="u-anna")
    real = ts.td.today_timetable

    def boom(tt, d, **kw):
        if tt.id == bad.id:
            raise ValueError("corrupt row")
        return real(tt, d, **kw)

    monkeypatch.setattr(ts.td, "today_timetable", boom)
    with caplog.at_level("WARNING"):
        got = await env.svc.today_for_user("u-anna", _at(MON))
    assert [t.timetable_id for t in got] == [good.id]
    assert any("corrupt row" in r.getMessage() for r in caplog.records)
