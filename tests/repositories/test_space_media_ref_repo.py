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


def _ref(name: str = "a.webp", space_id: str = "sp-1", **kw) -> SpaceMediaRef:
    args = {
        "filename": name,
        "space_id": space_id,
        "blob_id": "a" * 64,
        "suite": "aesgcm-256-pad-det",
        "epoch": 1,
        "size": 65_564,
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


async def test_save_and_get(repo) -> None:
    await repo.save_many([_ref("a.webp"), _ref("b.webp", blob_id="b" * 64)])
    got = await repo.get("a.webp")
    assert got is not None
    assert got.blob_id == "a" * 64
    assert got.space_id == "sp-1"
    assert got.epoch == 1
    assert got.created_at
    assert (await repo.get("b.webp")).blob_id == "b" * 64
    assert await repo.get("missing.webp") is None


async def test_save_many_empty_is_a_no_op(repo) -> None:
    await repo.save_many([])


async def test_a_newer_descriptor_of_the_same_space_replaces(repo) -> None:
    await repo.save_many([_ref(epoch=1)])
    await repo.save_many([_ref(epoch=2, blob_id="c" * 64)])
    got = await repo.get("a.webp")
    assert (got.epoch, got.blob_id) == (2, "c" * 64)


async def test_another_space_cannot_claim_a_file(repo) -> None:
    """A filename one space described is never re-pointed by another
    space's descriptor."""
    await repo.save_many([_ref(space_id="sp-1")])
    await repo.save_many([_ref(space_id="sp-2", blob_id="d" * 64)])
    got = await repo.get("a.webp")
    assert (got.space_id, got.blob_id) == ("sp-1", "a" * 64)


async def test_unknown_space_is_skipped(repo) -> None:
    await repo.save_many([_ref(space_id="nope")])
    assert await repo.get("a.webp") is None


async def test_delete(repo) -> None:
    await repo.save_many([_ref()])
    await repo.delete("a.webp")
    assert await repo.get("a.webp") is None
