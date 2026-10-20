"""SqliteSpaceAuthorProfileRepo — newest profile per (space, author)."""

from __future__ import annotations

from dataclasses import replace

from socialhome.domain.space_author_profile import SpaceAuthorProfile
from socialhome.repositories.space_author_profile_repo import (
    SqliteSpaceAuthorProfileRepo,
)

P = SpaceAuthorProfile(
    space_id="sp",
    author_user_id="u-noor",
    author_pk="ab" * 32,
    display_name="Noor",
    bio="Bees.",
    picture_hash="0123456789abcdef",
    profile_rev="r1",
    ts="2026-10-20T08:00:00.000000+00:00",
)


async def _repo(db) -> SqliteSpaceAuthorProfileRepo:
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'S', 'h', 'o', ?)",
        ("aa" * 32,),
    )
    return SqliteSpaceAuthorProfileRepo(db)


async def test_newest_wins(db):
    repo = await _repo(db)
    assert await repo.upsert_if_newer(P, picture_webp=b"RIFF....WEBP")
    older = replace(P, display_name="Old", ts="2026-10-20T07:00:00.000000+00:00")
    assert not await repo.upsert_if_newer(older, picture_webp=None)
    assert not await repo.upsert_if_newer(P, picture_webp=None)  # same stamp
    newer = replace(
        P, display_name="New", picture_hash=None, ts="2026-10-20T09:00:00.000000+00:00"
    )
    assert await repo.upsert_if_newer(newer, picture_webp=None)
    [got] = await repo.list_for("sp", ["u-noor", "u-other"])
    assert got.display_name == "New"
    assert await repo.get_picture("sp", "u-noor") is None


async def test_lists_and_pictures(db):
    repo = await _repo(db)
    await repo.upsert_if_newer(P, picture_webp=b"RIFF....WEBP")
    assert await repo.list_for("sp", []) == []
    assert [p.author_user_id for p in await repo.list_for_space("sp")] == ["u-noor"]
    assert await repo.get_picture("sp", "u-noor") == (
        b"RIFF....WEBP",
        "0123456789abcdef",
    )
    assert await repo.get_picture("sp", "nobody") is None


async def test_deleting_the_space_deletes_its_profiles(db):
    repo = await _repo(db)
    await repo.upsert_if_newer(P, picture_webp=None)
    await db.enqueue("DELETE FROM spaces WHERE id='sp'")
    assert await repo.list_for_space("sp") == []
