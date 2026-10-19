"""``services/local_community`` — the host's decision on the ~1 km point a
join carries, the joiner's side of sending it, and the daily budgets."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta, timezone

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import RemoteSpaceJoinRequestReceived
from socialhome.domain.local_community import Eligibility, coarse_join_point
from socialhome.domain.space import (
    HomeNotSetError,
    JoinMode,
    LocalAreaRateLimitedError,
    LocationRequiredError,
    OutsideAreaError,
    Space,
    SpaceFeatures,
    SpacePermissionError,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.local_area_attempt_repo import (
    SqliteLocalAreaAttemptRepo,
)
from socialhome.services.local_community import (
    MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD,
    MAX_LOCAL_AREA_CHECKS_PER_PERSON,
    LocalAreaBudget,
    file_remote_join_request,
    hosted_local_area,
    join_location_for,
    own_home_point,
    pending_join_request_id,
    require_local_eligibility,
)
from tests.local_area_budget import InMemoryLocalAreaAttemptRepo, memory_budget

HOST = "host-inst"
ZURICH = (47.3769, 8.5417)
INSIDE = (47.39, 8.56)  # ~2 km away
EDGE = (47.3769, 8.6082)  # ~5 km east: the ~1 km cell straddles the rim
BERN = (46.95, 7.45)  # ~95 km away


class _FedRepo:
    def __init__(self, *, own=None):
        self.own = own

    async def get_local_identity(self):
        if self.own is False:
            return None
        lat, lon = self.own or (None, None)
        return {"instance_id": HOST, "home_lat": lat, "home_lon": lon}


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _space(**kw) -> Space:
    base = {
        "id": "s1",
        "name": "Quartier",
        "owner_instance_id": HOST,
        "owner_username": "anna",
        "identity_public_key": "aa" * 32,
        "config_sequence": 0,
        "features": SpaceFeatures(),
        "space_type": SpaceType.PUBLIC,
        "join_mode": JoinMode.OPEN,
        "lat": ZURICH[0],
        "lon": ZURICH[1],
        "radius_km": 5.0,
    }
    base.update(kw)
    return Space(**base)


async def _attempt(space, household, point, budget, *, user="u1"):
    return await require_local_eligibility(
        space,
        household_instance_id=household,
        own_instance_id=HOST,
        join_point=point,
        budget=budget,
        seeker_user_id=user,
    )


async def _admin(space, household, point):
    return await require_local_eligibility(
        space,
        household_instance_id=household,
        own_instance_id=HOST,
        join_point=point,
        budget=None,
    )


# ── the decision ────────────────────────────────────────────────────────


async def test_inside_and_edge_are_returned_for_the_caller_to_act_on():
    budget = memory_budget(clock=_Clock())
    assert await _attempt(_space(), "b", INSIDE, budget) is Eligibility.INSIDE
    assert await _attempt(_space(), "b", EDGE, budget) is Eligibility.EDGE
    assert await _attempt(_space(), HOST, INSIDE, budget) is Eligibility.INSIDE


async def test_outside_is_refused_with_outside_area():
    with pytest.raises(OutsideAreaError) as exc:
        await _attempt(_space(), "b", BERN, memory_budget(clock=_Clock()))
    assert exc.value.status == 403
    assert exc.value.code == "OUTSIDE_AREA"
    assert exc.value.params == {"whose": "peer"}
    assert isinstance(exc.value, SpacePermissionError)


async def test_no_point_from_another_household_asks_for_it_and_spends_nothing():
    inner = InMemoryLocalAreaAttemptRepo()
    budget = LocalAreaBudget(inner, clock=_Clock())
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 2):
        with pytest.raises(LocationRequiredError) as exc:
            await _attempt(_space(), "b", None, budget)
    assert exc.value.code == "LOCATION_REQUIRED"
    assert inner.rows == {}
    assert await _attempt(_space(), "b", INSIDE, budget) is Eligibility.INSIDE


async def test_no_home_of_our_own_is_home_not_set():
    with pytest.raises(HomeNotSetError) as exc:
        await _attempt(_space(), HOST, None, memory_budget(clock=_Clock()))
    assert exc.value.code == "HOME_NOT_SET"
    assert exc.value.params == {"whose": "own"}


@pytest.mark.parametrize(
    "space",
    [
        _space(space_type=SpaceType.GLOBAL),
        _space(space_type=SpaceType.PRIVATE),
        _space(radius_km=None),
        _space(owner_instance_id="elsewhere"),
    ],
)
async def test_not_a_local_community_we_host_is_never_checked(space):
    assert await _attempt(space, "b", None, None) is None
    assert hosted_local_area(space, HOST) is None


async def test_without_an_own_instance_id_nothing_is_ours():
    got = await require_local_eligibility(
        _space(),
        household_instance_id="b",
        own_instance_id=None,
        join_point=None,
        budget=None,
        seeker_user_id="u",
    )
    assert got is None


async def test_a_more_precise_point_decides_exactly_like_its_cell():
    """The host never decides on more than the ~1 km cell: any home answers
    exactly as the point it rounds to — automatic answers reveal only that."""
    rng = random.Random(3)
    budget = memory_budget(clock=_Clock())
    for i in range(300):
        home = (
            ZURICH[0] + rng.uniform(-0.07, 0.07),
            ZURICH[1] + rng.uniform(-0.1, 0.1),
        )
        outcomes = []
        for point in (home, coarse_join_point(*home)):
            try:
                outcomes.append(
                    await _attempt(_space(), f"h{i}", point, budget, user="p")
                )
            except OutsideAreaError:
                outcomes.append("outside")
        assert outcomes[0] == outcomes[1]


async def test_an_admins_step_is_not_counted_and_needs_no_budget():
    """An invite, a direct add or an approval is the admin's: no
    ``seeker_user_id``, nothing spent — near the edge included."""
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 2):
        assert await _admin(_space(), HOST, EDGE) is Eligibility.EDGE
    with pytest.raises(OutsideAreaError):
        await _admin(_space(), HOST, BERN)


async def test_a_person_check_without_a_budget_fails_closed():
    with pytest.raises(RuntimeError):
        await _attempt(_space(), "b", INSIDE, None)


# ── the budgets (spam control) ──────────────────────────────────────────


async def test_a_person_gets_ten_checks_a_day_successful_ones_included():
    clock = _Clock()
    budget = memory_budget(clock=clock)
    assert MAX_LOCAL_AREA_CHECKS_PER_PERSON == 10
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        await _attempt(_space(), "b", INSIDE, budget)
    with pytest.raises(LocalAreaRateLimitedError) as exc:
        await _attempt(_space(), "b", INSIDE, budget)
    assert exc.value.status == 429
    assert exc.value.code == "LOCAL_AREA_RATE_LIMITED"
    assert exc.value.params == {"whose": "peer"}
    clock.now += timedelta(days=1)
    await _attempt(_space(), "b", INSIDE, budget)


async def test_edge_reviews_count_like_any_check():
    budget = memory_budget(clock=_Clock())
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        await _attempt(_space(), "b", EDGE, budget)
    with pytest.raises(LocalAreaRateLimitedError):
        await _attempt(_space(), "b", EDGE, budget)


async def test_one_person_does_not_block_the_rest_of_their_household():
    budget = memory_budget(clock=_Clock())
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        with pytest.raises(OutsideAreaError):
            await _attempt(_space(), "b", BERN, budget, user="spammer")
    with pytest.raises(LocalAreaRateLimitedError):
        await _attempt(_space(), "b", BERN, budget, user="spammer")
    with pytest.raises(OutsideAreaError):  # another person, same household
        await _attempt(_space(), "b", BERN, budget, user="sibling")
    with pytest.raises(OutsideAreaError):  # another community
        await _attempt(_space(id="s2"), "b", BERN, budget, user="spammer")


async def test_fresh_user_ids_stop_at_the_household_backstop_of_twenty():
    budget = memory_budget(clock=_Clock())
    assert MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD == 20
    for i in range(MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD):
        await _attempt(_space(), "b", INSIDE, budget, user=f"minted-{i}")
    with pytest.raises(LocalAreaRateLimitedError):
        await _attempt(_space(), "b", INSIDE, budget, user="minted-new")
    await _attempt(_space(), "c", INSIDE, budget, user="minted-new")


async def test_a_held_off_person_spends_nothing_of_their_households_backstop():
    inner = InMemoryLocalAreaAttemptRepo()
    budget = LocalAreaBudget(inner, clock=_Clock())
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 5):
        with pytest.raises((OutsideAreaError, LocalAreaRateLimitedError)):
            await _attempt(_space(), "b", BERN, budget, user="spammer")
    household = [v for k, v in inner.rows.items() if k[1] == "household"]
    assert household == [MAX_LOCAL_AREA_CHECKS_PER_PERSON]


async def test_the_hosts_own_people_have_a_person_budget_but_no_household_cap():
    budget = memory_budget(clock=_Clock())
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        await _attempt(_space(), HOST, INSIDE, budget, user="anna")
    with pytest.raises(LocalAreaRateLimitedError) as exc:
        await _attempt(_space(), HOST, INSIDE, budget, user="anna")
    assert exc.value.params == {"whose": "own"}
    for i in range(MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD + 2):
        await _attempt(_space(), HOST, INSIDE, budget, user=f"p{i}")


async def test_the_budget_survives_a_restart(tmp_dir):
    clock = _Clock()
    db = AsyncDatabase(tmp_dir / "budget.db", batch_timeout_ms=10)
    await db.startup()
    try:
        before = LocalAreaBudget(SqliteLocalAreaAttemptRepo(db), clock=clock)
        for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
            await _attempt(_space(), "b", INSIDE, before)
    finally:
        await db.shutdown()
    db = AsyncDatabase(tmp_dir / "budget.db", batch_timeout_ms=10)
    await db.startup()
    try:
        after = LocalAreaBudget(SqliteLocalAreaAttemptRepo(db), clock=clock)
        with pytest.raises(LocalAreaRateLimitedError):
            await _attempt(_space(), "b", INSIDE, after)
    finally:
        await db.shutdown()


async def test_days_are_utc_calendar_days():
    clock = _Clock()
    clock.now = datetime(2026, 10, 19, 23, 59, tzinfo=UTC)
    budget = memory_budget(clock=clock)
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        await _attempt(_space(), "b", INSIDE, budget)
    # 01:59 at UTC+2 is still 19 Oct in UTC.
    clock.now = datetime(2026, 10, 20, 1, 59, tzinfo=timezone(timedelta(hours=2)))
    with pytest.raises(LocalAreaRateLimitedError):
        await _attempt(_space(), "b", INSIDE, budget)
    clock.now = datetime(2026, 10, 20, 0, 1, tzinfo=UTC)
    await _attempt(_space(), "b", INSIDE, budget)


async def test_person_subjects_never_store_the_raw_user_id():
    inner = InMemoryLocalAreaAttemptRepo()
    budget = LocalAreaBudget(inner, clock=_Clock())
    await budget.spend_seat_check("s1", household="b", user_id="alice", remote=True)
    assert all("alice" not in k[2] for k in inner.rows)


# ── the joiner's side ───────────────────────────────────────────────────


async def test_our_own_home_is_rounded_to_the_join_precision():
    assert await own_home_point(_FedRepo(own=(47.376_912, 8.541_749))) == (
        47.38,
        8.54,
    )


@pytest.mark.parametrize(
    "repo", [None, _FedRepo(own=False), _FedRepo(own=None), _FedRepo(own=(1.0, None))]
)
async def test_no_home_is_no_point(repo):
    assert await own_home_point(repo) is None


async def test_nothing_leaves_without_consent():
    repo = _FedRepo(own=(47.376_912, 8.541_749))
    assert await join_location_for(repo, share_location=False) is None
    assert await join_location_for(repo, share_location=True) == {
        "lat": 47.38,
        "lon": 8.54,
    }


async def test_consent_without_a_home_is_home_not_set():
    with pytest.raises(HomeNotSetError):
        await join_location_for(_FedRepo(own=None), share_location=True)


# ── join requests ───────────────────────────────────────────────────────


class _Requests:
    def __init__(self, rows=None) -> None:
        self.rows = list(rows or [])
        self.saved: list[tuple[tuple, dict]] = []

    async def list_pending_join_requests(self, space_id):
        return [r for r in self.rows if r["space_id"] == space_id]

    async def save_join_request(self, *args, **kwargs):
        self.saved.append((args, kwargs))
        return kwargs.get("request_id") or "rid-new"


async def test_pending_request_lookup_tells_local_and_remote_apart():
    repo = _Requests(
        [
            {
                "id": "r1",
                "space_id": "s1",
                "user_id": "u",
                "remote_applicant_instance_id": None,
            },
            {
                "id": "r2",
                "space_id": "s1",
                "user_id": "u",
                "remote_applicant_instance_id": "b",
            },
            {
                "id": "r3",
                "space_id": "s1",
                "user_id": "v",
                "remote_applicant_instance_id": None,
                "requested_role": "admin",
            },
        ]
    )
    assert (
        await pending_join_request_id(repo, "s1", "u", household_instance_id=None)
        == "r1"
    )
    assert (
        await pending_join_request_id(repo, "s1", "u", household_instance_id="b")
        == "r2"
    )
    assert (
        await pending_join_request_id(repo, "s1", "u", household_instance_id="c")
        is None
    )
    # An admin elevation is not a join request.
    assert (
        await pending_join_request_id(repo, "s1", "v", household_instance_id=None)
        is None
    )


async def test_a_remote_request_keeps_only_the_verdict():
    repo = _Requests()
    bus = EventBus()
    seen: list = []

    async def _on(event):
        seen.append(event)

    bus.subscribe(RemoteSpaceJoinRequestReceived, _on)
    rid = await file_remote_join_request(
        repo,
        bus,
        space_id="s1",
        household_instance_id="b",
        user_id="u",
        area_verdict=Eligibility.EDGE,
        applicant_pk="pk",
    )
    assert rid == "rid-new"
    ((args, kwargs),) = repo.saved
    assert args == ("s1", "u")
    assert kwargs["area_verdict"] == "edge"
    assert kwargs["remote_applicant_instance_id"] == "b"
    assert not any("lat" in k or "lon" in k for k in kwargs)
    assert seen and seen[0].requester_user_id == "u"
