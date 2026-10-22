"""SqliteProfilePictureRepo — per-space picture blobs (real SQLite)."""

from __future__ import annotations

import pytest

from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.repositories.profile_picture_repo import SqliteProfilePictureRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo


@pytest.fixture
async def repo(db):
    spaces = SqliteSpaceRepo(db)
    for sid in ("sp-a", "sp-b"):
        await spaces.save(
            Space(
                id=sid,
                name=sid,
                owner_instance_id="host",
                owner_username="anna",
                identity_public_key="00" * 32,
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.PRIVATE,
                join_mode=JoinMode.INVITE_ONLY,
            )
        )
    return SqliteProfilePictureRepo(db)


async def _put(repo, space_id: str, user_id: str) -> None:
    await repo.set_member_picture(
        space_id,
        user_id,
        bytes_webp=b"webp",
        hash="0123456789abcdef",
        width=1,
        height=1,
    )


async def test_member_picture_round_trip_and_clear(repo):
    await _put(repo, "sp-a", "u1")
    assert await repo.get_member_picture("sp-a", "u1") == (
        b"webp",
        "0123456789abcdef",
    )
    await repo.clear_member_picture("sp-a", "u1")
    assert await repo.get_member_picture("sp-a", "u1") is None


async def test_list_member_picture_user_ids_is_per_space(repo):
    assert await repo.list_member_picture_user_ids("sp-a") == set()
    await _put(repo, "sp-a", "u1")
    await _put(repo, "sp-a", "u2")
    await _put(repo, "sp-b", "u3")
    assert await repo.list_member_picture_user_ids("sp-a") == {"u1", "u2"}
    await repo.clear_member_picture("sp-a", "u1")
    assert await repo.list_member_picture_user_ids("sp-a") == {"u2"}


async def test_changed_member_pictures_are_those_stamped_after_since(repo, db):
    """§25.6 incremental (migration 0122): the pictures changed after a
    watermark — re-applying one held unchanged does not count."""
    await _put(repo, "sp-a", "u1")
    since = await db.fetchval("SELECT seq FROM sync_seq_counter WHERE id=1")
    await _put(repo, "sp-a", "u1")  # same picture again: not a change
    await repo.set_member_picture(
        "sp-a", "u2", bytes_webp=b"new", hash="fedcba9876543210", width=1, height=1
    )
    await repo.set_member_picture(
        "sp-b", "u3", bytes_webp=b"new", hash="fedcba9876543210", width=1, height=1
    )
    changed = await repo.list_member_pictures_changed("sp-a", int(since))
    assert changed == [("u2", b"new", "fedcba9876543210")]
    everything = await repo.list_member_pictures_changed("sp-a", 0)
    assert {u for u, _b, _h in everything} == {"u1", "u2"}
