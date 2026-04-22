"""User repository — persistence for :class:`~socialhome.domain.user.User`,
:class:`~socialhome.domain.user.RemoteUser`, API tokens and user blocks.

The :class:`AbstractUserRepo` protocol is what services depend on;
:class:`SqliteUserRepo` is the production implementation. Tests substitute
in-memory fakes that implement the same surface.

Only the methods that are actually required by domain + services in v1 are
exposed. Additional spec methods (remote-user status, presence, preferences
patching, …) are added here as the services that need them come online.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.user import RemoteUser, User, UserStatus
from .base import bool_col, row_to_dict, rows_to_dicts


# ─── Abstract interface ───────────────────────────────────────────────────


@runtime_checkable
class AbstractUserRepo(Protocol):
    # Local users ---------------------------------------------------------
    async def get(self, username: str) -> User | None: ...
    async def get_by_user_id(self, user_id: str) -> User | None: ...
    async def save(self, user: User) -> User: ...
    async def list_active(self) -> list[User]: ...
    async def list_all(self) -> list[User]: ...
    async def list_by_ids(self, user_ids: set[str]) -> list[User]: ...
    async def set_admin(self, username: str, is_admin: bool) -> None: ...
    async def soft_delete(self, username: str, grace_days: int = 30) -> None: ...

    # Remote users --------------------------------------------------------
    async def get_remote(self, user_id: str) -> RemoteUser | None: ...
    async def upsert_remote(self, remote: RemoteUser) -> None: ...
    async def list_remote_for_instance(self, instance_id: str) -> list[RemoteUser]: ...
    async def get_instance_for_user(self, user_id: str) -> str | None: ...
    async def mark_remote_deprovisioned(
        self,
        user_id: str,
        *,
        at: str | None = None,
    ) -> None: ...

    # API tokens ----------------------------------------------------------
    async def list_api_tokens(self, user_id: str) -> list[dict]: ...
    async def list_all_api_tokens(self) -> list[dict]: ...
    async def create_api_token(
        self,
        user_id: str,
        token_hash: str,
        label: str,
        *,
        expires_at: str | None = None,
    ) -> str: ...
    async def revoke_api_token(self, token_id: str) -> None: ...
    async def get_user_by_token_hash(self, token_hash: str) -> User | None: ...

    # Blocks --------------------------------------------------------------
    async def block(self, blocker_user_id: str, blocked_user_id: str) -> None: ...
    async def unblock(self, blocker_user_id: str, blocked_user_id: str) -> None: ...
    async def is_blocked(self, blocker_user_id: str, blocked_user_id: str) -> bool: ...

    # Profile picture (hash only — bytes live in ProfilePictureRepo) ------
    async def set_picture_hash(
        self,
        user_id: str,
        picture_hash: str | None,
    ) -> None: ...
    async def set_remote_picture_hash(
        self,
        user_id: str,
        picture_hash: str | None,
    ) -> None: ...


# ─── Concrete SQLite implementation ───────────────────────────────────────


class SqliteUserRepo:
    """SQLite-backed :class:`AbstractUserRepo` implementation."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Local users ─────────────────────────────────────────────────────

    async def get(self, username: str) -> User | None:
        row = await self._db.fetchone(
            "SELECT * FROM users WHERE username=?",
            (username,),
        )
        return _row_to_user(row_to_dict(row))

    async def get_by_user_id(self, user_id: str) -> User | None:
        row = await self._db.fetchone(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        )
        return _row_to_user(row_to_dict(row))

    async def save(self, user: User) -> User:
        """Upsert a local user row.

        Uses ``username`` (the primary key) to resolve conflicts — callers
        keep the same username to update an existing row.
        """
        await self._db.enqueue(
            """
            INSERT INTO users(
                username, user_id, display_name, is_admin, picture_hash, state,
                bio, locale, theme, emoji_skin_tone_default,
                status_emoji, status_text, status_expires_at,
                public_key, public_key_version, is_new_member,
                preferences_json, email, phone, date_of_birth,
                declared_age, is_minor, child_protection_enabled,
                deleted_at, grace_until, created_at, source
            ) VALUES(
                ?,?,?,?,?,?,
                ?,?,?,?,
                ?,?,?,
                ?,?,?,
                ?,?,?,?,
                ?,?,?,
                ?,?,COALESCE(?, datetime('now')), ?
            )
            ON CONFLICT(username) DO UPDATE SET
                display_name=excluded.display_name,
                is_admin=excluded.is_admin,
                picture_hash=excluded.picture_hash,
                state=excluded.state,
                bio=excluded.bio,
                locale=excluded.locale,
                theme=excluded.theme,
                emoji_skin_tone_default=excluded.emoji_skin_tone_default,
                status_emoji=excluded.status_emoji,
                status_text=excluded.status_text,
                status_expires_at=excluded.status_expires_at,
                public_key=excluded.public_key,
                public_key_version=excluded.public_key_version,
                is_new_member=excluded.is_new_member,
                preferences_json=excluded.preferences_json,
                email=excluded.email,
                phone=excluded.phone,
                date_of_birth=excluded.date_of_birth,
                declared_age=excluded.declared_age,
                is_minor=excluded.is_minor,
                child_protection_enabled=excluded.child_protection_enabled,
                deleted_at=excluded.deleted_at,
                grace_until=excluded.grace_until,
                source=excluded.source
            """,
            (
                user.username,
                user.user_id,
                user.display_name,
                int(user.is_admin),
                user.picture_hash,
                user.state,
                user.bio,
                user.locale,
                user.theme,
                user.emoji_skin_tone_default,
                user.status.emoji,
                user.status.text,
                user.status.expires_at,
                user.public_key,
                user.public_key_version,
                int(user.is_new_member),
                user.preferences_json,
                user.email,
                user.phone,
                user.date_of_birth,
                user.declared_age,
                int(user.is_minor),
                int(user.child_protection_enabled),
                user.deleted_at,
                user.grace_until,
                user.created_at,
                user.source,
            ),
        )
        return user

    async def list_active(self) -> list[User]:
        rows = await self._db.fetchall(
            "SELECT * FROM users WHERE state='active' AND deleted_at IS NULL "
            "ORDER BY username",
        )
        return [u for u in (_row_to_user(d) for d in rows_to_dicts(rows)) if u]

    async def list_all(self) -> list[User]:
        rows = await self._db.fetchall("SELECT * FROM users ORDER BY username")
        return [u for u in (_row_to_user(d) for d in rows_to_dicts(rows)) if u]

    async def list_by_ids(self, user_ids: set[str]) -> list[User]:
        if not user_ids:
            return []
        placeholders = ",".join("?" for _ in user_ids)
        rows = await self._db.fetchall(
            f"SELECT * FROM users WHERE user_id IN ({placeholders})",
            tuple(user_ids),
        )
        return [u for u in (_row_to_user(d) for d in rows_to_dicts(rows)) if u]

    async def set_admin(self, username: str, is_admin: bool) -> None:
        await self._db.enqueue(
            "UPDATE users SET is_admin=? WHERE username=?",
            (int(is_admin), username),
        )

    async def soft_delete(self, username: str, grace_days: int = 30) -> None:
        now = datetime.now(timezone.utc).isoformat()
        grace = datetime.now(timezone.utc).timestamp() + grace_days * 86400
        grace_iso = datetime.fromtimestamp(grace, tz=timezone.utc).isoformat()
        await self._db.enqueue(
            "UPDATE users SET state='inactive', deleted_at=?, grace_until=? "
            "WHERE username=?",
            (now, grace_iso, username),
        )

    # ── Remote users ────────────────────────────────────────────────────

    async def get_remote(self, user_id: str) -> RemoteUser | None:
        row = await self._db.fetchone(
            "SELECT * FROM remote_users WHERE user_id=?",
            (user_id,),
        )
        return _row_to_remote_user(row_to_dict(row))

    async def upsert_remote(self, remote: RemoteUser) -> None:
        await self._db.enqueue(
            """
            INSERT INTO remote_users(
                user_id, instance_id, remote_username, display_name, alias,
                visible_to, picture_hash, bio, status_json,
                public_key, public_key_version, synced_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,COALESCE(?, datetime('now')))
            ON CONFLICT(user_id) DO UPDATE SET
                instance_id=excluded.instance_id,
                remote_username=excluded.remote_username,
                display_name=excluded.display_name,
                alias=excluded.alias,
                visible_to=excluded.visible_to,
                picture_hash=excluded.picture_hash,
                bio=excluded.bio,
                status_json=excluded.status_json,
                public_key=excluded.public_key,
                public_key_version=excluded.public_key_version,
                synced_at=excluded.synced_at
            """,
            (
                remote.user_id,
                remote.instance_id,
                remote.remote_username,
                remote.display_name,
                remote.alias,
                remote.visible_to,
                remote.picture_hash,
                remote.bio,
                remote.status_json,
                remote.public_key,
                remote.public_key_version,
                remote.synced_at,
            ),
        )

    async def list_remote_for_instance(self, instance_id: str) -> list[RemoteUser]:
        """List remote users from an instance, excluding deprovisioned rows."""
        rows = await self._db.fetchall(
            "SELECT * FROM remote_users "
            "WHERE instance_id=? AND deprovisioned_at IS NULL "
            "ORDER BY remote_username",
            (instance_id,),
        )
        return [r for r in (_row_to_remote_user(d) for d in rows_to_dicts(rows)) if r]

    async def mark_remote_deprovisioned(
        self,
        user_id: str,
        *,
        at: str | None = None,
    ) -> None:
        """Flag a remote user as gone. The row is kept so historical
        references (posts, comments) still render the display name.
        """
        await self._db.enqueue(
            "UPDATE remote_users SET deprovisioned_at=COALESCE(?, datetime('now')) "
            "WHERE user_id=?",
            (at, user_id),
        )

    async def get_instance_for_user(self, user_id: str) -> str | None:
        """Return the home ``instance_id`` of a ``user_id`` — local or remote.

        Checks the local ``instance_identity`` table first (our own users),
        then ``remote_users``. Returns ``None`` if the ``user_id`` is unknown.
        """
        own = await self._db.fetchone(
            "SELECT i.instance_id FROM users u "
            "JOIN instance_identity i ON i.id='self' "
            "WHERE u.user_id=?",
            (user_id,),
        )
        if own is not None:
            return own["instance_id"]
        row = await self._db.fetchone(
            "SELECT instance_id FROM remote_users WHERE user_id=?",
            (user_id,),
        )
        return row["instance_id"] if row else None

    # ── API tokens ──────────────────────────────────────────────────────

    async def list_api_tokens(self, user_id: str) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT token_id, label, created_at, expires_at, last_used_at, revoked_at "
            "FROM api_tokens WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        )
        return rows_to_dicts(rows)

    async def list_all_api_tokens(self) -> list[dict]:
        """§A7 — admin-wide token list. Every user's active tokens in
        one query so the admin sessions panel can render a household-
        scope session view rather than only the caller's tokens.
        """
        rows = await self._db.fetchall(
            """
            SELECT t.token_id, t.label, t.created_at, t.expires_at,
                   t.last_used_at, t.revoked_at,
                   t.user_id, u.username, u.display_name
              FROM api_tokens AS t
              JOIN users AS u ON u.user_id = t.user_id
             ORDER BY t.created_at DESC
            """,
        )
        return rows_to_dicts(rows)

    async def create_api_token(
        self,
        user_id: str,
        token_hash: str,
        label: str,
        *,
        expires_at: str | None = None,
    ) -> str:
        token_id = uuid.uuid4().hex
        await self._db.enqueue(
            """
            INSERT INTO api_tokens(token_id, user_id, label, token_hash, expires_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (token_id, user_id, label, token_hash, expires_at),
        )
        return token_id

    async def revoke_api_token(self, token_id: str) -> None:
        await self._db.enqueue(
            "UPDATE api_tokens SET revoked_at=COALESCE(revoked_at, datetime('now')) "
            "WHERE token_id=?",
            (token_id,),
        )

    async def get_user_by_token_hash(self, token_hash: str) -> User | None:
        row = await self._db.fetchone(
            """
            SELECT u.* FROM users u
             JOIN api_tokens t ON t.user_id = u.user_id
            WHERE t.token_hash=? AND t.revoked_at IS NULL
              AND (t.expires_at IS NULL OR t.expires_at > datetime('now'))
            """,
            (token_hash,),
        )
        if row is None:
            return None
        # Bump last_used_at so operator UI can show active tokens.
        await self._db.enqueue(
            "UPDATE api_tokens SET last_used_at=datetime('now') WHERE token_hash=?",
            (token_hash,),
        )
        return _row_to_user(row_to_dict(row))

    # ── Blocks ──────────────────────────────────────────────────────────

    async def block(self, blocker_user_id: str, blocked_user_id: str) -> None:
        if blocker_user_id == blocked_user_id:
            raise ValueError("Cannot block yourself")
        await self._db.enqueue(
            "INSERT OR IGNORE INTO user_blocks(blocker_user_id, blocked_user_id) "
            "VALUES(?, ?)",
            (blocker_user_id, blocked_user_id),
        )

    async def unblock(self, blocker_user_id: str, blocked_user_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM user_blocks WHERE blocker_user_id=? AND blocked_user_id=?",
            (blocker_user_id, blocked_user_id),
        )

    async def is_blocked(self, blocker_user_id: str, blocked_user_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM user_blocks WHERE blocker_user_id=? AND blocked_user_id=?",
            (blocker_user_id, blocked_user_id),
        )
        return row is not None

    async def set_picture_hash(
        self,
        user_id: str,
        picture_hash: str | None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE users SET picture_hash=? WHERE user_id=?",
            (picture_hash, user_id),
        )

    async def set_remote_picture_hash(
        self,
        user_id: str,
        picture_hash: str | None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE remote_users SET picture_hash=? WHERE user_id=?",
            (picture_hash, user_id),
        )


# ─── Row ↔ domain mapping ─────────────────────────────────────────────────


def _row_to_user(row: dict | None) -> User | None:
    if row is None:
        return None
    return User(
        user_id=row["user_id"],
        username=row["username"],
        display_name=row["display_name"],
        is_admin=bool_col(row.get("is_admin", 0)),
        picture_hash=row.get("picture_hash"),
        state=row.get("state", "active"),
        bio=row.get("bio"),
        locale=row.get("locale"),
        theme=row.get("theme", "auto"),
        emoji_skin_tone_default=row.get("emoji_skin_tone_default"),
        status=UserStatus(
            emoji=row.get("status_emoji"),
            text=row.get("status_text"),
            expires_at=row.get("status_expires_at"),
        ),
        public_key=row.get("public_key"),
        public_key_version=int(row.get("public_key_version") or 0),
        is_new_member=bool_col(row.get("is_new_member", 1)),
        deleted_at=row.get("deleted_at"),
        grace_until=row.get("grace_until"),
        email=row.get("email"),
        phone=row.get("phone"),
        date_of_birth=row.get("date_of_birth"),
        declared_age=row.get("declared_age"),
        is_minor=bool_col(row.get("is_minor", 0)),
        child_protection_enabled=bool_col(row.get("child_protection_enabled", 0)),
        preferences_json=row.get("preferences_json", "{}"),
        created_at=row.get("created_at"),
        source=row.get("source", "manual"),
    )


def _row_to_remote_user(row: dict | None) -> RemoteUser | None:
    if row is None:
        return None
    return RemoteUser(
        user_id=row["user_id"],
        instance_id=row["instance_id"],
        remote_username=row["remote_username"],
        display_name=row["display_name"],
        alias=row.get("alias"),
        visible_to=row.get("visible_to", '"all"'),
        picture_hash=row.get("picture_hash"),
        bio=row.get("bio"),
        status_json=row.get("status_json"),
        public_key=row.get("public_key"),
        public_key_version=int(row.get("public_key_version") or 0),
        synced_at=row.get("synced_at"),
    )
