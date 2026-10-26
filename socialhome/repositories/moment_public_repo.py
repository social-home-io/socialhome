"""Public-Momentum repos (§Momentum-public).

Two thin SQLite repos backing the public-moment author and follower
tables:

* :class:`AbstractMomentPublicRegistrationRepo` /
  :class:`SqliteMomentPublicRegistrationRepo` — author-side opt-in
  per ``(user_id, gfs_id)``.
* :class:`AbstractMomentPublicFollowRepo` /
  :class:`SqliteMomentPublicFollowRepo` — follower-side cache of
  whom the user follows publicly via which GFS, plus the
  followed user's home-instance pk for inbound signature verify.
* :class:`AbstractMomentPublicKeyRepo` /
  :class:`SqliteMomentPublicKeyRepo` — moment content keys (v_62): the
  ones minted for local authors and the ones followed authors delivered.

Holds no business logic; the service in
``services/moment_public_service.py`` orchestrates calls to the GFS
and lookups against these repos.
"""

from __future__ import annotations

import builtins
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.moment_public import (
    MomentPublicFollow,
    MomentPublicKey,
    MomentPublicRegistration,
    MomentPublicShare,
)
from .base import rows_to_dicts

# ── Registrations ────────────────────────────────────────────────────────


_REG_COLS = "user_id, gfs_id, registered_at, default_share, last_picture_digest"


@runtime_checkable
class AbstractMomentPublicRegistrationRepo(Protocol):
    async def upsert(
        self,
        *,
        user_id: str,
        gfs_id: str,
        default_share: bool = True,
    ) -> MomentPublicRegistration: ...
    async def delete(self, *, user_id: str, gfs_id: str) -> int: ...
    async def list_for_user(
        self, user_id: str
    ) -> builtins.list[MomentPublicRegistration]: ...
    async def list_for_gfs(
        self, gfs_id: str
    ) -> builtins.list[MomentPublicRegistration]: ...
    async def get(
        self, *, user_id: str, gfs_id: str
    ) -> MomentPublicRegistration | None: ...
    async def set_default_share(
        self, *, user_id: str, gfs_id: str, default_share: bool
    ) -> int: ...
    async def set_last_picture_digest(
        self, *, user_id: str, gfs_id: str, digest: str | None
    ) -> int: ...


class SqliteMomentPublicRegistrationRepo:
    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(
        self,
        *,
        user_id: str,
        gfs_id: str,
        default_share: bool = True,
    ) -> MomentPublicRegistration:
        await self._db.enqueue(
            "INSERT INTO moment_public_registrations(user_id, gfs_id, default_share) "
            "VALUES(?, ?, ?) "
            "ON CONFLICT(user_id, gfs_id) DO UPDATE SET default_share=excluded.default_share",
            (user_id, gfs_id, int(default_share)),
        )
        got = await self.get(user_id=user_id, gfs_id=gfs_id)
        assert got is not None  # we just upserted
        return got

    async def delete(self, *, user_id: str, gfs_id: str) -> int:
        return await self._db.enqueue(
            "DELETE FROM moment_public_registrations WHERE user_id=? AND gfs_id=?",
            (user_id, gfs_id),
        )

    async def list_for_user(
        self, user_id: str
    ) -> builtins.list[MomentPublicRegistration]:
        rows = await self._db.fetchall(
            f"SELECT {_REG_COLS} FROM moment_public_registrations WHERE user_id=?",
            (user_id,),
        )
        return [_to_reg(r) for r in rows_to_dicts(rows)]

    async def list_for_gfs(
        self, gfs_id: str
    ) -> builtins.list[MomentPublicRegistration]:
        rows = await self._db.fetchall(
            f"SELECT {_REG_COLS} FROM moment_public_registrations WHERE gfs_id=?",
            (gfs_id,),
        )
        return [_to_reg(r) for r in rows_to_dicts(rows)]

    async def get(
        self, *, user_id: str, gfs_id: str
    ) -> MomentPublicRegistration | None:
        rows = await self._db.fetchall(
            f"SELECT {_REG_COLS} "
            "FROM moment_public_registrations WHERE user_id=? AND gfs_id=?",
            (user_id, gfs_id),
        )
        dicts = rows_to_dicts(rows)
        return _to_reg(dicts[0]) if dicts else None

    async def set_default_share(
        self, *, user_id: str, gfs_id: str, default_share: bool
    ) -> int:
        return await self._db.enqueue(
            "UPDATE moment_public_registrations SET default_share=? "
            "WHERE user_id=? AND gfs_id=?",
            (int(default_share), user_id, gfs_id),
        )

    async def set_last_picture_digest(
        self, *, user_id: str, gfs_id: str, digest: str | None
    ) -> int:
        return await self._db.enqueue(
            "UPDATE moment_public_registrations SET last_picture_digest=? "
            "WHERE user_id=? AND gfs_id=?",
            (digest, user_id, gfs_id),
        )


def _to_reg(row: dict) -> MomentPublicRegistration:
    return MomentPublicRegistration(
        user_id=row["user_id"],
        gfs_id=row["gfs_id"],
        registered_at=row["registered_at"],
        default_share=bool(row["default_share"]),
        last_picture_digest=row.get("last_picture_digest"),
    )


# ── Follows ──────────────────────────────────────────────────────────────


_FOLLOW_COLS = (
    "follower_user_id, followed_user_id, gfs_id, followed_instance_pk, "
    "followed_username, followed_display_name, created_at, "
    "followed_bio, followed_picture_digest, followed_keywrap_pk, "
    "followed_keywrap_suite"
)


@runtime_checkable
class AbstractMomentPublicFollowRepo(Protocol):
    async def upsert(
        self,
        *,
        follower_user_id: str,
        followed_user_id: str,
        gfs_id: str,
        followed_instance_pk: str,
        followed_username: str,
        followed_display_name: str,
        followed_bio: str | None = None,
        followed_picture_digest: str | None = None,
    ) -> MomentPublicFollow: ...
    async def delete(
        self, *, follower_user_id: str, followed_user_id: str, gfs_id: str
    ) -> int: ...
    async def get(
        self, *, follower_user_id: str, followed_user_id: str, gfs_id: str
    ) -> MomentPublicFollow | None: ...
    async def list_for_follower(
        self, follower_user_id: str
    ) -> builtins.list[MomentPublicFollow]: ...
    async def lookup_followed_pk(
        self, *, follower_user_id: str, followed_user_id: str, gfs_id: str
    ) -> str | None: ...
    async def followers_of(
        self, followed_user_id: str
    ) -> builtins.list[MomentPublicFollow]: ...
    async def list_for_followed(
        self, followed_user_ids: builtins.list[str]
    ) -> builtins.list[MomentPublicFollow]: ...
    async def any_through(self, gfs_id: str) -> bool: ...
    async def set_profile(
        self,
        *,
        followed_user_id: str,
        gfs_id: str,
        display_name: str,
        bio: str | None,
        picture_digest: str | None,
    ) -> None: ...
    async def set_keywrap(
        self,
        *,
        followed_user_id: str,
        gfs_id: str,
        keywrap_pk: str,
        keywrap_suite: str,
    ) -> None: ...


class SqliteMomentPublicFollowRepo:
    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(
        self,
        *,
        follower_user_id: str,
        followed_user_id: str,
        gfs_id: str,
        followed_instance_pk: str,
        followed_username: str,
        followed_display_name: str,
        followed_bio: str | None = None,
        followed_picture_digest: str | None = None,
    ) -> MomentPublicFollow:
        await self._db.enqueue(
            "INSERT INTO moment_public_follows("
            "follower_user_id, followed_user_id, gfs_id, "
            "followed_instance_pk, followed_username, followed_display_name, "
            "followed_bio, followed_picture_digest) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(follower_user_id, followed_user_id, gfs_id) DO UPDATE SET "
            "followed_instance_pk=excluded.followed_instance_pk, "
            "followed_username=excluded.followed_username, "
            "followed_display_name=excluded.followed_display_name, "
            "followed_bio=excluded.followed_bio, "
            "followed_picture_digest=excluded.followed_picture_digest",
            (
                follower_user_id,
                followed_user_id,
                gfs_id,
                followed_instance_pk,
                followed_username,
                followed_display_name,
                followed_bio,
                followed_picture_digest,
            ),
        )
        got = await self.get(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
            gfs_id=gfs_id,
        )
        assert got is not None
        return got

    async def delete(
        self, *, follower_user_id: str, followed_user_id: str, gfs_id: str
    ) -> int:
        return await self._db.enqueue(
            "DELETE FROM moment_public_follows "
            "WHERE follower_user_id=? AND followed_user_id=? AND gfs_id=?",
            (follower_user_id, followed_user_id, gfs_id),
        )

    async def get(
        self, *, follower_user_id: str, followed_user_id: str, gfs_id: str
    ) -> MomentPublicFollow | None:
        rows = await self._db.fetchall(
            f"SELECT {_FOLLOW_COLS} FROM moment_public_follows "
            "WHERE follower_user_id=? AND followed_user_id=? AND gfs_id=?",
            (follower_user_id, followed_user_id, gfs_id),
        )
        dicts = rows_to_dicts(rows)
        return _to_follow(dicts[0]) if dicts else None

    async def list_for_follower(
        self, follower_user_id: str
    ) -> builtins.list[MomentPublicFollow]:
        rows = await self._db.fetchall(
            f"SELECT {_FOLLOW_COLS} FROM moment_public_follows "
            "WHERE follower_user_id=? ORDER BY created_at DESC",
            (follower_user_id,),
        )
        return [_to_follow(r) for r in rows_to_dicts(rows)]

    async def lookup_followed_pk(
        self, *, follower_user_id: str, followed_user_id: str, gfs_id: str
    ) -> str | None:
        got = await self.get(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
            gfs_id=gfs_id,
        )
        return got.followed_instance_pk if got else None

    async def followers_of(
        self, followed_user_id: str
    ) -> builtins.list[MomentPublicFollow]:
        rows = await self._db.fetchall(
            f"SELECT {_FOLLOW_COLS} FROM moment_public_follows "
            "WHERE followed_user_id=? ORDER BY created_at DESC",
            (followed_user_id,),
        )
        return [_to_follow(r) for r in rows_to_dicts(rows)]

    async def any_through(self, gfs_id: str) -> bool:
        """Whether any local user follows someone through ``gfs_id``."""
        row = await self._db.fetchone(
            "SELECT 1 FROM moment_public_follows WHERE gfs_id=? LIMIT 1", (gfs_id,)
        )
        return row is not None

    async def list_for_followed(
        self, followed_user_ids: builtins.list[str]
    ) -> builtins.list[MomentPublicFollow]:
        """Every local follow of any of ``followed_user_ids`` — one read."""
        if not followed_user_ids:
            return []
        marks = ",".join("?" for _ in followed_user_ids)
        rows = await self._db.fetchall(
            f"SELECT {_FOLLOW_COLS} FROM moment_public_follows "
            f"WHERE followed_user_id IN ({marks}) ORDER BY created_at DESC",
            tuple(followed_user_ids),
        )
        return [_to_follow(r) for r in rows_to_dicts(rows)]

    async def set_profile(
        self,
        *,
        followed_user_id: str,
        gfs_id: str,
        display_name: str,
        bio: str | None,
        picture_digest: str | None,
    ) -> None:
        """Refresh the author's public profile on every local follow of
        them through ``gfs_id``."""
        await self._db.enqueue(
            "UPDATE moment_public_follows SET followed_display_name=?, "
            "followed_bio=?, followed_picture_digest=? "
            "WHERE followed_user_id=? AND gfs_id=?",
            (display_name, bio, picture_digest, followed_user_id, gfs_id),
        )

    async def set_keywrap(
        self,
        *,
        followed_user_id: str,
        gfs_id: str,
        keywrap_pk: str,
        keywrap_suite: str,
    ) -> None:
        """Keep the author household's key-wrap key (v_68, hex) and the KEM
        suite it was named under on every local follow of them through
        ``gfs_id`` — what a reaction to their public moments is sealed to.
        Only rows holding another value are written."""
        await self._db.enqueue(
            "UPDATE moment_public_follows SET followed_keywrap_pk=?, "
            "followed_keywrap_suite=? "
            "WHERE followed_user_id=? AND gfs_id=? "
            "AND (followed_keywrap_pk IS NOT ? OR followed_keywrap_suite IS NOT ?)",
            (
                keywrap_pk,
                keywrap_suite,
                followed_user_id,
                gfs_id,
                keywrap_pk,
                keywrap_suite,
            ),
        )


def _to_follow(row: dict) -> MomentPublicFollow:
    return MomentPublicFollow(
        follower_user_id=row["follower_user_id"],
        followed_user_id=row["followed_user_id"],
        gfs_id=row["gfs_id"],
        followed_instance_pk=row["followed_instance_pk"],
        followed_username=row["followed_username"],
        followed_display_name=row["followed_display_name"],
        created_at=row["created_at"],
        followed_bio=row["followed_bio"],
        followed_picture_digest=row["followed_picture_digest"],
        followed_keywrap_pk=row["followed_keywrap_pk"],
        followed_keywrap_suite=row["followed_keywrap_suite"],
    )


# ── Moment content keys (v_62) ───────────────────────────────────────────


_KEY_COLS = (
    "author_user_id, key_gen, key_step, is_own, key_sealed, created_at, rotate_due"
)


@runtime_checkable
class AbstractMomentPublicKeyRepo(Protocol):
    async def save(self, key: MomentPublicKey) -> bool: ...
    async def advance(self, key: MomentPublicKey) -> bool: ...
    async def get(
        self, author_user_id: str, key_gen: int, *, own: bool
    ) -> MomentPublicKey | None: ...
    async def latest(
        self, author_user_id: str, *, own: bool
    ) -> MomentPublicKey | None: ...
    async def prune(self, author_user_id: str, *, own: bool, keep: int) -> int: ...
    async def delete_all(self, author_user_id: str, *, own: bool) -> int: ...
    async def mark_rotate_due(self, author_user_id: str) -> bool: ...


class SqliteMomentPublicKeyRepo:
    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(self, key: MomentPublicKey) -> bool:
        """Store a new generation. A generation already held is never
        replaced (``False``): the first key for a generation wins."""
        changed = await self._db.enqueue_rowcount(
            "INSERT OR IGNORE INTO moment_public_keys("
            "author_user_id, is_own, key_gen, key_step, key_sealed) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                key.author_user_id,
                int(key.is_own),
                key.key_gen,
                key.key_step,
                key.key_sealed,
            ),
        )
        return changed > 0

    async def advance(self, key: MomentPublicKey) -> bool:
        """Move a held generation's step key forward (never back): the old
        step key is overwritten, so it can't be recomputed from here."""
        changed = await self._db.enqueue_rowcount(
            "UPDATE moment_public_keys SET key_step=?, key_sealed=? "
            "WHERE author_user_id=? AND is_own=? AND key_gen=? AND key_step<?",
            (
                key.key_step,
                key.key_sealed,
                key.author_user_id,
                int(key.is_own),
                key.key_gen,
                key.key_step,
            ),
        )
        return changed > 0

    async def get(
        self, author_user_id: str, key_gen: int, *, own: bool
    ) -> MomentPublicKey | None:
        row = await self._db.fetchone(
            f"SELECT {_KEY_COLS} FROM moment_public_keys "
            "WHERE author_user_id=? AND is_own=? AND key_gen=?",
            (author_user_id, int(own), key_gen),
        )
        return _to_key(dict(row)) if row is not None else None

    async def latest(self, author_user_id: str, *, own: bool) -> MomentPublicKey | None:
        row = await self._db.fetchone(
            f"SELECT {_KEY_COLS} FROM moment_public_keys "
            "WHERE author_user_id=? AND is_own=? ORDER BY key_gen DESC LIMIT 1",
            (author_user_id, int(own)),
        )
        return _to_key(dict(row)) if row is not None else None

    async def prune(self, author_user_id: str, *, own: bool, keep: int) -> int:
        """Keep only the newest *keep* generations of one author."""
        return await self._db.enqueue_rowcount(
            "DELETE FROM moment_public_keys WHERE author_user_id=? AND is_own=? "
            "AND key_gen NOT IN (SELECT key_gen FROM moment_public_keys "
            "WHERE author_user_id=? AND is_own=? ORDER BY key_gen DESC LIMIT ?)",
            (author_user_id, int(own), author_user_id, int(own), max(keep, 0)),
        )

    async def delete_all(self, author_user_id: str, *, own: bool) -> int:
        return await self._db.enqueue_rowcount(
            "DELETE FROM moment_public_keys WHERE author_user_id=? AND is_own=?",
            (author_user_id, int(own)),
        )

    async def mark_rotate_due(self, author_user_id: str) -> bool:
        """Flag the author's current own generation for rotation.
        ``False`` when the author has no own key yet (nothing to rotate)."""
        changed = await self._db.enqueue_rowcount(
            "UPDATE moment_public_keys SET rotate_due=1 "
            "WHERE author_user_id=? AND is_own=1 AND key_gen=("
            "SELECT MAX(key_gen) FROM moment_public_keys "
            "WHERE author_user_id=? AND is_own=1)",
            (author_user_id, author_user_id),
        )
        return changed > 0


def _to_key(row: dict) -> MomentPublicKey:
    return MomentPublicKey(
        author_user_id=row["author_user_id"],
        key_gen=int(row["key_gen"]),
        key_step=int(row["key_step"]),
        is_own=bool(row["is_own"]),
        key_sealed=row["key_sealed"],
        created_at=row["created_at"],
        rotate_due=bool(row["rotate_due"]),
    )


# ── Share links ────────────────────────────────────────────────────


_SHARE_COLS = (
    "moment_id, author_user_id, item_key_sealed, media_by_gfs, share_blob_id, "
    "share_gfs_id, created_at"
)


@runtime_checkable
class AbstractMomentPublicShareRepo(Protocol):
    async def save(self, share: MomentPublicShare) -> bool: ...
    async def get(self, moment_id: str) -> MomentPublicShare | None: ...
    async def set_share_copy(
        self, moment_id: str, *, blob_id: str, gfs_id: str
    ) -> bool: ...
    async def set_media(self, moment_id: str, media_by_gfs: str) -> bool: ...


class SqliteMomentPublicShareRepo:
    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(self, share: MomentPublicShare) -> bool:
        """Keep a public moment's link material — once (``False`` when the
        moment already has it)."""
        changed = await self._db.enqueue_rowcount(
            "INSERT OR IGNORE INTO moment_public_shares(moment_id, author_user_id,"
            " item_key_sealed, media_by_gfs) VALUES(?, ?, ?, ?)",
            (
                share.moment_id,
                share.author_user_id,
                share.item_key_sealed,
                share.media_by_gfs,
            ),
        )
        return changed > 0

    async def get(self, moment_id: str) -> MomentPublicShare | None:
        row = await self._db.fetchone(
            f"SELECT {_SHARE_COLS} FROM moment_public_shares WHERE moment_id=?",
            (moment_id,),
        )
        if row is None:
            return None
        d = dict(row)
        return MomentPublicShare(
            moment_id=d["moment_id"],
            author_user_id=d["author_user_id"],
            item_key_sealed=d["item_key_sealed"],
            media_by_gfs=d["media_by_gfs"],
            share_blob_id=d["share_blob_id"],
            share_gfs_id=d["share_gfs_id"],
            created_at=d["created_at"],
        )

    async def set_share_copy(
        self, moment_id: str, *, blob_id: str, gfs_id: str
    ) -> bool:
        changed = await self._db.enqueue_rowcount(
            "UPDATE moment_public_shares SET share_blob_id=?, share_gfs_id=?"
            " WHERE moment_id=?",
            (blob_id, gfs_id, moment_id),
        )
        return changed > 0

    async def set_media(self, moment_id: str, media_by_gfs: str) -> bool:
        """Replace the per-GFS media descriptors (a link added a GFS that
        now holds the file)."""
        changed = await self._db.enqueue_rowcount(
            "UPDATE moment_public_shares SET media_by_gfs=? WHERE moment_id=?",
            (media_by_gfs, moment_id),
        )
        return changed > 0
