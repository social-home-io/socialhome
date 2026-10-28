"""Tests for :class:`SqliteDmContactRepo` (§23.47 contact requests)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from socialhome.domain.user import User
from socialhome.repositories.dm_contact_repo import SqliteDmContactRepo
from socialhome.repositories.user_repo import SqliteUserRepo


async def _user(db) -> str:
    user_id = uuid.uuid4().hex
    await SqliteUserRepo(db).save(
        User(user_id=user_id, username=f"u_{user_id[:8]}", display_name="U")
    )
    return user_id


async def test_save_and_list_pending(db):
    repo = SqliteDmContactRepo(db)
    to = await _user(db)
    rid = await repo.save_request(from_user_id="ru-ben", to_user_id=to)
    (row,) = await repo.list_pending_for(to)
    assert row["id"] == rid and row["status"] == "pending"
    assert (await repo.get(rid))["from_user_id"] == "ru-ben"


async def test_latest_between_prefers_a_pending_request(db):
    repo = SqliteDmContactRepo(db)
    to = await _user(db)
    assert await repo.latest_between("ru-ben", to) is None
    old = await repo.save_request(from_user_id="ru-ben", to_user_id=to)
    await repo.set_status(old, "declined")
    assert (await repo.latest_between("ru-ben", to))["status"] == "declined"
    await repo.save_request(from_user_id="ru-ben", to_user_id=to)
    assert (await repo.latest_between("ru-ben", to))["status"] == "pending"
    # Someone else's request to the same person is not theirs.
    assert await repo.latest_between("ru-kim", to) is None


async def test_answer_pending_answers_every_request_from_that_person_once(db):
    repo = SqliteDmContactRepo(db)
    to = await _user(db)
    await repo.save_request(from_user_id="ru-ben", to_user_id=to)
    await repo.save_request(from_user_id="ru-ben", to_user_id=to)
    kim = await repo.save_request(from_user_id="ru-kim", to_user_id=to)

    assert await repo.answer_pending("ru-ben", to, "accepted") == 2
    assert await repo.answer_pending("ru-ben", to, "declined") == 0
    assert (await repo.latest_between("ru-ben", to))["status"] == "accepted"
    assert (await repo.get(kim))["status"] == "pending"


async def test_two_answers_at_once_only_one_moves_the_request(db):
    repo = SqliteDmContactRepo(db)
    to = await _user(db)
    await repo.save_request(from_user_id="ru-ben", to_user_id=to)
    moved = await asyncio.gather(
        repo.answer_pending("ru-ben", to, "accepted"),
        repo.answer_pending("ru-ben", to, "declined"),
    )
    assert sorted(moved) == [0, 1]


async def test_answer_pending_refuses_an_unknown_answer(db):
    with pytest.raises(ValueError):
        await SqliteDmContactRepo(db).answer_pending("a", "b", "pending")
