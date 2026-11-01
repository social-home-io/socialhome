"""App engine state (v_78): one opaque blob per (table, engine), gone with
its table."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.repositories.app_engine_state_repo import (
    AbstractAppEngineStateRepo,
    SqliteAppEngineStateRepo,
)
from socialhome.repositories.app_group_repo import SqliteAppGroupRepo

from .test_app_group_repo import _table


@pytest.fixture
async def repos(db):
    return SqliteAppGroupRepo(db), SqliteAppEngineStateRepo(db)


async def test_state_per_table_and_engine_and_gone_with_the_table(repos):
    groups, state = repos
    assert isinstance(state, AbstractAppEngineStateRepo)
    await groups.save(_table())
    assert await state.get("t1", "draw") is None
    await state.set("t1", "draw", '{"v":1,"enc":"a"}')
    await state.set("t1", "deal", '{"v":1,"enc":"b"}')
    await state.set("t1", "draw", '{"v":1,"enc":"c"}')
    assert await state.get("t1", "draw") == '{"v":1,"enc":"c"}'
    assert await state.get("t1", "deal") == '{"v":1,"enc":"b"}'
    # A save of the table's roster leaves it alone; deleting the table
    # takes it.
    await groups.save(_table(roster_rev=2))
    assert await state.get("t1", "draw") == '{"v":1,"enc":"c"}'
    await groups.delete("t1")
    assert await state.get("t1", "draw") is None


async def test_no_state_for_a_table_this_household_doesnt_know(repos):
    _groups, state = repos
    with pytest.raises(sqlite3.IntegrityError):
        await state.set("nope", "draw", "{}")
    assert await state.get("nope", "draw") is None
