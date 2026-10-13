"""Members exporter — ``space_members`` rows."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

from .....domain.space import SpaceRole

if TYPE_CHECKING:
    from .....repositories.profile_picture_repo import AbstractProfilePictureRepo
    from .....repositories.space_repo import AbstractSpaceRepo
    from ..exporter import ResourceExporter


class MembersExporter:
    """One record per seat (``asdict(SpaceMember)``).

    ``picture_hash`` is our hash of the seat's per-space picture, and since
    v_58 a ``null`` is a reset the receiver acts on (it drops its copy).
    So ``null`` ships only when this household holds no picture blob for
    the seat; a seat with no hash but a blob (an old wipe whose blob hash
    could not be repaired — migration 0099) leaves the key out, which a
    receiver reads as "says nothing". Without a picture repo the blob
    state is unknown, so a ``null`` is never claimed.
    """

    resource = "members"

    __slots__ = ("_repo", "_pictures")

    def __init__(
        self,
        space_repo: "AbstractSpaceRepo",
        profile_picture_repo: "AbstractProfilePictureRepo | None" = None,
    ) -> None:
        self._repo = space_repo
        self._pictures = profile_picture_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        members = await self._repo.list_members(space_id)
        records = [asdict(m) for m in members]
        if not any(r["picture_hash"] is None for r in records):
            return records
        held = (
            await self._pictures.list_member_picture_user_ids(space_id)
            if self._pictures is not None
            else None
        )
        for r in records:
            if r["picture_hash"] is None and (held is None or r["user_id"] in held):
                del r["picture_hash"]
        return records


class PreModeratorMembersExporter:
    """The members stream for a requester below v_41.

    Such a receiver writes each record into its own ``space_members``,
    whose role CHECK predates ``moderator`` — one moderator row would raise
    and lose the rest of the chunk. So a moderator ships as ``member``: the
    role it knows that grants no more.
    """

    __slots__ = ("_inner",)

    resource = "members"

    def __init__(self, inner: "ResourceExporter") -> None:
        self._inner = inner

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return [
            {**r, "role": SpaceRole.MEMBER.value}
            if r.get("role") == SpaceRole.MODERATOR.value
            else r
            for r in await self._inner.list_records(space_id)
        ]
