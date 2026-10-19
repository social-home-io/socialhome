"""Invite-link / approved-request redeems into a LOCAL COMMUNITY.

The redeem carries the redeeming household's home rounded to ~1 km
(``join_location``) once the person agreed; the issuer (the host) decides on
it after the consume + ban check. Refused → the use is handed back and a
specific DENY reason goes out, which the redeemer maps to its OWN coded
error (never the issuer's text). Near the edge the redeem becomes a join
request for the admins and the DENY names it. The §D2 direct leg, the
mesh-routed leg and the §D2b bootstrap leg (a household met through a GFS)
alike — the location sealed to the host on every one.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.space import (
    JoinMode,
    JoinRequestSentError,
    LocalAreaRateLimitedError,
    LocationRequiredError,
    OutsideAreaError,
    SpaceType,
)
from socialhome.federation.invite_token_redeem import (
    REDEEM_DENY_REASON,
    REDEEM_DENY_REASON_JOIN_REQUESTED,
    REDEEM_DENY_REASON_LOCATION_REQUIRED,
    REDEEM_DENY_REASON_OUTSIDE_AREA,
)
from socialhome.services.local_community import MAX_LOCAL_AREA_CHECKS_PER_PERSON

from .test_invite_token_redeem import (
    _a_space,
    _bootstrap_pair,
    _FakeFederationRepo,
    _FakeFederationService,
    _FakeRouteService,
    _FakeSpaceRepo,
    _make_coordinator,
    _wire_pair,
)

NEAR = {"lat": 47.39, "lon": 8.56}
EDGE = {"lat": 47.38, "lon": 8.61}
BERN = {"lat": 46.95, "lon": 7.45}


def _community(space_id: str, owner: str, *, mode=JoinMode.REQUEST):
    return replace(
        _a_space(space_id, owner_instance_id=owner),
        space_type=SpaceType.PUBLIC,
        join_mode=mode,
        lat=47.3769,
        lon=8.5417,
        radius_km=5.0,
    )


def _linked():
    sender, issuer, _sf, issuer_fed, issuer_repo, members = _wire_pair(
        {"space_id": "sp-1", "created_by": "owner", "uses_remaining": 1}
    )
    issuer_repo.space_rows_for_get["sp-1"] = _community(
        "sp-1", issuer_fed.own_instance_id
    )
    # No pairing home anywhere: the host reads only what the redeem carries.
    issuer._federation_repo = _FakeFederationRepo(
        {
            "sender-1": SimpleNamespace(
                id="sender-1",
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
                home_lat=None,
                home_lon=None,
            )
        }
    )
    return sender, issuer_repo, members


async def _redeem(sender, location=None):
    return await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
        join_location=location,
    )


@pytest.mark.parametrize(
    "location,err",
    [(BERN, OutsideAreaError), (None, LocationRequiredError)],
)
async def test_direct_redeem_refused_maps_to_a_coded_error(location, err):
    sender, issuer_repo, members = _linked()
    with pytest.raises(err) as exc:
        await _redeem(sender, location)
    assert exc.value.status == 403
    assert exc.value.params == {"whose": "host"}
    # Nothing seated; the use came back for a retry.
    assert members.added == []
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 1


async def test_direct_redeem_inside_is_seated():
    sender, issuer_repo, members = _linked()
    result = await _redeem(sender, NEAR)
    assert result["space_id"] == "sp-1"
    assert len(members.added) == 1
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 0


async def test_a_redeem_near_the_edge_becomes_a_join_request_on_both_sides():
    """Never decided automatically: the host files a request for its admins
    (verdict only), hands the use back, and the DENY names the request so
    the redeemer keeps its side — the admins' approval then finds the
    person."""
    sender, issuer_repo, members = _linked()
    with pytest.raises(JoinRequestSentError) as exc:
        await _redeem(sender, EDGE)
    assert exc.value.code == "JOIN_REQUEST_SENT"
    assert members.added == []
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 1
    (host_row,) = issuer_repo.join_requests
    assert host_row["area_verdict"] == "edge"
    assert host_row["remote_applicant_instance_id"] == "sender-1"
    assert not {"lat", "lon", "join_location"} & set(host_row)
    (own_row,) = sender._spaces.join_requests
    assert own_row["id"] == host_row["id"]
    assert own_row["space_id"] == "sp-1"
    assert own_row["user_id"] == "u-local"
    assert own_row["remote_applicant_instance_id"] == "issuer-1"


async def test_redeeming_again_while_the_request_waits_spends_nothing():
    sender, issuer_repo, _members = _linked()
    with pytest.raises(JoinRequestSentError):
        await _redeem(sender, EDGE)
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 2):
        with pytest.raises(JoinRequestSentError):
            await _redeem(sender, None)
    assert len(issuer_repo.join_requests) == 1


async def test_an_approved_person_is_seated_without_a_location():
    """The admins' approval is the decision: the redeem of the token it
    minted (or the same link opened again) seats the person with no check —
    and no location is sent for it."""
    sender, issuer_repo, members = _linked()
    issuer_repo.approved.add(("sp-1", "u-local", "sender-1"))
    result = await _redeem(sender, None)
    assert result["space_id"] == "sp-1"
    assert len(members.added) == 1


def _issuer_event(location=None, *, token="tok"):
    payload = {"redeem_nonce": "n1", "invite_token": token, "redeemer_user_id": "u-x"}
    if location is not None:
        payload["join_location"] = location
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        from_instance="sender-1",
        to_instance="me",
        timestamp="2026-10-19T00:00:00Z",
        payload=payload,
    )


def _issuer(mode=JoinMode.REQUEST):
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo()
    repo.tokens["tok"] = {"space_id": "sp-1", "created_by": "o", "uses_remaining": 3}
    repo.space_rows_for_get["sp-1"] = _community("sp-1", fed.own_instance_id, mode=mode)
    coord = _make_coordinator(federation=fed, space_repo=repo)
    return fed, repo, coord


def _denies(fed):
    return [
        s["payload"]
        for s in fed.sent
        if s["event_type"] == FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
    ]


@pytest.mark.parametrize(
    "location,reason",
    [
        (BERN, REDEEM_DENY_REASON_OUTSIDE_AREA),
        (None, REDEEM_DENY_REASON_LOCATION_REQUIRED),
    ],
)
async def test_issuer_ships_the_specific_reason_and_releases_the_use(location, reason):
    fed, repo, coord = _issuer()
    await coord._on_redeem(_issuer_event(location))
    assert [d["reason"] for d in _denies(fed)] == [reason]
    # Nothing beyond the reason: no distance, no verdict detail.
    assert set(_denies(fed)[0]) == {"redeem_nonce", "reason"}
    assert repo.tokens["tok"]["uses_remaining"] == 3


@pytest.mark.parametrize("mode", [JoinMode.OPEN, JoinMode.REQUEST])
async def test_the_edge_deny_names_only_the_request(mode):
    """Whatever the join mode, the same answer: the request — and nothing
    about how far the home is."""
    fed, repo, coord = _issuer(mode)
    await coord._on_redeem(_issuer_event(EDGE))
    (deny,) = _denies(fed)
    assert deny == {
        "redeem_nonce": "n1",
        "reason": REDEEM_DENY_REASON_JOIN_REQUESTED,
        "space_id": "sp-1",
        "request_id": repo.join_requests[0]["id"],
    }
    assert repo.tokens["tok"]["uses_remaining"] == 3


async def test_without_a_budget_the_redeem_is_refused_and_the_use_kept():
    fed, repo, coord = _issuer()
    coord._local_area_budget = None
    await coord._on_redeem(_issuer_event(NEAR))
    assert [d["reason"] for d in _denies(fed)] == [REDEEM_DENY_REASON]
    assert repo.tokens["tok"]["uses_remaining"] == 3


async def test_a_dead_link_still_answers_the_uniform_reason():
    """The specific reason reaches only a holder of a LIVE link: an
    unknown token is refused before the area is looked at."""
    fed, _repo, coord = _issuer()
    await coord._on_redeem(_issuer_event(None, token="nope"))
    assert [d["reason"] for d in _denies(fed)] == [REDEEM_DENY_REASON]


async def test_a_redeem_carries_no_location_unless_given():
    route_svc = _FakeRouteService()
    route_svc.configure(["self", "issuer-1"], "eph-pub", "eph-priv")
    routed_handler = AsyncMock()
    sender = _make_coordinator(federation_repo=_FakeFederationRepo(), timeout=0.01)
    sender._route_service = route_svc
    sender._routed_handler = routed_handler
    for location in (None, NEAR):
        with pytest.raises(TimeoutError):
            await sender.request_redeem(
                "tkn",
                viewer_user_id="u-local",
                issuer_instance_id="issuer-1",
                join_location=location,
            )
    first, second = routed_handler.send_routed.await_args_list
    assert "join_location" not in first.kwargs["inner_payload"]
    # Mesh-routed: inside the SPACE_ROUTED inner payload the routed handler
    # seals to the issuer's ephemeral key — never on a relay's frame.
    assert second.kwargs["inner_payload"]["join_location"] == NEAR
    assert second.kwargs["target_eph_pk_b64"] == "eph-pub"


async def test_a_bootstrap_redeem_seals_the_location_from_the_gfs():
    """A household met through a GFS joins a local community on the
    location its redeem carries — sealed to the host: the relay sees the
    addressee and ciphertext, never the point."""
    env = _bootstrap_pair()
    env.issuer_spaces.space_rows_for_get["space-1"] = _community(
        "space-1", env.issuer_party.instance_id
    )
    with pytest.raises(LocationRequiredError):
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
        )
    assert env.issuer_spaces.tokens["tok-1"]["uses_remaining"] == 1
    result = await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=env.hint,
        join_location=NEAR,
    )
    assert result["space_id"] == "space-1"
    for _to, envelope in env.relay.envelopes:
        blob = json.dumps(envelope)
        assert "47.39" not in blob and "8.56" not in blob
        assert "join_location" not in blob


async def test_a_bootstrap_redeem_near_the_edge_files_a_request():
    env = _bootstrap_pair()
    env.issuer_spaces.space_rows_for_get["space-1"] = _community(
        "space-1", env.issuer_party.instance_id
    )
    with pytest.raises(JoinRequestSentError):
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
            join_location=EDGE,
        )
    (host_row,) = env.issuer_spaces.join_requests
    (own_row,) = env.redeemer_spaces.join_requests
    assert own_row["id"] == host_row["id"]
    assert env.issuer_members.live == {}


async def test_redeeming_over_and_over_is_held_off():
    """Spam control: past the person's daily budget the host answers its own
    reason with no verdict, mapped to LOCAL_AREA_RATE_LIMITED."""
    sender, issuer_repo, members = _linked()
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        with pytest.raises(OutsideAreaError):
            await _redeem(sender, BERN)
    with pytest.raises(LocalAreaRateLimitedError) as exc:
        await _redeem(sender, NEAR)
    assert exc.value.status == 429
    assert exc.value.params == {"whose": "host"}
    assert members.added == []
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 1


async def test_a_malformed_review_deny_files_nothing_on_the_redeemer():
    """The ids a JOIN_REQUESTED names are validated before the redeemer
    stores them."""
    sender = _make_coordinator()
    exc = JoinRequestSentError(params={})
    await sender._record_review_request(
        exc, viewer_user_id="u-local", issuer="issuer-1"
    )
    assert sender._spaces.join_requests == []
