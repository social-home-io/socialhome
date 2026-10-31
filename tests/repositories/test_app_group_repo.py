"""Tests for SqliteAppGroupRepo (``app_group_sessions``, 0136)."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.app_group import (
    END_CANCELLED,
    GROUP_ENDED,
    GROUP_LOBBY,
    GROUP_PLAYING,
    SEAT_INVITED,
    SEAT_SEATED,
    GroupSeat,
    GroupSession,
)
from socialhome.repositories.app_group_repo import SqliteAppGroupRepo

T0 = "2026-10-31T10:00:00+00:00"


def _table(session_id: str = "t1", **kw) -> GroupSession:
    base = {
        "session_id": session_id,
        "app_id": "quiz",
        "host_instance": "hub",
        "host_user": "anna",
        "hub_instance": "hub",
        "state": GROUP_LOBBY,
        "min_players": 3,
        "max_players": 6,
        "roster": (
            GroupSeat(
                instance="hub",
                user="anna",
                name="Anna",
                state=SEAT_SEATED,
                seat=1,
                joined_at=T0,
            ),
            GroupSeat(
                instance="b",
                user="ben",
                name="Ben",
                state=SEAT_INVITED,
                household_name="Okafor",
            ),
        ),
        "roster_rev": 1,
        "created_at": T0,
        "expires_at": "2026-11-03T10:00:00+00:00",
        "updated_at": T0,
        "opener_user": "anna",
    }
    base.update(kw)
    return GroupSession(**base)


@pytest.fixture
def repo(db):
    return SqliteAppGroupRepo(db)


async def test_save_and_get_round_trip(repo):
    t = _table(note="Snacks", late_join=True)
    await repo.save(t)
    assert await repo.get("t1") == t
    assert await repo.get("nope") is None


async def test_save_replaces_the_hubs_own_row(repo):
    await repo.save(_table())
    later = _table(state=GROUP_PLAYING, roster_rev=2, started_at=T0)
    await repo.save(later)
    assert (await repo.get("t1")) == later


async def test_save_newer_keeps_only_a_newer_snapshot(repo):
    assert await repo.save_newer(_table(roster_rev=3))
    assert not await repo.save_newer(_table(roster_rev=3, note="same rev"))
    assert not await repo.save_newer(_table(roster_rev=2, note="older"))
    assert (await repo.get("t1")).note is None
    assert await repo.save_newer(_table(roster_rev=4, note="newer"))
    assert (await repo.get("t1")).note == "newer"


async def test_another_hub_cant_take_a_table_over(repo):
    await repo.save(_table())
    assert not await repo.save_newer(
        _table(hub_instance="evil", host_instance="evil", roster_rev=9)
    )
    await repo.save(_table(hub_instance="evil", roster_rev=9))
    assert not await repo.save_newer(_table(app_id="other", roster_rev=9))
    got = await repo.get("t1")
    assert (got.hub_instance, got.app_id, got.roster_rev) == ("hub", "quiz", 1)


async def test_list_for_app_and_delete(repo):
    await repo.save(_table("t1"))
    await repo.save(_table("t2", app_id="chess"))
    assert [t.session_id for t in await repo.list_for_app("quiz")] == ["t1"]
    await repo.delete("t1")
    assert await repo.list_for_app("quiz") == []


async def test_list_hosted_by_person_and_time(repo):
    await repo.save(_table("old", created_at="2026-10-29T10:00:00+00:00"))
    await repo.save(_table("new"))
    await repo.save(_table("other", opener_user="cleo"))
    # A hand-over changes the host, never who opened it (the caps).
    await repo.save(_table("handed", host_user="ben", host_instance="b"))
    await repo.save(_table("mirror", hub_instance="b"))
    got = await repo.list_hosted(
        "quiz", "hub", "anna", since="2026-10-30T10:00:00+00:00"
    )
    assert sorted(t.session_id for t in got) == ["handed", "new"]


async def test_count_open_from_a_hub(repo):
    await repo.save(_table("a", hub_instance="b"))
    await repo.save(_table("b", hub_instance="b", state=GROUP_PLAYING))
    await repo.save(
        _table("c", hub_instance="b", state=GROUP_ENDED, end_reason=END_CANCELLED)
    )
    await repo.save(_table("d"))
    assert await repo.count_open_from("b") == 2


async def test_list_lobby_expired(repo):
    await repo.save(_table("due", expires_at="2026-10-31T09:00:00+00:00"))
    await repo.save(_table("later"))
    await repo.save(
        _table("playing", state=GROUP_PLAYING, expires_at="2026-10-31T09:00:00+00:00")
    )
    got = await repo.list_lobby_expired(before=T0)
    assert [t.session_id for t in got] == ["due"]


async def test_prune_ended_and_quiet(repo):
    await repo.save(
        _table(
            "ended-old",
            state=GROUP_ENDED,
            end_reason=END_CANCELLED,
            updated_at="2026-10-20T10:00:00+00:00",
        )
    )
    await repo.save(_table("ended-new", state=GROUP_ENDED, end_reason=END_CANCELLED))
    await repo.save(
        _table("quiet", state=GROUP_PLAYING, updated_at="2026-07-01T10:00:00+00:00")
    )
    await repo.save(_table("busy", state=GROUP_PLAYING))
    pruned = await repo.prune(
        ended_before="2026-10-24T10:00:00+00:00",
        quiet_before="2026-08-01T10:00:00+00:00",
    )
    assert pruned == 2
    left = {t.session_id for t in await repo.list_for_app("quiz")}
    assert left == {"ended-new", "busy"}


async def test_rows_are_frozen_values(repo):
    t = _table()
    await repo.save(t)
    got = await repo.get("t1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.state = GROUP_ENDED  # type: ignore[misc]
