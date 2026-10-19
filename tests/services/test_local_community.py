"""``services/local_community`` — whose home the host reads, and the verdict."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from socialhome.domain.space import (
    HomeNotSharedError,
    JoinMode,
    OutsideAreaError,
    Space,
    SpaceFeatures,
    SpacePermissionError,
    SpaceType,
)
from socialhome.services.local_community import (
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
