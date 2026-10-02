"""ScheduleFederationOutbound — mesh-routed SPACE_SCHEDULE_* fan-out (F2b)."""

from __future__ import annotations

import pytest

from socialhome.domain.events import (
    SchedulePollCreated,
    SchedulePollFinalized,
    SchedulePollResponded,
)
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.moderation_release import release_scope
from socialhome.services.schedule_federation_outbound import (
    ScheduleFederationOutbound,
)


class _FakeFed:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, FederationEventType, dict]] = []

    async def broadcast_to_space_members(
        self,
        space_id,
        event_type,
        payload,
        **kwargs,
    ):
        self.broadcasts.append((space_id, event_type, payload))


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFed()
    out = ScheduleFederationOutbound(bus=bus, federation_service=fed)
    out.wire()
    return bus, fed


async def test_household_schedule_response_not_federated(env):
    bus, fed = env
    await bus.publish(
        SchedulePollResponded(
            post_id="p1",
            slot_id="s1",
            user_id="u1",
            response="yes",
            space_id=None,
        ),
    )
    assert fed.broadcasts == []


async def test_schedule_response_broadcasts(env):
    bus, fed = env
    await bus.publish(
        SchedulePollResponded(
            post_id="p1",
            slot_id="s1",
            user_id="u1",
            response="yes",
            space_id="sp-A",
        ),
    )
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED
    assert payload["response"] == "yes"


async def test_schedule_finalized_broadcasts(env):
    bus, fed = env
    await bus.publish(
        SchedulePollFinalized(
            post_id="p1",
            slot_id="s1",
            slot_date="2026-06-01",
            start_time="14:00",
            end_time="15:00",
            title="Picnic",
            finalized_by="u-admin",
            space_id="sp-A",
        ),
    )
    assert len(fed.broadcasts) == 1
    _, event_type, payload = fed.broadcasts[0]
    assert event_type is FederationEventType.SPACE_SCHEDULE_FINALIZED
    assert payload["title"] == "Picnic"


# ─── F5: SCHEDULE_CREATED ──────────────────────────────────────────────


async def test_schedule_created_broadcasts(env):
    from socialhome.domain.events import SchedulePollCreated

    bus, fed = env
    await bus.publish(
        SchedulePollCreated(
            post_id="p1",
            title="Picnic?",
            deadline=None,
            slots=(
                {
                    "id": "s1",
                    "slot_date": "2026-07-01",
                    "start_time": "14:00",
                    "end_time": None,
                    "position": 0,
                },
            ),
            space_id="sp-A",
        ),
    )
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_SCHEDULE_CREATED
    assert payload["post_id"] == "p1"
    assert payload["title"] == "Picnic?"
    assert len(payload["slots"]) == 1
    assert payload["slots"][0]["id"] == "s1"


async def test_household_schedule_created_not_federated(env):
    from socialhome.domain.events import SchedulePollCreated

    bus, fed = env
    await bus.publish(
        SchedulePollCreated(
            post_id="p1",
            title="Local",
            deadline=None,
            slots=({"id": "s1", "slot_date": "2026-07-01"},),
            space_id=None,
        ),
    )
    assert fed.broadcasts == []


async def test_a_released_schedule_carries_the_approval_block(env):
    bus, fed = env
    with release_scope("item-1", "u-mod"):
        await bus.publish(
            SchedulePollCreated(
                post_id="p1",
                title="Picnic?",
                deadline=None,
                slots=(),
                space_id="sp-A",
            ),
        )
    assert fed.broadcasts[0][2]["moderation"] == {
        "item_id": "item-1",
        "approved_by": "u-mod",
    }
