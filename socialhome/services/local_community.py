"""The one local-community check every seat-granting path runs.

A local community (``domain/local_community``) is a public space bound to
an area; a household may take a seat only while its home lies inside.
:func:`require_local_eligibility` is that check, run by the HOUSEHOLD
HOSTING the space on every path that grants a seat — open join, a join
request, follow, an invite token / link redeem (local and over the wire —
direct, mesh-routed or relayed), an invite accept, a direct add. Nothing
else decides it.

**On the point the join carries** (owner decision 2026-10-19). The host
never reads a home it was given for something else (pairing's
``share_home``): every join from another household carries that
household's home rounded to about 1 km
(:data:`~socialhome.domain.local_community.JOIN_LOCATION_PRECISION_DP`),
sent with the person's consent, inside the join's own sealed payload, to
the host only. The host decides on it and keeps nothing but the verdict
(``space_join_requests.area_verdict`` on a request an admin still has to
look at). A local user's join is checked against this household's own home,
rounded the same way.

* clearly inside → the seat proceeds as it always did;
* clearly outside → refused (:class:`OutsideAreaError`);
* near the edge (the ~1 km cell straddles the rim) → never decided
  automatically: a step a person started becomes a pending join request
  for the admins, whatever the join mode, answered like any request; an
  admin's own step (an invite, a direct add, an approval) is that decision.

No point → :class:`LocationRequiredError` (a remote join: the SPA asks for
consent and sends it again) or :class:`HomeNotSetError` (this household has
no home set) — nothing decided, nothing counted.

Every check a PERSON starts is budgeted (:class:`LocalAreaBudget`, kept in
SQLite so a restart refills nothing) — spam control: a person's join retries
and a request flood, not an oracle (no answer is exact).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from ..domain.events import RemoteSpaceJoinRequestReceived
from ..domain.local_community import (
    Eligibility,
    LocalArea,
    coarse_join_point,
    join_location_wire,
    join_verdict,
    local_area_of,
)
from ..domain.space import (
    HomeNotSetError,
    LocalAreaRateLimitedError,
    LocationRequiredError,
    OutsideAreaError,
    SpaceRole,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.local_area_attempt_repo import (
    AbstractLocalAreaAttemptRepo,
    BudgetLine,
)

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_repo import AbstractSpaceRepo

__all__ = [
    "APPROVAL_HONOURED_DAYS",
    "MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD",
    "MAX_LOCAL_AREA_CHECKS_PER_PERSON",
    "LocalAreaBudget",
    "file_remote_join_request",
    "hosted_local_area",
    "join_location_for",
    "own_home_point",
    "pending_join_request_id",
    "require_local_eligibility",
]

log = logging.getLogger(__name__)

#: How many local-community checks one PERSON may start per community per
#: UTC day — open join, join request, follow, invite accept, link redeem;
#: successful ones and the ones that turn into an admin review count too.
#: Admin-started steps (invite, direct add, approval) are not counted. No
#: answer is exact, so this only stops spam.
MAX_LOCAL_AREA_CHECKS_PER_PERSON: Final = 10
#: The backstop per REMOTE household per community per UTC day: the user id
#: in a remote join is the household's to choose (the §24.11 envelope
#: authenticates the household, not the person), so fresh ids must not buy
#: an unbounded flood.
MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD: Final = 20
#: How long an admin's approval of a person's join request seats that
#: person on their next redeem without another check: the redeem of the
#: token the approval minted, or — for a household the approval cannot be
#: delivered to (met through a relayed invite link) — the same link opened
#: again. As long as a pending request lives.
APPROVAL_HONOURED_DAYS: Final = 7

#: Budget kinds (``local_area_attempts.subject_kind``, migration 0109).
_KIND_PERSON: Final = "person"
_KIND_HOUSEHOLD: Final = "household"


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

    async def spend_seat_check(
        self, space_id: str, *, household: str, user_id: str, remote: bool
    ) -> bool:
        """Spend one check of ``user_id`` at ``household`` on ``space_id``
        (and one of the household's, when ``remote``). ``False`` — nothing
        spent — when either budget is used up today. A person is stored as
        ``sha256(household \\n user id)``, never the raw id."""
        person = hashlib.sha256(f"{household}\n{user_id}".encode()).hexdigest()
        lines: list[BudgetLine] = [
            (_KIND_PERSON, person, MAX_LOCAL_AREA_CHECKS_PER_PERSON)
        ]
        if remote:
            lines.append(
                (_KIND_HOUSEHOLD, household, MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD)
            )
        today = self._clock().astimezone(UTC).date()
        return await self._repo.try_spend(
            space_id,
            today.isoformat(),
            lines,
            prune_before=(today - timedelta(days=1)).isoformat(),
        )


def hosted_local_area(space: Space, own_instance_id: str | None) -> LocalArea | None:
    """``space``'s area when it is a local community THIS household hosts
    (the only kind it checks), else ``None``."""
    if not own_instance_id or space.owner_instance_id != own_instance_id:
        return None
    return local_area_of(space)


async def own_home_point(
    federation_repo: AbstractFederationRepo | None,
) -> tuple[float, float] | None:
    """This household's home rounded to the join precision — what a join it
    sends carries, and what its own people's joins are checked with — or
    ``None`` when no home is set."""
    if federation_repo is None:
        return None
    identity = await federation_repo.get_local_identity()
    if identity is None:
        return None
    try:
        return coarse_join_point(identity.get("home_lat"), identity.get("home_lon"))
    except ValueError:
        return None


async def join_location_for(
    federation_repo: AbstractFederationRepo | None, *, share_location: bool
) -> dict[str, float] | None:
    """The JOINER's side: the ``join_location`` a join to another household
    carries — this household's home rounded to ~1 km — only when the person
    agreed (``share_location``), else ``None`` and nothing about the home
    leaves. Agreed but no home set → :class:`HomeNotSetError`."""
    if not share_location:
        return None
    point = await own_home_point(federation_repo)
    if point is None:
        raise HomeNotSetError(params={"whose": "own"})
    return join_location_wire(point)


async def require_local_eligibility(
    space: Space,
    *,
    household_instance_id: str,
    own_instance_id: str | None,
    join_point: tuple[float, float] | None,
    budget: LocalAreaBudget | None,
    seeker_user_id: str | None = None,
) -> Eligibility | None:
    """Decide a seat in ``space`` for ``household_instance_id`` on
    ``join_point`` — the coarse home the join carried (a remote household)
    or this household's own (:func:`own_home_point`, a local user).

    ``None`` — not a local community this household hosts: no check.
    :attr:`Eligibility.INSIDE` / :attr:`Eligibility.EDGE` — proceed; on
    EDGE a step a person started (``seeker_user_id`` set) files a join
    request for the admins instead of seating, while an admin's own step
    (``seeker_user_id`` ``None``) is that decision. Clearly outside raises
    :class:`OutsideAreaError` (403 ``OUTSIDE_AREA``).

    No ``join_point``: :class:`LocationRequiredError` for a remote
    household (the join came without the person's consent to send it) or
    :class:`HomeNotSetError` for this one — nothing spent.

    A person's check is answered only while their daily budget
    (:data:`MAX_LOCAL_AREA_CHECKS_PER_PERSON`; for a remote person also
    their household's, :data:`MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD`) has
    room — else :class:`LocalAreaRateLimitedError` (429). A missing
    ``budget`` for a person's check fails closed (:class:`RuntimeError`).
    The point is never logged or stored here.
    """
    area = hosted_local_area(space, own_instance_id)
    if area is None:
        return None
    remote = household_instance_id != own_instance_id
    # ``whose`` lets the SPA word it for the person reading it: ``own`` —
    # this household's home; ``peer`` — another household's.
    whose = "peer" if remote else "own"
    if join_point is None:
        log.info(
            "local community %s: no location with a join from household %s",
            space.id,
            household_instance_id,
        )
        if remote:
            raise LocationRequiredError(params={"whose": whose})
        raise HomeNotSetError(params={"whose": whose})
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
    verdict = join_verdict(area, join_point)
    log.info(
        "local community %s: household %s is %s",
        space.id,
        household_instance_id,
        verdict.value,
    )
    if verdict is Eligibility.OUTSIDE:
        raise OutsideAreaError(params={"whose": whose})
    return verdict


async def pending_join_request_id(
    space_repo: AbstractSpaceRepo,
    space_id: str,
    user_id: str,
    *,
    household_instance_id: str | None,
) -> str | None:
    """The id of ``user_id``'s pending join request for ``space_id``
    (``household_instance_id`` ``None`` for a local applicant), or
    ``None``. A pending admin elevation (``requested_role='admin'``, filed
    for somebody already seated) is not a join request."""
    for row in await space_repo.list_pending_join_requests(space_id):
        if (
            row.get("user_id") == user_id
            and (row.get("remote_applicant_instance_id") or None)
            == household_instance_id
            and row.get("requested_role") != SpaceRole.ADMIN.value
        ):
            return str(row["id"])
    return None


async def file_remote_join_request(
    space_repo: AbstractSpaceRepo,
    bus: EventBus,
    *,
    space_id: str,
    household_instance_id: str,
    user_id: str,
    area_verdict: Eligibility | None,
    applicant_pk: str | None = None,
    message: str | None = None,
    request_id: str | None = None,
) -> str:
    """File a pending join request of a REMOTE person for the admins — an
    inbound ``SPACE_JOIN_REQUEST``, or a remote step near the edge of a
    local community (a link redeem, an invite accept) turned into one. One
    shape either way, so the admins' queue and the notification never tell
    the two apart; ``area_verdict`` (inside / edge) is the only trace of the
    location the join carried."""
    rid = await space_repo.save_join_request(
        space_id,
        user_id,
        message=message,
        remote_applicant_instance_id=household_instance_id,
        remote_applicant_pk=applicant_pk,
        request_id=request_id,
        area_verdict=area_verdict.value if area_verdict is not None else None,
    )
    await bus.publish(
        RemoteSpaceJoinRequestReceived(space_id=space_id, requester_user_id=user_id)
    )
    return rid
