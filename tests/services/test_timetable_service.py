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
from socialhome.domain.space import SpacePermissionError
from socialhome.federation.owner_bound_id import (
    SPACE_TIMETABLE_KIND,
    OwnerBinding,
    check_owner_bound_id,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.timetable_repo import (
    SqliteSpaceTimetableRepo,
    SqliteTimetableRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import timetable_service as ts
from socialhome.services.timetable_service import (
    MAX_SPACE_TIMETABLES,
    MAX_TIMETABLES,
    SpaceTimetableService,
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


# ─── Space timetables (SpaceTimetableService) ────────────────────────────

SP = "sp-class"
SP2 = "sp-other"


@pytest.fixture
async def space_env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "space.db", batch_timeout_ms=10)
    await db.startup()
    for sid, tz, feature in ((SP, "Europe/Berlin", 1), (SP2, "UTC", 1)):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key, tz, feature_timetable) VALUES(?,?,?,?,?,?,?)",
            (sid, sid, "self", "anna", "00" * 32, tz, feature),
        )
    for sid, uid, role in (
        (SP, "u-owner", "owner"),
        (SP, "u-admin", "admin"),
        (SP, "u-member", "member"),
        (SP, "u-sub", "subscriber"),
        (SP2, "u-owner", "owner"),
    ):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            (sid, uid, role),
        )

    class Env:
        pass

    e = Env()
    e.db = db
    e.repo = SqliteSpaceTimetableRepo(db)
    e.bus = _Bus()
    e.svc = SpaceTimetableService(e.repo, SqliteSpaceRepo(db), e.bus)
    yield e
    await db.shutdown()


async def _space_tt(env, *, space_id=SP, by="u-admin", **kw) -> Timetable:
    kw.setdefault("name", "Klasse 5b")
    kw.setdefault("template", "empty")
    return await env.svc.scope(space_id, by).create(created_by=by, **kw)


async def test_space_create_is_owner_bound_school_template_in_space_tz(space_env):
    tt = await _space_tt(space_env, template="school")
    assert tt.tz == "Europe/Berlin"  # the space's tz
    assert tt.assignees == ()
    assert tt.created_by == tt.updated_by == "u-admin"
    assert len(tt.entries) == 8 * 5
    assert (
        check_owner_bound_id(
            SPACE_TIMETABLE_KIND, tt.id, space_id=SP, owner_user_id="u-admin"
        )
        is OwnerBinding.VALID
    )
    assert await space_env.repo.get(tt.id) == (SP, tt)
    ev = space_env.bus.events[-1]
    assert isinstance(ev, TimetableSaved)
    assert (ev.timetable, ev.space_id, ev.origin_instance_id) == (tt, SP, None)


async def test_space_owner_may_write_too(space_env):
    tt = await _space_tt(space_env, by="u-owner")
    assert tt.created_by == "u-owner"


@pytest.mark.parametrize("who", ["u-member", "u-sub", "u-stranger"])
async def test_space_non_admins_cannot_create(space_env, who):
    with pytest.raises(SpacePermissionError):
        await _space_tt(space_env, by=who)
    assert await space_env.repo.count_in_space(SP) == 0


@pytest.mark.parametrize("who", ["u-member", "u-sub"])
async def test_space_members_and_followers_read_but_never_edit(space_env, who):
    tt = await _space_tt(space_env, template="school")
    scope = space_env.svc.scope(SP, who)
    assert [t.id for t in await scope.list_all()] == [tt.id]
    assert (await scope.get(tt.id)).id == tt.id
    assert (await scope.resolve_week(tt.id, MON)).anchor == MON
    first = tt.entries[0]
    writes = [
        scope.update(tt.id, version=1, by=who, name="hijack"),
        scope.delete(tt.id, by=who),
        scope.duplicate(tt.id, name=None, by=who),
        scope.add_entry(
            tt.id,
            version=1,
            by=who,
            fields={"weekday": 0, "start": "14:00", "end": "14:45"},
        ),
        scope.update_entry(tt.id, first.id, version=1, by=who, fields={"title": "x"}),
        scope.delete_entry(tt.id, first.id, version=1, by=who),
        scope.replace_entries(tt.id, version=1, by=who, entries=[]),
        scope.generate_day(tt.id, 0, version=1, by=who, slots=[]),
        scope.copy_day(tt.id, 0, to_weekdays=(1,), version=1, by=who),
        scope.shift_after(tt.id, 0, from_time=time(8, 0), minutes=5, version=1, by=who),
        scope.set_validity(
            tt.id,
            version=1,
            by=who,
            valid_from=None,
            valid_until=None,
            excluded_weeks=(),
        ),
        scope.add_override(
            tt.id,
            version=1,
            by=who,
            fields={"date": MON.isoformat(), "kind": "cancel", "entry_id": first.id},
        ),
        scope.clear_week(tt.id, MON, version=1, by=who),
    ]
    for coro in writes:
        with pytest.raises(SpacePermissionError):
            await coro
    assert await space_env.repo.get(tt.id) == (SP, tt)


async def test_space_non_member_cannot_read(space_env):
    tt = await _space_tt(space_env)
    scope = space_env.svc.scope(SP, "u-stranger")
    for coro in (scope.list_all(), scope.get(tt.id), scope.resolve_day(tt.id, MON)):
        with pytest.raises(SpacePermissionError):
            await coro


async def test_space_feature_off_blocks_reads_and_writes(space_env):
    tt = await _space_tt(space_env)
    await space_env.db.enqueue(
        "UPDATE spaces SET feature_timetable=0 WHERE id=?", (SP,)
    )
    admin = space_env.svc.scope(SP, "u-admin")
    for coro in (
        admin.list_all(),
        admin.get(tt.id),
        admin.create(name="x", created_by="u-admin"),
        admin.update(tt.id, version=1, by="u-admin", name="y"),
        admin.delete(tt.id, by="u-admin"),
    ):
        with pytest.raises(FeatureDisabledError):
            await coro


async def test_space_cross_space_id_is_not_found(space_env):
    other = await _space_tt(space_env, space_id=SP2, by="u-owner")
    scope = space_env.svc.scope(SP, "u-owner")
    for coro in (
        scope.get(other.id),
        scope.update(other.id, version=1, by="u-owner", name="moved"),
        scope.delete(other.id, by="u-owner"),
        scope.duplicate(other.id, name=None, by="u-owner"),
    ):
        with pytest.raises(KeyError):
            await coro
    assert await space_env.repo.get(other.id) == (SP2, other)


async def test_space_timetables_carry_no_assignees(space_env):
    with pytest.raises(TimetableValidationError):
        await _space_tt(space_env, assignees=["u-member"])
    tt = await _space_tt(space_env, assignees=[])
    with pytest.raises(TimetableValidationError):
        await space_env.svc.scope(SP, "u-admin").update(
            tt.id, version=1, by="u-admin", assignees=["u-member"]
        )


async def test_space_limit(space_env):
    for i in range(MAX_SPACE_TIMETABLES):
        await _space_tt(space_env, name=f"T{i}")
    with pytest.raises(TimetableLimitError):
        await _space_tt(space_env, name="one more")
    first = (await space_env.repo.list_by_space(SP))[0]
    with pytest.raises(TimetableLimitError):
        await space_env.svc.scope(SP, "u-admin").duplicate(
            first.id, name=None, by="u-admin"
        )
    # The cap is per space.
    await _space_tt(space_env, space_id=SP2, by="u-owner")


async def test_space_edit_emits_space_scoped_event(space_env):
    tt = await _space_tt(space_env)
    new = await space_env.svc.scope(SP, "u-owner").update(
        tt.id, version=1, by="u-owner", name="Renamed"
    )
    assert (new.version, new.name, new.updated_by) == (2, "Renamed", "u-owner")
    ev = space_env.bus.events[-1]
    assert isinstance(ev, TimetableSaved) and ev.space_id == SP
    assert (await space_env.repo.get(tt.id))[1] == new
    with pytest.raises(TimetableConflictError):
        await space_env.svc.scope(SP, "u-owner").update(
            tt.id, version=1, by="u-owner", name="stale"
        )


async def test_space_delete_tombstones(space_env):
    tt = await _space_tt(space_env)
    await space_env.svc.scope(SP, "u-admin").delete(tt.id, by="u-admin")
    assert await space_env.repo.get(tt.id) is None
    assert await space_env.repo.is_tombstoned(tt.id)
    ev = space_env.bus.events[-1]
    assert isinstance(ev, TimetableDeleted)
    assert (ev.timetable_id, ev.space_id, ev.deleted_by, ev.created_by) == (
        tt.id,
        SP,
        "u-admin",
        "u-admin",
    )
    with pytest.raises(KeyError):
        await space_env.svc.scope(SP, "u-admin").delete(tt.id, by="u-admin")


async def test_space_duplicate_mints_a_bound_id_for_the_copier(space_env):
    tt = await _space_tt(space_env, template="school")
    dup = await space_env.svc.scope(SP, "u-owner").duplicate(
        tt.id, name=None, by="u-owner"
    )
    assert dup.id != tt.id and dup.name == "Klasse 5b (copy)"
    assert dup.created_by == "u-owner" and dup.assignees == ()
    assert (
        check_owner_bound_id(
            SPACE_TIMETABLE_KIND, dup.id, space_id=SP, owner_user_id="u-owner"
        )
        is OwnerBinding.VALID
    )
    assert len(dup.entries) == len(tt.entries)


async def test_space_pinned_for_user_filters_membership_and_feature(space_env):
    a = await _space_tt(space_env, name="A")
    b = await _space_tt(space_env, space_id=SP2, by="u-owner", name="B")
    # u-member is in SP only; a pin into SP2 (never joined) is ignored,
    # as is an unknown id; the order follows the pins.
    got = await space_env.svc.pinned_for_user("u-member", [b.id, "nope", a.id, a.id])
    assert [t.id for t in got] == [a.id]
    got = await space_env.svc.pinned_for_user("u-owner", [b.id, a.id])
    assert [t.id for t in got] == [b.id, a.id]
    # Feature off → its space's pins drop out; leaving drops them too.
    await space_env.db.enqueue(
        "UPDATE spaces SET feature_timetable=0 WHERE id=?", (SP2,)
    )
    assert [t.id for t in await space_env.svc.pinned_for_user("u-owner", [b.id])] == []
    await space_env.db.enqueue(
        "DELETE FROM space_members WHERE space_id=? AND user_id='u-member'", (SP,)
    )
    assert await space_env.svc.pinned_for_user("u-member", [a.id]) == []
    assert await space_env.svc.pinned_for_user("u-member", []) == []


async def test_space_edits_are_recorded_as_the_caller(space_env):
    """A scope never records an edit under somebody else's name."""
    tt = await _space_tt(space_env)
    scope = space_env.svc.scope(SP, "u-admin")
    for coro in (
        scope.create(name="x", created_by="u-owner"),
        scope.update(tt.id, version=1, by="u-owner", name="y"),
        scope.delete(tt.id, by="u-owner"),
        scope.duplicate(tt.id, name=None, by="u-owner"),
    ):
        with pytest.raises(SpacePermissionError):
            await coro
    assert await space_env.repo.get(tt.id) == (SP, tt)
