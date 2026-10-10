"""Who may act as a space's HOST on this household (migration 0093).

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
  space — and the join paths record one ONLY when the owner's signed
  statement named the owner and the inviter proved it holds the space seed
  (:func:`socialhome.services.space_service.resolve_join_owner`);
* only while the space's ``delegated_admin_authority`` is ON (the owner's
  own opt-in to admins acting for it) — turning it off ends the trust;
* only while that household still holds a live ``admin`` seat here;
* only for the authority-key epoch the inviter PROVED it holds the seed of
  (``spaces.invited_by_epoch``): after a rotation, only a fresh inviter
  proof for the new epoch resumes it.

It covers CONTENT and admin actions — the full §25.6 stream (roster and bans
included), role changes, live bans / unbans, remote-member removals,
proposal mirrors, the
signing-seed share, the host writer exemption. It never covers owner-only
settings: the owner-signed GFS list, ``private_gfs``, ``gfs_publish_mode``,
the authority cert / ``host_identity_pk``, the config, dissolve and the age
gate keep comparing against ``owner_instance_id`` alone, and even a trusted
inviter never acts on a user of the owner household
(:func:`touches_owner_household`). The inviter's admin seat came from its own
roster, so the trust truly ends when the owner's demotion or delegation-off
reaches this household. Nothing changes for the owner itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.space import SpaceRole
from ..space_owner_statement import MAX_STATEMENT_AGE_MS

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo


def owner_statement_current(space: "Space", *, now_ms: int | None = None) -> bool:
    """Whether the owner statement this household holds for *space* (already
    verified when stored) names the authority key and epoch the space pins
    and is younger than :data:`MAX_STATEMENT_AGE_MS`."""
    if not space.owner_statement:
        return False
    try:
        held = json.loads(space.owner_statement)
        issued_at = int(held["issued_at"])
        same_key = (
            str(held["authority_pk"]).lower()
            == str(space.identity_public_key or "").lower()
        )
        same_epoch = int(held["authority_key_epoch"]) == space.authority_key_epoch
    except ValueError, TypeError, KeyError:
        return False
    now = (
        int(datetime.now(timezone.utc).timestamp() * 1000) if now_ms is None else now_ms
    )
    return same_key and same_epoch and issued_at >= now - MAX_STATEMENT_AGE_MS


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
    if await space_repo.get_invited_by_epoch(space.id) != space.authority_key_epoch:
        # The inviter proved the seed of ANOTHER key epoch. After a rotation
        # the trust resumes only on a fresh proof for the new epoch (an
        # honest admin's §25.6 sentinel carries one) — never because the
        # pin moved: an admin the rotation revoked lacks the new seed.
        return False
    if not owner_statement_current(space):
        # The owner's word for the key we pin has lapsed (or the pin moved):
        # an admin revoked by a rotation must not stay trusted on the old
        # key. A refreshed statement from the owner restores it.
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


async def touches_owner_household(
    space_repo: "AbstractSpaceRepo",
    seat_repo: "AbstractSpaceRemoteMemberRepo | None",
    space: "Space",
    *,
    user_id: str,
    instance_id: str | None = None,
) -> bool:
    """Whether an admin action names a user of the OWNER household — the
    space's owner seat, or any seat the owner's household holds. A trusted
    inviting admin is host-equivalent for admin actions, but never over the
    owner's own people: such an action from it is refused (only the owner
    decides about its household's seats)."""
    if instance_id and instance_id == space.owner_instance_id:
        return True
    if user_id and user_id == await space_repo.get_owner_user_id(space.id):
        return True
    if seat_repo is not None and user_id:
        seat = await seat_repo.get(space.id, space.owner_instance_id, user_id)
        if seat is not None:
            return True
    return False


__all__ = [
    "acts_as_host",
    "is_trusted_inviter",
    "owner_statement_current",
    "touches_owner_household",
]
