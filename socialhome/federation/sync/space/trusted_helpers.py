"""Which households a member asks when a space's host does not answer (v_92).

One picker for both v_92 fallbacks — the scheduler's helper
``SPACE_SYNC_RESUME`` (``scheduler.SpaceSyncScheduler``) and the
``SPACE_MEDIA_WANTED`` providers after the host
(``services.space_media_wanted.SpaceMediaWanted``):

* a household holding a LIVE ``admin`` / ``moderator`` seat in the roster
  mirror (the v_86 file fills' content authority; the owner is the host,
  asked first anyway);
* never this household nor the households acting as the space's host;
* only households at v_92 (``space_member_supports``: a paired row's
  version, else the mesh claim) — an older helper would answer a resume
  with everyone's rows and no ``SPACE_MEDIA_WANTED`` at all. No helper at
  v_92: the member waits for the host;
* sorted by instance id, then started at a stable per-household offset
  (:func:`helper_offset`), so the members of a silent space spread over its
  helpers instead of all asking the first one. Fixed, never random.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Protocol

from ....domain.federation_capabilities import FederationCapability
from ....domain.space import SpaceRole

#: The seats a trusted helper holds.
HELPER_ROLES: frozenset[str] = frozenset(
    {SpaceRole.ADMIN.value, SpaceRole.MODERATOR.value}
)


class _Seats(Protocol):
    async def list_instances_with_roles(
        self, space_id: str, roles: frozenset[str]
    ) -> list[str]: ...


class _Versions(Protocol):
    async def space_member_supports(
        self, instance_id: str, *, min_version: int
    ) -> bool: ...


def helper_offset(own_instance_id: str, space_id: str) -> int:
    """Where *own_instance_id* starts in *space_id*'s sorted helper list:
    stable yet different per household."""
    digest = hashlib.sha256(f"{own_instance_id}\0{space_id}".encode()).hexdigest()
    return int(digest, 16)


async def trusted_helpers(
    seats: _Seats,
    versions: _Versions,
    space_id: str,
    *,
    exclude: Iterable[str],
    offset: int = 0,
) -> list[str]:
    """The trusted helper households of *space_id* at v_92, sorted, minus
    *exclude* (this household, the space's hosts), rotated to start at
    *offset* (modulo their number)."""
    skip = set(exclude) | {""}
    ids = sorted(
        set(await seats.list_instances_with_roles(space_id, HELPER_ROLES)) - skip
    )
    out = [
        iid
        for iid in ids
        if await versions.space_member_supports(
            iid, min_version=FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED
        )
    ]
    if not out:
        return []
    start = offset % len(out)
    return out[start:] + out[:start]


__all__ = ["HELPER_ROLES", "helper_offset", "trusted_helpers"]
