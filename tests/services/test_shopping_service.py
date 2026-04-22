"""Tests for socialhome.services.shopping_service and sticky_repo."""

from __future__ import annotations

import uuid

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.shopping_repo import SqliteShoppingRepo
from socialhome.repositories.sticky_repo import SqliteStickyRepo
from socialhome.services.shopping_service import ShoppingService


@pytest.fixture
async def env(tmp_dir):
    """Env with shopping and sticky repos over a real SQLite database."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.shopping_repo = SqliteShoppingRepo(db)
    e.sticky_repo = SqliteStickyRepo(db)
    e.shopping_svc = ShoppingService(e.shopping_repo)
    yield e
    await db.shutdown()


async def test_shopping_full_lifecycle(env):
    """Add items, complete one, clear completed, then delete the remaining item."""
    milk = await env.shopping_svc.add_item("Milk", created_by="u1")
    eggs = await env.shopping_svc.add_item("Eggs", created_by="u1")
    assert milk.text == "Milk"
    assert not milk.completed

    await env.shopping_svc.complete_item(milk.id)
    completed = await env.shopping_repo.get(milk.id)
    assert completed.completed

    await env.shopping_svc.uncomplete_item(milk.id)
    assert not (await env.shopping_repo.get(milk.id)).completed
    await env.shopping_svc.complete_item(milk.id)

    count = await env.shopping_svc.clear_completed()
    assert count >= 1

    remaining = await env.shopping_svc.list_items()
    remaining_ids = {i.id for i in remaining}
    assert eggs.id in remaining_ids
    assert milk.id not in remaining_ids

    await env.shopping_svc.delete_item(eggs.id)
    with pytest.raises(KeyError):
        await env.shopping_svc.get_item(eggs.id)


async def test_shopping_empty_text_rejected(env):
    """Empty or whitespace-only item text raises ValueError."""
    with pytest.raises(ValueError):
        await env.shopping_svc.add_item("", created_by="u1")

    with pytest.raises(ValueError):
        await env.shopping_svc.add_item("   ", created_by="u1")


async def test_shopping_publishes_bus_events(env):
    """§S1 — every mutation emits a domain event on the bus so
    :class:`RealtimeService` can fan it out over the household WS.
    """
    from socialhome.domain.events import (
        ShoppingItemAdded,
        ShoppingItemRemoved,
        ShoppingItemsCleared,
        ShoppingItemToggled,
    )
    from socialhome.infrastructure.event_bus import EventBus

    bus = EventBus()
    svc = ShoppingService(env.shopping_repo, bus)
    captured: list = []

    async def _grab(e):
        captured.append(e)

    bus.subscribe(ShoppingItemAdded, _grab)
    bus.subscribe(ShoppingItemToggled, _grab)
    bus.subscribe(ShoppingItemRemoved, _grab)
    bus.subscribe(ShoppingItemsCleared, _grab)

    item = await svc.add_item("Bread", created_by="u1")
    await svc.complete_item(item.id)
    await svc.uncomplete_item(item.id)
    await svc.complete_item(item.id)
    await svc.clear_completed()
    item2 = await svc.add_item("Butter", created_by="u1")
    await svc.delete_item(item2.id)

    kinds = [type(e).__name__ for e in captured]
    # add, toggle(on), toggle(off), toggle(on), cleared, add, remove
    assert kinds == [
        "ShoppingItemAdded",
        "ShoppingItemToggled",
        "ShoppingItemToggled",
        "ShoppingItemToggled",
        "ShoppingItemsCleared",
        "ShoppingItemAdded",
        "ShoppingItemRemoved",
    ]
    # Spot-check payload shape
    added_events = [e for e in captured if type(e).__name__ == "ShoppingItemAdded"]
    assert added_events[0].text == "Bread"
    assert added_events[0].created_by == "u1"


async def test_shopping_uncomplete_and_list(env):
    """uncomplete_item restores the item; list with include_completed shows all."""
    i1 = await env.shopping_svc.add_item("A", created_by="u1")
    _i2 = await env.shopping_svc.add_item("B", created_by="u1")
    await env.shopping_svc.complete_item(i1.id)
    await env.shopping_svc.uncomplete_item(i1.id)
    got = await env.shopping_svc.get_item(i1.id)
    assert not got.completed
    await env.shopping_svc.complete_item(i1.id)
    all_items = await env.shopping_svc.list_items(include_completed=True)
    assert len(all_items) == 2


async def test_sticky_space_scoped(env):
    """Household and space sticky boards are independent."""
    kp = generate_identity_keypair()
    space_id = uuid.uuid4().hex
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("owner_s", "uid-owner-s", "OwnerS"),
    )
    await env.db.enqueue(
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        (space_id, "StickySpace", env.iid, "owner_s", kp.public_key.hex()),
    )

    household = await env.sticky_repo.add(
        author="u1", content="Buy milk", space_id=None
    )
    space_sticky = await env.sticky_repo.add(
        author="u1", content="Meeting agenda", space_id=space_id
    )

    household_list = await env.sticky_repo.list(space_id=None)
    space_list = await env.sticky_repo.list(space_id=space_id)

    household_ids = {s.id for s in household_list}
    space_ids = {s.id for s in space_list}

    assert household.id in household_ids
    assert household.id not in space_ids
    assert space_sticky.id in space_ids
    assert space_sticky.id not in household_ids

    await env.sticky_repo.update_content(household.id, "Buy oat milk")
    updated = await env.sticky_repo.get(household.id)
    assert updated.content == "Buy oat milk"

    await env.sticky_repo.update_color(household.id, "#FF0000")
    colored = await env.sticky_repo.get(household.id)
    assert colored.color == "#FF0000"

    await env.sticky_repo.delete(household.id)
    await env.sticky_repo.delete(space_sticky.id)
    assert await env.sticky_repo.get(household.id) is None
    assert await env.sticky_repo.get(space_sticky.id) is None
