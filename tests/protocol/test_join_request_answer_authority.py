"""Release-blocker protocol tests: who may answer a cross-household join
request (``SPACE_JOIN_REQUEST_APPROVED`` / ``_DENIED`` / ``_EXPIRED`` /
``_WITHDRAWN`` / ``_REPLY_VIA``), against the real SQLite repo.

Each household's copy of a request records the other party in
``remote_applicant_instance_id``. Only the host answers the applicant's
copy; only the applicant household withdraws the host's copy. Everything
else is dropped with no state change and no event.

The attacks these pin down:

* the applicant household approves its own request ON THE HOST, so
  ``has_approved_join_request`` seats that person in a local community on
  their next redeem — past the admins' review and the area check;
* a third paired household answers someone else's request: a fake denial
  (the real approval is then ignored, pending-only since #1218) or a fake
  approval carrying a token of its choosing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from socialhome.domain.events import RemoteJoinRequestApproved, RemoteJoinRequestDenied
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.federation_inbound import SpaceInviteInboundHandlers

pytestmark = pytest.mark.security

OWN = "self"


class _Registry:
    def register(self, _t, _h) -> None:
        return None


class _Federation:
    own_instance_id = OWN
    _event_registry = _Registry()


def _event(event_type, payload, *, from_instance):
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=from_instance,
        to_instance=OWN,
        timestamp=datetime.now(UTC).isoformat(),
        payload=payload,
        space_id=None,
    )


@pytest.fixture
async def setup(db):
    spaces = SqliteSpaceRepo(db)
    await spaces.save(
        Space(
            id="sp-host",
            name="Community",
            owner_instance_id=OWN,
            owner_username="owner",
            identity_public_key="ab" * 32,
            config_sequence=1,
            features=SpaceFeatures(),
            space_type=SpaceType.PUBLIC,
            join_mode=JoinMode.REQUEST,
        )
    )
    # Our (host) copy of a request household B filed for our space.
    await spaces.save_join_request(
        "sp-host",
        "u-b",
        remote_applicant_instance_id="peer-b",
        request_id="req-in",
    )
    # Our (applicant) copy of a request our user sent to A, which hosts sp-r.
    await spaces.save_join_request(
        "sp-r",
        "u-ours",
        remote_applicant_instance_id="peer-a",
        request_id="req-out",
    )
    bus = EventBus()
    seen: list = []

    async def _seen(ev) -> None:
        seen.append(ev)

    bus.subscribe(RemoteJoinRequestApproved, _seen)
    bus.subscribe(RemoteJoinRequestDenied, _seen)
    handlers = SpaceInviteInboundHandlers(bus=bus, space_repo=spaces)
    handlers.attach_to(_Federation())  # type: ignore[arg-type]
    return spaces, handlers, seen


async def _status(spaces, request_id):
    row = await spaces.get_join_request(request_id)
    return row["status"]


async def test_an_applicant_cannot_approve_its_own_request_on_the_host(setup):
    spaces, handlers, seen = setup
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
            {"request_id": "req-in", "space_id": "sp-host", "invite_token": "t"},
            from_instance="peer-b",
        )
    )
    assert await _status(spaces, "req-in") == "pending"
    assert not await spaces.has_approved_join_request(
        "sp-host", "u-b", remote_applicant_instance_id="peer-b", within_days=7
    )
    assert seen == []


async def test_the_applicant_household_withdraws_its_request(setup):
    spaces, handlers, _seen = setup
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN,
            {"request_id": "req-in"},
            from_instance="peer-b",
        )
    )
    assert await _status(spaces, "req-in") == "withdrawn"


@pytest.mark.parametrize(
    "event_type",
    [
        FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
        FederationEventType.SPACE_JOIN_REQUEST_DENIED,
        FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN,
    ],
)
async def test_a_third_household_answers_nothing(setup, event_type):
    spaces, handlers, seen = setup
    for rid in ("req-in", "req-out"):
        await handlers._on_join_request_status(
            _event(
                event_type,
                {"request_id": rid, "invite_token": "t"},
                from_instance="peer-c",
            )
        )
    assert await _status(spaces, "req-in") == "pending"
    assert await _status(spaces, "req-out") == "pending"
    assert seen == []


async def test_the_host_still_answers_after_a_forged_denial(setup):
    spaces, handlers, seen = setup
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_DENIED,
            {"request_id": "req-out", "space_id": "sp-r"},
            from_instance="peer-c",
        )
    )
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
            {"request_id": "req-out", "space_id": "sp-r", "invite_token": "t"},
            from_instance="peer-a",
        )
    )
    assert await _status(spaces, "req-out") == "approved"
    (ev,) = seen
    assert isinstance(ev, RemoteJoinRequestApproved)
    assert (ev.space_id, ev.invite_token) == ("sp-r", "t")
    # A replay of the host's own answer stays ignored (#1218).
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_DENIED,
            {"request_id": "req-out"},
            from_instance="peer-a",
        )
    )
    assert await _status(spaces, "req-out") == "approved"
    assert len(seen) == 1


async def test_the_host_answer_cannot_name_another_space(setup):
    spaces, handlers, seen = setup
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
            {"request_id": "req-out", "space_id": "sp-elsewhere", "invite_token": "t"},
            from_instance="peer-a",
        )
    )
    assert await _status(spaces, "req-out") == "pending"
    assert seen == []
