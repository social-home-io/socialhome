"""A space's cache of its authors' profiles (v_60).

One row per ``(space_id, author_user_id)``: the newest verified
``author_profile`` item (``ts``, normalised to UTC by the caller).

- ``picture_hash`` is the author's SIGNED hash.
- ``picture_webp`` is our own re-encode of that picture. It survives a newer
  item that names the same hash.
- ``signed_item`` / ``item_epoch`` (the v_61 new-follower bundle's copy)
  are no longer written; the columns stay, inert, and an update clears them.

At most :data:`MAX_CACHED_AUTHORS_PER_SPACE` other households' rows per
space; the least recently stamped are pruned (in a space with more authors,
one whose profile is older than the newest 200 is then named again only on
its next profile change). Our own authors' rows are never pruned.

Holds no business logic.
:class:`~socialhome.services.space_item_inbound.SpaceItemInbound` verifies
and decodes, and
:class:`~socialhome.services.person_summary_service.PersonSummaryService`
reads.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_author_profile import (
    CachedAuthorProfile,
    SpaceAuthorProfile,
)
from .base import rows_to_dicts

#: The most authors cached per space; the most recently stamped are kept,
#: so a reader can name the people it is likely to see.
MAX_CACHED_AUTHORS_PER_SPACE: int = 200

_COLS = (
    "space_id, author_user_id, author_pk, display_name, bio, picture_hash, "
    "profile_rev, ts"
)

#: The display reads (:meth:`SqliteSpaceAuthorProfileRepo.list_for`,
#: :meth:`~SqliteSpaceAuthorProfileRepo.list_for_space`) name a picture only
#: once its bytes are held: a row whose sidecar was refused (or never
#: arrived) knows the signed hash without the bytes, and a picture URL then
#: would 404.
_DISPLAY_COLS = (
    "space_id, author_user_id, author_pk, display_name, bio, "
    "CASE WHEN picture_webp IS NOT NULL THEN picture_hash END AS picture_hash, "
    "profile_rev, ts"
)


@runtime_checkable
class AbstractSpaceAuthorProfileRepo(Protocol):
    async def upsert_if_newer(
        self,
        profile: SpaceAuthorProfile,
        *,
        picture_webp: bytes | None,
    ) -> bool: ...
    async def get(
        self, space_id: str, author_user_id: str
    ) -> CachedAuthorProfile | None: ...
    async def list_spaces_for_author(
        self, author_user_id: str
    ) -> builtins.list[str]: ...
    async def delete(self, space_id: str, author_user_id: str) -> None: ...
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
        self,
        profile: SpaceAuthorProfile,
        *,
        picture_webp: bytes | None,
    ) -> bool:
        """Store ``profile`` unless a row with a newer-or-equal ``ts`` is
        held. Returns ``True`` when it was stored.

        Without ``picture_webp``, the bytes already held are kept if the
        signed ``picture_hash`` is unchanged. Otherwise the row holds no
        picture until a newer item brings one. The space's oldest rows past
        :data:`MAX_CACHED_AUTHORS_PER_SPACE` are pruned."""
        changed = await self._db.enqueue_rowcount(
            "INSERT INTO space_author_profiles(space_id, author_user_id, "
            "author_pk, display_name, bio, picture_webp, picture_hash, "
            "profile_rev, ts) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(space_id, author_user_id) DO UPDATE SET "
            "author_pk=excluded.author_pk, display_name=excluded.display_name, "
            "bio=excluded.bio, "
            "picture_webp=CASE "
            "WHEN excluded.picture_webp IS NOT NULL THEN excluded.picture_webp "
            "WHEN excluded.picture_hash IS space_author_profiles.picture_hash "
            "THEN space_author_profiles.picture_webp "
            "ELSE NULL END, "
            "picture_hash=excluded.picture_hash, "
            "profile_rev=excluded.profile_rev, ts=excluded.ts, "
            "signed_item=NULL, item_epoch=NULL "
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
        if changed:
            # Our own authors' rows are never pruned: they are our record
            # of what we published (dropping one would re-send it to every
            # follower on the next post) and bounded by our user count.
            await self._db.enqueue(
                "DELETE FROM space_author_profiles WHERE space_id=? AND "
                "author_pk IS NOT (SELECT identity_public_key FROM "
                "instance_identity) AND author_user_id NOT IN (SELECT "
                "author_user_id FROM space_author_profiles WHERE space_id=? AND "
                "author_pk IS NOT (SELECT identity_public_key FROM "
                "instance_identity) ORDER BY ts DESC LIMIT ?)",
                (profile.space_id, profile.space_id, MAX_CACHED_AUTHORS_PER_SPACE),
            )
        return bool(changed)

    async def get(
        self, space_id: str, author_user_id: str
    ) -> CachedAuthorProfile | None:
        """The held row with its picture state, or ``None``."""
        row = await self._db.fetchone(
            f"SELECT {_COLS}, picture_webp IS NOT NULL AS has_picture "
            "FROM space_author_profiles WHERE space_id=? AND author_user_id=?",
            (space_id, author_user_id),
        )
        if row is None:
            return None
        data = dict(row)
        return CachedAuthorProfile(
            profile=_to_profile(data),
            has_picture=bool(data["has_picture"]),
        )

    async def list_spaces_for_author(self, author_user_id: str) -> builtins.list[str]:
        """The spaces holding a profile of this author — for one of our own
        users, where we published one (a user id is household-bound, so no
        other household's row can carry it)."""
        rows = await self._db.fetchall(
            "SELECT space_id FROM space_author_profiles WHERE author_user_id=?",
            (author_user_id,),
        )
        return [str(r["space_id"]) for r in rows]

    async def delete(self, space_id: str, author_user_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_author_profiles WHERE space_id=? AND author_user_id=?",
            (space_id, author_user_id),
        )

    async def list_for(
        self, space_id: str, author_user_ids: Iterable[str]
    ) -> builtins.list[SpaceAuthorProfile]:
        ids = sorted(set(author_user_ids))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = await self._db.fetchall(
            f"SELECT {_DISPLAY_COLS} FROM space_author_profiles "
            f"WHERE space_id=? AND author_user_id IN ({marks})",
            (space_id, *ids),
        )
        return [_to_profile(r) for r in rows_to_dicts(rows)]

    async def list_for_space(self, space_id: str) -> builtins.list[SpaceAuthorProfile]:
        rows = await self._db.fetchall(
            f"SELECT {_DISPLAY_COLS} FROM space_author_profiles WHERE space_id=? "
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
