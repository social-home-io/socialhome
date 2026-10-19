"""Tests for :class:`SpaceInviteInboundHandlers` (§11.2)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from socialhome.domain.events import (
    RemoteSpaceInviteReceived,
    RemoteSpaceJoinRequestReceived,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import SpaceInviteInboundHandlers
from socialhome.services.local_community import MAX_LOCAL_AREA_CHECKS_PER_PERSON
from tests.local_area_budget import memory_budget


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, t, h):
        self.registered.append((t, h))


class _FakeFederationService:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()
        self.own_instance_id = "self"


def _space(
    space_id: str = "sp-1",
    *,
    space_type: SpaceType = SpaceType.PUBLIC,
    join_mode: JoinMode = JoinMode.REQUEST,
    owner: str = "self",
) -> Space:
    return Space(
        id=space_id,
        name="S",
        owner_instance_id=owner,
        owner_username="o",
        identity_public_key="ab" * 32,
        config_sequence=1,
        features=SpaceFeatures(),
        space_type=space_type,
        join_mode=join_mode,
    )


class _FakeSpaceRepo:
    def __init__(self) -> None:
        self.invitations = []
        self.join_requests = []
        self.pending: list[dict] = []
        self.join_status = []
        self.spaces = {"sp-1": _space()}
        self.banned: set[tuple[str, str]] = set()

    async def get(self, space_id):
        return self.spaces.get(space_id)

    async def is_banned(self, space_id, user_id):
        return (space_id, user_id) in self.banned

    async def save_invitation(
        self, *, space_id, invited_user_id, invited_by, ttl_days=7
    ):
        self.invitations.append((space_id, invited_user_id, invited_by))
        return "inv-1"

    async def save_join_request(
        self,
        space_id=None,
        user_id=None,
        *,
        message=None,
        ttl_days=7,
        remote_applicant_instance_id=None,
        remote_applicant_pk=None,
        request_id=None,
        area_verdict=None,
    ):
        # Accept both positional (legacy) and keyword calls.
        self.pending.append(
            {
                "id": request_id or "req-1",
                "space_id": space_id,
                "user_id": user_id,
                "remote_applicant_instance_id": remote_applicant_instance_id,
                "area_verdict": area_verdict,
            }
        )
        self.join_requests.append(
            (
                space_id,
                user_id,
                message,
                remote_applicant_instance_id,
                request_id,
            )
        )
        return request_id or "req-1"

    async def list_pending_join_requests(self, space_id):
        return [r for r in self.pending if r["space_id"] == space_id]

    async def update_join_request_status(self, request_id, status, *, reviewed_by=None):
        self.join_status.append((request_id, status, reviewed_by))


def _event(event_type, payload, *, from_instance="peer-a", space_id=None):
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(UTC).isoformat(),
        payload=payload,
        space_id=space_id,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def repo():
    return _FakeSpaceRepo()


@pytest.fixture
def handlers(bus, repo):
    h = SpaceInviteInboundHandlers(bus=bus, space_repo=repo)
    h.attach_to(_FakeFederationService())
    return h


async def test_attach_registers_expected_event_types(bus, repo):
    h = SpaceInviteInboundHandlers(bus=bus, space_repo=repo)
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.SPACE_INVITE in types
    assert FederationEventType.SPACE_INVITE_VIA in types
    assert FederationEventType.SPACE_JOIN_REQUEST in types
    assert FederationEventType.SPACE_JOIN_REQUEST_APPROVED in types
    assert FederationEventType.SPACE_JOIN_REQUEST_DENIED in types
    assert FederationEventType.SPACE_JOIN_REQUEST_EXPIRED in types
    assert FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN in types


async def test_invite_persists_and_publishes(bus, repo, handlers):
    captured: list[RemoteSpaceInviteReceived] = []
    bus.subscribe(RemoteSpaceInviteReceived, captured.append)
    await handlers._on_invite(
        _event(
            FederationEventType.SPACE_INVITE,
            {"invitee_user_id": "u-1", "inviter_user_id": "u-admin"},
            space_id="sp-1",
        )
    )
    assert repo.invitations == [("sp-1", "u-1", "u-admin")]
    assert captured[0].invitee_user_id == "u-1"


async def test_invite_missing_fields_drops(repo, handlers):
    await handlers._on_invite(
        _event(
            FederationEventType.SPACE_INVITE,
            {},
            space_id="sp-1",
        )
    )
    assert repo.invitations == []


async def test_invite_via_delegates_to_invite(bus, repo, handlers):
    captured: list[RemoteSpaceInviteReceived] = []
    bus.subscribe(RemoteSpaceInviteReceived, captured.append)
    await handlers._on_invite_via(
        _event(
            FederationEventType.SPACE_INVITE_VIA,
            {"invitee_user_id": "u-1", "inviter_user_id": "u-admin"},
            space_id="sp-1",
        )
    )
    assert len(repo.invitations) == 1


async def test_join_request_persists_and_publishes(bus, repo, handlers):
    captured: list[RemoteSpaceJoinRequestReceived] = []

    async def _append(e):
        captured.append(e)

    bus.subscribe(RemoteSpaceJoinRequestReceived, _append)
    await handlers._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST,
            {"user_id": "u-1", "message": "please let me in"},
            space_id="sp-1",
        )
    )
    assert repo.join_requests == [
        ("sp-1", "u-1", "please let me in", "peer-a", None),
    ]
    assert captured[0].requester_user_id == "u-1"


async def test_join_request_missing_user_id_drops(repo, handlers):
    await handlers._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST,
            {},
            space_id="sp-1",
        )
    )
    assert repo.join_requests == []


_PRIVATE = _space("sp-x", space_type=SpaceType.PRIVATE)
_HOUSEHOLD = _space("sp-x", space_type=SpaceType.HOUSEHOLD, join_mode=JoinMode.OPEN)
_INVITE_ONLY = _space("sp-x", join_mode=JoinMode.INVITE_ONLY)
_ELSEWHERE = _space("sp-x", owner="peer-b")
_DISSOLVED = Space(
    id="sp-x",
    name="S",
    owner_instance_id="self",
    owner_username="o",
    identity_public_key="ab" * 32,
    config_sequence=1,
    features=SpaceFeatures(),
    space_type=SpaceType.PUBLIC,
    join_mode=JoinMode.REQUEST,
    dissolved=True,
)


@pytest.mark.parametrize(
    "space",
    [None, _PRIVATE, _HOUSEHOLD, _INVITE_ONLY, _ELSEWHERE, _DISSOLVED],
    ids=[
        "unknown",
        "private",
        "household",
        "invite_only",
        "hosted_elsewhere",
        "dissolved",
    ],
)
async def test_join_request_only_for_a_public_space_we_host(bus, repo, handlers, space):
    """A paired peer may ask to join a public / global space THIS household
    hosts that takes requests — never a private one (invite only), a
    household one (local people only), another host's space, a dissolved
    or an unknown one. Anything else is dropped: nothing filed, no
    notification for the admins."""
    if space is not None:
        repo.spaces["sp-x"] = space
    captured: list[RemoteSpaceJoinRequestReceived] = []

    async def _append(e):
        captured.append(e)

    bus.subscribe(RemoteSpaceJoinRequestReceived, _append)
    await handlers._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST, {"user_id": "u-1"}, space_id="sp-x"
        )
    )
    assert repo.join_requests == []
    assert captured == []


async def test_join_request_for_a_global_space_is_filed(repo, handlers):
    repo.spaces["sp-g"] = _space("sp-g", space_type=SpaceType.GLOBAL)
    await handlers._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST, {"user_id": "u-1"}, space_id="sp-g"
        )
    )
    assert [r[0] for r in repo.join_requests] == ["sp-g"]


async def test_join_request_from_a_banned_user_is_dropped(repo, handlers):
    repo.banned.add(("sp-1", "u-1"))
    await handlers._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST, {"user_id": "u-1"}, space_id="sp-1"
        )
    )
    assert repo.join_requests == []


async def test_join_request_before_attach_fails_closed(bus, repo):
    """No federation attached → no own instance id → nothing is "ours"."""
    h = SpaceInviteInboundHandlers(bus=bus, space_repo=repo)
    await h._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST, {"user_id": "u-1"}, space_id="sp-1"
        )
    )
    assert repo.join_requests == []


async def test_join_request_status_approved(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
            {"request_id": "req-1", "reviewed_by": "admin-a"},
        )
    )
    assert repo.join_status == [("req-1", "approved", "admin-a")]


async def test_join_request_status_denied(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_DENIED,
            {"request_id": "req-1"},
        )
    )
    assert repo.join_status == [("req-1", "denied", None)]


async def test_join_request_status_withdrawn(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN,
            {"request_id": "req-1"},
        )
    )
    assert repo.join_status == [("req-1", "withdrawn", None)]


async def test_join_request_status_expired(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_EXPIRED,
            {"request_id": "req-1"},
        )
    )
    assert repo.join_status == [("req-1", "expired", None)]


async def test_join_request_reply_via_uses_status_field(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_REPLY_VIA,
            {"request_id": "req-1", "status": "approved"},
        )
    )
    assert repo.join_status == [("req-1", "approved", None)]


async def test_join_request_status_missing_id_is_noop(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
            {},
        )
    )
    assert repo.join_status == []


# ── local communities ───────────────────────────────────────────────────


NEAR = {"lat": 47.39, "lon": 8.56}  # ~2 km from the centre
EDGE = {"lat": 47.38, "lon": 8.61}  # its ~1 km cell straddles the 5 km rim
BERN = {"lat": 46.95, "lon": 7.45}


def _local_handlers(bus, repo, budget=None):
    repo.spaces["sp-1"] = replace(_space(), lat=47.3769, lon=8.5417, radius_km=5.0)
    h = SpaceInviteInboundHandlers(
        bus=bus,
        space_repo=repo,
        local_area_budget=budget if budget is not None else memory_budget(),
    )
    h.attach_to(_FakeFederationService())
    return h


async def _request(h, from_instance, location, user_id="u-1", request_id=None):
    payload: dict = {"user_id": user_id}
    if request_id:
        payload["request_id"] = request_id
    if location is not None:
        payload["join_location"] = location
    await h._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST,
            payload,
            from_instance=from_instance,
            space_id="sp-1",
        )
    )


@pytest.mark.parametrize("location,verdict", [(NEAR, "inside"), (EDGE, "edge")])
async def test_a_local_community_files_inside_and_edge_alike(
    bus, repo, location, verdict
):
    """The applicant hears nothing either way until an admin answers; the
    admins see the verdict, never the point."""
    h = _local_handlers(bus, repo)
    await _request(h, "near", location)
    assert [r[3] for r in repo.join_requests] == ["near"]
    (row,) = repo.pending
    assert row["area_verdict"] == verdict


@pytest.mark.parametrize(
    "location",
    [
        BERN,  # outside
        None,  # no consent / an older household: no location
        {"lat": "47.39", "lon": 8.56},  # malformed
    ],
)
async def test_a_local_community_drops_a_request_from_outside_or_without_a_point(
    bus, repo, location
):
    """Never in front of the admins — and nothing to approve later."""
    h = _local_handlers(bus, repo)
    await _request(h, "far", location)
    assert repo.join_requests == []


async def test_a_mesh_or_link_joined_household_is_decided_like_a_paired_one(bus, repo):
    """The host reads only the point the request carries — no pairing row
    (and no shared home) is needed."""
    h = _local_handlers(bus, repo)
    await _request(h, "stranger-over-the-mesh", NEAR)
    assert [r[3] for r in repo.join_requests] == ["stranger-over-the-mesh"]


async def test_a_request_spends_the_applicants_budget(bus, repo):
    budget = memory_budget()
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        assert await budget.spend_seat_check(
            "sp-1", household="near", user_id="u-1", remote=True
        )
    h = _local_handlers(bus, repo, budget)
    await _request(h, "near", NEAR, user_id="u-1")
    assert repo.join_requests == []
    await _request(h, "near", NEAR, user_id="u-2")
    assert [r[1] for r in repo.join_requests] == ["u-2"]


async def test_asking_again_while_pending_spends_nothing(bus, repo):
    budget = memory_budget()
    h = _local_handlers(bus, repo, budget)
    for i in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 3):
        await _request(h, "near", EDGE, request_id=f"req-{i}")
    assert [r[4] for r in repo.join_requests] == ["req-0"]
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON - 1):
        assert await budget.spend_seat_check(
            "sp-1", household="near", user_id="u-1", remote=True
        )


async def test_a_local_community_fails_closed_without_a_budget(bus, repo):
    repo.spaces["sp-1"] = replace(_space(), lat=47.3769, lon=8.5417, radius_km=5.0)
    h = SpaceInviteInboundHandlers(bus=bus, space_repo=repo)
    h.attach_to(_FakeFederationService())
    await _request(h, "near", NEAR)
    assert repo.join_requests == []
