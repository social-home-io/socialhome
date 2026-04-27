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

log = logging.getLogger(__name__)


class GalleryFederationOutbound:
    """Publish space-scoped gallery item mutations to paired peers."""

    __slots__ = ("_bus", "_federation", "_gallery_repo", "_space_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        gallery_repo: "AbstractGalleryRepo",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._gallery_repo = gallery_repo
        self._space_repo = space_repo

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
        # §S-9: thumbnail-only projection for the wire — the full file
        # is fetched on demand by the receiver, never preloaded.
        payload = item.to_thumbnail_dict()
        await self._fan_out(
            space_id,
            FederationEventType.SPACE_GALLERY_ITEM_CREATED,
            payload,
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
        try:
            peers = await self._space_repo.list_member_instances(space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("gallery-outbound: list peers failed: %s", exc)
            return
        own = getattr(self._federation, "_own_instance_id", "")
        for instance_id in peers:
            if instance_id == own or not instance_id:
                continue
            try:
                await self._federation.send_event(
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
