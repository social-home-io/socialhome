"""PollFederationOutbound — mesh-routed SPACE_POLL_* fan-out (F2b).

After F2b the outbound uses ``broadcast_to_space_members`` so members
behind a relay receive poll mutations too. The test asserts the
broadcast call shape, not per-peer ``send_event``.
"""

from __future__ import annotations

import pytest

from socialhome.domain.events import PollClosed, PollCreated, PollVoted
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.poll_federation_outbound import (
    PollFederationOutbound,
)


class _FakeFed:
    def __init__(self, *, raise_on_broadcast: bool = False) -> None:
        self.broadcasts: list[tuple[str, FederationEventType, dict]] = []
        self._raise = raise_on_broadcast

    async def broadcast_to_space_members(
        self,
        space_id,
        event_type,
        payload,
        **kwargs,
    ):
        if self._raise:
            raise RuntimeError("simulated transport failure")
        self.broadcasts.append((space_id, event_type, payload))


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFed()
    out = PollFederationOutbound(bus=bus, federation_service=fed)
    out.wire()
    return bus, fed


async def test_household_poll_is_not_federated(env):
    bus, fed = env
    await bus.publish(
        PollCreated(
            post_id="p1",
            question="?",
            allow_multiple=False,
            space_id=None,
        ),
    )
    assert fed.broadcasts == []


async def test_poll_created_broadcasts(env):
    bus, fed = env
    await bus.publish(
        PollCreated(
            post_id="p1",
            question="?",
            allow_multiple=False,
            space_id="sp-A",
        ),
    )
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_POLL_CREATED
    assert payload["post_id"] == "p1"
    assert payload["question"] == "?"
    assert payload["allow_multiple"] is False


async def test_poll_voted_broadcasts(env):
    bus, fed = env
    await bus.publish(
        PollVoted(
            post_id="p1",
            voter_user_id="u1",
            option_ids=("o1",),
            space_id="sp-A",
        ),
    )
    assert len(fed.broadcasts) == 1
    _, event_type, payload = fed.broadcasts[0]
    assert event_type is FederationEventType.SPACE_POLL_VOTE_CAST
    assert payload["option_ids"] == ["o1"]


async def test_poll_closed_broadcasts(env):
    bus, fed = env
    await bus.publish(PollClosed(post_id="p1", space_id="sp-A"))
    assert len(fed.broadcasts) == 1
    _, event_type, _ = fed.broadcasts[0]
    assert event_type is FederationEventType.SPACE_POLL_CLOSED


async def test_a_peers_poll_close_is_not_echoed_back(env):
    """A close that came in as ``SPACE_POLL_CLOSED`` is republished locally
    (so voters here hear it) — never broadcast again."""
    bus, fed = env
    await bus.publish(
        PollClosed(post_id="p1", space_id="sp-A", origin_instance_id="peer-a")
    )
    assert fed.broadcasts == []


async def test_broadcast_failure_is_swallowed():
    bus = EventBus()
    fed = _FakeFed(raise_on_broadcast=True)
    PollFederationOutbound(bus=bus, federation_service=fed).wire()
    # Must not raise.
    await bus.publish(
        PollCreated(
            post_id="p1",
            question="?",
            allow_multiple=False,
            space_id="sp-A",
        ),
    )
    assert fed.broadcasts == []
