"""``SqliteSpaceMediaRefRepo`` against a real migrated database."""

from __future__ import annotations

import pytest

from socialhome.domain.space_media_ref import SpaceMediaRef
from socialhome.repositories.space_media_ref_repo import (
    AbstractSpaceMediaRefRepo,
    SqliteSpaceMediaRefRepo,
)

pytestmark = pytest.mark.asyncio


async def _space(db, space_id: str) -> None:
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'S', 'inst', 'admin', 'aa')",
        (space_id,),
    )


def _ref(local: str = "sg-a.webp", space_id: str = "sp-1", **kw) -> SpaceMediaRef:
    args = {
        "filename": "a.webp",
        "local_name": local,
        "space_id": space_id,
        "blob_id": "a" * 64,
        "suite": "aesgcm-256-pad-det",
        "epoch": 1,
        "size": 65_564,
        "scope": "space",
        "addr_suite": "sm1",
        "item_id": "post-1",
        "origin": "peer-a",
    }
    args.update(kw)
    return SpaceMediaRef(**args)


@pytest.fixture
async def repo(db):
    await _space(db, "sp-1")
    await _space(db, "sp-2")
    return SqliteSpaceMediaRefRepo(db)


async def test_satisfies_protocol(repo) -> None:
    assert isinstance(repo, AbstractSpaceMediaRefRepo)


async def test_save_and_get_by_local_name(repo) -> None:
    await repo.save_many(
        [
            _ref("sg-a.webp"),
            _ref("sg-b.webp", filename="b.webp", blob_id="b" * 64),
        ]
    )
    got = await repo.get("sg-a.webp")
    assert got is not None
    assert (got.filename, got.local_name) == ("a.webp", "sg-a.webp")
    assert got.blob_id == "a" * 64
    assert got.space_id == "sp-1"
    assert got.epoch == 1
    assert (got.scope, got.addr_suite) == ("space", "sm1")
    assert (got.item_id, got.origin) == ("post-1", "peer-a")
    assert got.created_at
    assert (await repo.get("sg-b.webp")).blob_id == "b" * 64
    assert await repo.get("a.webp") is None
    assert await repo.get("missing.webp") is None


async def test_save_many_empty_is_a_no_op(repo) -> None:
    await repo.save_many([])


async def test_unknown_space_is_skipped(repo) -> None:
    await repo.save_many([_ref(space_id="nope")])
    assert await repo.get("sg-a.webp") is None


async def test_list_for_item(repo) -> None:
    await repo.save_many(
        [
            _ref("sg-a.webp"),
            _ref("sg-b.webp", filename="b.webp"),
            _ref("sg-c.webp", filename="c.webp", item_id="post-2"),
            _ref("sg-d.webp", space_id="sp-2"),
        ]
    )
    rows = await repo.list_for_item("sp-1", "post-1")
    assert sorted(r.local_name for r in rows) == ["sg-a.webp", "sg-b.webp"]
    assert await repo.list_for_item("sp-1", "nope") == []


async def test_another_item_never_re_points_a_row(repo) -> None:
    await repo.save_many([_ref(epoch=1)])
    await repo.save_many([_ref(epoch=2, blob_id="c" * 64, item_id="post-9")])
    got = await repo.get("sg-a.webp")
    assert (got.item_id, got.epoch, got.blob_id) == ("post-1", 1, "a" * 64)


async def test_the_same_item_moves_its_row_to_a_newer_epoch(repo) -> None:
    """#1309 re-review 4: after a key rotation the item's file is sealed
    again under the new epoch; the old row (whose blob the GFS drops at its
    TTL) must follow, or the file becomes a permanent miss."""
    await repo.save_many([_ref(epoch=1)])
    await repo.save_many(
        [_ref(epoch=2, blob_id="c" * 64, size=70_000, scope="channel")]
    )
    got = await repo.get("sg-a.webp")
    assert (got.epoch, got.blob_id, got.size, got.scope) == (
        2,
        "c" * 64,
        70_000,
        "channel",
    )
    # Never back to an older epoch, never re-pointed at the same epoch.
    await repo.save_many([_ref(epoch=1, blob_id="d" * 64)])
    await repo.save_many([_ref(epoch=2, blob_id="e" * 64)])
    assert (await repo.get("sg-a.webp")).blob_id == "c" * 64


async def test_the_repo_has_no_delete() -> None:
    """Rows go with their space (ON DELETE CASCADE); nothing else deletes."""
    assert not hasattr(SqliteSpaceMediaRefRepo, "delete")
