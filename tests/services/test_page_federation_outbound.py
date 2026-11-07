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


# ── v_81: a host version's new files reach every member household ──────


class _Media:
    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[dict] = []
        self.boom = boom

    async def enqueue_for_blob(self, **kw) -> None:
        if self.boom:
            raise OSError("disk")
        self.calls.append(kw)


class _Spaces:
    async def list_member_instances(self, space_id):
        return ["us", "peer-new", "peer-old", "reader", ""]


class _VersionedFed(_FakeFed):
    own_instance_id = "us"

    def __init__(self) -> None:
        super().__init__()
        self.versions = {"peer-new": 81, "peer-old": 80, "reader": 81}

    async def space_member_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 0) >= min_version


class _Plan:
    def wire(self):
        return {"x": {"blob_id": "b" * 64}}


class _Gfs:
    def __init__(self) -> None:
        self.plans: list[tuple[str, list[str], str]] = []
        self.uploaded: list = []

    async def plan(self, space_id, urls, *, item_id, followers):
        self.plans.append((space_id, list(urls), item_id))
        return _Plan()

    async def is_relay_reader(self, iid):
        return iid in ("reader", "old-reader")

    def upload(self, plan):
        self.uploaded.append(plan)


BODY = "![a](api/media/a.webp) ![b](/api/media/b.webp?exp=1)"


@pytest.fixture
def media_env():
    bus = EventBus()
    fed = _VersionedFed()
    media = _Media()
    gfs = _Gfs()
    out = PageFederationOutbound(bus=bus, federation_service=fed)
    out.attach_media(media_sync=media, space_repo=_Spaces())
    out.attach_media_gfs(gfs)
    out.wire()
    return bus, fed, media, gfs


async def test_a_host_version_ships_its_files_to_every_v81_member(media_env):
    """A new page's cover and body pictures go to every other member
    household at v_81 (paired or mesh — the outbox reaches both), never to
    an older one, correlated by the page."""
    bus, _fed, media, gfs = media_env
    await bus.publish(
        PageCreated(
            page_id="p1",
            space_id="sp-A",
            title="N",
            content=BODY,
            canonical={**_CANON, "seq": 1, "cover_image_url": "api/media/c.webp"},
        )
    )
    assert media.calls == [
        {
            "space_id": "sp-A",
            "correlation_id": "p1",
            "target_instance_ids": ["peer-new", "reader"],
            "media_urls": [
                "api/media/c.webp",
                "api/media/a.webp",
                "/api/media/b.webp",
            ],
        }
    ]
    assert gfs.plans == [("sp-A", media.calls[0]["media_urls"], "p1")]
    assert len(gfs.uploaded) == 1


async def test_a_host_edit_ships_only_the_files_it_adds(media_env):
    bus, _fed, media, gfs = media_env
    await bus.publish(
        PageUpdated(
            page_id="p1",
            space_id="sp-A",
            title="N",
            content=BODY + " ![n](api/media/n.webp)",
            canonical=dict(_CANON),
            shown_before=("api/media/a.webp", "api/media/b.webp"),
        )
    )
    assert [c["media_urls"] for c in media.calls] == [["api/media/n.webp"]]
    assert [p[1] for p in gfs.plans] == [["api/media/n.webp"]]
    media.calls.clear()
    await bus.publish(
        PageUpdated(
            page_id="p1",
            space_id="sp-A",
            title="Renamed",
            content=BODY,
            canonical=dict(_CANON),
            shown_before=("api/media/a.webp", "/api/media/b.webp"),
        )
    )
    assert media.calls == []


async def test_only_a_v81_link_joined_member_gets_the_page_descriptor(media_env):
    bus, fed, _media, _gfs = media_env
    fed.versions["old-reader"] = 80
    await bus.publish(
        PageCreated(
            page_id="p1",
            space_id="sp-A",
            title="N",
            content=BODY,
            canonical={**_CANON, "seq": 1},
        )
    )
    per_peer = fed.kwargs[0]["per_peer"]
    shared = fed.broadcasts[0][2]
    assert (await per_peer("reader", shared))["media_refs"] == _Plan().wire()
    assert "media_refs" not in await per_peer("old-reader", shared)
    assert "media_refs" not in await per_peer("peer-new", shared)
    assert "media_refs" not in shared


async def test_no_files_and_member_drafts_ship_nothing(media_env):
    bus, fed, media, gfs = media_env
    await bus.publish(
        PageCreated(
            page_id="p1",
            space_id="sp-A",
            title="N",
            content="no pictures",
            canonical={**_CANON, "seq": 1},
        )
    )
    await bus.publish(
        PageUpdated(page_id="p1", space_id="sp-A", title="N", content=BODY)
    )
    assert media.calls == [] and gfs.plans == []
    assert "per_peer" not in fed.kwargs[0]


async def test_a_failed_file_queue_never_stops_the_page(media_env):
    bus, fed, media, _gfs = media_env
    media.boom = True
    await bus.publish(
        PageCreated(
            page_id="p1",
            space_id="sp-A",
            title="N",
            content=BODY,
            canonical={**_CANON, "seq": 1},
        )
    )
    assert len(fed.broadcasts) == 1
