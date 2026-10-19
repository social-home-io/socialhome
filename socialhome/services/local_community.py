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

import hashlib
import hmac
import logging
import struct
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

from ..domain.local_community import (
    Eligibility,
    LocalArea,
    edge_cell,
    edge_max_shift_km,
    eligibility,
    local_area_of,
)
from ..domain.space import (
    HomeNotSharedError,
    LocalAreaRateLimitedError,
    OutsideAreaError,
)

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.federation_repo import AbstractFederationRepo

__all__ = [
    "MAX_OUTSIDE_AREA_REFUSALS",
    "OUTSIDE_AREA_ATTEMPTS",
    "OUTSIDE_AREA_WINDOW_SECONDS",
    "OutsideAreaAttempts",
    "derive_edge_key",
    "edge_shift_km",
    "household_home",
    "require_local_eligibility",
]

log = logging.getLogger(__name__)

#: HKDF domain separation for the directory verdict's band-shift key
#: (:func:`derive_edge_key`). Bumping the version redraws every shift.
_EDGE_KEY_INFO: bytes = b"sh-local-community-edge-v1"


def derive_edge_key(identity_seed: bytes) -> bytes:
    """The host's secret for :func:`edge_shift_km`, HKDF-Expanded from the
    household identity seed (the ``media_signer`` pattern): stable across
    restarts, never stored, never sent, and unknown to every other
    household — so nobody can predict where the "unsure" band sits."""
    if not identity_seed:
        raise ValueError("identity_seed must be non-empty")
    hkdf = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=_EDGE_KEY_INFO)
    return hkdf.derive(identity_seed)


def edge_shift_km(
    key: bytes, space_id: str, area: LocalArea, home: tuple[float, float]
) -> float:
    """The host's secret slide of the "unsure" band for the grid cell
    ``home`` lies in (:func:`~socialhome.domain.local_community.edge_cell`),
    within ± :func:`~socialhome.domain.local_community.edge_max_shift_km`.
    One draw per (space, radius, cell): every home in a cell gets the same
    one, so probing a spot again learns nothing new, and a neighbouring
    cell's draw says nothing about this one's."""
    row, col = edge_cell(area, home)
    msg = b"|".join(
        (
            space_id.encode(),
            repr(round(float(area.radius_km), 1)).encode(),
            struct.pack(">qq", row, col),
        )
    )
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    unit = int.from_bytes(digest[:8], "big") / float(1 << 64)  # [0, 1)
    return (unit * 2 - 1) * edge_max_shift_km(area.radius_km)


def _coords(lat: object, lon: object) -> tuple[float, float] | None:
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


#: How many OUTSIDE_AREA refusals one paired household may get for one
#: local community within :data:`OUTSIDE_AREA_WINDOW_SECONDS` …
MAX_OUTSIDE_AREA_REFUSALS: Final = 3
#: … a rolling day.
OUTSIDE_AREA_WINDOW_SECONDS: Final = 24 * 60 * 60


class OutsideAreaAttempts:
    """Counts OUTSIDE_AREA refusals per (local community, paired household).

    A seat attempt is answered with the exact distance (the directory's
    "unsure" band is for showing, not for deciding), so a household moving
    its shared home and asking again and again could find the rim — and
    the centre — that way. After :data:`MAX_OUTSIDE_AREA_REFUSALS` in a
    rolling :data:`OUTSIDE_AREA_WINDOW_SECONDS` the host stops looking at
    that household's home for that community
    (:class:`~socialhome.domain.space.LocalAreaRateLimitedError`) until the
    oldest refusal ages out. In memory only: a restart forgets it, which
    costs a prober a host restart it does not control.
    """

    __slots__ = ("_clock", "_refusals")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._refusals: dict[tuple[str, str], deque[float]] = {}

    def _recent(self, key: tuple[str, str]) -> deque[float]:
        window = self._refusals.get(key)
        if window is None:
            return deque()
        cutoff = self._clock() - OUTSIDE_AREA_WINDOW_SECONDS
        while window and window[0] <= cutoff:
            window.popleft()
        if not window:
            del self._refusals[key]
        return window

    def held_off(self, space_id: str, household: str) -> bool:
        """Has ``household`` used up its refusals for ``space_id``?"""
        return len(self._recent((space_id, household))) >= MAX_OUTSIDE_AREA_REFUSALS

    def record_refusal(self, space_id: str, household: str) -> None:
        key = (space_id, household)
        self._recent(key)
        self._refusals.setdefault(key, deque()).append(self._clock())

    def clear(self) -> None:
        self._refusals.clear()


#: The process-wide counter every seat path shares (tests reset it).
OUTSIDE_AREA_ATTEMPTS: Final = OutsideAreaAttempts()


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
    attempts: OutsideAreaAttempts | None = None,
) -> None:
    """Raise unless ``household_instance_id`` may take a seat in ``space``.

    A no-op for a space that is not a local community, and for one another
    household hosts (the host checks). Otherwise the household's home must
    be known here (:class:`HomeNotSharedError`, 403 ``HOME_NOT_SHARED``)
    and inside the area (:class:`OutsideAreaError`, 403 ``OUTSIDE_AREA``),
    on the exact distance. Another household refused as outside too often
    today is held off without its home being looked at
    (:class:`LocalAreaRateLimitedError`, 429 ``LOCAL_AREA_RATE_LIMITED``;
    ``attempts`` defaults to :data:`OUTSIDE_AREA_ATTEMPTS`).
    """
    area = local_area_of(space)
    if area is None:
        return
    if not own_instance_id or space.owner_instance_id != own_instance_id:
        return
    counter = OUTSIDE_AREA_ATTEMPTS if attempts is None else attempts
    # Our own people see the centre anyway: only another household is
    # counted, and only "outside" answers (what traces the rim).
    remote = household_instance_id != own_instance_id
    if remote and counter.held_off(space.id, household_instance_id):
        log.info(
            "local community %s: held off household %s (refused as outside "
            "too often today)",
            space.id,
            household_instance_id,
        )
        raise LocalAreaRateLimitedError(params={"whose": "peer"})
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
    if remote:
        counter.record_refusal(space.id, household_instance_id)
    raise OutsideAreaError(params={"whose": whose})
