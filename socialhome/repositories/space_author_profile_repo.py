"""A space's cache of its authors' profiles (v_60/v_61).

One row per ``(space_id, author_user_id)``: the newest verified
``author_profile`` item (``ts``, normalised to UTC by the caller).

- ``picture_hash`` is the author's SIGNED hash.
- ``picture_webp`` is our own re-encode of that picture. It survives a newer
  item that names the same hash.
- ``signed_item`` is kept only where a new-follower bundle can be built; the
  caller decides, and only a seed holder keeps it.

At most :data:`MAX_CACHED_AUTHORS_PER_SPACE` rows per space; the least
recently stamped rows are pruned.

Holds no business logic.
:class:`~socialhome.services.space_item_inbound.SpaceItemInbound` verifies
and decodes, and
:class:`~socialhome.services.person_summary_service.PersonSummaryService`
reads.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_author_profile import (
    PROFILE_PICTURE_FIELD,
    CachedAuthorProfile,
    SpaceAuthorProfile,
)
from ..domain.space_author_profile_bundle import BundleEntry
from .base import rows_to_dicts

#: The most authors cached per space; the most recently stamped are kept.
#: That is four bundles' worth, so a host always has the newest
#: :data:`~socialhome.domain.space_author_profile_bundle.MAX_BUNDLE_AUTHORS`,
#: and a reader can name the people it is likely to see.
MAX_CACHED_AUTHORS_PER_SPACE: int = 200

_COLS = (
    "space_id, author_user_id, author_pk, display_name, bio, picture_hash, "
    "profile_rev, ts"
)


@runtime_checkable
class AbstractSpaceAuthorProfileRepo(Protocol):
    async def upsert_if_newer(
        self,
        profile: SpaceAuthorProfile,
        *,
        picture_webp: bytes | None,
        signed_item: dict | None = None,
        item_epoch: int | None = None,
    ) -> bool: ...
    async def set_picture(
        self,
        space_id: str,
        author_user_id: str,
        picture_hash: str,
        picture_webp: bytes,
    ) -> bool: ...
    async def get(
        self, space_id: str, author_user_id: str
    ) -> CachedAuthorProfile | None: ...
    async def list_signed_items(
        self, space_id: str, *, limit: int
    ) -> builtins.list[BundleEntry]: ...
    async def list_spaces_for_author(
        self, author_user_id: str, author_pk: str
    ) -> builtins.list[str]: ...
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
        signed_item: dict | None = None,
        item_epoch: int | None = None,
    ) -> bool:
        """Store ``profile`` unless a row with a newer-or-equal ``ts`` is
        held. Returns ``True`` when it was stored.

        Without ``picture_webp``, the bytes already held are kept if the
        signed ``picture_hash`` is unchanged. Otherwise the row holds no
        picture until one arrives through :meth:`set_picture`.
        ``signed_item`` is the author-signed item and ``item_epoch`` its
        epoch, as a bundle forwards them. The space's oldest rows past
        :data:`MAX_CACHED_AUTHORS_PER_SPACE` are pruned."""
        changed = await self._db.enqueue_rowcount(
            "INSERT INTO space_author_profiles(space_id, author_user_id, "
            "author_pk, display_name, bio, picture_webp, picture_hash, "
            "profile_rev, ts, signed_item, item_epoch) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
            "signed_item=excluded.signed_item, item_epoch=excluded.item_epoch "
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
                json.dumps(signed_item, separators=(",", ":"))
                if signed_item is not None
                else None,
                item_epoch,
            ),
        )
        if changed:
            await self._db.enqueue(
                "DELETE FROM space_author_profiles WHERE space_id=? AND "
                "author_user_id NOT IN (SELECT author_user_id FROM "
                "space_author_profiles WHERE space_id=? ORDER BY ts DESC LIMIT ?)",
                (profile.space_id, profile.space_id, MAX_CACHED_AUTHORS_PER_SPACE),
            )
        return bool(changed)

    async def set_picture(
        self,
        space_id: str,
        author_user_id: str,
        picture_hash: str,
        picture_webp: bytes,
    ) -> bool:
        """Attach picture bytes to the held row, but only while its signed
        hash is ``picture_hash``. Returns ``True`` when a row took them."""
        changed = await self._db.enqueue_rowcount(
            "UPDATE space_author_profiles SET picture_webp=? "
            "WHERE space_id=? AND author_user_id=? AND picture_hash=?",
            (picture_webp, space_id, author_user_id, picture_hash),
        )
        return bool(changed)

    async def get(
        self, space_id: str, author_user_id: str
    ) -> CachedAuthorProfile | None:
        """The held row with its picture state, or ``None``."""
        row = await self._db.fetchone(
            f"SELECT {_COLS}, picture_webp IS NOT NULL AS has_picture, signed_item "
            "FROM space_author_profiles WHERE space_id=? AND author_user_id=?",
            (space_id, author_user_id),
        )
        if row is None:
            return None
        data = dict(row)
        item = _load_item(data.get("signed_item"))
        sidecar = item.get(PROFILE_PICTURE_FIELD) if item is not None else None
        return CachedAuthorProfile(
            profile=_to_profile(data),
            has_picture=bool(data["has_picture"]),
            picture_sidecar=sidecar if isinstance(sidecar, str) and sidecar else None,
        )

    async def list_signed_items(
        self, space_id: str, *, limit: int
    ) -> builtins.list[BundleEntry]:
        """The author-signed items held for ``space_id``, at most ``limit``,
        the most recently stamped first. Each comes with its own epoch and
        its signed picture hash."""
        rows = await self._db.fetchall(
            "SELECT signed_item, item_epoch, picture_hash FROM space_author_profiles "
            "WHERE space_id=? AND signed_item IS NOT NULL AND item_epoch IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (space_id, limit),
        )
        out: builtins.list[BundleEntry] = []
        for row in rows:
            item = _load_item(row["signed_item"])
            if item is not None:
                out.append(
                    BundleEntry(
                        epoch=int(row["item_epoch"]),
                        item=item,
                        picture_hash=row["picture_hash"],
                    )
                )
        return out

    async def list_spaces_for_author(
        self, author_user_id: str, author_pk: str
    ) -> builtins.list[str]:
        """The spaces holding a profile of this author under this
        household key, i.e. where a household's own author has published
        one."""
        rows = await self._db.fetchall(
            "SELECT space_id FROM space_author_profiles "
            "WHERE author_user_id=? AND author_pk=?",
            (author_user_id, author_pk),
        )
        return [str(r["space_id"]) for r in rows]

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


def _load_item(raw: object) -> dict | None:
    if not isinstance(raw, str):
        return None
    try:
        item = json.loads(raw)
    except ValueError:
        return None
    return item if isinstance(item, dict) else None


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
