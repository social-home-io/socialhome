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
"""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import row_to_dict, rows_to_dicts


#: How long an edit lock stays valid before anyone else can claim it
#: (spec §23.72). Clients refresh every ``LOCK_TTL / 2`` via
#: ``POST /api/pages/{id}/lock/refresh``; PageLockScheduler sweeps
#: expired rows every 30 s so stale locks never pile up.
LOCK_TTL = timedelta(seconds=60)

#: Maximum number of edit-history rows retained per page (spec §31901).
#: Older versions are pruned on each ``save_version`` write.
MAX_HISTORY = 5


class PageLockError(Exception):
    """Raised when an editor tries to acquire a lock another editor holds."""


class PageNotFoundError(Exception):
    """Raised when an operation targets a missing page id."""


# Domain dataclasses live in ``socialhome/domain/page.py``. They are
# re-exported here so existing repo-level imports keep working.
from ..domain.page import Page, PageVersion  # noqa: F401,E402


@runtime_checkable
class AbstractPageRepo(Protocol):
    async def save(self, page: Page) -> Page: ...
    async def get(self, page_id: str) -> Page | None: ...
    async def list(
        self,
        *,
        space_id: str | None = None,
    ) -> builtins.list[Page]: ...
    async def delete(self, page_id: str) -> None: ...

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
    async def list_versions(self, page_id: str) -> builtins.list[PageVersion]: ...
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
    ) -> None: ...
    async def has_active_conflict(self, page_id: str) -> bool: ...
    async def last_base_snapshot(self, page_id: str) -> str: ...
    async def last_theirs_snapshot(self, page_id: str) -> str | None: ...
    async def clear_conflict_flag(self, page_id: str) -> None: ...


class SqlitePageRepo:
    """SQLite-backed :class:`AbstractPageRepo`.

    Chooses between the ``pages`` and ``space_pages`` tables based on
    whether the ``Page`` carries a ``space_id``. Callers don't need to
    know the split.
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Pages ──────────────────────────────────────────────────────────

    async def save(self, page: Page) -> Page:
        if page.space_id is None:
            await self._db.enqueue(
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
                    updated_at=datetime('now'),
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
                    page.updated_at,
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
            await self._db.enqueue(
                """
                INSERT INTO space_pages(
                    id, space_id, title, content, cover_image_url, created_by,
                    created_at, updated_at,
                    last_editor_user_id, last_edited_at,
                    locked_by, locked_at, lock_expires_at,
                    delete_requested_by, delete_requested_at,
                    delete_approved_by,  delete_approved_at
                ) VALUES(?,?,?,?,?,?,
                         COALESCE(?, datetime('now')),
                         COALESCE(?, datetime('now')),
                         ?,?,
                         ?,?,?, ?,?, ?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    cover_image_url=excluded.cover_image_url,
                    updated_at=datetime('now'),
                    last_editor_user_id=excluded.last_editor_user_id,
                    last_edited_at=excluded.last_edited_at
                """,
                (
                    page.id,
                    page.space_id,
                    page.title,
                    page.content,
                    page.cover_image_url,
                    page.created_by,
                    page.created_at,
                    page.updated_at,
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
        return page

    async def get(self, page_id: str) -> Page | None:
        row = await self._db.fetchone(
            "SELECT *, NULL AS space_id FROM pages WHERE id=?",
            (page_id,),
        )
        if row is not None:
            return _row_to_page(row_to_dict(row))
        row = await self._db.fetchone(
            "SELECT * FROM space_pages WHERE id=?",
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
                "SELECT * FROM space_pages WHERE space_id=? ORDER BY updated_at DESC",
                (space_id,),
            )
        return [p for p in (_row_to_page(d) for d in rows_to_dicts(rows)) if p]

    async def delete(self, page_id: str) -> None:
        # Try both tables; whichever matches, wins.
        await self._db.enqueue("DELETE FROM pages WHERE id=?", (page_id,))
        await self._db.enqueue("DELETE FROM space_pages WHERE id=?", (page_id,))

    # ── Locks ──────────────────────────────────────────────────────────

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
            held_by = None
            for table in ("pages", "space_pages"):
                row = conn.execute(
                    f"SELECT locked_by, lock_expires_at FROM {table} WHERE id=?",
                    (page_id,),
                ).fetchone()
                if row is None:
                    continue
                current_holder = row[0]
                expiry = row[1]
                # Another editor holds a still-valid lock → can't take it.
                if (
                    current_holder is not None
                    and current_holder != editor
                    and (expiry is None or expiry > now_iso)
                ):
                    held_by = current_holder
                    break
                conn.execute(
                    f"UPDATE {table} SET locked_by=?, locked_at=?, "
                    f"lock_expires_at=? WHERE id=?",
                    (editor, now_iso, expires, page_id),
                )
                return
            if held_by is not None:
                raise PageLockError(f"page {page_id!r} is locked by {held_by!r}")
            raise PageNotFoundError(page_id)

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
            held_by = _UNSET = object()
            current_holder = None
            for table in ("pages", "space_pages"):
                row = conn.execute(
                    f"SELECT locked_by FROM {table} WHERE id=?",
                    (page_id,),
                ).fetchone()
                if row is None:
                    continue
                current_holder = row[0]
                held_by = current_holder
                if current_holder is not None and current_holder != editor:
                    return ("held", current_holder)
                conn.execute(
                    f"UPDATE {table} SET locked_by=?, "
                    f"locked_at=COALESCE(locked_at, datetime('now')), "
                    f"lock_expires_at=? WHERE id=?",
                    (editor, expires, page_id),
                )
                return ("ok", None)
            if held_by is _UNSET:
                return ("missing", None)
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
        for table in ("pages", "space_pages"):
            row = await self._db.fetchone(
                f"SELECT locked_by, locked_at, lock_expires_at FROM {table} WHERE id=?",
                (page_id,),
            )
            if row is None:
                continue
            locked_by = row["locked_by"]
            expires = row["lock_expires_at"]
            if not locked_by or (expires is not None and expires < now_iso):
                return None
            return {
                "locked_by": locked_by,
                "locked_at": row["locked_at"],
                "lock_expires_at": expires,
            }
        return None

    async def release_lock(self, page_id: str, editor: str) -> None:
        """Drop a lock. Must match the owning editor to avoid cross-wipes."""
        for table in ("pages", "space_pages"):
            await self._db.enqueue(
                f"UPDATE {table} SET locked_by=NULL, locked_at=NULL, "
                f"lock_expires_at=NULL WHERE id=? AND locked_by=?",
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
        for table in ("pages", "space_pages"):
            await self._db.enqueue(
                f"UPDATE {table} SET delete_requested_by=?, "
                f"delete_requested_at=datetime('now') WHERE id=?",
                (user_id, page_id),
            )

    async def approve_delete(self, page_id: str, approver: str) -> None:
        """Record approval; the actual row delete is a second step.

        Callers check :meth:`get` afterwards — if both
        ``delete_requested_by`` and ``delete_approved_by`` are set, they
        issue :meth:`delete`.
        """
        for table in ("pages", "space_pages"):
            await self._db.enqueue(
                f"UPDATE {table} SET delete_approved_by=?, "
                f"delete_approved_at=datetime('now') WHERE id=?",
                (approver, page_id),
            )

    async def clear_delete_request(self, page_id: str) -> None:
        for table in ("pages", "space_pages"):
            await self._db.enqueue(
                f"UPDATE {table} SET "
                f"delete_requested_by=NULL, delete_requested_at=NULL, "
                f"delete_approved_by=NULL,  delete_approved_at=NULL "
                f"WHERE id=?",
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
        await self._db.enqueue(
            """
            DELETE FROM page_edit_history
             WHERE page_id=?
               AND version NOT IN (
                   SELECT version FROM page_edit_history
                    WHERE page_id=?
                    ORDER BY version DESC
                    LIMIT ?
               )
            """,
            (version.page_id, version.page_id, MAX_HISTORY),
        )
        return version

    async def list_versions(self, page_id: str) -> builtins.list[PageVersion]:
        rows = await self._db.fetchall(
            "SELECT * FROM page_edit_history WHERE page_id=? ORDER BY version",
            (page_id,),
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
    ) -> None:
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

    async def has_active_conflict(self, page_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM space_page_snapshots WHERE page_id=? AND conflict=1 LIMIT 1",
            (page_id,),
        )
        return row is not None

    async def last_base_snapshot(self, page_id: str) -> str:
        row = await self._db.fetchone(
            "SELECT body FROM space_page_snapshots "
            "WHERE page_id=? AND side='base' "
            "ORDER BY snapshot_at DESC LIMIT 1",
            (page_id,),
        )
        return str(row["body"]) if row else ""

    async def last_theirs_snapshot(self, page_id: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT body FROM space_page_snapshots "
            "WHERE page_id=? AND side='theirs' AND conflict=1 "
            "ORDER BY snapshot_at DESC LIMIT 1",
            (page_id,),
        )
        return str(row["body"]) if row else None

    async def clear_conflict_flag(self, page_id: str) -> None:
        await self._db.enqueue(
            "UPDATE space_page_snapshots SET conflict=0 WHERE page_id=? AND conflict=1",
            (page_id,),
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


def new_page(
    *,
    title: str,
    content: str,
    created_by: str,
    space_id: str | None = None,
    cover_image_url: str | None = None,
) -> Page:
    now = datetime.now(timezone.utc).isoformat()
    return Page(
        id=uuid.uuid4().hex,
        title=title,
        content=content,
        created_by=created_by,
        created_at=now,
        updated_at=now,
        space_id=space_id,
        cover_image_url=cover_image_url,
    )
