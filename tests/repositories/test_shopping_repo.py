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


# ─── Store column + catalogue ─────────────────────────────────────────────


async def test_add_with_store_persists_and_creates_catalogue_row(env):
    """add(store=…) persists the field AND auto-upserts a catalogue row."""
    item = await env.repo.add("Milk", created_by="uid-alice", store="Aldi")
    assert item.store == "Aldi"

    fetched = await env.repo.get(item.id)
    assert fetched.store == "Aldi"

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi"]
    assert stores[0].sort_order == 0


async def test_add_without_store_leaves_catalogue_empty(env):
    """Plain add (no store) does NOT seed the catalogue."""
    await env.repo.add("Eggs", created_by="uid-alice")
    assert await env.repo.list_stores() == []


async def test_touch_store_assigns_increasing_sort_order(env):
    """Each new store gets MAX(sort_order)+1; re-touching is idempotent."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")
    await env.repo.touch_store("Whole Foods")
    # Idempotent — re-touching Aldi MUST NOT bump its order.
    await env.repo.touch_store("Aldi")

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi", "Bakery", "Whole Foods"]
    assert [s.sort_order for s in stores] == [0, 1, 2]


async def test_touch_store_ignores_empty(env):
    """Empty / whitespace-only names don't create catalogue rows."""
    await env.repo.touch_store("")
    assert await env.repo.list_stores() == []


async def test_add_store_matches_existing_case_insensitively(env):
    """Adding ``@ aldi`` when ``Aldi`` already exists reuses the
    existing catalogue casing instead of spawning a duplicate row —
    and the item carries the canonical name so grouping stays merged."""
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    item = await env.repo.add("Eggs", created_by="u1", store="aldi")

    # The item is stored under the catalogue's existing casing.
    assert item.store == "Aldi"
    # No duplicate catalogue row.
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi"]


async def test_add_new_store_keeps_its_own_casing(env):
    """A genuinely new store keeps exactly the casing the user typed —
    canonicalisation only kicks in against an existing match."""
    item = await env.repo.add("Milk", created_by="u1", store="Whole Foods")
    assert item.store == "Whole Foods"
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Whole Foods"]


async def test_update_item_store_matches_existing_case_insensitively(env):
    """update_item canonicalises a case-variant store the same way add
    does, so editing an item's store can't fork the catalogue."""
    await env.repo.touch_store("Bakery")
    item = await env.repo.add("Bread", created_by="u1")

    updated = await env.repo.update_item(item.id, store="bakery")

    assert updated.store == "Bakery"
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Bakery"]


async def test_update_item_text_only_keeps_store(env):
    """update_item(text=…) without store sentinel leaves the store alone."""
    item = await env.repo.add("Milk", created_by="uid-alice", store="Aldi")
    updated = await env.repo.update_item(item.id, text="Whole Milk")
    assert updated.text == "Whole Milk"
    assert updated.store == "Aldi"


async def test_update_item_clears_store_with_none(env):
    """update_item(store=None) clears the field."""
    item = await env.repo.add("Milk", created_by="uid-alice", store="Aldi")
    updated = await env.repo.update_item(item.id, store=None)
    assert updated.store is None
    fetched = await env.repo.get(item.id)
    assert fetched.store is None


async def test_update_item_sets_new_store_and_upserts_catalogue(env):
    """update_item with a brand-new store auto-creates the catalogue row."""
    item = await env.repo.add("Milk", created_by="uid-alice", store="Aldi")
    await env.repo.update_item(item.id, store="Whole Foods")

    stores = await env.repo.list_stores()
    names = [s.name for s in stores]
    assert "Aldi" in names
    assert "Whole Foods" in names
    # Whole Foods was added last, so it gets the tail order.
    whole_foods = next(s for s in stores if s.name == "Whole Foods")
    aldi = next(s for s in stores if s.name == "Aldi")
    assert whole_foods.sort_order > aldi.sort_order


async def test_update_item_unknown_returns_none(env):
    """update_item on an unknown id returns None (route maps to 404)."""
    assert await env.repo.update_item("nope") is None


async def test_reorder_stores_applies_input_order(env):
    """reorder_stores assigns sort_order = index for each named store."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")
    await env.repo.touch_store("Whole Foods")

    await env.repo.reorder_stores(["Whole Foods", "Bakery", "Aldi"])

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Whole Foods", "Bakery", "Aldi"]
    assert [s.sort_order for s in stores] == [0, 1, 2]


async def test_reorder_stores_ignores_unknown_names(env):
    """Unknown names in the input are silently dropped."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")

    await env.repo.reorder_stores(["Bakery", "Ghost Store", "Aldi"])

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Bakery", "Aldi"]


async def test_reorder_stores_keeps_missing_names_past_tail(env):
    """Catalogue rows the input forgot retain their relative order past
    the explicitly-ordered tail."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")
    await env.repo.touch_store("Whole Foods")

    await env.repo.reorder_stores(["Whole Foods"])

    stores = await env.repo.list_stores()
    # "Whole Foods" first; the other two keep their original sort
    # (Aldi was touched before Bakery) tucked past it.
    assert [s.name for s in stores] == ["Whole Foods", "Aldi", "Bakery"]
    assert [s.sort_order for s in stores] == [0, 1, 2]


async def test_reorder_stores_dedupes_input(env):
    """A name listed twice in the input is only positioned by its first
    occurrence — defends against a buggy client posting duplicates."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")

    await env.repo.reorder_stores(["Aldi", "Bakery", "Aldi"])

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi", "Bakery"]


async def test_rename_store_updates_catalogue_and_items(env):
    """Renaming cascades to every item whose ``store`` matched."""
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    await env.repo.add("Eggs", created_by="u1", store="Aldi")
    await env.repo.add("Bread", created_by="u1", store="Bakery")

    result = await env.repo.rename_store("Aldi", "Coop")

    assert result is not None
    stores = await env.repo.list_stores()
    assert "Aldi" not in [s.name for s in stores]
    assert "Coop" in [s.name for s in stores]
    items = await env.repo.list()
    by_text = {i.text: i for i in items}
    assert by_text["Milk"].store == "Coop"
    assert by_text["Eggs"].store == "Coop"
    assert by_text["Bread"].store == "Bakery"


async def test_rename_store_same_name_is_noop(env):
    """Renaming a store to its current name shortcuts without
    touching the DB. Reports no move and no merge."""
    await env.repo.touch_store("Aldi")
    result = await env.repo.rename_store("Aldi", "Aldi")
    assert result is not None
    assert result.merged is False
    assert result.moved_items == 0
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi"]


async def test_delete_store_clears_items_and_removes_catalogue_row(env):
    """Delete drops the catalogue row + sets ``store=NULL`` on every
    item that referenced it. Returns the count of items cleared."""
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    await env.repo.add("Eggs", created_by="u1", store="Aldi")
    await env.repo.add("Bread", created_by="u1", store="Bakery")

    cleared = await env.repo.delete_store("Aldi")

    assert cleared == 2
    stores = await env.repo.list_stores()
    assert "Aldi" not in [s.name for s in stores]
    items = await env.repo.list()
    by_text = {i.text: i for i in items}
    assert by_text["Milk"].store is None
    assert by_text["Eggs"].store is None
    assert by_text["Bread"].store == "Bakery"


async def test_delete_store_missing_is_zero(env):
    """Delete-on-missing returns zero rather than raising — operators
    double-clicking the trash icon shouldn't see an error."""
    await env.repo.touch_store("Aldi")
    cleared = await env.repo.delete_store("Migrso")
    assert cleared == 0
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi"]


# ─── Case-insensitive store catalogue (§0048 NOCASE guard) ───────────────


async def test_touch_store_case_variant_does_not_fork_or_raise(env):
    """The 0048 NOCASE unique index makes a differently-cased
    ``touch_store`` a no-op rather than an IntegrityError — the
    ``ON CONFLICT ... DO NOTHING`` clause covers any uniqueness
    violation on the row, not just the BINARY primary key."""
    await env.repo.touch_store("Migros")
    await env.repo.touch_store("migros")

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Migros"]


async def test_rename_store_resolves_old_name_case_insensitively(env):
    """``rename_store`` finds the catalogue row regardless of the
    casing the caller typed."""
    await env.repo.add("Milk", created_by="u1", store="Aldi")

    result = await env.repo.rename_store("aLdI", "Coop")

    assert result is not None
    assert result.old_name == "Aldi"
    assert result.new_name == "Coop"
    assert result.merged is False
    assert result.moved_items == 1
    assert [s.name for s in await env.repo.list_stores()] == ["Coop"]


async def test_rename_store_onto_existing_store_merges(env):
    """A collision is a MERGE: items fold onto the target's exact
    spelling, the old catalogue row goes, and the target keeps its
    own ``sort_order``."""
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    await env.repo.add("Eggs", created_by="u1", store="Aldi")
    await env.repo.add("Bread", created_by="u1", store="Migros")
    await env.repo.reorder_stores(["Migros", "Aldi"])
    before = {s.name: s.sort_order for s in await env.repo.list_stores()}

    result = await env.repo.rename_store("Aldi", "migros")

    assert result is not None
    assert result.merged is True
    assert result.moved_items == 2
    assert result.old_name == "Aldi"
    assert result.new_name == "Migros"
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Migros"]
    assert stores[0].sort_order == before["Migros"]
    items = await env.repo.list()
    assert {i.text: i.store for i in items} == {
        "Milk": "Migros",
        "Eggs": "Migros",
        "Bread": "Migros",
    }


async def test_rename_store_pure_case_change_keeps_sort_order(env):
    """``"migros"`` → ``"Migros"`` is a rename in place, not a merge —
    the row keeps its position in the trip order."""
    await env.repo.touch_store("Aldi")
    await env.repo.add("Bread", created_by="u1", store="migros")

    result = await env.repo.rename_store("migros", "Migros")

    assert result is not None
    assert result.merged is False
    assert result.new_name == "Migros"
    stores = await env.repo.list_stores()
    assert [(s.name, s.sort_order) for s in stores] == [("Aldi", 0), ("Migros", 1)]
    items = await env.repo.list()
    assert items[0].store == "Migros"


async def test_rename_store_moves_items_whose_casing_diverged(env):
    """A legacy item whose ``store`` casing diverged from the catalogue
    is carried along by the rename (NOCASE item match)."""
    await env.repo.touch_store("Aldi")
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    await env.db.enqueue(
        "UPDATE shopping_list_items SET store='aldi' WHERE text='Milk'",
    )

    result = await env.repo.rename_store("Aldi", "Coop")

    assert result is not None
    assert result.moved_items == 1
    items = await env.repo.list()
    assert items[0].store == "Coop"


async def test_rename_store_missing_returns_none(env):
    """Unknown old name → ``None`` so the route can map to a 404."""
    await env.repo.touch_store("Aldi")
    assert await env.repo.rename_store("Migrso", "Migros") is None


async def test_delete_store_clears_items_whose_casing_diverged(env):
    """``delete_store`` matches items case-insensitively, so a legacy
    row with divergent casing is not stranded pointing at a store that
    no longer exists."""
    await env.repo.touch_store("Aldi")
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    await env.db.enqueue(
        "UPDATE shopping_list_items SET store='ALDI' WHERE text='Milk'",
    )

    cleared = await env.repo.delete_store("aldi")

    assert cleared == 1
    assert [s.name for s in await env.repo.list_stores()] == []
    items = await env.repo.list()
    assert items[0].store is None


async def test_create_store_appends_past_max_sort_order(env):
    """``create_store`` puts a brand-new store at the end of the trip
    order, exactly like ``touch_store``."""
    await env.repo.touch_store("Aldi")

    store = await env.repo.create_store("Bakery")

    assert store.name == "Bakery"
    assert store.sort_order == 1
    assert [s.name for s in await env.repo.list_stores()] == ["Aldi", "Bakery"]


async def test_create_store_is_idempotent_case_insensitively(env):
    """Creating a store that already exists under different casing
    returns the EXISTING row — same spelling, same ``sort_order``."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Migros")
    await env.repo.reorder_stores(["Migros", "Aldi"])

    store = await env.repo.create_store("aldi")

    assert store.name == "Aldi"
    assert store.sort_order == 1
    assert [s.name for s in await env.repo.list_stores()] == ["Migros", "Aldi"]
