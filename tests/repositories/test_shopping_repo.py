"""Tests for SqliteShoppingRepo — shopping list CRUD."""

from __future__ import annotations

import pytest

from socialhome.repositories.shopping_repo import SqliteShoppingRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with a shopping repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteShoppingRepo(db)
    yield e
    await db.shutdown()


async def test_add_and_get_item(env):
    """add creates a shopping item; get retrieves it by id."""
    item = await env.repo.add("Milk", created_by="uid-alice")
    assert item.text == "Milk"
    assert item.completed is False
    fetched = await env.repo.get(item.id)
    assert fetched is not None
    assert fetched.text == "Milk"


async def test_add_empty_text_raises(env):
    """add raises ValueError when text is empty or whitespace."""
    with pytest.raises(ValueError, match="must not be empty"):
        await env.repo.add("   ", created_by="uid-alice")


async def test_get_missing_returns_none(env):
    """get returns None for an unknown item id."""
    assert await env.repo.get("no-such-id") is None


async def test_list_excludes_completed_by_default(env):
    """list() without include_completed only returns pending items."""
    item1 = await env.repo.add("Eggs", created_by="uid-alice")
    item2 = await env.repo.add("Butter", created_by="uid-alice")
    await env.repo.complete(item1.id)
    result = await env.repo.list()
    ids = [i.id for i in result]
    assert item1.id not in ids
    assert item2.id in ids


async def test_list_with_completed(env):
    """list(include_completed=True) returns both pending and completed items."""
    item = await env.repo.add("Sugar", created_by="uid-alice")
    await env.repo.complete(item.id)
    result = await env.repo.list(include_completed=True)
    assert any(i.id == item.id for i in result)


async def test_complete_and_uncomplete(env):
    """complete marks an item done; uncomplete reverses it."""
    item = await env.repo.add("Cheese", created_by="uid-alice")
    await env.repo.complete(item.id)
    fetched = await env.repo.get(item.id)
    assert fetched.completed is True
    await env.repo.uncomplete(item.id)
    fetched2 = await env.repo.get(item.id)
    assert fetched2.completed is False


async def test_delete_item(env):
    """delete removes the item from the list."""
    item = await env.repo.add("Bread", created_by="uid-alice")
    await env.repo.delete(item.id)
    assert await env.repo.get(item.id) is None


async def test_clear_completed(env):
    """clear_completed removes all completed items and returns the count."""
    i1 = await env.repo.add("A", created_by="uid-alice")
    i2 = await env.repo.add("B", created_by="uid-alice")
    i3 = await env.repo.add("C", created_by="uid-alice")
    await env.repo.complete(i1.id)
    await env.repo.complete(i2.id)
    cleared = await env.repo.clear_completed()
    assert cleared == 2
    # Uncompleted item remains
    assert await env.repo.get(i3.id) is not None
    # Completed items are gone
    assert await env.repo.get(i1.id) is None
