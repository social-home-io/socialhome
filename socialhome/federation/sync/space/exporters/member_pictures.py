"""Member-pictures exporter — per-space WebP avatars for catch-up (F6).

The ``members`` resource ships ``picture_hash`` for every member, but
the actual bytes live in ``space_member_profile_pictures`` (DB BLOB,
not the media filesystem). Realtime
:data:`FederationEventType.SPACE_MEMBER_PROFILE_UPDATED` inlines the
WebP as ``picture_webp_base64``; this exporter does the same for the
§25.6 catch-up path so a new joiner sees existing members' avatars
instead of broken ``<img>`` fallbacks.

Bytes are kept small by the upload pipeline (≤ 256x256 WebP, typically
< 30 KB each) so inlining base64 in chunks doesn't blow the 8 KB budget
by much per record — :class:`ChunkBuilder` halves the chunk size until
it fits.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .....repositories.profile_picture_repo import (
        AbstractProfilePictureRepo,
    )
    from .....repositories.space_repo import AbstractSpaceRepo


class MemberPicturesExporter:
    resource = "member_pictures"

    __slots__ = ("_picture_repo", "_space_repo")

    def __init__(
        self,
        space_repo: AbstractSpaceRepo,
        profile_picture_repo: AbstractProfilePictureRepo,
    ) -> None:
        self._space_repo = space_repo
        self._picture_repo = profile_picture_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        members = await self._space_repo.list_members(space_id)
        out: list[dict[str, Any]] = []
        for m in members:
            if not m.picture_hash:
                continue
            pic = await self._picture_repo.get_member_picture(
                space_id,
                m.user_id,
            )
            if pic is None:
                continue
            bytes_webp, hash_ = pic
            out.append(_record(space_id, m.user_id, bytes_webp, hash_))
        return out

    async def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The pictures changed after ``since`` (§25.6 incremental, migration
        0122), of members who still use a per-space picture — the full
        stream's rule. Re-applying a picture held unchanged stamps nothing,
        so a quiet space streams none."""
        using = {
            m.user_id
            for m in await self._space_repo.list_members(space_id)
            if m.picture_hash
        }
        yield [
            _record(space_id, user_id, bytes_webp, hash_)
            for user_id, bytes_webp, hash_ in (
                await self._picture_repo.list_member_pictures_changed(space_id, since)
            )
            if user_id in using
        ]


def _record(
    space_id: str, user_id: str, bytes_webp: bytes, hash_: str
) -> dict[str, Any]:
    return {
        "space_id": space_id,
        "user_id": user_id,
        "picture_hash": hash_,
        "picture_webp_base64": base64.b64encode(bytes_webp).decode("ascii"),
    }
