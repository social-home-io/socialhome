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
