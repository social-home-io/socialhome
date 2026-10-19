"""Invite-link / approved-request redeems into a LOCAL COMMUNITY.

The issuer (the host) checks the redeeming household's shared home against
the area after the consume + ban check: refused → the use is handed back
and a specific DENY reason goes out, which the redeemer maps to its OWN
coded error (never the issuer's text). The §D2 direct leg and the §D2b
bootstrap leg (a household met through a GFS, whose home the host can
never know) both.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.space import (
    HomeNotSharedError,
    JoinMode,
    LocalAreaRateLimitedError,
    OutsideAreaError,
    SpaceType,
)
from socialhome.federation.invite_token_redeem import (
    REDEEM_DENY_REASON_HOME_NOT_SHARED,
    REDEEM_DENY_REASON_OUTSIDE_AREA,
)
from socialhome.services.local_community import MAX_OUTSIDE_AREA_REFUSALS

from .test_invite_token_redeem import (
    _a_space,
    _bootstrap_pair,
    _FakeFederationRepo,
    _FakeFederationService,
    _FakeSpaceRepo,
    _make_coordinator,
    _wire_pair,
)

NEAR = (47.3920, 8.5600)
BERN = (46.9481, 7.4474)


def _community(space_id: str, owner: str):
    return replace(
        _a_space(space_id, owner_instance_id=owner),
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.REQUEST,
        lat=47.3769,
        lon=8.5417,
        radius_km=5.0,
    )


def _peer(home):
    lat, lon = home if home is not None else (None, None)
    return SimpleNamespace(
        id="sender-1",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        home_lat=lat,
        home_lon=lon,
    )


def _linked(home):
    sender, issuer, _sf, issuer_fed, issuer_repo, members = _wire_pair(
        {"space_id": "sp-1", "created_by": "owner", "uses_remaining": 1}
    )
    issuer_repo.space_rows_for_get["sp-1"] = _community(
        "sp-1", issuer_fed.own_instance_id
    )
    issuer._federation_repo = _FakeFederationRepo({"sender-1": _peer(home)})
    return sender, issuer_repo, members


async def _redeem(sender):
    return await sender.request_redeem(
        "good-token", viewer_user_id="u-local", issuer_instance_id="issuer-1"
    )


@pytest.mark.parametrize(
    "home,err",
    [(BERN, OutsideAreaError), (None, HomeNotSharedError)],
)
async def test_direct_redeem_refused_maps_to_a_coded_error(home, err):
    sender, issuer_repo, members = _linked(home)
    with pytest.raises(err) as exc:
        await _redeem(sender)
    assert exc.value.status == 403
    # The joiner's own home, as the host sees it.
    assert exc.value.params == {"whose": "host"}
    # Nothing seated; the use came back for a retry once the home is shared.
    assert members.added == []
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 1


async def test_direct_redeem_inside_is_seated():
    sender, issuer_repo, members = _linked(NEAR)
    result = await _redeem(sender)
    assert result["space_id"] == "sp-1"
    assert len(members.added) == 1
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 0


@pytest.mark.parametrize(
    "home,reason",
    [
        (BERN, REDEEM_DENY_REASON_OUTSIDE_AREA),
        (None, REDEEM_DENY_REASON_HOME_NOT_SHARED),
    ],
)
async def test_issuer_ships_the_specific_reason_and_releases_the_use(home, reason):
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo()
    repo.tokens["tok"] = {"space_id": "sp-1", "created_by": "o", "uses_remaining": 3}
    repo.space_rows_for_get["sp-1"] = _community("sp-1", fed.own_instance_id)
    coord = _make_coordinator(
        federation=fed,
        space_repo=repo,
        federation_repo=_FakeFederationRepo({"sender-1": _peer(home)}),
    )
    await coord._on_redeem(
        FederationEvent(
            msg_id="m",
            event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            from_instance="sender-1",
            to_instance="me",
            timestamp="2026-10-19T00:00:00Z",
            payload={
                "redeem_nonce": "n1",
                "invite_token": "tok",
                "redeemer_user_id": "u-x",
            },
        )
    )
    denies = [
        s["payload"]["reason"]
        for s in fed.sent
        if s["event_type"] == FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
    ]
    assert denies == [reason]
    assert repo.tokens["tok"]["uses_remaining"] == 3


async def test_a_dead_link_still_answers_the_uniform_reason():
    """The specific reason reaches only a holder of a LIVE link: an
    unknown token is refused before the area is looked at."""
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo()
    repo.space_rows_for_get["sp-1"] = _community("sp-1", fed.own_instance_id)
    coord = _make_coordinator(federation=fed, space_repo=repo)
    await coord._on_redeem(
        FederationEvent(
            msg_id="m",
            event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            from_instance="sender-1",
            to_instance="me",
            timestamp="2026-10-19T00:00:00Z",
            payload={
                "redeem_nonce": "n1",
                "invite_token": "nope",
                "redeemer_user_id": "u-x",
            },
        )
    )
    reasons = {s["payload"].get("reason") for s in fed.sent}
    assert REDEEM_DENY_REASON_HOME_NOT_SHARED not in reasons
    assert REDEEM_DENY_REASON_OUTSIDE_AREA not in reasons


async def test_a_bootstrap_redeem_is_refused_for_want_of_a_shared_home():
    """A household met through a GFS never shares a home with the host, so
    a local community's link never seats it — and the joiner sees our own
    coded wording, not the stranger's text."""
    env = _bootstrap_pair()
    env.issuer_spaces.space_rows_for_get["space-1"] = _community(
        "space-1", env.issuer_party.instance_id
    )
    with pytest.raises(HomeNotSharedError):
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
        )
    assert env.issuer_members.live == {}
    assert env.issuer_spaces.tokens["tok-1"]["uses_remaining"] == 1


async def test_redeeming_over_and_over_from_outside_is_held_off():
    """Each redeem is an exact yes / no on the host, and a refused use is
    handed back — so a household moving its shared home could ask over and
    over. After three OUTSIDE_AREA refusals in a day the host answers with
    its own reason without looking at the home, and the redeemer maps it to
    LOCAL_AREA_RATE_LIMITED."""
    sender, issuer_repo, members = _linked(BERN)
    for _ in range(MAX_OUTSIDE_AREA_REFUSALS):
        with pytest.raises(OutsideAreaError):
            await _redeem(sender)
    with pytest.raises(LocalAreaRateLimitedError) as exc:
        await _redeem(sender)
    assert exc.value.status == 429
    assert exc.value.params == {"whose": "host"}
    assert members.added == []
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 1
