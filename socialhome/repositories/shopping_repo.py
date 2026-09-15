"""Shopping list repository (§23.120).

Household-scoped. Two tables:

* ``shopping_list_items`` — the actual list. Items optionally carry a
  ``store`` (free-form name of the shop where the item should be
  bought).
* ``shopping_stores`` — the household's store catalogue, the only
  table that durably remembers a store's "trip order". Auto-upserted
  on first sighting from an item's ``store`` field so the catalogue
  grows organically; rows are NOT removed when items that referenced
  them go away, so a family keeps its drag-defined order across empty
  shopping lists.

The service layer is responsible for cleaning up completed items on
a schedule or on user action.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..db.unit_of_work import UnitOfWork
from .base import bool_col, row_to_dict, rows_to_dicts


# Domain dataclasses live in ``socialhome/domain/shopping.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.shopping import (  # noqa: F401,E402
    ShoppingItem,
    ShoppingStore,
    StoreRenameResult,
)


# The repo has a method named ``list`` which shadows the builtin
# inside class scope under mypy's strict resolution. Use this alias
# for any ``list[...]`` annotation declared in the class body.
_list = builtins.list


#: Sentinel for :meth:`SqliteShoppingRepo.update_item` so the route
#: layer can express "leave this field alone" alongside "clear" /
#: "set to value". ``None`` already means "clear" for ``store``; we
#: need a third state to mean "no change" without overloading the
#: ``None`` slot. Same shape as ``UNSET_COVER`` / ``UNSET_LOCATION``
#: in the calendar service.
_UNSET: object = object()
UNSET_FIELD: object = _UNSET


@runtime_checkable
class AbstractShoppingRepo(Protocol):
    async def add(
        self,
        text: str,
        *,
        created_by: str,
        store: str | None = None,
    ) -> ShoppingItem: ...
    async def get(self, item_id: str) -> ShoppingItem | None: ...
    async def list(self, *, include_completed: bool = False) -> _list[ShoppingItem]: ...
    async def update_item(
        self,
        item_id: str,
        *,
        text: str | None = None,
        store: object = _UNSET,
    ) -> ShoppingItem | None: ...
    async def complete(self, item_id: str) -> None: ...
    async def uncomplete(self, item_id: str) -> None: ...
    async def delete(self, item_id: str) -> None: ...
    async def clear_completed(self) -> int: ...
    async def list_stores(self) -> _list[ShoppingStore]: ...
    async def create_store(self, name: str) -> ShoppingStore: ...
    async def touch_store(self, name: str) -> None: ...
    async def reorder_stores(self, ordered_names: _list[str]) -> None: ...
    async def rename_store(
        self,
        old_name: str,
        new_name: str,
    ) -> StoreRenameResult | None: ...
    async def delete_store(self, name: str) -> int: ...


class SqliteShoppingRepo:
    """SQLite-backed :class:`AbstractShoppingRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ─── Items ───────────────────────────────────────────────────────────

    async def _canonical_store_name(self, name: str) -> str:
        """Resolve ``name`` against the catalogue case-insensitively.

        Returns the existing catalogue row's casing when one matches
        (``"aldi"`` → ``"Aldi"``), else ``name`` unchanged. Keeps the
        store catalogue from forking into ``"Aldi"`` / ``"aldi"`` rows
        when a member types a store name with different capitalisation —
        the item is then stored under the canonical name so grouping
        stays merged. ASCII-case folding via SQLite's ``NOCASE``
        collation is plenty for free-form store names.
        """
        row = await self._db.fetchone(
            "SELECT name FROM shopping_stores WHERE name = ? COLLATE NOCASE",
            (name,),
        )
        existing = row_to_dict(row)
        return existing["name"] if existing else name

    async def add(
        self,
        text: str,
        *,
        created_by: str,
        store: str | None = None,
    ) -> ShoppingItem:
        text = text.strip()
        if not text:
            raise ValueError("shopping item text must not be empty")
        if store:
            store = await self._canonical_store_name(store)
        now = datetime.now(timezone.utc).isoformat()
        item = ShoppingItem(
            id=uuid.uuid4().hex,
            text=text,
            completed=False,
            created_by=created_by,
            created_at=now,
            completed_at=None,
            store=store,
        )
        await self._db.enqueue(
            """
            INSERT INTO shopping_list_items(
                id, text, completed, created_by, created_at,
                completed_at, store
            ) VALUES(?, ?, 0, ?, ?, NULL, ?)
            """,
            (item.id, item.text, item.created_by, item.created_at, item.store),
        )
        if store:
            await self.touch_store(store)
        return item

    async def get(self, item_id: str) -> ShoppingItem | None:
        row = await self._db.fetchone(
            "SELECT * FROM shopping_list_items WHERE id=?",
            (item_id,),
        )
        return _row_to_item(row_to_dict(row))

    async def list(
        self,
        *,
        include_completed: bool = False,
    ) -> _list[ShoppingItem]:
        if include_completed:
            rows = await self._db.fetchall(
                "SELECT * FROM shopping_list_items ORDER BY completed ASC, created_at",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM shopping_list_items WHERE completed=0 "
                "ORDER BY created_at",
            )
        return [i for i in (_row_to_item(d) for d in rows_to_dicts(rows)) if i]

    async def update_item(
        self,
        item_id: str,
        *,
        text: str | None = None,
        store: object = _UNSET,
    ) -> ShoppingItem | None:
        """Patch a single item's text / store. Returns the row after the
        write so the service layer can publish a fresh-state event
        without a second round-trip.

        ``text=None`` means "leave unchanged" — empty strings are
        rejected by the service layer before reaching here. ``store``
        uses the :data:`_UNSET` sentinel because ``None`` is a
        meaningful value (clear the field).
        """
        existing = await self.get(item_id)
        if existing is None:
            return None

        new_text = text if text is not None else existing.text
        if store is _UNSET:
            new_store = existing.store
        else:
            assert store is None or isinstance(store, str)
            new_store = store
            if new_store:
                new_store = await self._canonical_store_name(new_store)

        await self._db.enqueue(
            "UPDATE shopping_list_items SET text=?, store=? WHERE id=?",
            (new_text, new_store, item_id),
        )
        if new_store:
            await self.touch_store(new_store)
        return ShoppingItem(
            id=existing.id,
            text=new_text,
            completed=existing.completed,
            created_by=existing.created_by,
            created_at=existing.created_at,
            completed_at=existing.completed_at,
            store=new_store,
        )

    async def complete(self, item_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE shopping_list_items
               SET completed=1, completed_at=datetime('now')
             WHERE id=? AND completed=0
            """,
            (item_id,),
        )

    async def uncomplete(self, item_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE shopping_list_items
               SET completed=0, completed_at=NULL
             WHERE id=?
            """,
            (item_id,),
        )

    async def delete(self, item_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM shopping_list_items WHERE id=?",
            (item_id,),
        )

    async def clear_completed(self) -> int:
        """Delete every completed item. Returns the count removed."""
        count = await self._db.fetchval(
            "SELECT COUNT(*) FROM shopping_list_items WHERE completed=1",
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM shopping_list_items WHERE completed=1",
        )
        return int(count)

    # ─── Stores ──────────────────────────────────────────────────────────

    async def list_stores(self) -> _list[ShoppingStore]:
        rows = await self._db.fetchall(
            "SELECT name, sort_order FROM shopping_stores ORDER BY sort_order, name",
        )
        return [
            ShoppingStore(name=d["name"], sort_order=int(d["sort_order"]))
            for d in rows_to_dicts(rows)
        ]

    async def _find_store(self, name: str) -> ShoppingStore | None:
        """Look a catalogue row up case-insensitively.

        The ``ux_shopping_stores_name_nocase`` guard (migration 0048)
        makes at most one row match, so every store-scoped operation
        can resolve the caller's spelling to the catalogue's own.
        """
        row = await self._db.fetchone(
            "SELECT name, sort_order FROM shopping_stores "
            "WHERE name = ? COLLATE NOCASE",
            (name,),
        )
        found = row_to_dict(row)
        if not found:
            return None
        return ShoppingStore(name=found["name"], sort_order=int(found["sort_order"]))

    async def create_store(self, name: str) -> ShoppingStore:
        """Add a catalogue row directly, without an item to hang it on.

        Idempotent case-insensitively: when a store already matches,
        the EXISTING row comes back untouched — same casing, same
        ``sort_order`` — so the SPA's "+ Add store" action is safe to
        replay and can never re-spell a store the household curated.
        A genuinely new store appends past the current max, exactly
        like :meth:`touch_store`.
        """
        name = name.strip()
        if not name:
            raise ValueError("store name must not be empty")
        existing = await self._find_store(name)
        if existing is not None:
            return existing
        await self.touch_store(name)
        created = await self._find_store(name)
        assert created is not None  # touch_store just wrote it
        return created

    async def touch_store(self, name: str) -> None:
        """Ensure ``name`` exists in the catalogue, appending it past
        the current max if new. Idempotent — a second call with the
        same name leaves the existing ``sort_order`` alone, which is
        what we want for autosuggest re-typing.
        """
        if not name:
            return
        max_order = await self._db.fetchval(
            "SELECT COALESCE(MAX(sort_order), -1) FROM shopping_stores",
            default=-1,
        )
        next_order = int(max_order) + 1
        await self._db.enqueue(
            """
            INSERT INTO shopping_stores(name, sort_order) VALUES(?, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (name, next_order),
        )

    async def reorder_stores(self, ordered_names: _list[str]) -> None:
        """Assign ``sort_order = index`` to each name in ``ordered_names``.

        Names not in the input keep their relative order but shift
        past the end (their ``sort_order`` is bumped to
        ``len(ordered_names) + i``). Unknown names in the input are
        ignored — the user might be reordering on stale data.
        """
        current = await self.list_stores()
        current_names = [s.name for s in current]
        known = set(current_names)
        seen: set[str] = set()
        new_order: _list[str] = []
        for name in ordered_names:
            if name in known and name not in seen:
                seen.add(name)
                new_order.append(name)
        # Any catalogue rows the input forgot keep their relative
        # order tucked behind the explicitly-ordered tail.
        for name in current_names:
            if name not in seen:
                new_order.append(name)
        for idx, name in enumerate(new_order):
            await self._db.enqueue(
                "UPDATE shopping_stores SET sort_order=? WHERE name=?",
                (idx, name),
            )

    async def rename_store(
        self,
        old_name: str,
        new_name: str,
    ) -> StoreRenameResult | None:
        """Rename a catalogue row + cascade to every item that
        references it. Returns ``None`` when no store matches
        ``old_name`` (caller maps it to a 404).

        Both names resolve case-insensitively, in step with the
        ``ux_shopping_stores_name_nocase`` guard from migration 0048:

        * a DIFFERENT store already holding ``new_name`` → **merge**.
          The old store's items move onto the survivor's exact
          spelling, the old catalogue row goes, and the survivor keeps
          its own ``sort_order`` — the place in the trip order the
          household dragged it to. Collapsing a duplicate is the only
          way out of a pre-0048 case fork, so this is a success, not a
          conflict.
        * the only match being the old row itself (a pure case change,
          ``"migros"`` → ``"Migros"``) → rename in place, ``sort_order``
          untouched.

        Cascade rule: ``shopping_list_items.store`` is free text with no
        FK, so the items are re-pointed by an explicit UPDATE — matched
        ``COLLATE NOCASE`` so a legacy item whose casing diverged from
        the catalogue is carried along rather than left behind pointing
        at a store that no longer exists.
        """
        old_name = old_name.strip()
        new_name = new_name.strip()
        if not old_name or not new_name:
            raise ValueError("store names must be non-empty")

        old = await self._find_store(old_name)
        if old is None:
            return None
        target = await self._find_store(new_name)

        if target is not None and target.name != old.name:
            survivor, merged = target.name, True
        elif old.name == new_name:
            # Exact no-op — nothing to write, nothing moved.
            return StoreRenameResult(
                old_name=old.name,
                new_name=old.name,
                merged=False,
                moved_items=0,
            )
        else:
            survivor, merged = new_name, False

        moved = int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM shopping_list_items "
                "WHERE store = ? COLLATE NOCASE",
                (old.name,),
                default=0,
            )
        )
        # Both writes go in ONE transaction. ``enqueue`` commits per
        # statement, so a crash between them would leave items pointing
        # at a catalogue row that no longer exists — and an item whose
        # store has no catalogue row renders in no section at all, which
        # is the exact bug this whole change exists to fix. All-or-
        # nothing instead.
        async with UnitOfWork(self._db) as uow:
            if merged:
                await uow.exec(
                    "DELETE FROM shopping_stores WHERE name = ? COLLATE NOCASE",
                    (old.name,),
                )
            else:
                await uow.exec(
                    "UPDATE shopping_stores SET name=? WHERE name=?",
                    (survivor, old.name),
                )
            await uow.exec(
                "UPDATE shopping_list_items SET store=? WHERE store = ? COLLATE NOCASE",
                (survivor, old.name),
            )
        return StoreRenameResult(
            old_name=old.name,
            new_name=survivor,
            merged=merged,
            moved_items=moved,
        )

    async def delete_store(self, name: str) -> int:
        """Remove a store from the catalogue + clear it from every
        item that currently references it. Returns the count of
        items whose ``store`` was set to NULL (zero is fine — the
        store may have had nothing on it).

        Every match is ``COLLATE NOCASE``, in step with the 0048
        guard: an item whose casing diverged from the catalogue would
        otherwise keep pointing at a store that no longer exists, and
        the SPA's grouped view renders such an item in NO section at
        all — invisible, not merely misfiled.

        Catalogue row may not exist (already-deleted from another
        tab). That's a no-op return-zero, not a 4xx — operators
        often double-click.
        """
        name = name.strip()
        if not name:
            return 0
        affected = await self._db.fetchval(
            "SELECT COUNT(*) FROM shopping_list_items WHERE store = ? COLLATE NOCASE",
            (name,),
            default=0,
        )
        await self._db.enqueue(
            "UPDATE shopping_list_items SET store=NULL WHERE store = ? COLLATE NOCASE",
            (name,),
        )
        await self._db.enqueue(
            "DELETE FROM shopping_stores WHERE name = ? COLLATE NOCASE",
            (name,),
        )
        return int(affected)


def _row_to_item(row: dict | None) -> ShoppingItem | None:
    if row is None:
        return None
    return ShoppingItem(
        id=row["id"],
        text=row["text"],
        completed=bool_col(row.get("completed", 0)),
        created_by=row["created_by"],
        created_at=row["created_at"],
        completed_at=row.get("completed_at"),
        store=row.get("store"),
    )
