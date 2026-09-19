"""GalleryFederationOutbound — per-event fan-out for space gallery items."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from socialhome.domain.events import GalleryItemDeleted, GalleryItemUploaded
from socialhome.domain.federation import FederationEventType
from socialhome.domain.gallery import GalleryAlbum, GalleryItem
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.gallery_federation_outbound import (
    GalleryFederationOutbound,
)


class _FakeFederationService:
    def __init__(self, own_instance_id: str = "own-inst") -> None:
        self._own_instance_id = own_instance_id
        self.sent: list[tuple] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        # Kept for back-compat — older tests still call this path.
        self.sent.append((to_instance_id, event_type, payload, space_id))

    async def send_with_mesh_fallback(
        self,
        *,
        to_instance_id,
        event_type,
        payload,
        space_id=None,
    ):
        # Gallery outbound now uses mesh-fallback so members joined
        # via §D1b mesh path receive the event too.
        from socialhome.domain.federation import DeliveryResult

        self.sent.append((to_instance_id, event_type, payload, space_id))
        return DeliveryResult(instance_id=to_instance_id, ok=True)


class _FakeSpaceRepo:
    def __init__(self, members: dict[str, list[str]]) -> None:
        self._members = members

    async def list_member_instances(self, space_id: str) -> list[str]:
        return list(self._members.get(space_id, []))


class _FakeGalleryRepo:
    def __init__(
        self,
        albums: dict[str, GalleryAlbum] | None = None,
        items: dict[str, GalleryItem] | None = None,
    ) -> None:
        self.albums = albums or {}
        self.items = items or {}

    async def get_album(self, album_id: str) -> GalleryAlbum | None:
        return self.albums.get(album_id)

    async def get_item(self, item_id: str) -> GalleryItem | None:
        return self.items.get(item_id)


def _album(album_id: str, *, space_id: str | None) -> GalleryAlbum:
    return GalleryAlbum(
        id=album_id,
        space_id=space_id,
        owner_user_id="alice",
        name=f"Album {album_id}",
    )


def _item(item_id: str, album_id: str) -> GalleryItem:
    return GalleryItem(
        id=item_id,
        album_id=album_id,
        uploaded_by="alice",
        item_type="photo",
        url=f"/api/media/{item_id}.jpg",
        thumbnail_url=f"/api/media/{item_id}-thumb.jpg",
        width=1920,
        height=1080,
        created_at="2026-04-10T12:00:00+00:00",
    )


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    space_repo = _FakeSpaceRepo(
        {"sp-A": ["own-inst", "peer-1", "peer-2"], "sp-B": ["peer-3"]},
    )
    gallery = _FakeGalleryRepo(
        albums={
            "alb-space-A": _album("alb-space-A", space_id="sp-A"),
            "alb-house": _album("alb-house", space_id=None),
        },
        items={
            "it-1": _item("it-1", "alb-space-A"),
            "it-house": _item("it-house", "alb-house"),
        },
    )
    out = GalleryFederationOutbound(
        bus=bus,
        federation_service=fed,
        gallery_repo=gallery,
        space_repo=space_repo,
    )
    out.wire()
    return bus, fed, gallery


async def test_household_album_item_is_not_federated(env):
    """Items in NULL-space-id albums stay local."""
    bus, fed, _ = env
    await bus.publish(
        GalleryItemUploaded(
            item_id="it-house",
            album_id="alb-house",
            item_type="photo",
            uploader="alice",
        ),
    )
    assert fed.sent == []


async def test_space_item_fanouts_to_peers_excluding_self(env):
    bus, fed, _ = env
    await bus.publish(
        GalleryItemUploaded(
            item_id="it-1",
            album_id="alb-space-A",
            item_type="photo",
            uploader="alice",
        ),
    )
    targets = {entry[0] for entry in fed.sent}
    assert targets == {"peer-1", "peer-2"}  # excludes own-inst
    types = {entry[1] for entry in fed.sent}
    assert types == {FederationEventType.SPACE_GALLERY_ITEM_CREATED}
    payload = fed.sent[0][2]
    # §S-9 thumbnail-only projection — full ``url`` excluded.
    assert "url" not in payload
    assert {"id", "album_id", "thumbnail_url", "uploaded_by"} <= set(payload)
    assert all(entry[3] == "sp-A" for entry in fed.sent)


async def test_unknown_album_drops_silently(env):
    """Album lookup miss → no fanout, no error."""
    bus, fed, _ = env
    await bus.publish(
        GalleryItemUploaded(
            item_id="x",
            album_id="missing",
            item_type="photo",
            uploader="alice",
        ),
    )
    assert fed.sent == []


async def test_deleted_event_emits_delete(env):
    bus, fed, _ = env
    await bus.publish(
        GalleryItemDeleted(item_id="it-1", album_id="alb-space-A"),
    )
    types = {entry[1] for entry in fed.sent}
    assert types == {FederationEventType.SPACE_GALLERY_ITEM_DELETED}
    payload = fed.sent[0][2]
    # ``space_id`` rides the payload so a mesh-relayed envelope — which
    # carries no plaintext routing space_id — is still attributable to a
    # space by the receiver's §24.11 space-writer gate.
    assert payload == {
        "id": "it-1",
        "album_id": "alb-space-A",
        "space_id": "sp-A",
    }


async def test_deleted_event_for_household_album_skipped(env):
    bus, fed, _ = env
    await bus.publish(
        GalleryItemDeleted(item_id="it-house", album_id="alb-house"),
    )
    assert fed.sent == []


async def test_uploaded_event_for_missing_item_skipped(env):
    """Race: item deleted before outbound runs → no payload to send."""
    bus, fed, gallery = env
    gallery.items.pop("it-1", None)
    await bus.publish(
        GalleryItemUploaded(
            item_id="it-1",
            album_id="alb-space-A",
            item_type="photo",
            uploader="alice",
        ),
    )
    assert fed.sent == []


async def test_uploaded_event_enqueues_media_blob_for_peers():
    """Gallery item upload enqueues both thumbnail + full URLs in
    the space-media outbox so the receiver renders the thumbnail
    AND can zoom to full. Without this the SPA shows a broken
    image because the URLs point at the receiver's own (empty)
    media path."""
    from socialhome.services.gallery_federation_outbound import (
        GalleryFederationOutbound,
    )

    bus = EventBus()
    fed = _FakeFederationService(own_instance_id="self-id")
    item = GalleryItem(
        id="it-mediated",
        album_id="alb-space",
        uploaded_by="alice",
        item_type="photo",
        url="api/media/full.webp",
        thumbnail_url="api/media/thumb.webp",
        width=1024,
        height=768,
    )
    gallery = _FakeGalleryRepo(
        albums={"alb-space": _album("alb-space", space_id="sp-mediated")},
        items={"it-mediated": item},
    )
    space_repo = _FakeSpaceRepo({"sp-mediated": ["peer-a", "peer-b", "self-id"]})
    media_sync = AsyncMock()
    media_sync.enqueue_for_blob = AsyncMock()
    GalleryFederationOutbound(
        bus=bus,
        federation_service=fed,
        gallery_repo=gallery,
        space_repo=space_repo,
        media_sync=media_sync,
    ).wire()
    await bus.publish(
        GalleryItemUploaded(
            item_id="it-mediated",
            album_id="alb-space",
            item_type="photo",
            uploader="alice",
        ),
    )
    media_sync.enqueue_for_blob.assert_awaited_once()
    call = media_sync.enqueue_for_blob.call_args
    assert call.kwargs["space_id"] == "sp-mediated"
    assert call.kwargs["correlation_id"] == "it-mediated"
    # Self instance is filtered out.
    assert set(call.kwargs["target_instance_ids"]) == {"peer-a", "peer-b"}
    # Thumbnail AND full URLs enqueued; dedup on identical URLs.
    assert set(call.kwargs["media_urls"]) == {
        "api/media/thumb.webp",
        "api/media/full.webp",
    }
