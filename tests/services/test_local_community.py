"""``services/local_community`` — whose home the host reads, and the verdict."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

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
from socialhome.services.local_community import (
    MAX_OUTSIDE_AREA_REFUSALS,
    OUTSIDE_AREA_WINDOW_SECONDS,
    OutsideAreaAttempts,
    derive_edge_key,
    edge_shift_km,
    household_home,
    require_local_eligibility,
)

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


# ── refused "outside" seats are rate-limited per household ──────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def _attempt(space, household, repo, guard):
    await require_local_eligibility(
        space,
        household_instance_id=household,
        own_instance_id=HOST,
        federation_repo=repo,
        attempts=guard,
    )


async def test_a_household_refused_outside_three_times_is_then_held_off():
    """A seat attempt is an exact yes / no, so it must not be repeatable at
    will: after three OUTSIDE_AREA refusals in a day the host answers
    LOCAL_AREA_RATE_LIMITED without looking at the home — even one that
    has since moved inside."""
    clock = _Clock()
    guard = OutsideAreaAttempts(clock=clock)
    repo = _FedRepo(peers={"b": _Peer(*BERN)})
    for _ in range(MAX_OUTSIDE_AREA_REFUSALS):
        with pytest.raises(OutsideAreaError):
            await _attempt(_space(), "b", repo, guard)
    repo.peers["b"] = _Peer(*INSIDE)
    with pytest.raises(LocalAreaRateLimitedError) as exc:
        await _attempt(_space(), "b", repo, guard)
    assert exc.value.status == 429
    assert exc.value.code == "LOCAL_AREA_RATE_LIMITED"
    assert exc.value.params == {"whose": "peer"}
    assert isinstance(exc.value, SpacePermissionError)
    # A day after the first refusal the household may try again.
    clock.now += OUTSIDE_AREA_WINDOW_SECONDS + 1
    await _attempt(_space(), "b", repo, guard)


async def test_the_limit_is_per_space_and_per_household():
    guard = OutsideAreaAttempts(clock=_Clock())
    repo = _FedRepo(peers={"b": _Peer(*BERN), "c": _Peer(*BERN)})
    for _ in range(MAX_OUTSIDE_AREA_REFUSALS):
        with pytest.raises(OutsideAreaError):
            await _attempt(_space(), "b", repo, guard)
    with pytest.raises(OutsideAreaError):  # another household
        await _attempt(_space(), "c", repo, guard)
    with pytest.raises(OutsideAreaError):  # another community
        await _attempt(_space(id="s2"), "b", repo, guard)


async def test_inside_and_home_not_shared_are_not_counted():
    """Only "outside" answers are counted: they are what tells a prober
    where the rim is not."""
    guard = OutsideAreaAttempts(clock=_Clock())
    repo = _FedRepo(peers={"b": _Peer(*INSIDE), "n": _Peer(None, None)})
    for _ in range(MAX_OUTSIDE_AREA_REFUSALS + 2):
        await _attempt(_space(), "b", repo, guard)
        with pytest.raises(HomeNotSharedError):
            await _attempt(_space(), "n", repo, guard)


async def test_this_households_own_people_are_never_held_off():
    """The host's own people see the centre anyway (Settings)."""
    guard = OutsideAreaAttempts(clock=_Clock())
    repo = _FedRepo(own=BERN)
    for _ in range(MAX_OUTSIDE_AREA_REFUSALS + 2):
        with pytest.raises(OutsideAreaError):
            await _attempt(_space(), HOST, repo, guard)


async def test_by_default_the_process_wide_counter_is_used():
    repo = _FedRepo(peers={"b": _Peer(*BERN)})
    for _ in range(MAX_OUTSIDE_AREA_REFUSALS):
        with pytest.raises(OutsideAreaError):
            await _check(_space(id="s-default"), "b", repo)
    with pytest.raises(LocalAreaRateLimitedError):
        await _check(_space(id="s-default"), "b", repo)
