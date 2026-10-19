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

Every check a PERSON starts is budgeted (:class:`LocalAreaBudget`, kept in
SQLite so a restart refills nothing): a few a UTC day per person and
community, successful ones included, plus a backstop per remote household;
see :data:`MAX_LOCAL_AREA_CHECKS_PER_PERSON`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import struct
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
from ..repositories.local_area_attempt_repo import (
    AbstractLocalAreaAttemptRepo,
    BudgetLine,
)

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.federation_repo import AbstractFederationRepo

__all__ = [
    "MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD",
    "MAX_LOCAL_AREA_CHECKS_PER_PERSON",
    "MAX_VERDICT_HOMES_PER_DAY",
    "LocalAreaBudget",
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


#: How many local-community checks one PERSON may start per community per
#: UTC day — open join, join request, follow, invite accept, link redeem;
#: successful ones count too (join → leave → move the home → rejoin would
#: otherwise be an exact "inside?" answer each time). Admin-started checks
#: (invite, direct add, approval) are not counted.
MAX_LOCAL_AREA_CHECKS_PER_PERSON: Final = 3
#: The backstop per REMOTE household per community per UTC day: the user id
#: in a remote request is the household's to choose (the §24.11 envelope
#: authenticates the household, not the person), so fresh ids must not buy
#: fresh answers. Above a few people's budgets, so one prober cannot use up
#: a whole household's day.
MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD: Final = 10
#: How many different shared homes of one paired household the directory
#: verdicts follow per UTC day
#: (:class:`~socialhome.services.peer_directory_service.PeerDirectoryService`).
#: A home rarely moves; one that moves over and over is a probe.
MAX_VERDICT_HOMES_PER_DAY: Final = 4

#: Budget kinds (``local_area_attempts.subject_kind``, migration 0109).
_KIND_PERSON: Final = "person"
_KIND_HOUSEHOLD: Final = "household"
_KIND_VERDICT_HOME: Final = "verdict_home"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LocalAreaBudget:
    """The local-community daily budgets, kept in SQLite
    (``local_area_attempts``) so a restart forgets nothing.

    Days are UTC calendar days: a budget refills at 00:00 UTC.
    """

    __slots__ = ("_clock", "_repo")

    def __init__(
        self,
        repo: AbstractLocalAreaAttemptRepo,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo = repo
        self._clock = clock

    def _days(self) -> tuple[str, str]:
        """Today, and the first day still kept (yesterday)."""
        today = self._clock().astimezone(UTC).date()
        return today.isoformat(), (today - timedelta(days=1)).isoformat()

    async def spend_seat_check(
        self, space_id: str, *, household: str, user_id: str, remote: bool
    ) -> bool:
        """Spend one check of ``user_id`` at ``household`` on ``space_id``
        (and one of the household's, when ``remote``). ``False`` — nothing
        spent — when either budget is used up today."""
        person = hashlib.sha256(f"{household}\n{user_id}".encode()).hexdigest()
        lines: list[BudgetLine] = [
            (_KIND_PERSON, person, MAX_LOCAL_AREA_CHECKS_PER_PERSON)
        ]
        if remote:
            lines.append(
                (_KIND_HOUSEHOLD, household, MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD)
            )
        today, keep_from = self._days()
        return await self._repo.try_spend(
            space_id, today, lines, prune_before=keep_from
        )

    async def take_verdict_home(self, peer_id: str) -> bool:
        """Spend one of ``peer_id``'s new shared homes for today's directory
        verdicts; ``False`` when it already moved
        :data:`MAX_VERDICT_HOMES_PER_DAY` times today."""
        today, keep_from = self._days()
        return await self._repo.try_spend(
            "",
            today,
            [(_KIND_VERDICT_HOME, peer_id, MAX_VERDICT_HOMES_PER_DAY)],
            prune_before=keep_from,
        )


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
    seeker_user_id: str | None = None,
    budget: LocalAreaBudget | None = None,
) -> None:
    """Raise unless ``household_instance_id`` may take a seat in ``space``.

    A no-op for a space that is not a local community, and for one another
    household hosts (the host checks). Otherwise the household's home must
    be known here (:class:`HomeNotSharedError`, 403 ``HOME_NOT_SHARED``)
    and inside the area (:class:`OutsideAreaError`, 403 ``OUTSIDE_AREA``),
    on the exact distance.

    ``seeker_user_id`` — the person who started this check (a join, a
    request, a follow, an accept, a redeem); ``None`` for an admin's own
    step (an invite, a direct add, an approval). A person's check is
    answered only while their daily budget (:class:`LocalAreaBudget`,
    :data:`MAX_LOCAL_AREA_CHECKS_PER_PERSON`; for a remote person also
    their household's, :data:`MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD`) has
    room — else :class:`LocalAreaRateLimitedError` (429
    ``LOCAL_AREA_RATE_LIMITED``) and no answer. Inside and outside alike
    spend one; "home unknown" spends nothing (it says nothing about where
    the area is).
    """
    area = local_area_of(space)
    if area is None:
        return
    if not own_instance_id or space.owner_instance_id != own_instance_id:
        return
    remote = household_instance_id != own_instance_id
    # ``whose`` lets the SPA word it for the person reading it: ``own`` —
    # this household's home (a local user, or an admin seating one);
    # ``peer`` — another household's (an admin inviting or approving it, or
    # a remote person's request the host refuses).
    whose = "peer" if remote else "own"
    home = await household_home(
        federation_repo, household_instance_id, own_instance_id=own_instance_id
    )
    verdict = eligibility(area, home)
    # Never the coordinates in the log: which household, and why.
    if verdict is Eligibility.HOME_UNKNOWN:
        log.info(
            "local community %s: refused a seat for household %s (home unknown)",
            space.id,
            household_instance_id,
        )
        raise HomeNotSharedError(params={"whose": whose})
    if seeker_user_id is not None:
        if budget is None:
            # Fail closed: an unwired budget must never mean "unlimited".
            raise RuntimeError("local community check without a LocalAreaBudget")
        if not await budget.spend_seat_check(
            space.id,
            household=household_instance_id,
            user_id=seeker_user_id,
            remote=remote,
        ):
            log.info(
                "local community %s: held off a person of household %s "
                "(daily check budget used up)",
                space.id,
                household_instance_id,
            )
            raise LocalAreaRateLimitedError(params={"whose": whose})
    if verdict is Eligibility.INSIDE:
        return
    log.info(
        "local community %s: refused a seat for household %s (%s)",
        space.id,
        household_instance_id,
        verdict.value,
    )
    raise OutsideAreaError(params={"whose": whose})
