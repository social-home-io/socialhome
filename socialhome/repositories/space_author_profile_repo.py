"""A GFS follower's cache of a space's author profiles (v_60).

One row per ``(space_id, author_user_id)``: the newest verified
``author_profile`` item (``ts``, normalised to UTC by the caller). Holds no
business logic — :class:`~socialhome.services.space_item_inbound.SpaceItemInbound`
verifies and decodes, :class:`~socialhome.services.person_summary_service.PersonSummaryService`
reads.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_author_profile import SpaceAuthorProfile
from .base import rows_to_dicts

_COLS = (
    "space_id, author_user_id, author_pk, display_name, bio, picture_hash, "
    "profile_rev, ts"
)


@runtime_checkable
class AbstractSpaceAuthorProfileRepo(Protocol):
    async def upsert_if_newer(
        self, profile: SpaceAuthorProfile, *, picture_webp: bytes | None
    ) -> bool: ...
    async def list_for(
        self, space_id: str, author_user_ids: Iterable[str]
    ) -> builtins.list[SpaceAuthorProfile]: ...
    async def list_for_space(
        self, space_id: str
    ) -> builtins.list[SpaceAuthorProfile]: ...
    async def get_picture(
        self, space_id: str, author_user_id: str
    ) -> tuple[bytes, str] | None: ...


class SqliteSpaceAuthorProfileRepo:
    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert_if_newer(
        self, profile: SpaceAuthorProfile, *, picture_webp: bytes | None
    ) -> bool:
        """Store ``profile`` unless a row with a newer-or-equal ``ts`` is
        held. ``True`` when it was stored."""
        changed = await self._db.enqueue_rowcount(
            "INSERT INTO space_author_profiles(space_id, author_user_id, "
            "author_pk, display_name, bio, picture_webp, picture_hash, "
            "profile_rev, ts) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(space_id, author_user_id) DO UPDATE SET "
            "author_pk=excluded.author_pk, display_name=excluded.display_name, "
            "bio=excluded.bio, picture_webp=excluded.picture_webp, "
            "picture_hash=excluded.picture_hash, "
            "profile_rev=excluded.profile_rev, ts=excluded.ts "
            "WHERE excluded.ts > space_author_profiles.ts",
            (
                profile.space_id,
                profile.author_user_id,
                profile.author_pk,
                profile.display_name,
                profile.bio,
                picture_webp,
                profile.picture_hash,
                profile.profile_rev,
                profile.ts,
            ),
        )
        return bool(changed)

    async def list_for(
        self, space_id: str, author_user_ids: Iterable[str]
    ) -> builtins.list[SpaceAuthorProfile]:
        ids = sorted(set(author_user_ids))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = await self._db.fetchall(
            f"SELECT {_COLS} FROM space_author_profiles "
            f"WHERE space_id=? AND author_user_id IN ({marks})",
            (space_id, *ids),
        )
        return [_to_profile(r) for r in rows_to_dicts(rows)]

    async def list_for_space(self, space_id: str) -> builtins.list[SpaceAuthorProfile]:
        rows = await self._db.fetchall(
            f"SELECT {_COLS} FROM space_author_profiles WHERE space_id=? "
            "ORDER BY display_name",
            (space_id,),
        )
        return [_to_profile(r) for r in rows_to_dicts(rows)]

    async def get_picture(
        self, space_id: str, author_user_id: str
    ) -> tuple[bytes, str] | None:
        row = await self._db.fetchone(
            "SELECT picture_webp, picture_hash FROM space_author_profiles "
            "WHERE space_id=? AND author_user_id=? AND picture_webp IS NOT NULL",
            (space_id, author_user_id),
        )
        if row is None:
            return None
        return bytes(row["picture_webp"]), str(row["picture_hash"])


def _to_profile(row: dict) -> SpaceAuthorProfile:
    return SpaceAuthorProfile(
        space_id=row["space_id"],
        author_user_id=row["author_user_id"],
        author_pk=row["author_pk"],
        display_name=row["display_name"],
        bio=row["bio"],
        picture_hash=row["picture_hash"],
        profile_rev=row["profile_rev"],
        ts=row["ts"],
    )
