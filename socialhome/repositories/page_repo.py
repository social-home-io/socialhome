"""Page repository — household (``pages``) and space (``space_pages``)
Markdown documents plus edit locks and version history.

Locks (§5.2 page locking):

* Any editor acquires an exclusive lock on a page before beginning edits;
  the lock expires 30 minutes after acquisition unless refreshed. A second
  editor is blocked until the lock expires or the holder releases it.
* Deletion is two-step — a user "requests" deletion, a second user with
  admin/editor rights "approves" it, only then does the actual row delete.
  The two-step dance is tracked on the row (``delete_requested_by`` /
  ``delete_approved_by``).

Versions: every save appends a row to ``page_edit_history`` keyed on
``(page_id, version)`` so rollback and diff tooling can reconstruct state.
For a space page that history is also its v_48 ancestry (the versions its
body absorbed), so it keeps :data:`MAX_SPACE_HISTORY` rows.

Snapshots (``space_page_snapshots``): the open sides of a concurrent-edit
conflict (``conflict=1``, each a whole version as canonical JSON) and a
short record of resolved ones (``conflict=0``).
"""

from __future__ import annotations

import builtins
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.page_version import DraftBase, PageConflictSide, version_hash
from ..federation.owner_bound_id import SPACE_PAGE_KIND, mint_owner_bound_id
from .base import row_to_dict, rows_to_dicts


#: How long an edit lock stays valid before anyone else can claim it
#: (spec §23.72). Clients refresh every ``LOCK_TTL / 2`` via
#: ``POST /api/pages/{id}/lock/refresh``; PageLockScheduler sweeps
#: expired rows every 30 s so stale locks never pile up.
LOCK_TTL = timedelta(seconds=60)

#: Maximum number of edit-history rows retained per household page (spec
#: §31901). Older versions are pruned on each ``save_version`` write.
MAX_HISTORY = 5

#: Edit-history cap for a SPACE page (v_48). On the space's host the
#: history is also where a proposal's base is looked up (by hash) for a
#: three-way merge, so it keeps enough rows for a member that edited a
#: while ago (``HOST_HISTORY`` in the sequencer).
MAX_SPACE_HISTORY = 50

#: Cap on resolved (``conflict=0``) snapshot rows kept per page and scope.
#: Pruned on each ``insert_snapshot`` write and dropped with the page.
#: Open sides (``conflict=1``) and a draft's base (``side='base'``) are
#: never pruned: the sequencer caps sides itself.
MAX_PAGE_SNAPSHOTS = 10


class PageLockError(Exception):
    """Raised when an editor tries to acquire a lock another editor holds."""


class PageNotFoundError(Exception):
    """Raised when an operation targets a missing page id."""


class _CrossSpaceWrite(Exception):
    """Rolls a :meth:`SqlitePageRepo.commit_version` transaction back."""


# Domain dataclasses live in ``socialhome/domain/page.py``. They are
# re-exported here so existing repo-level imports keep working.
from ..domain.page import Page, PageTombstone, PageVersion  # noqa: F401,E402


@runtime_checkable
class AbstractPageRepo(Protocol):
    async def save(self, page: Page, *, space_id: str | None) -> bool: ...
    async def get(self, page_id: str) -> Page | None: ...
    async def get_space_page(self, page_id: str, *, space_id: str) -> Page | None: ...
    async def get_household_page(self, page_id: str) -> Page | None: ...
    async def list(
        self,
        *,
        space_id: str | None = None,
    ) -> builtins.list[Page]: ...
    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> builtins.list[Page]: ...
    async def delete(
        self,
        page_id: str,
        *,
        space_id: str | None,
        deleted_by: str = "",
        confirmed: bool = True,
    ) -> bool: ...
    async def confirm_delete(self, page_id: str, *, space_id: str) -> bool: ...
    async def revive(self, page_id: str, *, space_id: str, seq: int) -> bool: ...
    async def tombstone(
        self,
        page_id: str,
        *,
        space_id: str,
        created_by: str,
        deleted_by: str = "",
    ) -> bool: ...
    async def is_page_deleted(self, page_id: str, *, space_id: str) -> bool: ...
    async def list_page_tombstones(
        self,
        space_id: str,
        *,
        since: str | None = None,
        limit: int = 500,
        before: tuple[str, str] | None = None,
    ) -> builtins.list[PageTombstone]: ...
    async def raise_seq(self, page_id: str, *, space_id: str, seq: int) -> bool: ...

    async def acquire_lock(
        self,
        page_id: str,
        editor: str,
        *,
        ttl: timedelta = LOCK_TTL,
    ) -> None: ...
    async def refresh_lock(
        self,
        page_id: str,
        editor: str,
        *,
        ttl: timedelta = LOCK_TTL,
    ) -> None: ...
    async def release_lock(self, page_id: str, editor: str) -> None: ...
    async def release_expired_locks(self) -> int: ...
    async def get_lock(self, page_id: str) -> dict | None: ...

    async def request_delete(self, page_id: str, user_id: str) -> None: ...
    async def approve_delete(self, page_id: str, approver: str) -> None: ...
    async def clear_delete_request(self, page_id: str) -> None: ...

    async def save_version(self, version: PageVersion) -> PageVersion: ...
    async def list_versions(
        self, page_id: str, *, space_id: str | None
    ) -> builtins.list[PageVersion]: ...
    async def next_version_number(self, page_id: str) -> int: ...

    # Snapshot bookkeeping for §4.4.4.1 conflict resolution.
    async def insert_snapshot(
        self,
        *,
        page_id: str,
        space_id: str | None,
        body: str,
        author_user_id: str,
        side: str,
        conflict: bool,
        title: str | None = None,
    ) -> None: ...
    async def has_active_conflict(self, page_id: str, *, space_id: str) -> bool: ...
    async def list_conflict_sides(
        self, page_id: str, *, space_id: str
    ) -> builtins.list[PageConflictSide]: ...
    async def set_conflict_sides(
        self,
        page_id: str,
        *,
        space_id: str,
        sides: Sequence[PageConflictSide],
    ) -> None: ...
    async def space_pages_in_conflict(self, space_id: str) -> set[str]: ...
    async def commit_version(
        self,
        page: Page,
        *,
        space_id: str,
        history: Sequence[PageVersion],
        sides: Sequence[PageConflictSide],
    ) -> bool: ...
    async def clear_conflict_flag(self, page_id: str, *, space_id: str) -> None: ...
    async def set_draft_base(
        self, page_id: str, *, space_id: str, base: DraftBase
    ) -> None: ...
    async def get_draft_base(
        self, page_id: str, *, space_id: str
    ) -> DraftBase | None: ...
    async def clear_draft_base(self, page_id: str, *, space_id: str) -> None: ...
    async def list_pending_drafts(
        self, *, space_id: str | None = None
    ) -> builtins.list[tuple[str, str]]: ...


class SqlitePageRepo:
    """SQLite-backed :class:`AbstractPageRepo`.

    Chooses between the ``pages`` and ``space_pages`` tables based on
    whether the ``Page`` carries a ``space_id``. Callers don't need to
    know the split.
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Pages ──────────────────────────────────────────────────────────

    async def save(self, page: Page, *, space_id: str | None) -> bool:
        """Upsert a page into the table its scope selects.

        ``space_id`` is authoritative (§24.11) — for an inbound
        federation write it is the space the pipeline gated the sender
        on, and it decides *both* which table is touched (``None`` →
        the household ``pages`` table, otherwise ``space_pages``) and
        which rows may be updated. An id that already belongs to
        another space is refused: ``False`` means nothing was written.

        ``updated_at`` is the page's own (the writer stamps it — a local
        edit with ``datetime.now(timezone.utc).isoformat()``, a mirrored
        one with the sender's), on insert and update alike, so the column
        keeps one tz-aware ISO-8601 shape and the value an editor reads
        back is exactly what its next ``base_updated_at`` must match. An
        empty one is stamped now.
        """
        stamp = page.updated_at or datetime.now(timezone.utc).isoformat()
        if space_id is None:
            n = await self._db.enqueue_rowcount(
                """
                INSERT INTO pages(
                    id, title, content, cover_image_url, created_by,
                    created_at, updated_at,
                    last_editor_user_id, last_edited_at,
                    locked_by, locked_at, lock_expires_at,
                    delete_requested_by, delete_requested_at,
                    delete_approved_by,  delete_approved_at
                ) VALUES(?,?,?,?,?,
                         COALESCE(?, datetime('now')),
                         COALESCE(?, datetime('now')),
                         ?,?,
                         ?,?,?, ?,?, ?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    cover_image_url=excluded.cover_image_url,
                    updated_at=excluded.updated_at,
                    last_editor_user_id=excluded.last_editor_user_id,
                    last_edited_at=excluded.last_edited_at
                """,
                (
                    page.id,
                    page.title,
                    page.content,
                    page.cover_image_url,
                    page.created_by,
                    page.created_at,
                    stamp,
                    page.last_editor_user_id,
                    page.last_edited_at,
                    page.locked_by,
                    page.locked_at,
                    page.lock_expires_at,
                    page.delete_requested_by,
                    page.delete_requested_at,
                    page.delete_approved_by,
                    page.delete_approved_at,
                ),
            )
        else:
            n = await self._db.enqueue_rowcount(
                _SPACE_PAGE_UPSERT, _space_page_params(page, space_id)
            )
        return n > 0

    async def get(self, page_id: str) -> Page | None:
        row = await self._db.fetchone(
            "SELECT *, NULL AS space_id FROM pages WHERE id=?",
            (page_id,),
        )
        if row is not None:
            return _row_to_page(row_to_dict(row))
        row = await self._db.fetchone(
            "SELECT * FROM space_pages WHERE id=? AND deleted_at IS NULL",
            (page_id,),
        )
        return _row_to_page(row_to_dict(row))

    async def get_space_page(self, page_id: str, *, space_id: str) -> Page | None:
        """The page ``page_id`` of space ``space_id`` — never a household page.

        :meth:`get` looks in the household ``pages`` table first, so it can
        answer with a different row than the space page a caller means when
        the two tables share an id. Space-scoped callers use this.
        """
        row = await self._db.fetchone(
            "SELECT * FROM space_pages"
            " WHERE id=? AND space_id=? AND deleted_at IS NULL",
            (page_id, space_id),
        )
        return _row_to_page(row_to_dict(row))

    async def get_household_page(self, page_id: str) -> Page | None:
        """The household page ``page_id`` — never a space page.

        The household ``/api/pages/{id}`` routes use this so a space
        page id (from any space, member or not) is simply not found
        there (§24.11).
        """
        row = await self._db.fetchone(
            "SELECT *, NULL AS space_id FROM pages WHERE id=?",
            (page_id,),
        )
        return _row_to_page(row_to_dict(row))

    async def list(
        self,
        *,
        space_id: str | None = None,
    ) -> builtins.list[Page]:
        if space_id is None:
            rows = await self._db.fetchall(
                "SELECT *, NULL AS space_id FROM pages ORDER BY updated_at DESC",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM space_pages WHERE space_id=? AND deleted_at IS NULL"
                " ORDER BY updated_at DESC",
                (space_id,),
            )
        return [p for p in (_row_to_page(d) for d in rows_to_dicts(rows)) if p]

    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> builtins.list[Page]:
        """Pages updated after ``since`` (ISO-8601), oldest-first.

        Used by ``SpaceSyncResumeProvider`` to replay missed page edits
        on long-offline catch-up. ``updated_at`` (not ``created_at``) is
        the cursor so renamed/edited pages get re-emitted too.

        Shape trap: every write now stamps the tz-aware ISO shape, but a
        row last edited before that still holds SQLite's naive
        ``datetime('now')``. The only caller today passes the
        ``1970-01-01T00:00:00+00:00`` epoch cursor, whose year digits
        decide the comparison long before the separator does, so the raw
        ``>`` is safe. A real (non-epoch) ``since`` must wrap both sides
        in ``datetime()`` — "T" (0x54) sorts above " " (0x20) in a raw
        TEXT compare, so same-day rows would be skipped on resume.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM space_pages "
            "WHERE space_id=? AND deleted_at IS NULL AND updated_at > ? "
            "ORDER BY updated_at ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        return [p for p in (_row_to_page(d) for d in rows_to_dicts(rows)) if p]

    async def delete(
        self,
        page_id: str,
        *,
        space_id: str | None,
        deleted_by: str = "",
        confirmed: bool = True,
    ) -> bool:
        """Delete a page from the table its scope selects.

        The two tables are never both touched (§24.11): a
        ``SPACE_PAGE_DELETED`` gated on a space can only ever reach
        ``space_pages`` — naming a household page id leaves the
        household's personal page alone. ``space_id=None`` is the
        household path and likewise cannot reach a space page.

        A space page is **tombstoned**, not removed (migration 0073): the
        row keeps its id, creator and ``seq`` with ``deleted_at`` set and
        ``deleted_by`` naming who authorised the delete (empty when nobody
        can be named), so sync and resume can tell a household that missed
        it, and no stale copy can bring it back. Its title / content /
        cover are blanked, any draft dropped; the migration-0073 trigger
        drops its snapshots and history in this space. ``confirmed``: the
        space's host stands behind the delete (final); an unconfirmed
        tombstone may still yield to the host (:meth:`revive`).
        """
        if space_id is None:
            n = await self._db.enqueue_rowcount(
                "DELETE FROM pages WHERE id=?",
                (page_id,),
            )
            # Conflict snapshots carry no ON DELETE CASCADE FK, so drop
            # them explicitly — otherwise a deleted page's full-body
            # snapshots leak. Household snapshots carry a NULL space_id.
            await self._db.enqueue(
                "DELETE FROM space_page_snapshots WHERE page_id=? AND space_id IS NULL",
                (page_id,),
            )
            return n > 0
        n = await self._db.enqueue_rowcount(
            "UPDATE space_pages SET deleted_at=datetime('now'),"
            " deleted_by=NULLIF(?, ''), delete_confirmed=?, title='', content='',"
            " cover_image_url=NULL, pending_base_seq=NULL,"
            " locked_by=NULL, locked_at=NULL, lock_expires_at=NULL"
            " WHERE id=? AND space_id=? AND deleted_at IS NULL",
            (deleted_by, 1 if confirmed else 0, page_id, space_id),
        )
        return n > 0

    async def confirm_delete(self, page_id: str, *, space_id: str) -> bool:
        """The space's host stands behind an unconfirmed tombstone of
        ``space_id``: make it final. ``True`` when it changed."""
        n = await self._db.enqueue_rowcount(
            "UPDATE space_pages SET delete_confirmed=1"
            " WHERE id=? AND space_id=? AND deleted_at IS NOT NULL"
            " AND delete_confirmed=0",
            (page_id, space_id),
        )
        return n > 0

    async def revive(self, page_id: str, *, space_id: str, seq: int) -> bool:
        """Bring an **unconfirmed** tombstone of ``space_id`` back because
        the host still holds the page at ``seq`` — at or above the
        tombstone's own ``seq`` (the host refused, or never saw, the
        delete). The row comes back blank at ``seq`` 0, so the host's
        version the caller mirrors next applies over it. A confirmed
        tombstone, or an older host version, never revives. ``True`` when
        it did."""
        n = await self._db.enqueue_rowcount(
            "UPDATE space_pages SET deleted_at=NULL, deleted_by=NULL, seq=0"
            " WHERE id=? AND space_id=? AND deleted_at IS NOT NULL"
            " AND delete_confirmed=0 AND seq <= ?",
            (page_id, space_id, int(seq)),
        )
        return n > 0

    async def tombstone(
        self,
        page_id: str,
        *,
        space_id: str,
        created_by: str,
        deleted_by: str = "",
    ) -> bool:
        """Record a delete of a space page never held here: a content-free
        stub row, so a stale copy streamed or replayed later can't create
        it.

        Insert-only — an id already held (live or tombstoned, in any space)
        is never touched. ``False`` says nothing was written. The caller
        must have proven the id is this space's (owner-bound to
        ``created_by`` in ``space_id``): page ids are global, so a stub for
        another space's id would block that space's real page here.
        """
        n = await self._db.enqueue_rowcount(
            "INSERT INTO space_pages(id, space_id, title, content, created_by,"
            " deleted_at, deleted_by, delete_confirmed)"
            " VALUES(?, ?, '', '', ?, datetime('now'), NULLIF(?, ''), 1)"
            " ON CONFLICT(id) DO NOTHING",
            (page_id, space_id, created_by, deleted_by),
        )
        return n > 0

    async def is_page_deleted(self, page_id: str, *, space_id: str) -> bool:
        """Whether ``page_id`` is a tombstone of ``space_id`` here."""
        row = await self._db.fetchone(
            "SELECT 1 FROM space_pages"
            " WHERE id=? AND space_id=? AND deleted_at IS NOT NULL",
            (page_id, space_id),
        )
        return row is not None

    async def list_page_tombstones(
        self,
        space_id: str,
        *,
        since: str | None = None,
        limit: int = 500,
        before: tuple[str, str] | None = None,
    ) -> builtins.list[PageTombstone]:
        """The space's deleted pages, newest delete first (so a ``limit``
        keeps the deletes a peer is likeliest to have missed) — all of
        them, or those deleted at or after ``since`` (``deleted_at`` is
        naive UTC, ``since`` ISO 8601, so both go through ``datetime()``;
        second precision, hence ``>=``)."""
        sql = (
            "SELECT id, created_by, deleted_at, deleted_by FROM space_pages"
            " WHERE space_id=? AND deleted_at IS NOT NULL"
        )
        params: tuple = (space_id,)
        if since is not None:
            sql += " AND datetime(deleted_at) >= datetime(?)"
            params += (since,)
        if before is not None:
            # Keyset paging (§25.6 export): strictly after ``before`` =
            # (deleted_at, id) of the previous page's last row.
            sql += " AND (deleted_at < ? OR (deleted_at = ? AND id < ?))"
            params += (before[0], before[0], before[1])
        sql += " ORDER BY deleted_at DESC, id DESC LIMIT ?"
        rows = await self._db.fetchall(sql, (*params, int(limit)))
        return [
            PageTombstone(
                id=d["id"],
                deleted_at=d["deleted_at"],
                created_by=d["created_by"] or "",
                deleted_by=d["deleted_by"] or "",
            )
            for d in rows_to_dicts(rows)
        ]

    async def raise_seq(self, page_id: str, *, space_id: str, seq: int) -> bool:
        """Raise a live space page's ``seq`` to ``seq`` — never lower it,
        never touch its content. ``True`` when it moved."""
        n = await self._db.enqueue_rowcount(
            "UPDATE space_pages SET seq=?"
            " WHERE id=? AND space_id=? AND deleted_at IS NULL AND seq < ?",
            (int(seq), page_id, space_id, int(seq)),
        )
        return n > 0

    # ── Locks ──────────────────────────────────────────────────────────
    #
    # Edit locks and the two-step delete are a HOUSEHOLD-page surface:
    # their only callers are the ``/api/pages/{id}/…`` routes, which must
    # never reach a space page (§24.11). These methods therefore touch
    # the ``pages`` table only; a space page id is "not found" here.
    # (``release_expired_locks`` still sweeps both tables.)

    async def acquire_lock(
        self,
        page_id: str,
        editor: str,
        *,
        ttl: timedelta = LOCK_TTL,
    ) -> None:
        """Atomically claim the edit lock.

        Raises :class:`PageLockError` if another editor holds a lock that
        has not yet expired. Raises :class:`PageNotFoundError` if no row
        matches ``page_id``.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        expires = (datetime.now(timezone.utc) + ttl).isoformat()

        def _run(conn):
            row = conn.execute(
                "SELECT locked_by, lock_expires_at FROM pages WHERE id=?",
                (page_id,),
            ).fetchone()
            if row is None:
                raise PageNotFoundError(page_id)
            current_holder = row[0]
            expiry = row[1]
            # Another editor holds a still-valid lock → can't take it.
            if (
                current_holder is not None
                and current_holder != editor
                and (expiry is None or expiry > now_iso)
            ):
                raise PageLockError(f"page {page_id!r} is locked by {current_holder!r}")
            conn.execute(
                "UPDATE pages SET locked_by=?, locked_at=?, "
                "lock_expires_at=? WHERE id=?",
                (editor, now_iso, expires, page_id),
            )

        await self._db.transact(_run)

    async def refresh_lock(
        self,
        page_id: str,
        editor: str,
        *,
        ttl: timedelta = LOCK_TTL,
    ) -> None:
        """Extend a lock the caller already owns.

        Raises :class:`PageLockError` if the current holder is someone
        else (so the ``/lock/refresh`` route can return 409), or
        :class:`PageNotFoundError` if the page is gone.
        """
        expires = (datetime.now(timezone.utc) + ttl).isoformat()

        def _run(conn):
            row = conn.execute(
                "SELECT locked_by FROM pages WHERE id=?",
                (page_id,),
            ).fetchone()
            if row is None:
                return ("missing", None)
            current_holder = row[0]
            if current_holder is not None and current_holder != editor:
                return ("held", current_holder)
            conn.execute(
                "UPDATE pages SET locked_by=?, "
                "locked_at=COALESCE(locked_at, datetime('now')), "
                "lock_expires_at=? WHERE id=?",
                (editor, expires, page_id),
            )
            return ("ok", None)

        status, holder = await self._db.transact(_run)
        if status == "held":
            raise PageLockError(
                f"page {page_id!r} is locked by {holder!r}",
            )
        if status == "missing":
            raise PageNotFoundError(page_id)

    async def get_lock(self, page_id: str) -> dict | None:
        """Return current lock row ``{locked_by, locked_at,
        lock_expires_at}`` or ``None`` if the page is unlocked /
        missing / the lock has already expired.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        row = await self._db.fetchone(
            "SELECT locked_by, locked_at, lock_expires_at FROM pages WHERE id=?",
            (page_id,),
        )
        if row is None:
            return None
        locked_by = row["locked_by"]
        expires = row["lock_expires_at"]
        if not locked_by or (expires is not None and expires < now_iso):
            return None
        return {
            "locked_by": locked_by,
            "locked_at": row["locked_at"],
            "lock_expires_at": expires,
        }

    async def release_lock(self, page_id: str, editor: str) -> None:
        """Drop a lock. Must match the owning editor to avoid cross-wipes."""
        await self._db.enqueue(
            "UPDATE pages SET locked_by=NULL, locked_at=NULL, "
            "lock_expires_at=NULL WHERE id=? AND locked_by=?",
            (page_id, editor),
        )

    async def release_expired_locks(self) -> int:
        """Free any lock whose ``lock_expires_at`` is in the past.

        Returns the count released. Scheduled periodically by
        ``PageLockExpiryScheduler``.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        count = 0
        for table in ("pages", "space_pages"):
            n = await self._db.fetchval(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE locked_by IS NOT NULL AND lock_expires_at < ?",
                (now_iso,),
                default=0,
            )
            count += int(n or 0)
            await self._db.enqueue(
                f"UPDATE {table} SET locked_by=NULL, locked_at=NULL, "
                f"lock_expires_at=NULL WHERE lock_expires_at < ?",
                (now_iso,),
            )
        return count

    # ── Two-step delete ────────────────────────────────────────────────

    async def request_delete(self, page_id: str, user_id: str) -> None:
        await self._db.enqueue(
            "UPDATE pages SET delete_requested_by=?, "
            "delete_requested_at=datetime('now') WHERE id=?",
            (user_id, page_id),
        )

    async def approve_delete(self, page_id: str, approver: str) -> None:
        """Record approval; the actual row delete is a second step.

        Callers check :meth:`get` afterwards — if both
        ``delete_requested_by`` and ``delete_approved_by`` are set, they
        issue :meth:`delete`.
        """
        await self._db.enqueue(
            "UPDATE pages SET delete_approved_by=?, "
            "delete_approved_at=datetime('now') WHERE id=?",
            (approver, page_id),
        )

    async def clear_delete_request(self, page_id: str) -> None:
        await self._db.enqueue(
            "UPDATE pages SET "
            "delete_requested_by=NULL, delete_requested_at=NULL, "
            "delete_approved_by=NULL,  delete_approved_at=NULL "
            "WHERE id=?",
            (page_id,),
        )

    # ── Versions ───────────────────────────────────────────────────────

    async def save_version(self, version: PageVersion) -> PageVersion:
        await self._db.enqueue(
            """
            INSERT INTO page_edit_history(
                id, page_id, space_id, title, content, cover_image_url,
                edited_by, edited_at, version
            ) VALUES(?,?,?,?,?,?,?, COALESCE(?, datetime('now')), ?)
            """,
            (
                version.id,
                version.page_id,
                version.space_id,
                version.title,
                version.content,
                version.cover_image_url,
                version.edited_by,
                version.edited_at,
                int(version.version),
            ),
        )
        # Prune old history rows — keep the latest ``MAX_HISTORY`` per
        # page. A fresh INSERT may not yet be flushed to disk when this
        # DELETE runs, but both statements hit the same async-write
        # batch so order is preserved. The DELETE filters on
        # ``version`` descending, so it never touches the row we just
        # inserted unless we actually overflow the cap.
        # A space page keeps more (its history is its v_48 ancestry), and
        # only rows of the same scope count against — or are pruned by —
        # the cap (``IS`` matches NULL to NULL for a household page).
        cap = MAX_HISTORY if version.space_id is None else MAX_SPACE_HISTORY
        await self._db.enqueue(
            """
            DELETE FROM page_edit_history
             WHERE page_id=? AND space_id IS ?
               AND version NOT IN (
                   SELECT version FROM page_edit_history
                    WHERE page_id=? AND space_id IS ?
                    ORDER BY version DESC
                    LIMIT ?
               )
            """,
            (
                version.page_id,
                version.space_id,
                version.page_id,
                version.space_id,
                cap,
            ),
        )
        return version

    async def list_versions(
        self, page_id: str, *, space_id: str | None
    ) -> builtins.list[PageVersion]:
        """Edit history of ``page_id`` in one scope (§24.11).

        ``space_id=None`` is the household page's history; a space id
        only that space's snapshots — a history row recorded under any
        other scope (another space, or the household, sharing the page
        id) is never returned. ``IS`` matches NULL to NULL.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM page_edit_history WHERE page_id=? AND space_id IS ?"
            " ORDER BY version",
            (page_id, space_id),
        )
        return [_row_to_version(d) for d in rows_to_dicts(rows)]

    async def next_version_number(self, page_id: str) -> int:
        """Pick the next version number atomically.

        Using ``MAX(version) + 1`` inside ``transact`` guarantees no two
        concurrent editors end up with the same version row.
        """

        def _run(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 "
                "FROM page_edit_history WHERE page_id=?",
                (page_id,),
            ).fetchone()
            return int(row[0])

        return await self._db.transact(_run)

    # ── Snapshots (§4.4.4.1 conflict resolution) ───────────────────────

    async def insert_snapshot(
        self,
        *,
        page_id: str,
        space_id: str | None,
        body: str,
        author_user_id: str,
        side: str,
        conflict: bool,
        title: str | None = None,
    ) -> None:
        """Store one version of a page as a snapshot row.

        ``title`` given: the row's ``body`` holds the whole version as
        canonical JSON ``{"content", "title"}`` (an open conflict side,
        v_48), so a side can be restored with its own title. Without it,
        ``body`` is the bare content.
        """
        if title is not None:
            body = encode_side_body(title, body)
        # microsecond-precision timestamp avoids primary-key collisions
        # under rapid concurrent inserts.
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        await self._db.enqueue(
            """
            INSERT INTO space_page_snapshots(
                page_id, space_id, snapshot_at, body,
                snapshot_by, side, conflict
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                page_id,
                space_id,
                now,
                body,
                author_user_id,
                side,
                1 if conflict else 0,
            ),
        )
        # Prune the RESOLVED snapshots of this page in this scope to the
        # newest ``MAX_PAGE_SNAPSHOTS``. Never an open side (``conflict=1``
        # — pruning one would silently drop a version nobody resolved), and
        # never another scope's rows sharing the page id. Both statements
        # share the same async-write batch so the INSERT lands first.
        await self._db.enqueue(
            """
            DELETE FROM space_page_snapshots
             WHERE page_id=? AND space_id IS ? AND conflict=0 AND side!='base'
               AND rowid NOT IN (
                   SELECT rowid FROM space_page_snapshots
                    WHERE page_id=? AND space_id IS ? AND conflict=0
                      AND side!='base'
                    ORDER BY snapshot_at DESC
                    LIMIT ?
               )
            """,
            (page_id, space_id, page_id, space_id, MAX_PAGE_SNAPSHOTS),
        )

    async def has_active_conflict(self, page_id: str, *, space_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM space_page_snapshots"
            " WHERE page_id=? AND space_id=? AND conflict=1 LIMIT 1",
            (page_id, space_id),
        )
        return row is not None

    async def list_conflict_sides(
        self, page_id: str, *, space_id: str
    ) -> builtins.list[PageConflictSide]:
        """The open conflict sides of a space page, oldest first. A legacy
        row (bare content) reads with the page's current title."""
        rows = await self._db.fetchall(
            "SELECT s.body, s.snapshot_by, s.snapshot_at, p.title AS page_title"
            " FROM space_page_snapshots s"
            " LEFT JOIN space_pages p ON p.id = s.page_id AND p.space_id = s.space_id"
            " WHERE s.page_id=? AND s.space_id=? AND s.conflict=1"
            " ORDER BY s.snapshot_at ASC",
            (page_id, space_id),
        )
        out: builtins.list[PageConflictSide] = []
        for d in rows_to_dicts(rows):
            v = decode_side_body(
                str(d["body"]), fallback_title=str(d.get("page_title") or "")
            )
            out.append(
                PageConflictSide(
                    hash=version_hash(v.title, v.content, v.cover_image_url),
                    title=v.title,
                    content=v.content,
                    cover_image_url=v.cover_image_url,
                    base_seq=v.seq,
                    by=str(d["snapshot_by"]),
                    at=str(d["snapshot_at"]),
                )
            )
        return out

    async def set_conflict_sides(
        self,
        page_id: str,
        *,
        space_id: str,
        sides: Sequence[PageConflictSide],
    ) -> None:
        """Make ``sides`` the page's open conflict — exactly them, with
        their own ``at`` (the host's stamps, so every household holds the
        same set). Atomic."""
        rows = [
            (
                page_id,
                space_id,
                side.at,
                encode_side_body(
                    side.title,
                    side.content,
                    cover_image_url=side.cover_image_url,
                    seq=side.base_seq,
                ),
                side.by,
            )
            for side in sides
        ]

        def _run(conn):
            conn.execute(
                "DELETE FROM space_page_snapshots"
                " WHERE page_id=? AND space_id=? AND conflict=1",
                (page_id, space_id),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO space_page_snapshots("
                " page_id, space_id, snapshot_at, body, snapshot_by, side, conflict"
                ") VALUES(?,?,?,?,?,'theirs',1)",
                rows,
            )

        await self._db.transact(_run)

    async def commit_version(
        self,
        page: Page,
        *,
        space_id: str,
        history: Sequence[PageVersion],
        sides: Sequence[PageConflictSide],
    ) -> bool:
        """One sequenced step of a space page, atomically: the ``history``
        rows (numbered here, then pruned to :data:`MAX_SPACE_HISTORY`), the
        page row, and exactly ``sides`` as its open conflict. ``False``
        (nothing written) when the page id belongs to another space."""
        side_rows = [
            (
                page.id,
                space_id,
                side.at,
                encode_side_body(
                    side.title,
                    side.content,
                    cover_image_url=side.cover_image_url,
                    seq=side.base_seq,
                ),
                side.by,
            )
            for side in sides
        ]

        def _run(conn) -> bool:
            for v in history:
                (number,) = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM page_edit_history"
                    " WHERE page_id=?",
                    (page.id,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO page_edit_history(id, page_id, space_id, title,"
                    " content, cover_image_url, edited_by, edited_at, version)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        v.id,
                        page.id,
                        space_id,
                        v.title,
                        v.content,
                        v.cover_image_url,
                        v.edited_by,
                        v.edited_at,
                        int(number),
                    ),
                )
            if history:
                conn.execute(
                    "DELETE FROM page_edit_history WHERE page_id=? AND space_id IS ?"
                    " AND version NOT IN (SELECT version FROM page_edit_history"
                    " WHERE page_id=? AND space_id IS ? ORDER BY version DESC"
                    " LIMIT ?)",
                    (page.id, space_id, page.id, space_id, MAX_SPACE_HISTORY),
                )
            cur = conn.execute(_SPACE_PAGE_UPSERT, _space_page_params(page, space_id))
            if cur.rowcount == 0:
                raise _CrossSpaceWrite
            conn.execute(
                "DELETE FROM space_page_snapshots"
                " WHERE page_id=? AND space_id=? AND conflict=1",
                (page.id, space_id),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO space_page_snapshots("
                " page_id, space_id, snapshot_at, body, snapshot_by, side, conflict"
                ") VALUES(?,?,?,?,?,'theirs',1)",
                side_rows,
            )
            return True

        try:
            return await self._db.transact(_run)
        except _CrossSpaceWrite:
            return False

    async def space_pages_in_conflict(self, space_id: str) -> set[str]:
        """Ids of this space's pages with an open conflict (the list badge)."""
        rows = await self._db.fetchall(
            "SELECT DISTINCT page_id FROM space_page_snapshots"
            " WHERE space_id=? AND conflict=1",
            (space_id,),
        )
        return {str(r["page_id"]) for r in rows}

    async def clear_conflict_flag(self, page_id: str, *, space_id: str) -> None:
        """Drop every open side of the page (their bodies live on in the
        history, written by the sequencer before it clears them)."""
        await self._db.enqueue(
            "DELETE FROM space_page_snapshots"
            " WHERE page_id=? AND space_id=? AND conflict=1",
            (page_id, space_id),
        )

    # ── A member's draft base (v_48) ────────────────────────────────────

    async def set_draft_base(
        self, page_id: str, *, space_id: str, base: DraftBase
    ) -> None:
        """Remember the canonical version a local draft was made from (one
        row per page, ``side='base'``) — sent as the proposal's base."""
        body = encode_side_body(
            base.title,
            base.content,
            cover_image_url=base.cover_image_url,
            seq=base.seq,
            resolves=base.resolves,
            sent=base.sent,
        )

        def _run(conn):
            conn.execute(
                "DELETE FROM space_page_snapshots"
                " WHERE page_id=? AND space_id=? AND side='base'",
                (page_id, space_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO space_page_snapshots("
                " page_id, space_id, snapshot_at, body, snapshot_by, side, conflict"
                ") VALUES(?,?,?,?,?,'base',0)",
                (page_id, space_id, f"base:{page_id}", body, base.by),
            )

        await self._db.transact(_run)

    async def get_draft_base(self, page_id: str, *, space_id: str) -> DraftBase | None:
        row = await self._db.fetchone(
            "SELECT body, snapshot_by FROM space_page_snapshots"
            " WHERE page_id=? AND space_id=? AND side='base' LIMIT 1",
            (page_id, space_id),
        )
        if row is None:
            return None
        v = decode_side_body(str(row["body"]), fallback_title="")
        return DraftBase(
            title=v.title,
            content=v.content,
            cover_image_url=v.cover_image_url,
            seq=v.seq,
            by=str(row["snapshot_by"]),
            resolves=v.resolves,
            sent=v.sent,
        )

    async def clear_draft_base(self, page_id: str, *, space_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_page_snapshots"
            " WHERE page_id=? AND space_id=? AND side='base'",
            (page_id, space_id),
        )

    async def list_pending_drafts(
        self, *, space_id: str | None = None
    ) -> builtins.list[tuple[str, str]]:
        """``(space_id, page_id)`` of every page holding a local draft."""
        if space_id is None:
            rows = await self._db.fetchall(
                "SELECT space_id, id FROM space_pages"
                " WHERE pending_base_seq IS NOT NULL AND deleted_at IS NULL"
                " ORDER BY updated_at",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT space_id, id FROM space_pages"
                " WHERE space_id=? AND pending_base_seq IS NOT NULL"
                " AND deleted_at IS NULL ORDER BY updated_at",
                (space_id,),
            )
        return [(str(r["space_id"]), str(r["id"])) for r in rows]


@dataclass(slots=True, frozen=True)
class _StoredVersion:
    """Repo-local DTO: a decoded snapshot ``body``."""

    title: str
    content: str
    cover_image_url: str | None = None
    seq: int = 0
    resolves: tuple[str, ...] = ()
    sent: str | None = None


def encode_side_body(
    title: str,
    content: str,
    *,
    cover_image_url: str | None = None,
    seq: int = 0,
    resolves: Sequence[str] = (),
    sent: str | None = None,
) -> str:
    """A whole version as a snapshot ``body`` — canonical JSON."""
    data: dict = {"content": content, "title": title}
    if cover_image_url:
        data["cover"] = cover_image_url
    if seq:
        data["seq"] = int(seq)
    if resolves:
        data["resolves"] = list(resolves)
    if sent:
        data["sent"] = sent
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode_side_body(body: str, *, fallback_title: str) -> _StoredVersion:
    """A snapshot ``body``: the JSON a v_48 row is stored as, else a legacy
    bare-content row under ``fallback_title``."""
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if (
            isinstance(data, dict)
            and {"content", "title"} <= set(data)
            and set(data) <= {"content", "title", "cover", "seq", "resolves", "sent"}
            and isinstance(data["content"], str)
            and isinstance(data["title"], str)
        ):
            cover = data.get("cover")
            resolves = data.get("resolves") or []
            sent = data.get("sent")
            return _StoredVersion(
                title=data["title"],
                content=data["content"],
                cover_image_url=cover if isinstance(cover, str) else None,
                seq=int(data.get("seq") or 0),
                resolves=tuple(str(r) for r in resolves if isinstance(r, str)),
                sent=sent if isinstance(sent, str) else None,
            )
    return _StoredVersion(title=fallback_title, content=body)


#: Upsert of a space page — ``space_id`` decides which rows it may update
#: (an id of another space is refused by the WHERE, and so is a tombstone:
#: a deleted page never comes back, migration 0073).
_SPACE_PAGE_UPSERT = """
INSERT INTO space_pages(
    id, space_id, title, content, cover_image_url, created_by,
    created_at, updated_at,
    last_editor_user_id, last_edited_at,
    locked_by, locked_at, lock_expires_at,
    delete_requested_by, delete_requested_at,
    delete_approved_by,  delete_approved_at,
    seq, pending_base_seq
) VALUES(?,?,?,?,?,?,
         COALESCE(?, datetime('now')),
         COALESCE(?, datetime('now')),
         ?,?,
         ?,?,?, ?,?, ?,?, ?,?)
ON CONFLICT(id) DO UPDATE SET
    title=excluded.title,
    content=excluded.content,
    cover_image_url=excluded.cover_image_url,
    updated_at=excluded.updated_at,
    last_editor_user_id=excluded.last_editor_user_id,
    last_edited_at=excluded.last_edited_at,
    seq=excluded.seq,
    pending_base_seq=excluded.pending_base_seq
WHERE space_pages.space_id = excluded.space_id
  AND space_pages.deleted_at IS NULL
"""


def _space_page_params(page: Page, space_id: str) -> tuple:
    stamp = page.updated_at or datetime.now(timezone.utc).isoformat()
    return (
        page.id,
        space_id,
        page.title,
        page.content,
        page.cover_image_url,
        page.created_by,
        page.created_at,
        stamp,
        page.last_editor_user_id,
        page.last_edited_at,
        page.locked_by,
        page.locked_at,
        page.lock_expires_at,
        page.delete_requested_by,
        page.delete_requested_at,
        page.delete_approved_by,
        page.delete_approved_at,
        int(page.seq),
        page.pending_base_seq,
    )


# ─── Row → domain ─────────────────────────────────────────────────────────


def _row_to_page(row: dict | None) -> Page | None:
    if row is None:
        return None
    return Page(
        id=row["id"],
        title=row["title"],
        content=row.get("content") or "",
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        space_id=row.get("space_id"),
        cover_image_url=row.get("cover_image_url"),
        last_editor_user_id=row.get("last_editor_user_id"),
        last_edited_at=row.get("last_edited_at"),
        locked_by=row.get("locked_by"),
        locked_at=row.get("locked_at"),
        lock_expires_at=row.get("lock_expires_at"),
        delete_requested_by=row.get("delete_requested_by"),
        delete_requested_at=row.get("delete_requested_at"),
        delete_approved_by=row.get("delete_approved_by"),
        delete_approved_at=row.get("delete_approved_at"),
        seq=int(row.get("seq") or 0),
        pending_base_seq=(
            int(row["pending_base_seq"])
            if row.get("pending_base_seq") is not None
            else None
        ),
    )


def _row_to_version(row: dict) -> PageVersion:
    return PageVersion(
        id=row["id"],
        page_id=row["page_id"],
        version=int(row["version"]),
        title=row["title"],
        content=row.get("content") or "",
        edited_by=row["edited_by"],
        edited_at=row["edited_at"],
        space_id=row.get("space_id"),
        cover_image_url=row.get("cover_image_url"),
    )


def mint_page_id(*, space_id: str | None, created_by: str) -> str:
    """A page id. A space page federates, so its id is owner-bound (v_36):
    only the creator's household can announce it. A household page never
    leaves the household."""
    if space_id is None:
        return uuid.uuid4().hex
    return mint_owner_bound_id(
        SPACE_PAGE_KIND, space_id=space_id, owner_user_id=created_by
    )


def new_page(
    *,
    title: str,
    content: str,
    created_by: str,
    space_id: str | None = None,
    cover_image_url: str | None = None,
    page_id: str | None = None,
) -> Page:
    """A fresh page row. ``page_id`` is the id a moderation-queue item
    minted at submit time (owner-bound to the same creator)."""
    now = datetime.now(timezone.utc).isoformat()
    return Page(
        id=page_id or mint_page_id(space_id=space_id, created_by=created_by),
        title=title,
        content=content,
        created_by=created_by,
        created_at=now,
        updated_at=now,
        space_id=space_id,
        cover_image_url=cover_image_url,
    )
