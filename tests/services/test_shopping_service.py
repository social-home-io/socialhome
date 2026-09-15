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


async def test_shopping_add_with_store_event_carries_store(env):
    """ShoppingItemAdded.store carries the cleaned store name so the
    WS frame can include it without a second fetch."""
    from socialhome.domain.events import ShoppingItemAdded
    from socialhome.infrastructure.event_bus import EventBus

    bus = EventBus()
    svc = ShoppingService(env.shopping_repo, bus)
    captured: list = []
    bus.subscribe(ShoppingItemAdded, lambda e: captured.append(e))

    await svc.add_item("Bread", created_by="u1", store="Bakery")

    assert len(captured) == 1
    assert captured[0].text == "Bread"
    assert captured[0].store == "Bakery"


async def test_shopping_add_store_is_trimmed_and_clamped(env):
    """Service strips whitespace and clamps store to 80 chars."""
    long_name = "A" * 200
    item = await env.shopping_svc.add_item(
        "Milk",
        created_by="u1",
        store=f"  {long_name}  ",
    )
    assert item.store == "A" * 80


async def test_shopping_add_blank_store_collapses_to_none(env):
    """A blank / whitespace-only store collapses to None."""
    item = await env.shopping_svc.add_item("Milk", created_by="u1", store="   ")
    assert item.store is None


async def test_shopping_update_item_sets_store_and_emits_event(env):
    """update_item changes text + store and fires ShoppingItemUpdated."""
    from socialhome.domain.events import ShoppingItemUpdated
    from socialhome.infrastructure.event_bus import EventBus

    bus = EventBus()
    svc = ShoppingService(env.shopping_repo, bus)
    captured: list = []
    bus.subscribe(ShoppingItemUpdated, lambda e: captured.append(e))

    item = await svc.add_item("Milk", created_by="u1", store="Aldi")
    updated = await svc.update_item(item.id, text="Whole Milk", store="Whole Foods")

    assert updated.text == "Whole Milk"
    assert updated.store == "Whole Foods"
    assert len(captured) == 1
    assert captured[0].item_id == item.id
    assert captured[0].text == "Whole Milk"
    assert captured[0].store == "Whole Foods"


async def test_shopping_update_item_clear_store(env):
    """update_item(store=None) clears the field on disk."""
    item = await env.shopping_svc.add_item("Milk", created_by="u1", store="Aldi")
    updated = await env.shopping_svc.update_item(item.id, store=None)
    assert updated.store is None


async def test_shopping_update_item_keep_store_with_sentinel(env):
    """Omitting ``store`` (UNSET_FIELD default) keeps the existing value."""
    item = await env.shopping_svc.add_item("Milk", created_by="u1", store="Aldi")
    updated = await env.shopping_svc.update_item(item.id, text="Whole Milk")
    assert updated.text == "Whole Milk"
    assert updated.store == "Aldi"


async def test_shopping_update_item_empty_text_rejected(env):
    """Whitespace-only text raises ValueError."""
    item = await env.shopping_svc.add_item("Milk", created_by="u1")
    with pytest.raises(ValueError):
        await env.shopping_svc.update_item(item.id, text="   ")


async def test_shopping_update_item_missing_raises(env):
    """Unknown id raises KeyError → 404 at the route layer."""
    with pytest.raises(KeyError):
        await env.shopping_svc.update_item("nope", text="x")


async def test_shopping_reorder_stores_emits_event(env):
    """reorder_stores broadcasts ShoppingStoresReordered with the
    canonical post-reorder name sequence."""
    from socialhome.domain.events import ShoppingStoresReordered
    from socialhome.infrastructure.event_bus import EventBus

    bus = EventBus()
    svc = ShoppingService(env.shopping_repo, bus)
    captured: list = []
    bus.subscribe(ShoppingStoresReordered, lambda e: captured.append(e))

    await svc.add_item("Bread", created_by="u1", store="Bakery")
    await svc.add_item("Milk", created_by="u1", store="Aldi")
    await svc.add_item("Apples", created_by="u1", store="Whole Foods")

    result = await svc.reorder_stores(["Whole Foods", "Bakery", "Aldi"])
    names = [s.name for s in result]
    assert names == ["Whole Foods", "Bakery", "Aldi"]

    assert len(captured) == 1
    assert captured[0].order == ("Whole Foods", "Bakery", "Aldi")


async def test_shopping_rename_store_emits_event_and_cascades(env):
    """Rename publishes ``ShoppingStoreRenamed`` once the repo cascade
    has run; items at the old name now point at the new name."""
    from socialhome.domain.events import ShoppingStoreRenamed
    from socialhome.infrastructure.event_bus import EventBus

    bus = EventBus()
    svc = ShoppingService(env.shopping_repo, bus)
    captured: list = []
    bus.subscribe(ShoppingStoreRenamed, lambda e: captured.append(e))

    await svc.add_item("Milk", created_by="u1", store="Aldi")
    await svc.add_item("Eggs", created_by="u1", store="Aldi")

    result = await svc.rename_store("Aldi", "Coop")

    assert result is not None
    assert len(captured) == 1
    assert captured[0].old_name == "Aldi"
    assert captured[0].new_name == "Coop"
    items = await svc.list_items()
    assert {i.text: i.store for i in items} == {"Milk": "Coop", "Eggs": "Coop"}


async def test_shopping_rename_store_collision_merges_and_emits(env):
    """A collision is a merge, not an error: items fold onto the
    surviving store and ``ShoppingStoreRenamed`` still fires so the SPA
    can collapse the duplicate locally."""
    from socialhome.domain.events import ShoppingStoreRenamed
    from socialhome.infrastructure.event_bus import EventBus

    bus = EventBus()
    svc = ShoppingService(env.shopping_repo, bus)
    captured: list = []
    bus.subscribe(ShoppingStoreRenamed, lambda e: captured.append(e))

    await svc.add_item("Milk", created_by="u1", store="Aldi")
    await svc.add_item("Bread", created_by="u1", store="Migros")

    result = await svc.rename_store("Aldi", "Migros")

    assert result is not None
    assert result.merged is True
    assert result.moved_items == 1
    assert len(captured) == 1
    assert captured[0].old_name == "Aldi"
    assert captured[0].new_name == "Migros"
    stores = await svc.list_stores()
    assert [s.name for s in stores] == ["Migros"]


async def test_shopping_rename_store_clamps_new_name(env):
    """The new name goes through the same cleaning as every other
    store input — trimmed and clamped to 80 chars."""
    await env.shopping_svc.add_item("Milk", created_by="u1", store="Aldi")

    result = await env.shopping_svc.rename_store("Aldi", "  " + "A" * 200 + "  ")

    assert result is not None
    assert result.new_name == "A" * 80
    stores = await env.shopping_svc.list_stores()
    assert [s.name for s in stores] == ["A" * 80]


async def test_shopping_rename_store_missing_returns_none(env):
    """Unknown old name → ``None`` so the route can map to a 404."""
    assert await env.shopping_svc.rename_store("Ghost", "Coop") is None


async def test_shopping_rename_store_blank_new_name_rejected(env):
    """A blank new name is a 422, not a silent no-op."""
    import pytest

    await env.shopping_svc.add_item("Milk", created_by="u1", store="Aldi")
    with pytest.raises(ValueError):
        await env.shopping_svc.rename_store("Aldi", "   ")


async def test_shopping_create_store_creates_and_is_idempotent(env):
    """``create_store`` adds a catalogue row without an item, and a
    second call with different casing returns the existing row
    unchanged."""
    first = await env.shopping_svc.create_store("  Bakery  ")
    assert first.name == "Bakery"
    assert first.sort_order == 0

    again = await env.shopping_svc.create_store("bakery")
    assert again.name == "Bakery"
    assert again.sort_order == 0
    stores = await env.shopping_svc.list_stores()
    assert [s.name for s in stores] == ["Bakery"]


async def test_shopping_create_store_blank_rejected(env):
    """A blank store name raises ValueError (route → 422)."""
    import pytest

    with pytest.raises(ValueError):
        await env.shopping_svc.create_store("   ")


async def test_shopping_delete_store_emits_event(env):
    """Delete clears item ``store`` to NULL and publishes
    ``ShoppingStoreDeleted`` with the cleared store's name."""
    from socialhome.domain.events import ShoppingStoreDeleted
    from socialhome.infrastructure.event_bus import EventBus

    bus = EventBus()
    svc = ShoppingService(env.shopping_repo, bus)
    captured: list = []
    bus.subscribe(ShoppingStoreDeleted, lambda e: captured.append(e))

    await svc.add_item("Milk", created_by="u1", store="Aldi")
    await svc.add_item("Eggs", created_by="u1", store="Aldi")

    cleared = await svc.delete_store("Aldi")

    assert cleared == 2
    assert len(captured) == 1
    assert captured[0].name == "Aldi"
    stores = await svc.list_stores()
    assert "Aldi" not in [s.name for s in stores]


async def test_shopping_list_stores_returns_catalogue(env):
    """list_stores returns rows in canonical sort_order."""
    await env.shopping_svc.add_item("Bread", created_by="u1", store="Bakery")
    await env.shopping_svc.add_item("Milk", created_by="u1", store="Aldi")

    stores = await env.shopping_svc.list_stores()
    assert [s.name for s in stores] == ["Bakery", "Aldi"]
    assert [s.sort_order for s in stores] == [0, 1]


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
