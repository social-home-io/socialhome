"""Outbound federation for space-scoped gallery items (§23.119).

Subscribes to :class:`GalleryItemUploaded` / :class:`GalleryItemDeleted`
domain events. When the item lives in a space-scoped album (not a
household album), fans the matching ``SPACE_GALLERY_ITEM_*``
federation event out to every peer instance that's a member of the
space.

Per-event push complements the chunked initial-sync path
(``federation/sync/space/exporters/gallery.py``): subscribers still
receive the full album + items snapshot on their next sync tick, but
between ticks they see new uploads in near real-time, and
``SPACE_SYNC_RESUME`` (§4.4) replays them on long-offline catch-up.

Household-scoped items (album with ``space_id IS NULL``) stay local —
no peer has a right to know about them. The album lookup is the
gate: we never emit when the resolved album has a NULL
``space_id``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import GalleryItemDeleted, GalleryItemUploaded
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.gallery_repo import AbstractGalleryRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .space_media_sync_service import SpaceMediaSyncService

log = logging.getLogger(__name__)


class GalleryFederationOutbound:
    """Publish space-scoped gallery item mutations to paired peers."""

    __slots__ = (
        "_bus",
        "_federation",
        "_gallery_repo",
        "_space_repo",
        "_media_sync",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        gallery_repo: "AbstractGalleryRepo",
        space_repo: "AbstractSpaceRepo",
        media_sync: "SpaceMediaSyncService | None" = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._gallery_repo = gallery_repo
        self._space_repo = space_repo
        #: Optional — when wired, the gallery item's full + thumbnail
        #: bytes federate via the shared ``space_media_outbox``. The
        #: receiver writes them to its own media path so the
        #: thumbnail_url + url on the rendered item resolves. Without
        #: this the SPA renders a broken thumbnail (spec §S-9 promises
        #: an on-demand fetch endpoint that doesn't exist yet).
        self._media_sync = media_sync

    def wire(self) -> None:
        """Subscribe handlers on the bus. Idempotent."""
        self._bus.subscribe(GalleryItemUploaded, self._on_uploaded)
        self._bus.subscribe(GalleryItemDeleted, self._on_deleted)

    async def _on_uploaded(self, event: GalleryItemUploaded) -> None:
        space_id = await self._space_id_for_album(event.album_id)
        if space_id is None:
            return  # household-level — no federation
        item = await self._gallery_repo.get_item(event.item_id)
        if item is None:
            return  # raced with delete
        # System-album mirrors don't federate as gallery events: the
        # source post propagates via SPACE_POST_CREATED, the receiver
        # rebuilds its own mirror locally. Federating here would
        # double-add on the peer.
        if item.source_post_id is not None:
            return
        # §S-9: thumbnail-only projection for the wire — the full file
        # is fetched on demand by the receiver, never preloaded.
        payload = item.to_thumbnail_dict()
        await self._fan_out(
            space_id,
            FederationEventType.SPACE_GALLERY_ITEM_CREATED,
            payload,
        )
        # Ship the actual bytes via the shared media outbox so
        # remote members can render the thumbnail + full image. The
        # gallery item carries TWO URLs (thumbnail + full); both get
        # an outbox row per peer. Same pattern post media uses.
        if self._media_sync is not None:
            media_urls: list[str] = []
            if item.thumbnail_url:
                media_urls.append(item.thumbnail_url)
            if item.url and item.url != item.thumbnail_url:
                media_urls.append(item.url)
            if media_urls:
                try:
                    peers = await self._space_repo.list_member_instances(
                        space_id,
                    )
                except Exception:
                    log.exception(
                        "gallery-outbound: enqueue list peers failed for "
                        "space=%s item=%s",
                        space_id,
                        item.id,
                    )
                    peers = []
                own = getattr(self._federation, "_own_instance_id", "") or ""
                targets = [p for p in peers if p and p != own]
                if targets:
                    try:
                        await self._media_sync.enqueue_for_blob(
                            space_id=space_id,
                            correlation_id=item.id,
                            target_instance_ids=targets,
                            media_urls=media_urls,
                        )
                    except Exception:
                        log.exception(
                            "gallery-outbound: media-sync enqueue failed "
                            "for space=%s item=%s",
                            space_id,
                            item.id,
                        )

    async def _on_deleted(self, event: GalleryItemDeleted) -> None:
        space_id = await self._space_id_for_album(event.album_id)
        if space_id is None:
            return
        await self._fan_out(
            space_id,
            FederationEventType.SPACE_GALLERY_ITEM_DELETED,
            {"id": event.item_id, "album_id": event.album_id},
        )

    async def _space_id_for_album(self, album_id: str) -> str | None:
        try:
            album = await self._gallery_repo.get_album(album_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("gallery-outbound: album lookup failed: %s", exc)
            return None
        if album is None:
            return None
        return album.space_id

    async def _fan_out(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        # The space rides the payload as well as the routing field. A
        # mesh-relayed envelope (SPACE_ROUTED) carries no plaintext
        # routing ``space_id`` — a relay must not learn which space is
        # being served — so the payload's copy is the only thing the
        # receiver's §24.11 space-writer gate can read there. Additive;
        # older peers ignore it.
        payload = {**payload, "space_id": space_id}
        try:
            peers = await self._space_repo.list_member_instances(space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("gallery-outbound: list peers failed: %s", exc)
            return
        own = getattr(self._federation, "_own_instance_id", "")
        for instance_id in peers:
            if instance_id == own or not instance_id:
                continue
            # ``send_with_mesh_fallback`` lets mesh-only members
            # (joined via §D1b through a relay) receive the event
            # over ``SPACE_ROUTED`` instead of failing
            # ``not_confirmed``. Matches the post-content fanout
            # behaviour.
            try:
                await self._federation.send_with_mesh_fallback(
                    to_instance_id=instance_id,
                    event_type=event_type,
                    payload=payload,
                    space_id=space_id,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "gallery-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )
