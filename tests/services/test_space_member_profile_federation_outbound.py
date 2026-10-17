"""SpaceMemberProfileFederationOutbound — per-space profile fan-out."""

from __future__ import annotations

import pytest

from socialhome.domain.events import SpaceMemberProfileUpdated
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.space_member_profile_federation_outbound import (
    SpaceMemberProfileFederationOutbound,
)


class _FakeFederationService:
    def __init__(self, own_instance_id: str = "own-inst") -> None:
        self._own_instance_id = own_instance_id
        self.sent: list[tuple[str, FederationEventType, dict, str | None]] = []

    async def send_event(
        self,
        *,
        to_instance_id,
        event_type,
        payload,
        space_id=None,
    ):
        self.sent.append((to_instance_id, event_type, payload, space_id))


class _FakeSpaceRepo:
    def __init__(self, members: dict[str, list[str]]) -> None:
        self._members = members

    async def list_member_instances(self, space_id: str) -> list[str]:
        return list(self._members.get(space_id, []))


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo({"sp": ["own-inst", "peer-1", "peer-2"]})
    out = SpaceMemberProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=repo,
    )
    out.wire()
    return bus, fed


async def test_fanouts_to_space_members_excluding_self(env):
    bus, fed = env
    await bus.publish(
        SpaceMemberProfileUpdated(
            space_id="sp",
            user_id="u1",
            space_display_name="Alice in sp",
            picture_hash="h1",
            picture_webp=None,
        )
    )
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1", "peer-2"]
    for _to, event_type, payload, space_id in fed.sent:
        assert event_type is FederationEventType.SPACE_MEMBER_PROFILE_UPDATED
        assert space_id == "sp"
        assert payload == {
            "space_id": "sp",
            "user_id": "u1",
            "space_display_name": "Alice in sp",
            "picture_hash": "h1",
        }


async def test_picture_bytes_base64d_when_present(env):
    bus, fed = env
    await bus.publish(
        SpaceMemberProfileUpdated(
            space_id="sp",
            user_id="u1",
            space_display_name=None,
            picture_hash="h2",
            picture_webp=b"\xff\xfe",
        )
    )
    for _to, _ev, payload, _sp in fed.sent:
        # base64 of ``\xff\xfe`` is ``//4=``.
        assert payload["picture_webp_base64"] == "//4="


async def test_empty_member_id_is_skipped():
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo({"sp": ["", "peer-1", "own-inst"]})
    out = SpaceMemberProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=repo,
    )
    out.wire()
    await bus.publish(
        SpaceMemberProfileUpdated(
            space_id="sp",
            user_id="u1",
            space_display_name="X",
            picture_hash=None,
        )
    )
    assert [r[0] for r in fed.sent] == ["peer-1"]


async def test_no_members_no_sends():
    bus = EventBus()
    fed = _FakeFederationService()
    out = SpaceMemberProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=_FakeSpaceRepo({}),
    )
    out.wire()
    await bus.publish(
        SpaceMemberProfileUpdated(
            space_id="sp-unknown",
            user_id="u1",
            space_display_name="X",
            picture_hash=None,
        )
    )
    assert fed.sent == []


async def test_a_change_applied_from_another_household_is_never_echoed(env):
    """An event re-published after applying a peer's change (live
    ``SPACE_MEMBER_PROFILE_UPDATED`` inbound, or a §25.6 catch-up) carries
    the sender as ``origin_instance_id``: local clients hear about it, but
    it is never fanned back out to the member households."""
    bus, fed = env
    await bus.publish(
        SpaceMemberProfileUpdated(
            space_id="sp",
            user_id="u1",
            space_display_name="Alice in sp",
            picture_hash="h1",
            origin_instance_id="peer-1",
        )
    )
    assert fed.sent == []
