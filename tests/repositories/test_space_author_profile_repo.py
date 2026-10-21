"""SqliteSpaceAuthorProfileRepo — newest profile per (space, author)."""

from __future__ import annotations

from dataclasses import replace

from socialhome.domain.space_author_profile import (
    PROFILE_PICTURE_FIELD,
    CachedAuthorProfile,
    SpaceAuthorProfile,
)
from socialhome.domain.space_author_profile_bundle import BundleEntry
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


async def test_signed_items_of_any_epoch_newest_first(db):
    """What a host's new-follower bundle forwards: the author-signed items
    held for the space, newest first, each with ITS OWN epoch and signed
    picture hash (a rotation never forces a republish)."""
    repo = await _repo(db)
    item_a = {"item_type": "author_profile", "author_user_id": "u-noor"}
    item_b = {"item_type": "author_profile", "author_user_id": "u-erin"}
    await repo.upsert_if_newer(P, picture_webp=None, signed_item=item_a, item_epoch=3)
    erin = replace(
        P,
        author_user_id="u-erin",
        picture_hash=None,
        ts="2026-10-20T09:00:00.000000+00:00",
    )
    await repo.upsert_if_newer(
        erin, picture_webp=None, signed_item=item_b, item_epoch=5
    )
    bare = replace(P, author_user_id="u-bare", ts="2026-10-20T10:00:00.000000+00:00")
    await repo.upsert_if_newer(bare, picture_webp=None)  # no signed item kept
    assert await repo.list_signed_items("sp", limit=10) == [
        BundleEntry(epoch=5, item=item_b, picture_hash=None),
        BundleEntry(epoch=3, item=item_a, picture_hash="0123456789abcdef"),
    ]
    assert [e.epoch for e in await repo.list_signed_items("sp", limit=1)] == [5]


async def test_a_newer_profile_replaces_the_signed_item(db):
    repo = await _repo(db)
    await repo.upsert_if_newer(P, picture_webp=None, signed_item={"v": 1}, item_epoch=3)
    newer = replace(P, ts="2026-10-20T09:00:00.000000+00:00")
    await repo.upsert_if_newer(
        newer, picture_webp=None, signed_item={"v": 2}, item_epoch=4
    )
    assert [(e.epoch, e.item) for e in await repo.list_signed_items("sp", limit=5)] == [
        (4, {"v": 2})
    ]


async def test_an_unreadable_signed_item_is_skipped(db):
    repo = await _repo(db)
    await repo.upsert_if_newer(P, picture_webp=None, signed_item={"v": 1}, item_epoch=3)
    await db.enqueue("UPDATE space_author_profiles SET signed_item='not json'")
    assert await repo.list_signed_items("sp", limit=5) == []
    await db.enqueue("UPDATE space_author_profiles SET signed_item='[1]'")
    assert await repo.list_signed_items("sp", limit=5) == []


async def test_a_newer_profile_with_the_same_picture_keeps_the_held_bytes(db):
    """A name or bio change goes out without the picture bytes; the
    follower keeps the picture it holds for that signed hash."""
    repo = await _repo(db)
    await repo.upsert_if_newer(P, picture_webp=b"RIFF-old-WEBP")
    renamed = replace(P, display_name="Noor H.", ts="2026-10-20T09:00:00.000000+00:00")
    assert await repo.upsert_if_newer(renamed, picture_webp=None)
    assert await repo.get_picture("sp", "u-noor") == (
        b"RIFF-old-WEBP",
        "0123456789abcdef",
    )
    repictured = replace(
        P, picture_hash="fedcba9876543210", ts="2026-10-20T10:00:00.000000+00:00"
    )
    assert await repo.upsert_if_newer(repictured, picture_webp=None)
    assert await repo.get_picture("sp", "u-noor") is None


async def test_a_picture_attaches_only_to_its_signed_hash(db):
    repo = await _repo(db)
    await repo.upsert_if_newer(P, picture_webp=None)
    assert not await repo.set_picture("sp", "u-noor", "fedcba9876543210", b"X")
    assert await repo.set_picture("sp", "u-noor", "0123456789abcdef", b"RIFFokWEBP")
    assert await repo.get_picture("sp", "u-noor") == (b"RIFFokWEBP", "0123456789abcdef")
    assert not await repo.set_picture("sp", "nobody", "0123456789abcdef", b"X")


async def test_get_reports_the_row_its_picture_and_sidecar(db):
    repo = await _repo(db)
    assert await repo.get("sp", "u-noor") is None
    item = {"author_user_id": "u-noor", PROFILE_PICTURE_FIELD: "UklGRg=="}
    await repo.upsert_if_newer(
        P, picture_webp=b"RIFF..WEBP", signed_item=item, item_epoch=2
    )
    got = await repo.get("sp", "u-noor")
    assert got == CachedAuthorProfile(
        profile=P, has_picture=True, picture_sidecar="UklGRg=="
    )


async def test_the_cache_keeps_the_newest_authors_per_space(db, monkeypatch):
    """M2: a follower's cache never grows past the per-space cap — the
    least recently stamped authors are pruned."""
    monkeypatch.setattr(
        "socialhome.repositories.space_author_profile_repo."
        "MAX_CACHED_AUTHORS_PER_SPACE",
        3,
    )
    repo = await _repo(db)
    for i in range(5):
        await repo.upsert_if_newer(
            replace(
                P, author_user_id=f"u{i}", ts=f"2026-10-20T0{i}:00:00.000000+00:00"
            ),
            picture_webp=None,
        )
    kept = sorted(p.author_user_id for p in await repo.list_for_space("sp"))
    assert kept == ["u2", "u3", "u4"]


async def test_spaces_an_author_has_a_profile_in(db):
    repo = await _repo(db)
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp2', 'S2', 'h', 'o', ?)",
        ("aa" * 32,),
    )
    await repo.upsert_if_newer(P, picture_webp=None)
    await repo.upsert_if_newer(replace(P, space_id="sp2"), picture_webp=None)
    await repo.upsert_if_newer(
        replace(P, space_id="sp2", author_pk="cd" * 32, author_user_id="u-x"),
        picture_webp=None,
    )
    assert sorted(await repo.list_spaces_for_author("u-noor", "ab" * 32)) == [
        "sp",
        "sp2",
    ]
    assert await repo.list_spaces_for_author("u-noor", "cd" * 32) == []
