"""Tests for SqliteTimetableRepo and SqliteSpaceTimetableRepo (real SQLite)."""

from __future__ import annotations

import copy
from datetime import date, datetime, time, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.timetable import (
    MAX_REMOTE_VERSION_JUMP,
    EntryKind,
    OverrideKind,
    Timetable,
    TimetableDefaults,
    TimetableEntry,
    TimetableOverride,
    TimetableValidity,
)
from socialhome.repositories.timetable_repo import (
    AbstractSpaceTimetableRepo,
    AbstractTimetableRepo,
    SqliteSpaceTimetableRepo,
    SqliteTimetableRepo,
)

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
MON = date(2026, 9, 28)


@pytest.fixture
async def env(tmp_dir):
    """Timetable repos over a real, migrated SQLite database."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )
    for sid in ("sp-1", "sp-2"):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?,?,?,?,?)",
            (sid, f"Space {sid}", "inst-x", "alice", "aabb" * 16),
        )

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteTimetableRepo(db)
    e.space_repo = SqliteSpaceTimetableRepo(db)
    yield e
    await db.shutdown()


def _tt(tid: str = "tt-1", name: str = "Anna 5b", **kw) -> Timetable:
    base = dict(
        id=tid,
        name=name,
        created_by="uid-alice",
        created_at=T0,
        updated_at=T0,
    )
    base.update(kw)
    return Timetable(**base)


def _rich(tid: str = "tt-1", **kw) -> Timetable:
    """A timetable that exercises every column."""
    entries = (
        TimetableEntry(
            id="m1",
            weekday=0,
            start=time(8, 0),
            end=time(8, 45),
            label="1.",
            title="Mathe",
            room="A1",
            teacher="Fr. B",
            note="Heft",
            color="teal",
            icon="🔢",
        ),
        TimetableEntry(
            id="mb",
            weekday=0,
            start=time(9, 35),
            end=time(9, 55),
            kind=EntryKind.BREAK,
            title="Große Pause",
        ),
    )
    overrides = (
        TimetableOverride(id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m1"),
        TimetableOverride(
            id="o2",
            date=MON,
            kind=OverrideKind.ADD,
            start=time(12, 0),
            end=time(12, 45),
            title="AG",
            icon="⚽",
        ),
    )
    base = dict(
        color="moss",
        week_start=6,
        tz="Europe/Berlin",
        days=(0, 1, 2, 3, 4, 6),
        defaults=TimetableDefaults(
            lesson_minutes=50, gap_minutes=10, day_start=time(7, 45)
        ),
        entries=entries,
        overrides=overrides,
        validity=TimetableValidity(
            valid_from=date(2026, 9, 1),
            valid_until=date(2027, 7, 31),
            excluded_weeks=(date(2026, 10, 25), date(2026, 12, 20)),
        ),
        version=3,
        updated_by="uid-bob",
    )
    base.update(kw)
    return _tt(tid, **base)


def test_concrete_repos_satisfy_protocols(env):
    assert isinstance(env.repo, AbstractTimetableRepo)
    assert isinstance(env.space_repo, AbstractSpaceTimetableRepo)


# ─── Household ───────────────────────────────────────────────────────────


class TestHousehold:
    async def test_insert_get_round_trip(self, env):
        tt = _rich(assignees=("uid-alice", "uid-kid"))
        await env.repo.insert(tt)
        assert await env.repo.get("tt-1") == tt

    async def test_get_missing(self, env):
        assert await env.repo.get("nope") is None

    async def test_icon_lives_in_entries_json_and_old_rows_read_none(self, env):
        await env.repo.insert(_rich())
        row = await env.db.fetchone(
            "SELECT entries_json FROM timetables WHERE id=?", ("tt-1",)
        )
        assert '"icon":"🔢"' in row["entries_json"]
        # A row written before icons existed has no "icon" key.
        await env.db.enqueue(
            "UPDATE timetables SET entries_json=? WHERE id=?",
            (
                '[{"id":"m1","weekday":0,"start":"08:00","end":"08:45",'
                '"kind":"lesson"}]',
                "tt-1",
            ),
        )
        got = await env.repo.get("tt-1")
        assert got is not None and got.entries[0].icon is None

    async def test_defaults_json_shape(self, env):
        await env.repo.insert(_rich())
        row = await env.db.fetchone(
            "SELECT defaults_json, days_json, updated_at FROM timetables WHERE id=?",
            ("tt-1",),
        )
        assert row["defaults_json"] == (
            '{"day_start":"07:45","gap_minutes":10,"lesson_minutes":50}'
        )
        assert row["days_json"] == "[0,1,2,3,4,6]"
        # tz-aware ISO, like task_repo.
        assert row["updated_at"].endswith("+00:00")

    async def test_naive_timestamps_stored_as_utc(self, env):
        naive = datetime(2026, 9, 1, 8, 0)
        await env.repo.insert(_tt(created_at=naive, updated_at=naive))
        got = await env.repo.get("tt-1")
        assert got.created_at == naive.replace(tzinfo=timezone.utc)

    async def test_list_all_ordered_by_name_then_created(self, env):
        await env.repo.insert(_tt("b", "Zoe"))
        await env.repo.insert(_tt("c", "Anna", created_at=T0 + timedelta(hours=1)))
        await env.repo.insert(_tt("a", "Anna"))
        assert [t.id for t in await env.repo.list_all()] == ["a", "c", "b"]

    async def test_count_and_delete(self, env):
        assert await env.repo.count() == 0
        await env.repo.insert(_tt("a"))
        await env.repo.insert(_tt("b"))
        assert await env.repo.count() == 2
        assert await env.repo.delete("a") is True
        assert await env.repo.delete("a") is False
        assert await env.repo.count() == 1
        assert await env.repo.get("a") is None

    async def test_save_cas(self, env):
        tt = _rich()
        await env.repo.insert(tt)
        new = copy.replace(
            tt,
            name="Anna 6a",
            version=4,
            entries=tt.entries[:1],
            overrides=(),
            assignees=("uid-x",),
            updated_at=T0 + timedelta(days=1),
            updated_by="uid-carol",
        )
        assert await env.repo.save(new, expected_version=3) is True
        assert await env.repo.get("tt-1") == new

    async def test_save_stale_version_returns_false(self, env):
        tt = _rich()
        await env.repo.insert(tt)
        stale = copy.replace(tt, name="Lost", version=4)
        assert await env.repo.save(stale, expected_version=2) is False
        assert (await env.repo.get("tt-1")).name == "Anna 5b"

    async def test_save_missing_returns_false(self, env):
        assert await env.repo.save(_tt(), expected_version=1) is False


# ─── Space ───────────────────────────────────────────────────────────────


class TestSpace:
    async def test_insert_get_round_trip(self, env):
        tt = _rich("s-1")
        assert await env.space_repo.insert(tt, space_id="sp-1") is True
        assert await env.space_repo.get("s-1") == ("sp-1", tt)

    async def test_assignees_never_stored(self, env):
        tt = _rich("s-1", assignees=("uid-alice",))
        await env.space_repo.insert(tt, space_id="sp-1")
        _, got = await env.space_repo.get("s-1")
        assert got.assignees == ()

    async def test_insert_missing_space_refused(self, env):
        assert await env.space_repo.insert(_tt("s-1"), space_id="sp-nope") is False
        assert await env.space_repo.get("s-1") is None

    async def test_insert_existing_id_refused(self, env):
        assert await env.space_repo.insert(_tt("s-1"), space_id="sp-1") is True
        clash = _tt("s-1", name="Other")
        assert await env.space_repo.insert(clash, space_id="sp-2") is False
        sid, got = await env.space_repo.get("s-1")
        assert sid == "sp-1" and got.name == "Anna 5b"

    async def test_list_by_space_count_and_ids(self, env):
        await env.space_repo.insert(_tt("s-b", "Zoe"), space_id="sp-1")
        await env.space_repo.insert(_tt("s-a", "Anna"), space_id="sp-1")
        await env.space_repo.insert(_tt("s-c", "Carl"), space_id="sp-2")
        assert [t.id for t in await env.space_repo.list_by_space("sp-1")] == [
            "s-a",
            "s-b",
        ]
        assert await env.space_repo.count_in_space("sp-1") == 2
        assert await env.space_repo.count_in_space("sp-2") == 1
        got = await env.space_repo.list_by_ids(["s-c", "s-a", "missing"])
        assert sorted((sid, t.id) for sid, t in got) == [
            ("sp-1", "s-a"),
            ("sp-2", "s-c"),
        ]
        assert await env.space_repo.list_by_ids([]) == []

    async def test_save_cas_scoped_by_space(self, env):
        tt = _tt("s-1")
        await env.space_repo.insert(tt, space_id="sp-1")
        new = copy.replace(tt, name="Renamed", version=2)
        # Wrong space → refused, even with the right version.
        assert (
            await env.space_repo.save(new, space_id="sp-2", expected_version=1) is False
        )
        # Stale version → refused.
        assert (
            await env.space_repo.save(new, space_id="sp-1", expected_version=5) is False
        )
        assert (
            await env.space_repo.save(new, space_id="sp-1", expected_version=1) is True
        )
        assert await env.space_repo.get("s-1") == ("sp-1", new)

    async def test_soft_delete(self, env):
        tt = _rich("s-1")
        await env.space_repo.insert(tt, space_id="sp-1")
        at = T0 + timedelta(days=2)
        assert await env.space_repo.soft_delete("s-1", space_id="sp-2", at=at) is False
        assert await env.space_repo.soft_delete("s-1", space_id="sp-1", at=at) is True
        assert await env.space_repo.soft_delete("s-1", space_id="sp-1", at=at) is False
        assert await env.space_repo.get("s-1") is None
        assert await env.space_repo.is_tombstoned("s-1") is True
        assert await env.space_repo.list_by_space("sp-1") == []
        assert await env.space_repo.list_by_ids(["s-1"]) == []
        assert await env.space_repo.count_in_space("sp-1") == 0
        row = await env.db.fetchone(
            "SELECT entries_json, overrides_json, deleted_at FROM space_timetables"
            " WHERE id=?",
            ("s-1",),
        )
        assert row["entries_json"] == "[]"
        assert row["overrides_json"] == "[]"
        assert datetime.fromisoformat(row["deleted_at"]) == at

    async def test_save_after_soft_delete_refused(self, env):
        tt = _tt("s-1")
        await env.space_repo.insert(tt, space_id="sp-1")
        await env.space_repo.soft_delete("s-1", space_id="sp-1", at=T0)
        new = copy.replace(tt, version=2)
        assert (
            await env.space_repo.save(new, space_id="sp-1", expected_version=1) is False
        )

    async def test_insert_over_tombstone_refused(self, env):
        await env.space_repo.insert(_tt("s-1"), space_id="sp-1")
        await env.space_repo.soft_delete("s-1", space_id="sp-1", at=T0)
        assert await env.space_repo.insert(_tt("s-1"), space_id="sp-1") is False

    async def test_is_tombstoned_false_for_live_and_missing(self, env):
        await env.space_repo.insert(_tt("s-1"), space_id="sp-1")
        assert await env.space_repo.is_tombstoned("s-1") is False
        assert await env.space_repo.is_tombstoned("nope") is False


class TestApplyRemote:
    async def test_inserts_new(self, env):
        tt = _rich("r-1")
        assert await env.space_repo.apply_remote(tt, space_id="sp-1") is True
        assert await env.space_repo.get("r-1") == ("sp-1", tt)

    async def test_insert_requires_space(self, env):
        assert await env.space_repo.apply_remote(_tt("r-1"), space_id="nope") is False
        assert await env.space_repo.get("r-1") is None

    async def test_newer_version_wins(self, env):
        tt = _tt("r-1", version=2)
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        newer = copy.replace(tt, name="Newer", version=3)
        assert await env.space_repo.apply_remote(newer, space_id="sp-1") is True
        assert (await env.space_repo.get("r-1"))[1] == newer

    async def test_a_version_jump_past_the_limit_is_refused(self, env):
        """A replica can't freeze the row by leaping its version ahead."""
        tt = _tt("r-1", version=2)
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        leap = copy.replace(tt, name="Leap", version=2 + MAX_REMOTE_VERSION_JUMP + 1)
        assert await env.space_repo.apply_remote(leap, space_id="sp-1") is False
        step = copy.replace(tt, name="Step", version=2 + MAX_REMOTE_VERSION_JUMP)
        assert await env.space_repo.apply_remote(step, space_id="sp-1") is True
        assert (await env.space_repo.get("r-1"))[1] == step

    async def test_older_version_ignored(self, env):
        tt = _tt("r-1", version=5)
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        older = copy.replace(
            tt, name="Older", version=4, updated_at=T0 + timedelta(days=9)
        )
        assert await env.space_repo.apply_remote(older, space_id="sp-1") is False
        assert (await env.space_repo.get("r-1"))[1] == tt

    async def test_equal_version_newer_updated_at_wins(self, env):
        tt = _tt("r-1", version=2)
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        later = copy.replace(tt, name="Later", updated_at=T0 + timedelta(seconds=1))
        assert await env.space_repo.apply_remote(later, space_id="sp-1") is True
        assert (await env.space_repo.get("r-1"))[1].name == "Later"

    async def test_equal_version_older_or_same_updated_at_ignored(self, env):
        tt = _tt("r-1", version=2, updated_at=T0 + timedelta(hours=1))
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        earlier = copy.replace(tt, name="Earlier", updated_at=T0)
        assert await env.space_repo.apply_remote(earlier, space_id="sp-1") is False
        same = copy.replace(tt, name="Same")
        assert await env.space_repo.apply_remote(same, space_id="sp-1") is False
        assert (await env.space_repo.get("r-1"))[1].name == "Anna 5b"

    async def test_updated_at_compared_as_utc_instant(self, env):
        # A peer stamping +02:00 must not win a string comparison it would
        # lose as an instant: stored values are normalised to UTC.
        cest = timezone(timedelta(hours=2))
        tt = _tt("r-1", version=2, updated_at=T0 + timedelta(hours=1))
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        # 10:30+02:00 == 08:30Z, which is *earlier* than 09:00Z.
        earlier = copy.replace(
            tt, name="Earlier", updated_at=datetime(2026, 9, 1, 10, 30, tzinfo=cest)
        )
        assert await env.space_repo.apply_remote(earlier, space_id="sp-1") is False

    async def test_cross_space_id_refused(self, env):
        tt = _tt("r-1")
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        hijack = copy.replace(tt, name="Hijack", version=9)
        assert await env.space_repo.apply_remote(hijack, space_id="sp-2") is False
        assert await env.space_repo.get("r-1") == ("sp-1", tt)

    async def test_never_resurrects_tombstone(self, env):
        tt = _rich("r-1")
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        await env.space_repo.soft_delete("r-1", space_id="sp-1", at=T0)
        replay = copy.replace(tt, version=99, updated_at=T0 + timedelta(days=30))
        assert await env.space_repo.apply_remote(replay, space_id="sp-1") is False
        assert await env.space_repo.get("r-1") is None
        assert await env.space_repo.is_tombstoned("r-1") is True


class TestReviewFixes:
    async def test_household_list_is_case_insensitive(self, env):
        for tid, name in (("1", "bob"), ("2", "Anna"), ("3", "alice")):
            await env.repo.insert(_tt(tid, name))
        assert [t.name for t in await env.repo.list_all()] == ["alice", "Anna", "bob"]

    async def test_space_list_is_case_insensitive(self, env):
        for tid, name in (("1", "bob"), ("2", "Anna"), ("3", "alice")):
            await env.space_repo.insert(_tt(tid, name), space_id="sp-1")
        got = await env.space_repo.list_by_space("sp-1")
        assert [t.name for t in got] == ["alice", "Anna", "bob"]

    @pytest.mark.parametrize(
        ("stored", "incoming", "wins"),
        [
            ("uid-a", "uid-b", True),
            ("uid-b", "uid-a", False),
            (None, "uid-a", True),
            ("uid-a", None, False),
            ("uid-a", "uid-a", False),
        ],
    )
    async def test_apply_remote_full_tie_breaks_on_updated_by(
        self, env, stored, incoming, wins
    ):
        tt = _tt("r-1", version=2, updated_by=stored)
        await env.space_repo.apply_remote(tt, space_id="sp-1")
        other = copy.replace(tt, name="Other", updated_by=incoming)
        assert await env.space_repo.apply_remote(other, space_id="sp-1") is wins
        got = (await env.space_repo.get("r-1"))[1]
        assert got.name == ("Other" if wins else "Anna 5b")

    async def test_replicas_converge_regardless_of_arrival_order(self, env):
        a = _tt("r-1", version=2, name="From A", updated_by="uid-a")
        b = copy.replace(a, name="From B", updated_by="uid-b")
        # Replica 1 sees A then B; replica 2 (id r-2) sees B then A.
        for first, second, tid in ((a, b, "r-1"), (b, a, "r-2")):
            await env.space_repo.apply_remote(
                copy.replace(first, id=tid), space_id="sp-1"
            )
            await env.space_repo.apply_remote(
                copy.replace(second, id=tid), space_id="sp-1"
            )
        assert (await env.space_repo.get("r-1"))[1].name == "From B"
        assert (await env.space_repo.get("r-2"))[1].name == "From B"


class TestTombstone:
    async def test_unseen_id_is_tombstoned(self, env):
        at = T0 + timedelta(days=1)
        assert await env.space_repo.tombstone("ghost", space_id="sp-1", at=at) is True
        assert await env.space_repo.is_tombstoned("ghost") is True
        assert await env.space_repo.get("ghost") is None
        assert await env.space_repo.list_by_space("sp-1") == []
        assert await env.space_repo.count_in_space("sp-1") == 0
        row = await env.db.fetchone(
            "SELECT * FROM space_timetables WHERE id=?", ("ghost",)
        )
        assert row["space_id"] == "sp-1"
        assert row["name"] == "" and row["created_by"] == ""
        assert row["entries_json"] == "[]" and row["overrides_json"] == "[]"
        assert datetime.fromisoformat(row["deleted_at"]) == at
        # Timestamps keep the column's tz-aware shape, not datetime('now').
        assert datetime.fromisoformat(row["updated_at"]) == at

    async def test_reordered_upsert_cannot_resurrect(self, env):
        # The delete arrives before the create it deletes.
        await env.space_repo.tombstone("r-1", space_id="sp-1", at=T0)
        late = _rich("r-1", version=50, updated_at=T0 + timedelta(days=5))
        assert await env.space_repo.apply_remote(late, space_id="sp-1") is False
        assert await env.space_repo.insert(late, space_id="sp-1") is False
        assert await env.space_repo.get("r-1") is None
        assert await env.space_repo.is_tombstoned("r-1") is True

    async def test_tombstones_live_row_and_clears_content(self, env):
        await env.space_repo.insert(_rich("s-1"), space_id="sp-1")
        assert await env.space_repo.tombstone("s-1", space_id="sp-1", at=T0) is True
        assert await env.space_repo.get("s-1") is None
        row = await env.db.fetchone(
            "SELECT name, entries_json, overrides_json FROM space_timetables"
            " WHERE id=?",
            ("s-1",),
        )
        assert row["name"] == "Anna 5b"
        assert row["entries_json"] == "[]" and row["overrides_json"] == "[]"

    async def test_cross_space_id_refused(self, env):
        tt = _rich("s-1")
        await env.space_repo.insert(tt, space_id="sp-1")
        assert await env.space_repo.tombstone("s-1", space_id="sp-2", at=T0) is False
        assert await env.space_repo.get("s-1") == ("sp-1", tt)

    async def test_missing_space_refused(self, env):
        assert await env.space_repo.tombstone("x", space_id="sp-nope", at=T0) is False
        assert await env.space_repo.is_tombstoned("x") is False


# ── §25.6 incremental reads (migration 0088 change stamps) ────────────────


async def test_space_timetables_changed_since_a_stamp(env):
    repo = env.space_repo
    for tid in ("tt-a", "tt-b"):
        assert await repo.insert(_tt(tid, name=tid), space_id="sp-1")
    row = await env.db.fetchone("SELECT seq FROM sync_seq_counter WHERE id=1")
    mark = int(row["seq"])
    assert await repo.list_by_space("sp-1", since_seq=mark) == []
    edited = _tt("tt-b", name="renamed", version=2)
    assert await repo.save(edited, space_id="sp-1", expected_version=1)
    assert [t.id for t in await repo.list_by_space("sp-1", since_seq=mark)] == ["tt-b"]
    assert len(await repo.list_by_space("sp-1")) == 2
