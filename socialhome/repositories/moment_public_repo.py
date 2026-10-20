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

Holds no business logic; the service in
``services/moment_public_service.py`` orchestrates calls to the GFS
and lookups against these repos.
"""

from __future__ import annotations

import builtins
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.moment_public import MomentPublicFollow, MomentPublicRegistration
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
    "followed_bio, followed_picture_digest"
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
    async def set_profile(
        self,
        *,
        followed_user_id: str,
        gfs_id: str,
        display_name: str,
        bio: str | None,
        picture_digest: str | None,
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
    )
