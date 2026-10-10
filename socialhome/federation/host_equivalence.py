"""Who may act as a space's HOST on this household (migration 0091).

The host is the space's owner household (``spaces.owner_instance_id``). One
more household is host-equivalent, on a MEMBER household only: the delegated
admin household that issued THIS household's invite
(``spaces.invited_by_instance``, written by the §D1b join paths) — owner
decision 2026-10-10, "trust the inviting admin for sync". A household an
admin invited may reach the space only through that admin while the owner is
offline; treating the admin as host there keeps its catch-up stream and its
admin actions working as they did when such a stub named the admin as owner.

The trust is narrow:

* only the household recorded as THIS household's inviter, only for that
  space;
* only while the space's ``delegated_admin_authority`` is ON (the owner's
  own opt-in to admins acting for it) — turning it off ends the trust;
* only while that household still holds a live ``admin`` seat here.

It covers CONTENT and admin actions — the full §25.6 stream (roster and bans
included), role changes, remote-member removals, proposal mirrors, the
signing-seed share, the host writer exemption. It never covers owner-only
settings: the owner-signed GFS list, ``private_gfs``, ``gfs_publish_mode``,
the authority cert / ``host_identity_pk``, the config, dissolve and the age
gate keep comparing against ``owner_instance_id`` alone. Nothing changes for
the owner itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.space import SpaceRole

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo


async def is_trusted_inviter(
    space_repo: "AbstractSpaceRepo",
    seat_repo: "AbstractSpaceRemoteMemberRepo | None",
    space: "Space | None",
    sender: str,
) -> bool:
    """*sender* is the delegated admin household that invited this household
    into *space*, delegation is on, and it still holds a live admin seat."""
    if (
        space is None
        or not sender
        or seat_repo is None
        or sender == space.owner_instance_id
    ):
        return False
    # Fail closed on a row that carries no feature flags at all.
    features = getattr(space, "features", None)
    if not getattr(features, "delegated_admin_authority", False):
        return False
    if await space_repo.get_invited_by(space.id) != sender:
        return False
    seats = await seat_repo.list_for_instance(
        space.id, sender, include_tombstoned=False
    )
    return any(seat.role == SpaceRole.ADMIN.value for seat in seats)


async def acts_as_host(
    space_repo: "AbstractSpaceRepo",
    seat_repo: "AbstractSpaceRemoteMemberRepo | None",
    space: "Space | None",
    sender: str,
) -> bool:
    """*sender* may act as the host of *space* here: its owner, or the
    trusted inviting admin (:func:`is_trusted_inviter`)."""
    if space is None or not sender:
        return False
    if space.owner_instance_id == sender:
        return True
    return await is_trusted_inviter(space_repo, seat_repo, space, sender)


__all__ = ["acts_as_host", "is_trusted_inviter"]
