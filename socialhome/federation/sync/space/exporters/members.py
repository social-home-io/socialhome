"""Members exporter — ``space_members`` rows."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from .....domain.space import SpaceRole
from ....host_equivalence import touches_owner_household

if TYPE_CHECKING:
    from .....repositories.profile_picture_repo import AbstractProfilePictureRepo
    from .....repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
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

    The owner household's people (the owner seat, any seat its household
    holds) are the OWNER's to stream: a household that does not own the
    space — a delegated admin that synced with the owner holds those rows
    too — leaves them out (``own_instance_id`` given). A receiver drops
    them from anybody but the owner anyway, with a WARNING
    (``SpaceSyncReceiver._spare_owner_people``) that an honest stream must
    not trip.
    """

    resource = "members"

    __slots__ = ("_own_instance_id", "_pictures", "_repo", "_seats")

    def __init__(
        self,
        space_repo: AbstractSpaceRepo,
        profile_picture_repo: AbstractProfilePictureRepo | None = None,
        *,
        own_instance_id: str | None = None,
        remote_member_repo: AbstractSpaceRemoteMemberRepo | None = None,
    ) -> None:
        self._repo = space_repo
        self._pictures = profile_picture_repo
        self._own_instance_id = own_instance_id
        self._seats = remote_member_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        members = await self._repo.list_members(space_id)
        space = await self._repo.get(space_id) if self._own_instance_id else None
        if space is not None and space.owner_instance_id != self._own_instance_id:
            members = [
                m
                for m in members
                if not await touches_owner_household(
                    self._repo, self._seats, space, user_id=m.user_id
                )
            ]
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

    def __init__(self, inner: ResourceExporter) -> None:
        self._inner = inner

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return [
            {**r, "role": SpaceRole.MEMBER.value}
            if r.get("role") == SpaceRole.MODERATOR.value
            else r
            for r in await self._inner.list_records(space_id)
        ]
