"""PageFederationOutbound — mesh-routed SPACE_PAGE_* fan-out (F3).

Before this PR pages had an inbound handler in
``federation_inbound/space_content.py`` but no matching outbound,
so wiki edits stayed purely local until the next §25.6 catch-up.
"""

from __future__ import annotations

import pytest

from socialhome.domain.events import PageCreated, PageDeleted, PageUpdated
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.moderation_release import release_scope
from socialhome.services.page_federation_outbound import (
    PageFederationOutbound,
)


class _FakeFed:
    def __init__(self, *, raise_on_broadcast: bool = False) -> None:
        self.broadcasts: list[tuple[str, FederationEventType, dict]] = []
        self.kwargs: list[dict] = []
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
        self.kwargs.append(kwargs)


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFed()
    out = PageFederationOutbound(bus=bus, federation_service=fed)
    out.wire()
    return bus, fed


async def test_household_page_is_not_federated(env):
    bus, fed = env
    await bus.publish(
        PageCreated(
            page_id="p1",
            space_id=None,
            title="Notes",
            content="body",
        ),
    )
    assert fed.broadcasts == []


async def test_page_created_broadcasts(env):
    bus, fed = env
    await bus.publish(
        PageCreated(
            page_id="p1",
            space_id="sp-A",
            title="Notes",
            content="body",
        ),
    )
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_PAGE_CREATED
    assert payload["id"] == "p1"
    assert payload["page_id"] == "p1"
    assert payload["title"] == "Notes"
    assert payload["content"] == "body"


async def test_page_updated_broadcasts(env):
    bus, fed = env
    await bus.publish(
        PageUpdated(
            page_id="p1",
            space_id="sp-A",
            title="Notes v2",
            content="new body",
        ),
    )
    assert len(fed.broadcasts) == 1
    _, event_type, payload = fed.broadcasts[0]
    assert event_type is FederationEventType.SPACE_PAGE_UPDATED
    assert payload["title"] == "Notes v2"


async def test_page_deleted_with_space_id_broadcasts(env):
    bus, fed = env
    await bus.publish(PageDeleted(page_id="p1", space_id="sp-A"))
    assert len(fed.broadcasts) == 1
    _, event_type, payload = fed.broadcasts[0]
    assert event_type is FederationEventType.SPACE_PAGE_DELETED
    assert payload == {
        "id": "p1",
        "page_id": "p1",
        "space_id": "sp-A",
    }


async def test_page_deleted_without_space_id_is_not_federated(env):
    """Household-scoped page deletion (space_id None) stays local."""
    bus, fed = env
    await bus.publish(PageDeleted(page_id="p1", space_id=None))
    assert fed.broadcasts == []


async def test_broadcast_failure_is_swallowed():
    bus = EventBus()
    fed = _FakeFed(raise_on_broadcast=True)
    PageFederationOutbound(bus=bus, federation_service=fed).wire()
    # Must not raise.
    await bus.publish(
        PageCreated(
            page_id="p1",
            space_id="sp-A",
            title="t",
            content="b",
        ),
    )
    assert fed.broadcasts == []


async def test_every_write_carries_its_actor(env):
    """v_42: the actor rides inside the sealed payload so receivers can
    judge the write against the space's ``pages`` access level."""
    bus, fed = env
    await bus.publish(
        PageCreated(
            page_id="p1", space_id="sp-A", title="T", content="c", actor_user_id="u-a"
        )
    )
    await bus.publish(
        PageUpdated(
            page_id="p1", space_id="sp-A", title="T", content="d", actor_user_id="u-b"
        )
    )
    await bus.publish(PageDeleted(page_id="p1", space_id="sp-A", actor_user_id="u-c"))
    assert [p["actor_user_id"] for _s, _t, p in fed.broadcasts] == [
        "u-a",
        "u-b",
        "u-c",
    ]
    # A create also names its creator, so a receiver attributes the row.
    assert fed.broadcasts[0][2]["created_by"] == "u-a"


async def test_a_moderation_release_carries_the_approval_block(env):
    bus, fed = env
    with release_scope("item-1", "u-mod"):
        await bus.publish(
            PageCreated(page_id="p1", space_id="sp", title="T", content="b")
        )
        await bus.publish(
            PageUpdated(page_id="p1", space_id="sp", title="T", content="c")
        )
        await bus.publish(PageDeleted(page_id="p1", space_id="sp"))
    block = {"item_id": "item-1", "approved_by": "u-mod"}
    assert [p.get("moderation") for _s, _t, p in fed.broadcasts] == [block] * 3


_CANON = {
    "seq": 4,
    "version_hash": "sha256:" + "a" * 64,
    "conflict": [],
    "updated_at": "2026-10-03T00:00:00+00:00",
    "last_editor_user_id": "u1",
    "cover_image_url": None,
}


async def test_a_host_version_carries_seq_and_a_legacy_payload_for_v47(env):
    """v_48: the host's canonical version ships its ``seq``, hash and
    conflict list; a member below v_48 gets the plain fields (its release
    check refuses unknown keys) and stays last write wins."""
    from socialhome.domain.federation_capabilities import FederationCapability

    bus, fed = env
    await bus.publish(
        PageUpdated(
            page_id="p1",
            space_id="sp-A",
            title="Notes",
            content="body",
            actor_user_id="u1",
            canonical=dict(_CANON),
        ),
    )
    (_sid, et, payload) = fed.broadcasts[0]
    assert et is FederationEventType.SPACE_PAGE_UPDATED
    assert payload["seq"] == 4 and payload["conflict"] == []
    kw = fed.kwargs[0]
    assert kw["legacy_below"] == FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES
    legacy = kw["legacy_payload"]
    assert not set(_CANON) & set(legacy)
    assert {k: v for k, v in payload.items() if k not in _CANON} == legacy


async def test_a_host_create_carries_its_canonical_fields(env):
    bus, fed = env
    await bus.publish(
        PageCreated(
            page_id="p1",
            space_id="sp-A",
            title="N",
            content="b",
            actor_user_id="u1",
            canonical={**_CANON, "seq": 1, "created_by": "u1"},
        )
    )
    (_s, et, payload) = fed.broadcasts[0]
    assert et is FederationEventType.SPACE_PAGE_CREATED and payload["seq"] == 1
    assert "seq" not in fed.kwargs[0]["legacy_payload"]


async def test_a_released_version_carries_the_block_on_both_variants(env):
    bus, fed = env
    with release_scope("item-1", "u-mod"):
        await bus.publish(
            PageUpdated(
                page_id="p1",
                space_id="sp-A",
                title="N",
                content="b",
                canonical=dict(_CANON),
            )
        )
    payload = fed.broadcasts[0][2]
    assert payload["moderation"]["item_id"] == "item-1"
    assert fed.kwargs[0]["legacy_payload"]["moderation"]["item_id"] == "item-1"


async def test_a_members_draft_is_never_broadcast(env):
    """A ``proposal`` goes to the host alone — the forwarder's job."""
    bus, fed = env
    await bus.publish(
        PageUpdated(
            page_id="p1", space_id="sp-A", title="N", content="b", proposal=True
        )
    )
    await bus.publish(
        PageCreated(
            page_id="p2", space_id="sp-A", title="N", content="b", proposal=True
        )
    )
    assert fed.broadcasts == []


async def test_a_legacy_write_goes_out_as_before(env):
    bus, fed = env
    await bus.publish(
        PageCreated(page_id="p1", space_id="sp-A", title="N", content="b")
    )
    await bus.publish(
        PageUpdated(page_id="p1", space_id="sp-A", title="N", content="c")
    )
    await bus.publish(PageDeleted(page_id="p1", space_id="sp-A"))
    assert all("seq" not in p for _s, _e, p in fed.broadcasts)
    assert fed.kwargs == [{}, {}, {}]
