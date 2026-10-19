"""The one local-community check every seat-granting path runs.

A local community (``domain/local_community``) is a public space bound to
an area; a household may hold a seat only while its home lies inside.
:func:`require_local_eligibility` is that check, run by the HOUSEHOLD
HOSTING the space on every path that grants a seat — open join, a join
request (filed and again when approved), follow, an invite token / link
redeem (local and over the wire), a direct invite (sent and again when
accepted), a direct add. Nothing else decides it.

Whose home:

* a LOCAL user of the host → this household's own home
  (``instance_identity.home_lat`` / ``home_lon``);
* a REMOTE household → the home it already shares with us through
  pairing (``remote_instances.home_lat`` / ``home_lon``, set by the pairing
  handshake and ``LOCAL_HOME_LOCATION_CHANGED``, cleared when that
  household turns ``share_home`` off toward us). A household with no row
  (met over the mesh) or one met through an invite link
  (``space_session``, which never receives a home) has none.

A home that is not known is refused (:class:`HomeNotSharedError`), never
assumed inside — and nobody is ever asked to send coordinates inside a
join request. A space hosted by another household is that household's to
check: on a member household's stub this is a no-op.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.local_community import Eligibility, eligibility, local_area_of
from ..domain.space import HomeNotSharedError, OutsideAreaError

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.federation_repo import AbstractFederationRepo

__all__ = ["household_home", "require_local_eligibility"]

log = logging.getLogger(__name__)


def _coords(lat: object, lon: object) -> tuple[float, float] | None:
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


async def household_home(
    federation_repo: AbstractFederationRepo | None,
    household_instance_id: str,
    *,
    own_instance_id: str | None,
) -> tuple[float, float] | None:
    """The home ``household_instance_id`` shares with this household, or
    ``None`` when it is not known here (see the module doc)."""
    if federation_repo is None or not household_instance_id:
        return None
    if own_instance_id and household_instance_id == own_instance_id:
        identity = await federation_repo.get_local_identity()
        if identity is None:
            return None
        return _coords(identity.get("home_lat"), identity.get("home_lon"))
    peer = await federation_repo.get_instance(household_instance_id)
    if peer is None:
        return None
    return _coords(peer.home_lat, peer.home_lon)


async def require_local_eligibility(
    space: Space,
    *,
    household_instance_id: str,
    own_instance_id: str | None,
    federation_repo: AbstractFederationRepo | None,
) -> None:
    """Raise unless ``household_instance_id`` may take a seat in ``space``.

    A no-op for a space that is not a local community, and for one another
    household hosts (the host checks). Otherwise the household's home must
    be known here (:class:`HomeNotSharedError`, 403 ``HOME_NOT_SHARED``)
    and inside the area (:class:`OutsideAreaError`, 403 ``OUTSIDE_AREA``).
    """
    area = local_area_of(space)
    if area is None:
        return
    if not own_instance_id or space.owner_instance_id != own_instance_id:
        return
    home = await household_home(
        federation_repo, household_instance_id, own_instance_id=own_instance_id
    )
    verdict = eligibility(area, home)
    if verdict is Eligibility.INSIDE:
        return
    # Never the coordinates in the log: which household was refused, and why.
    log.info(
        "local community %s: refused a seat for household %s (%s)",
        space.id,
        household_instance_id,
        verdict.value,
    )
    # ``whose`` lets the SPA word it for the person reading it: ``own`` —
    # this household's home (a local user, or an admin seating one);
    # ``peer`` — another household's (an admin inviting or approving it).
    whose = "own" if household_instance_id == own_instance_id else "peer"
    if verdict is Eligibility.HOME_UNKNOWN:
        raise HomeNotSharedError(params={"whose": whose})
    raise OutsideAreaError(params={"whose": whose})
