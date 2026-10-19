"""``services/local_community`` — whose home the host reads, and the verdict."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.local_community import (
    LocalArea,
    directory_verdict,
    edge_band_km,
    edge_cell,
    edge_max_shift_km,
    eligibility,
)
from socialhome.domain.space import (
    HomeNotSharedError,
    JoinMode,
    LocalAreaRateLimitedError,
    OutsideAreaError,
    Space,
    SpaceFeatures,
    SpacePermissionError,
    SpaceType,
)
from socialhome.repositories.local_area_attempt_repo import (
    SqliteLocalAreaAttemptRepo,
)
from socialhome.services.local_community import (
    MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD,
    MAX_LOCAL_AREA_CHECKS_PER_PERSON,
    MAX_VERDICT_HOMES_PER_DAY,
    LocalAreaBudget,
    derive_edge_key,
    edge_shift_km,
    household_home,
    require_local_eligibility,
)
from tests.local_area_budget import InMemoryLocalAreaAttemptRepo, memory_budget

HOST = "host-inst"
ZURICH = (47.3769, 8.5417)
INSIDE = (47.3920, 8.5600)  # ~2 km away
BERN = (46.9481, 7.4474)  # ~95 km away


@dataclass
class _Peer:
    home_lat: float | None
    home_lon: float | None


class _FedRepo:
    def __init__(self, *, own=None, peers=None):
        self.own = own
        self.peers = peers or {}

    async def get_local_identity(self):
        if self.own is False:
            return None
        lat, lon = self.own or (None, None)
        return {"instance_id": HOST, "home_lat": lat, "home_lon": lon}

    async def get_instance(self, iid):
        return self.peers.get(iid)


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


async def _check(space, household, repo):
    await require_local_eligibility(
        space,
        household_instance_id=household,
        own_instance_id=HOST,
        federation_repo=repo,
    )


# ── household_home ──────────────────────────────────────────────────────


async def test_own_household_reads_instance_identity():
    repo = _FedRepo(own=INSIDE)
    assert await household_home(repo, HOST, own_instance_id=HOST) == INSIDE


async def test_a_peer_reads_its_shared_home_on_the_pairing_row():
    repo = _FedRepo(peers={"b": _Peer(*INSIDE)})
    assert await household_home(repo, "b", own_instance_id=HOST) == INSIDE


@pytest.mark.parametrize(
    "repo,household",
    [
        (None, "b"),
        (_FedRepo(), ""),
        (_FedRepo(own=False), HOST),
        (_FedRepo(own=(None, None)), HOST),
        (_FedRepo(), "unknown"),
        (_FedRepo(peers={"b": _Peer(None, None)}), "b"),
        (_FedRepo(peers={"b": _Peer(47.0, None)}), "b"),
    ],
)
async def test_unknown_homes_are_none(repo, household):
    assert await household_home(repo, household, own_instance_id=HOST) is None


# ── require_local_eligibility ───────────────────────────────────────────


async def test_inside_passes_for_a_local_user_and_a_peer():
    repo = _FedRepo(own=INSIDE, peers={"b": _Peer(*INSIDE)})
    await _check(_space(), HOST, repo)
    await _check(_space(), "b", repo)


async def test_outside_is_refused_with_outside_area():
    repo = _FedRepo(own=BERN, peers={"b": _Peer(*BERN)})
    with pytest.raises(OutsideAreaError) as exc:
        await _check(_space(), "b", repo)
    assert exc.value.status == 403
    assert exc.value.code == "OUTSIDE_AREA"
    assert exc.value.params == {"whose": "peer"}
    with pytest.raises(OutsideAreaError) as own:
        await _check(_space(), HOST, repo)
    assert own.value.params == {"whose": "own"}


async def test_unknown_home_is_refused_with_home_not_shared():
    """A peer that turned ``share_home`` off toward us has a NULL home on
    its row — refused, never assumed inside."""
    repo = _FedRepo(own=(None, None), peers={"b": _Peer(None, None)})
    with pytest.raises(HomeNotSharedError) as exc:
        await _check(_space(), "b", repo)
    assert exc.value.code == "HOME_NOT_SHARED"
    assert exc.value.params == {"whose": "peer"}
    with pytest.raises(HomeNotSharedError) as own:
        await _check(_space(), HOST, repo)
    assert own.value.params == {"whose": "own"}
    # No federation repo wired: fail closed.
    with pytest.raises(HomeNotSharedError):
        await _check(_space(), "b", None)


async def test_refusals_are_space_permission_errors():
    """Every caller that already handles a refused seat
    (``SpacePermissionError``) handles these too."""
    assert issubclass(HomeNotSharedError, SpacePermissionError)
    assert issubclass(OutsideAreaError, SpacePermissionError)


@pytest.mark.parametrize(
    "space",
    [
        _space(radius_km=None),
        _space(lat=None, lon=None, radius_km=None),
        _space(space_type=SpaceType.GLOBAL),
        _space(space_type=SpaceType.PRIVATE),
    ],
)
async def test_not_a_local_community_is_never_gated(space):
    await _check(space, "b", _FedRepo())


async def test_a_space_hosted_elsewhere_is_the_hosts_to_check():
    await _check(_space(owner_instance_id="other"), "b", _FedRepo())


async def test_without_an_own_instance_id_nothing_is_ours():
    await require_local_eligibility(
        _space(),
        household_instance_id="b",
        own_instance_id=None,
        federation_repo=_FedRepo(),
    )


# ── the host's secret band shift ────────────────────────────────────────

KEY = derive_edge_key(b"\x01" * 32)
AREA = LocalArea(0.0, 0.0, 5.0)  # band 0.5 km = one grid cell
_KM_PER_DEG = 111.19492664455873


def _east(km: float, north_km: float = 0.0) -> tuple[float, float]:
    return (north_km / _KM_PER_DEG, km / _KM_PER_DEG)


def test_the_edge_key_is_derived_from_the_identity_seed():
    assert len(KEY) == 32
    assert derive_edge_key(b"\x01" * 32) == KEY
    assert derive_edge_key(b"\x02" * 32) != KEY
    with pytest.raises(ValueError):
        derive_edge_key(b"")


def test_the_shift_is_per_cell_and_within_its_bound():
    most = edge_max_shift_km(5.0)
    shifts = {edge_shift_km(KEY, "s1", AREA, _east(4.0 + i * 0.5)) for i in range(40)}
    assert len(shifts) > 30  # a fresh draw per cell
    assert all(-most <= s <= most for s in shifts)
    # Every home in one cell gets the same shift.
    assert edge_shift_km(KEY, "s1", AREA, _east(4.01)) == edge_shift_km(
        KEY, "s1", AREA, _east(4.24)
    )
    # Another space, or another household's key, draws other shifts.
    assert edge_shift_km(KEY, "s2", AREA, _east(4.1)) != edge_shift_km(
        KEY, "s1", AREA, _east(4.1)
    )
    other = derive_edge_key(b"\x09" * 32)
    assert edge_shift_km(other, "s1", AREA, _east(4.1)) != edge_shift_km(
        KEY, "s1", AREA, _east(4.1)
    )


# ── the probing attack: a household stepping its shared home ────────────


def _walk(answer, centre_km: float, *, per_step: bool = False) -> dict:
    """A household moves the home it shares with the host in 20 m steps
    along the east-west line through the centre (worst case: it knows the
    line) and records the answer at every step. ``answer(area, home)`` is
    the host. Kept one observation per grid cell (every home in a cell gets
    the same coarsened answer), or per step for the exact baseline."""
    area = LocalArea(0.0, centre_km / _KM_PER_DEG, 5.0)
    seen: dict = {}
    for i in range(801):
        home = _east(centre_km - 8.0 + i * 0.02)
        key = home if per_step else edge_cell(area, home)
        seen.setdefault(key, (home, answer(area, home)))
    return seen


def _feasible(observations: dict, judge) -> list[float]:
    """Every centre (east-west, 10 m grid, within 3 km) the walker cannot
    rule out: ``judge(area, home, verdict)`` says whether a candidate
    area could have produced ``verdict`` for ``home``."""
    out = []
    for k in range(-300, 301):
        area = LocalArea(0.0, k * 0.01 / _KM_PER_DEG, 5.0)
        if all(judge(area, h, v) for h, v in observations.values()):
            out.append(k * 0.01)
    return out


def _coarsened_judge(area, home, verdict) -> bool:
    """The walker knows the radius, the band rule and the (absolute) grid —
    but not the host's per-cell shift, only its bound."""
    most = edge_max_shift_km(5.0)
    return any(
        directory_verdict(area, home, shift_km=most * (i / 4 - 1)) is verdict
        for i in range(9)
    )


def test_a_stepping_home_cannot_find_the_centre_better_than_the_band():
    """The owner's bar. A household moving its shared home in 20 m steps
    straight through the area (no rate limit — the host caps that too) and
    watching every answer narrows the centre only to about the band
    (0.5 km here) on average, and never below 0.4 of it — across 24 host
    secrets and centres."""
    band = edge_band_km(5.0)
    spans = []
    for n in range(24):
        key = derive_edge_key(bytes([n + 1]) * 32)
        centre = round(0.093 * n - 1.0, 2)
        obs = _walk(
            lambda area, home, key=key: directory_verdict(
                area, home, shift_km=edge_shift_km(key, "s1", area, home)
            ),
            centre,
        )
        feasible = _feasible(obs, _coarsened_judge)
        assert any(abs(c - centre) < 0.011 for c in feasible)  # truth survives
        spans.append(max(feasible) - min(feasible))
    assert min(spans) >= 0.4 * band
    assert sum(spans) / len(spans) >= band


def test_the_exact_answer_would_give_the_centre_away():
    """The baseline that shows the test can see a leak: answering with the
    exact inside / outside (the seat check) hands the same walker the
    centre to within a few steps."""
    obs = _walk(lambda area, home: eligibility(area, home), 0.0, per_step=True)
    feasible = _feasible(obs, lambda area, h, v: eligibility(area, h) is v)
    assert max(feasible) - min(feasible) <= 0.05


# ── every check a person starts is budgeted, per person ─────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _attempt(space, household, repo, budget, *, user="u1"):
    await require_local_eligibility(
        space,
        household_instance_id=household,
        own_instance_id=HOST,
        federation_repo=repo,
        seeker_user_id=user,
        budget=budget,
    )


async def test_a_person_gets_three_checks_a_day_successful_ones_included():
    """Join → leave → move the shared home → rejoin is an exact "inside?"
    each time, so a success spends the budget like a refusal. The fourth
    check that day is answered 429 without a verdict — even inside."""
    clock = _Clock()
    budget = memory_budget(clock=clock)
    repo = _FedRepo(peers={"b": _Peer(*INSIDE)})
    await _attempt(_space(), "b", repo, budget)
    repo.peers["b"] = _Peer(*BERN)
    with pytest.raises(OutsideAreaError):
        await _attempt(_space(), "b", repo, budget)
    repo.peers["b"] = _Peer(*INSIDE)
    await _attempt(_space(), "b", repo, budget)
    assert MAX_LOCAL_AREA_CHECKS_PER_PERSON == 3
    with pytest.raises(LocalAreaRateLimitedError) as exc:
        await _attempt(_space(), "b", repo, budget)
    # The 429 shape #1054 shipped.
    assert exc.value.status == 429
    assert exc.value.code == "LOCAL_AREA_RATE_LIMITED"
    assert exc.value.params == {"whose": "peer"}
    assert isinstance(exc.value, SpacePermissionError)
    # The next UTC day refills it.
    clock.now += timedelta(days=1)
    await _attempt(_space(), "b", repo, budget)


async def test_one_person_probing_does_not_block_the_rest_of_their_household():
    budget = memory_budget(clock=_Clock())
    repo = _FedRepo(peers={"b": _Peer(*BERN), "c": _Peer(*BERN)})
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        with pytest.raises(OutsideAreaError):
            await _attempt(_space(), "b", repo, budget, user="prober")
    with pytest.raises(LocalAreaRateLimitedError):
        await _attempt(_space(), "b", repo, budget, user="prober")
    with pytest.raises(OutsideAreaError):  # another person, same household
        await _attempt(_space(), "b", repo, budget, user="sibling")
    with pytest.raises(OutsideAreaError):  # another household
        await _attempt(_space(), "c", repo, budget, user="prober")
    with pytest.raises(OutsideAreaError):  # another community
        await _attempt(_space(id="s2"), "b", repo, budget, user="prober")


async def test_a_busy_community_does_not_lock_out_newcomers():
    """No overall counter: many people joining in one day never hold off
    the next one."""
    budget = memory_budget(clock=_Clock())
    peers = {f"h{i}": _Peer(*INSIDE) for i in range(20)}
    repo = _FedRepo(own=INSIDE, peers=peers)
    for household in peers:
        for user in ("a", "b"):
            await _attempt(_space(), household, repo, budget, user=user)
    for i in range(20):
        await _attempt(_space(), HOST, repo, budget, user=f"local-{i}")


async def test_fresh_user_ids_do_not_buy_a_household_fresh_answers():
    """The user id in a remote request is the household's to choose (the
    envelope authenticates the household, not the person): past
    MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD checks a day the household is held
    off whatever id it names."""
    budget = memory_budget(clock=_Clock())
    repo = _FedRepo(peers={"b": _Peer(*BERN), "c": _Peer(*BERN)})
    for i in range(MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD):
        with pytest.raises(OutsideAreaError):
            await _attempt(_space(), "b", repo, budget, user=f"minted-{i}")
    with pytest.raises(LocalAreaRateLimitedError):
        await _attempt(_space(), "b", repo, budget, user="minted-new")
    with pytest.raises(OutsideAreaError):  # another household is untouched
        await _attempt(_space(), "c", repo, budget, user="minted-new")


async def test_a_held_off_person_spends_nothing_of_their_households_backstop():
    """Asking again past the person's budget is refused before anything is
    spent — one prober cannot drain the household's backstop."""
    inner = InMemoryLocalAreaAttemptRepo()
    budget = LocalAreaBudget(inner, clock=_Clock())
    repo = _FedRepo(peers={"b": _Peer(*BERN)})
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 5):
        with pytest.raises((OutsideAreaError, LocalAreaRateLimitedError)):
            await _attempt(_space(), "b", repo, budget, user="prober")
    household = [v for k, v in inner.rows.items() if k[1] == "household"]
    assert household == [MAX_LOCAL_AREA_CHECKS_PER_PERSON]


async def test_the_hosts_own_people_have_a_person_budget_but_no_household_cap():
    budget = memory_budget(clock=_Clock())
    repo = _FedRepo(own=BERN)
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        with pytest.raises(OutsideAreaError):
            await _attempt(_space(), HOST, repo, budget, user="anna")
    with pytest.raises(LocalAreaRateLimitedError) as exc:
        await _attempt(_space(), HOST, repo, budget, user="anna")
    assert exc.value.params == {"whose": "own"}
    for i in range(MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD + 2):
        with pytest.raises(OutsideAreaError):
            await _attempt(_space(), HOST, repo, budget, user=f"p{i}")


async def test_home_not_shared_spends_nothing():
    """ "Home unknown" says nothing about where the area is."""
    budget = memory_budget(clock=_Clock())
    repo = _FedRepo(peers={"n": _Peer(None, None)})
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 2):
        with pytest.raises(HomeNotSharedError):
            await _attempt(_space(), "n", repo, budget)
    repo.peers["n"] = _Peer(*INSIDE)
    await _attempt(_space(), "n", repo, budget)


async def test_an_admins_step_is_not_counted():
    """An invite, a direct add or an approval is the admin's, not the
    joining person's: no ``seeker_user_id``, no budget spent."""
    repo = _FedRepo(peers={"b": _Peer(*INSIDE)})
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 2):
        await _check(_space(), "b", repo)


async def test_a_person_check_without_a_budget_fails_closed():
    repo = _FedRepo(peers={"b": _Peer(*INSIDE)})
    with pytest.raises(RuntimeError):
        await require_local_eligibility(
            _space(),
            household_instance_id="b",
            own_instance_id=HOST,
            federation_repo=repo,
            seeker_user_id="u1",
        )


async def test_the_budget_survives_a_restart(tmp_dir):
    """Kept in SQLite: a new budget over the same database (a host restart)
    still holds the person off."""
    clock = _Clock()
    repo = _FedRepo(peers={"b": _Peer(*INSIDE)})
    db = AsyncDatabase(tmp_dir / "budget.db", batch_timeout_ms=10)
    await db.startup()
    try:
        before = LocalAreaBudget(SqliteLocalAreaAttemptRepo(db), clock=clock)
        for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
            await _attempt(_space(), "b", repo, before)
    finally:
        await db.shutdown()
    db = AsyncDatabase(tmp_dir / "budget.db", batch_timeout_ms=10)
    await db.startup()
    try:
        after = LocalAreaBudget(SqliteLocalAreaAttemptRepo(db), clock=clock)
        with pytest.raises(LocalAreaRateLimitedError):
            await _attempt(_space(), "b", repo, after)
    finally:
        await db.shutdown()


async def test_days_are_utc_calendar_days():
    """A check at 23:59 UTC and one at 00:01 UTC fall on different days,
    whatever zone the clock reads in."""
    clock = _Clock()
    clock.now = datetime(2026, 10, 19, 23, 59, tzinfo=UTC)
    budget = memory_budget(clock=clock)
    repo = _FedRepo(peers={"b": _Peer(*INSIDE)})
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        await _attempt(_space(), "b", repo, budget)
    # 01:59 at UTC+2 is still 19 Oct in UTC.
    clock.now = datetime(2026, 10, 20, 1, 59, tzinfo=timezone(timedelta(hours=2)))
    with pytest.raises(LocalAreaRateLimitedError):
        await _attempt(_space(), "b", repo, budget)
    clock.now = datetime(2026, 10, 20, 0, 1, tzinfo=UTC)
    await _attempt(_space(), "b", repo, budget)


async def test_take_verdict_home_allows_four_new_homes_a_day():
    budget = memory_budget(clock=_Clock())
    assert MAX_VERDICT_HOMES_PER_DAY == 4
    for _ in range(MAX_VERDICT_HOMES_PER_DAY):
        assert await budget.take_verdict_home("peer-x") is True
    assert await budget.take_verdict_home("peer-x") is False
    assert await budget.take_verdict_home("peer-y") is True


async def test_person_subjects_never_store_the_raw_user_id():
    inner = InMemoryLocalAreaAttemptRepo()
    budget = LocalAreaBudget(inner, clock=_Clock())
    await budget.spend_seat_check("s1", household="b", user_id="alice", remote=True)
    assert all("alice" not in k[2] for k in inner.rows)
