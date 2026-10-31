"""App-registry repo — the ``installed_apps`` table.

Wraps the SQL surface used by :class:`AppService` so the service depends
only on the abstract protocol. Mirrors the preferences-repo pattern.

Unlike preferences (which f-strings *column names* against an allow-list),
every value here — including the manifest JSON and the app id — is a
bound ``?`` parameter, so there is no injection surface to allow-list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase
from ..domain.apps import (
    ACTIVE_SESSION_TTL_SECONDS,
    PENDING_SESSION_TTL_SECONDS,
    SESSION_ACTIVE,
    SESSION_INVITED,
    AppKvEntry,
    AppManifest,
    AppPendingSession,
    InstalledApp,
)

_PENDING_SESSION_TTL_SECONDS = PENDING_SESSION_TTL_SECONDS
_ACTIVE_SESSION_TTL_SECONDS = ACTIVE_SESSION_TTL_SECONDS
_PENDING_SESSION_MAX_PER_PAIR = 50


@runtime_checkable
class AbstractAppRepo(Protocol):
    async def list_installed(self) -> list[InstalledApp]: ...
    async def get(self, app_id: str) -> InstalledApp | None: ...
    async def install(self, app: InstalledApp) -> None: ...
    async def update_installed(self, app: InstalledApp) -> None: ...
    async def set_enabled(self, app_id: str, *, enabled: bool) -> None: ...
    async def set_min_age(self, app_id: str, min_age: int) -> None: ...
    async def uninstall(self, app_id: str) -> None: ...
    async def kv_get(
        self, app_id: str, user_id: str, key: str
    ) -> AppKvEntry | None: ...
    async def kv_list(self, app_id: str, user_id: str) -> list[AppKvEntry]: ...
    async def kv_set(
        self,
        app_id: str,
        user_id: str,
        key: str,
        value_json: str,
        updated_at: str,
    ) -> None: ...
    async def kv_delete(self, app_id: str, user_id: str, key: str) -> None: ...
    async def kv_count(self, app_id: str, user_id: str) -> int: ...
    async def kv_people(self, app_id: str) -> int: ...
    async def add_pending_session(self, s: AppPendingSession) -> None: ...
    async def prune_pending_sessions(
        self,
        *,
        max_age_seconds: int = _PENDING_SESSION_TTL_SECONDS,
        active_max_age_seconds: int = _ACTIVE_SESSION_TTL_SECONDS,
    ) -> int: ...

    async def get_pending_session(
        self, app_id: str, user_id: str, session_id: str
    ) -> AppPendingSession | None: ...
    async def take_pending_session(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_instance: str | None = None,
        from_user: str | None = None,
    ) -> AppPendingSession | None: ...
    async def activate_pending_session(
        self, app_id: str, user_id: str, session_id: str, *, now: str
    ) -> AppPendingSession | None: ...
    async def delete_pending_session(
        self, app_id: str, user_id: str, session_id: str
    ) -> None: ...
    async def add_active_session(self, s: AppPendingSession) -> None: ...
    async def touch_session(
        self, app_id: str, user_id: str, session_id: str, *, now: str
    ) -> None: ...
    async def hold_message(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_instance: str,
        from_user: str,
        held: dict,
        now: str,
    ) -> bool: ...
    async def take_held(self, app_id: str, user_id: str) -> list[AppPendingSession]: ...
    async def list_sessions(
        self, app_id: str, user_id: str
    ) -> list[AppPendingSession]: ...
    async def withdraw_session(
        self, app_id: str, user_id: str, session_id: str
    ) -> None: ...
    async def list_app_sessions(self, app_id: str) -> list[AppPendingSession]: ...
    async def list_session_rows(
        self, app_id: str, session_id: str
    ) -> list[AppPendingSession]: ...


def _row_to_kv(row) -> AppKvEntry:
    return AppKvEntry(
        app_id=str(row["app_id"]),
        user_id=str(row["user_id"]),
        key=str(row["key"]),
        value_json=str(row["value_json"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_pending(row) -> AppPendingSession:
    return AppPendingSession(
        app_id=str(row["app_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        from_instance=str(row["from_instance"]),
        from_user=row["from_user"],
        payload=orjson.loads(row["payload_json"]),
        created_at=str(row["created_at"]),
        state=str(row["state"]),
        held=orjson.loads(row["held_json"]) if row["held_json"] else None,
        updated_at=row["updated_at"],
    )


def _row_to_app(row) -> InstalledApp:
    # min_age column added in migration 0022; default to 0 if absent (safety).
    try:
        min_age = int(row["min_age"] or 0)
    except KeyError, TypeError, ValueError:
        min_age = 0
    return InstalledApp(
        app_id=str(row["app_id"]),
        name=str(row["name"]),
        version=str(row["version"]),
        enabled=bool(row["enabled"]),
        manifest=AppManifest.from_dict(orjson.loads(row["manifest_json"])),
        bundle_path=str(row["bundle_path"]),
        bundle_sha256=str(row["bundle_sha256"]),
        source_url=str(row["source_url"]),
        installed_by=row["installed_by"],
        installed_at=str(row["installed_at"]),
        min_age=min_age,
    )


class SqliteAppRepo:
    """SQLite-backed :class:`AbstractAppRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def list_installed(self) -> list[InstalledApp]:
        rows = await self._db.fetchall(
            "SELECT * FROM installed_apps ORDER BY name COLLATE NOCASE",
        )
        return [_row_to_app(r) for r in rows]

    async def get(self, app_id: str) -> InstalledApp | None:
        row = await self._db.fetchone(
            "SELECT * FROM installed_apps WHERE app_id = ?",
            (app_id,),
        )
        return _row_to_app(row) if row is not None else None

    async def install(self, app: InstalledApp) -> None:
        await self._db.enqueue(
            """INSERT INTO installed_apps
                 (app_id, name, version, enabled, manifest_json, bundle_path,
                  bundle_sha256, source_url, installed_by, installed_at, min_age)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                app.app_id,
                app.name,
                app.version,
                1 if app.enabled else 0,
                orjson.dumps(app.manifest.to_dict()).decode(),
                app.bundle_path,
                app.bundle_sha256,
                app.source_url,
                app.installed_by,
                app.installed_at,
                app.min_age,
            ),
        )

    async def update_installed(self, app: InstalledApp) -> None:
        await self._db.enqueue(
            """UPDATE installed_apps
               SET name = ?, version = ?, manifest_json = ?,
                   bundle_path = ?, bundle_sha256 = ?, source_url = ?,
                   min_age = ?
               WHERE app_id = ?""",
            (
                app.name,
                app.version,
                orjson.dumps(app.manifest.to_dict()).decode(),
                app.bundle_path,
                app.bundle_sha256,
                app.source_url,
                app.min_age,
                app.app_id,
            ),
        )

    async def set_enabled(self, app_id: str, *, enabled: bool) -> None:
        await self._db.enqueue(
            "UPDATE installed_apps SET enabled = ? WHERE app_id = ?",
            (1 if enabled else 0, app_id),
        )

    async def set_min_age(self, app_id: str, min_age: int) -> None:
        await self._db.enqueue(
            "UPDATE installed_apps SET min_age = ? WHERE app_id = ?",
            (min_age, app_id),
        )

    async def uninstall(self, app_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM installed_apps WHERE app_id = ?",
            (app_id,),
        )

    async def kv_get(self, app_id: str, user_id: str, key: str) -> AppKvEntry | None:
        row = await self._db.fetchone(
            "SELECT * FROM app_kv WHERE app_id = ? AND user_id = ? AND key = ?",
            (app_id, user_id, key),
        )
        return _row_to_kv(row) if row is not None else None

    async def kv_list(self, app_id: str, user_id: str) -> list[AppKvEntry]:
        rows = await self._db.fetchall(
            "SELECT * FROM app_kv WHERE app_id = ? AND user_id = ? ORDER BY key",
            (app_id, user_id),
        )
        return [_row_to_kv(r) for r in rows]

    async def kv_set(
        self,
        app_id: str,
        user_id: str,
        key: str,
        value_json: str,
        updated_at: str,
    ) -> None:
        await self._db.enqueue(
            """INSERT INTO app_kv(app_id, user_id, key, value_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(app_id, user_id, key)
               DO UPDATE SET value_json = excluded.value_json,
                             updated_at = excluded.updated_at""",
            (app_id, user_id, key, value_json, updated_at),
        )

    async def kv_delete(self, app_id: str, user_id: str, key: str) -> None:
        await self._db.enqueue(
            "DELETE FROM app_kv WHERE app_id = ? AND user_id = ? AND key = ?",
            (app_id, user_id, key),
        )

    async def kv_count(self, app_id: str, user_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS cnt FROM app_kv WHERE app_id = ? AND user_id = ?",
            (app_id, user_id),
        )
        return int(row["cnt"]) if row is not None else 0

    async def kv_people(self, app_id: str) -> int:
        """How many people have saved anything in *app_id* (the uninstall
        dialog: "4 people lose their saved games and settings")."""
        return int(
            await self._db.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM app_kv WHERE app_id = ?",
                (app_id,),
                default=0,
            )
        )

    # ── Pending-session invites ──────────────────────────────────────────

    async def add_pending_session(self, s: AppPendingSession) -> None:
        await self._db.enqueue(
            """INSERT INTO app_pending_sessions
                 (app_id, user_id, session_id, from_instance, from_user,
                  payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(app_id, user_id, session_id) DO UPDATE SET
                 from_user=excluded.from_user,
                 payload_json=excluded.payload_json,
                 created_at=excluded.created_at
               -- A pending invite is only ever refreshed by the household
               -- that opened it; another one reusing the id changes nothing.
               -- A game already played or sent (``active``) is never
               -- rewritten by an open reusing its id.
               -- A seat at a group table (v_76) and a 1:1 invite never
               -- refresh each other.
               WHERE app_pending_sessions.from_instance = excluded.from_instance
                 AND app_pending_sessions.from_user IS excluded.from_user
                 AND app_pending_sessions.state = 'invited'
                 AND json_type(app_pending_sessions.payload_json, '$.group') IS NULL
                 AND json_type(excluded.payload_json, '$.group') IS NULL""",
            (
                s.app_id,
                s.user_id,
                s.session_id,
                s.from_instance,
                s.from_user,
                orjson.dumps(s.payload).decode(),
                s.created_at,
            ),
        )
        # Bound a flood from a paired peer: keep at most
        # ``_PENDING_SESSION_MAX_PER_PAIR`` waiting invites per (app, user) — never a
        # game being played or an invite this person sent (D3) — deleting the
        # oldest beyond the cap. Same write-queue batch as the insert, so the
        # per-pair invariant holds continuously (mirrors the notifications
        # per-user cap).
        await self._db.enqueue(
            """DELETE FROM app_pending_sessions
                WHERE rowid IN (
                    SELECT rowid FROM app_pending_sessions
                     WHERE app_id = ? AND user_id = ? AND state = 'invited'
                     ORDER BY COALESCE(updated_at, created_at) DESC
                     LIMIT -1 OFFSET ?
                )""",
            (s.app_id, s.user_id, _PENDING_SESSION_MAX_PER_PAIR),
        )

    async def prune_pending_sessions(
        self,
        *,
        max_age_seconds: int = _PENDING_SESSION_TTL_SECONDS,
        active_max_age_seconds: int = _ACTIVE_SESSION_TTL_SECONDS,
    ) -> int:
        """Delete sessions quiet for longer than their TTL — an invite
        nobody played (``max_age_seconds``, 14 days), a game nobody moved in
        (``active_max_age_seconds``, 90 days), and whatever they still held.
        Returns purge count.

        Counts before deleting (aiosqlite's DELETE doesn't surface a row
        count at this revision) — same mechanism as
        ``federation_repo.prune_replay_cache``.
        """
        now = datetime.now(UTC)
        invited_cut = (now - timedelta(seconds=max_age_seconds)).isoformat()
        active_cut = (now - timedelta(seconds=active_max_age_seconds)).isoformat()
        where = (
            " WHERE (state = ? AND COALESCE(updated_at, created_at) < ?)"
            " OR (state = ? AND COALESCE(updated_at, created_at) < ?)"
        )
        args = (SESSION_ACTIVE, active_cut, SESSION_INVITED, invited_cut)
        before = await self._db.fetchval(
            "SELECT COUNT(*) FROM app_pending_sessions" + where, args, default=0
        )
        await self._db.enqueue("DELETE FROM app_pending_sessions" + where, args)
        return int(before)

    async def get_pending_session(
        self, app_id: str, user_id: str, session_id: str
    ) -> AppPendingSession | None:
        """One known session — a waiting invite (expired or not: the
        caller compares ``created_at`` with the TTL) or a game being
        played. One primary-key read."""
        row = await self._db.fetchone(
            """SELECT * FROM app_pending_sessions
               WHERE app_id = ? AND user_id = ? AND session_id = ?""",
            (app_id, user_id, session_id),
        )
        return _row_to_pending(row) if row is not None else None

    async def take_pending_session(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_instance: str | None = None,
        from_user: str | None = None,
    ) -> AppPendingSession | None:
        """Remove one waiting invite — the one atomic step a decline, a
        withdrawal and Play race on. Returns the invite when this call
        removed it, ``None`` when it was gone already, is a game already
        played (``active``), or is not from ``from_instance`` /
        ``from_user`` when they are given: only the household and person
        who sent it may withdraw it."""
        invite = await self.get_pending_session(app_id, user_id, session_id)
        if invite is None or invite.state != SESSION_INVITED:
            return None
        if from_instance is not None and invite.from_instance != from_instance:
            return None
        if from_user is not None and invite.from_user != from_user:
            return None
        changed = await self._db.enqueue_rowcount(
            """DELETE FROM app_pending_sessions
               WHERE app_id = ? AND user_id = ? AND session_id = ?
                 AND from_instance = ? AND state = ?""",
            (app_id, user_id, session_id, invite.from_instance, SESSION_INVITED),
        )
        return invite if changed == 1 else None

    async def activate_pending_session(
        self, app_id: str, user_id: str, session_id: str, *, now: str
    ) -> AppPendingSession | None:
        """Play: a waiting invite becomes a game being played, in one
        atomic step like :meth:`take_pending_session` (a decline or a
        withdrawal racing it wins or loses as a whole). Returns the invite
        as it waited, ``None`` when it no longer waits. The row stays: it
        is the session's mailbox from now on."""
        invite = await self.get_pending_session(app_id, user_id, session_id)
        if invite is None or invite.state != SESSION_INVITED:
            return None
        changed = await self._db.enqueue_rowcount(
            """UPDATE app_pending_sessions SET state = ?, updated_at = ?
               WHERE app_id = ? AND user_id = ? AND session_id = ?
                 AND state = ?""",
            (SESSION_ACTIVE, now, app_id, user_id, session_id, SESSION_INVITED),
        )
        return invite if changed == 1 else None

    async def delete_pending_session(
        self, app_id: str, user_id: str, session_id: str
    ) -> None:
        """Forget one session (an invite played past its TTL, or across a
        guardian block)."""
        await self._db.enqueue(
            """DELETE FROM app_pending_sessions
               WHERE app_id = ? AND user_id = ? AND session_id = ?""",
            (app_id, user_id, session_id),
        )

    async def add_active_session(self, s: AppPendingSession) -> None:
        """Know a game this person plays (they sent the invite, or made
        their first move in a game from before the mailbox). A row the
        session already has is left as it is."""
        await self._db.enqueue(
            """INSERT INTO app_pending_sessions
                 (app_id, user_id, session_id, from_instance, from_user,
                  payload_json, created_at, state, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(app_id, user_id, session_id) DO NOTHING""",
            (
                s.app_id,
                s.user_id,
                s.session_id,
                s.from_instance,
                s.from_user,
                orjson.dumps(s.payload).decode(),
                s.created_at,
                SESSION_ACTIVE,
                s.last_change,
            ),
        )
        await self._db.enqueue(
            """DELETE FROM app_pending_sessions
                WHERE rowid IN (
                    SELECT rowid FROM app_pending_sessions
                     WHERE app_id = ? AND user_id = ? AND state = 'invited'
                     ORDER BY COALESCE(updated_at, created_at) DESC
                     LIMIT -1 OFFSET ?
                )""",
            (s.app_id, s.user_id, _PENDING_SESSION_MAX_PER_PAIR),
        )

    async def touch_session(
        self, app_id: str, user_id: str, session_id: str, *, now: str
    ) -> None:
        """A move in the game: it stays known for another TTL."""
        await self._db.enqueue(
            """UPDATE app_pending_sessions SET updated_at = ?
               WHERE app_id = ? AND user_id = ? AND session_id = ?""",
            (now, app_id, user_id, session_id),
        )

    async def hold_message(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_instance: str,
        from_user: str,
        held: dict,
        now: str,
    ) -> bool:
        """The mailbox (D11): keep *held* as the latest message of this
        session for this person, replacing an older one, but only when the
        session is known for them and its other side is ``from_instance``
        / ``from_user``. One primary-key write. ``True`` when kept."""
        # An invite this person sent is answered once the other side says
        # anything: it stops counting towards the open-invite cap.
        changed = await self._db.enqueue_rowcount(
            """UPDATE app_pending_sessions SET held_json = ?, updated_at = ?,
                 payload_json = CASE
                   WHEN json_type(payload_json, '$.sent') = 'true'
                   THEN json_set(payload_json, '$.answered', json('true'))
                   ELSE payload_json END
               WHERE app_id = ? AND user_id = ? AND session_id = ?
                 AND from_instance = ? AND from_user = ?
                 -- A group seat's mailbox is per sender seat (v_76 part II).
                 AND json_type(payload_json, '$.group') IS NULL""",
            (
                orjson.dumps(held).decode(),
                now,
                app_id,
                user_id,
                session_id,
                from_instance,
                from_user,
            ),
        )
        return changed == 1

    async def list_sessions(self, app_id: str, user_id: str) -> list[AppPendingSession]:
        """Every session this household knows for this person in this app
        — invites waiting for them, games they play, invites they sent —
        latest change first, by the existing index. Bounded: at most
        ``_PENDING_SESSION_MAX_PER_PAIR`` waiting invites, plus games and sent
        invites (≤ 30 sent a day) that the 14-day TTL prunes."""
        rows = await self._db.fetchall(
            """SELECT * FROM app_pending_sessions
               WHERE app_id = ? AND user_id = ?
               ORDER BY COALESCE(updated_at, created_at) DESC""",
            (app_id, user_id),
        )
        return [_row_to_pending(r) for r in rows]

    async def withdraw_session(
        self, app_id: str, user_id: str, session_id: str
    ) -> None:
        """The person took back an invite they sent: the row stays (it
        still counts towards the day's invites), marked withdrawn."""
        await self._db.enqueue(
            """UPDATE app_pending_sessions
                  SET payload_json = json_set(payload_json, '$.withdrawn', json('true'))
               WHERE app_id = ? AND user_id = ? AND session_id = ?""",
            (app_id, user_id, session_id),
        )

    async def take_held(self, app_id: str, user_id: str) -> list[AppPendingSession]:
        """The person's app opened: hand over every held message (oldest
        change first) and clear them. A message held again meanwhile (a
        newer move) is not cleared: it waits for the next open."""
        rows = await self._db.fetchall(
            """SELECT * FROM app_pending_sessions
               WHERE app_id = ? AND user_id = ? AND held_json IS NOT NULL AND state = 'active'
               ORDER BY COALESCE(updated_at, created_at)""",
            (app_id, user_id),
        )
        out: list[AppPendingSession] = []
        for row in rows:
            held = _row_to_pending(row)
            changed = await self._db.enqueue_rowcount(
                """UPDATE app_pending_sessions SET held_json = NULL
                   WHERE app_id = ? AND user_id = ? AND session_id = ?
                     AND held_json IS NOT NULL
                     AND COALESCE(updated_at, created_at) = ?""",
                (app_id, user_id, held.session_id, held.last_change),
            )
            if changed == 1:
                out.append(held)
        return out

    async def list_app_sessions(self, app_id: str) -> list[AppPendingSession]:
        """Every session row of *app_id*, for everyone here — what an
        uninstall ends (D9). Bounded like :meth:`list_sessions`, per
        person."""
        rows = await self._db.fetchall(
            """SELECT * FROM app_pending_sessions WHERE app_id = ?
               ORDER BY user_id, COALESCE(updated_at, created_at)""",
            (app_id,),
        )
        return [_row_to_pending(r) for r in rows]

    async def list_session_rows(
        self, app_id: str, session_id: str
    ) -> list[AppPendingSession]:
        """Everyone here with a row for *session_id* — the people of this
        household at one group table (v_76): invited, or seated."""
        rows = await self._db.fetchall(
            """SELECT * FROM app_pending_sessions
               WHERE app_id = ? AND session_id = ? ORDER BY user_id""",
            (app_id, session_id),
        )
        return [_row_to_pending(r) for r in rows]
