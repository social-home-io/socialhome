"""§25.6 sync of a space's gallery: albums, items and their tombstones."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ....domain.events import GalleryAlbumDeleted, GalleryItemDeleted
from ....domain.federation import FederationEvent
from ....domain.gallery import GalleryAlbum, GalleryItem, valid_taken_day
from ....domain.media_constraints import CAPTION_MAX
from ....media.cleanup import unlink_unreferenced
from ....services.inbound_media_store import space_media_ref
from ...owner_bound_id import (
    GALLERY_ALBUM_KIND,
    GALLERY_ITEM_KIND,
    OwnerBinding,
    check_owner_bound_id,
)
from .receiver_base import ReceiverBase, _bound_here, _log_tombstone_refusals
from .stream_health import MAX_TRACKED_STREAMS, HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)


class GalleryMixin(ReceiverBase):
    """The gallery resources of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_gallery(
        self,
        space_id: str,
        records: list[dict[str, Any]],
        *,
        provider: str,
        held_back: HeldBack,
    ) -> None:
        # Albums first, then items — preserve the exporter's order.
        from_host = await self._is_host(space_id, provider)
        recount: set[str] = set()
        for r in records:
            kind = r.get("kind")
            if kind == "album":
                if await self._persist_album(
                    r, space_id, from_host=from_host, held_back=held_back
                ):
                    recount.add(str(r["id"]))
            elif kind == "item" and await self._persist_gallery_item(
                r, space_id, held_back, from_host=from_host, provider=provider
            ):
                recount.add(str(r.get("album_id") or ""))
        # The album's count is what this household holds — the provider's
        # figure counts the provider's rows, and a sync item lands without
        # a bump — so it is recounted, whatever order the album record,
        # its items and their tombstones were applied in.
        for album_id in sorted(recount):
            await self._gallery_repo.recount_items(album_id)

    async def _authored_album_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_GALLERY_ALBUM_DELETED rule
        # (``_gallery_album_mutable``): a user album held live here
        # IN THIS SPACE, deleted by its owner's household or one
        # with settings authority (host / admin — not a moderator).
        held_album = await self._gallery_repo.get_album(rid) if rid else None
        if (
            held_album is None
            or held_album.space_id != space_id
            or held_album.is_system
        ):
            return False
        return await auth.may_mutate(
            event, space_id, held_album.owner_user_id or "", settings=True
        )

    async def _authored_item_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_GALLERY_ITEM_DELETED rule: an item held live
        # here in an album of THIS SPACE, deleted by its uploader's
        # household or one with content authority.
        held_item = await self._gallery_repo.get_item(rid) if rid else None
        if held_item is None:
            return False
        item_album = await self._gallery_repo.get_album(held_item.album_id)
        if item_album is None or item_album.space_id != space_id:
            return False
        return await auth.may_mutate(event, space_id, held_item.uploaded_by)

    async def _authored_gallery(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        if r.get("kind") == "album":
            if r.get("is_system"):
                return False
            if not rid or await self._gallery_repo.get_album(rid) is not None:
                return False  # a member household only adds albums
            if await self._gallery_repo.is_album_deleted(rid, space_id=space_id):
                return False  # deleted here; the tombstone wins
            owner = str(r.get("owner_user_id") or r.get("owner_id") or "")
            if await auth.acts_for(event, space_id, owner):
                return True
            if await auth.trails_seat(space_id, owner):
                held_back.add()
            return False
        if not rid or await self._gallery_repo.get_item(rid) is not None:
            return False
        if await self._gallery_repo.is_item_deleted(rid, space_id=space_id):
            return False  # deleted here; the tombstone wins
        return await self._may_author(
            held_back,
            event,
            space_id,
            str(r.get("uploaded_by") or r.get("uploader") or ""),
        )

    async def _persist_album_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed gallery-album deletes (``gallery_albums_deleted``,
        migration 0085).

        A user album held live here in this space is tombstoned with its
        items (the 0085 trigger), their files unlinked unless another row
        still names them, and ``GalleryAlbumDeleted`` published — what a
        live ``SPACE_GALLERY_ALBUM_DELETED`` does (its origin the provider,
        so nothing is re-broadcast). A member household's records were
        admitted only under the live rule. From the **host**, an id never
        held here gets a stub tombstone when the id is owner-bound to its
        ``owner_user_id`` in THIS space.
        """
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            album_id = str(r.get("id") or r.get("album_id") or "")
            if not album_id:
                continue
            deleted_by = await self._vetted_actor(r, space_id, provider, from_host)
            held = await self._gallery_repo.get_album(album_id)
            if held is not None:
                if held.space_id != space_id or held.is_system:
                    cross_space.append(album_id)
                    continue
                media = await self._gallery_repo.list_album_media(album_id)
                if await self._gallery_repo.delete_album_in_space(
                    album_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await unlink_unreferenced(self._media_dir, self._media_refs, media)
                    await self._bus.publish(
                        GalleryAlbumDeleted(
                            album_id=album_id,
                            space_id=space_id,
                            owner_id=held.owner_user_id,
                            origin_instance_id=provider,
                        )
                    )
                continue
            if not from_host or await self._gallery_repo.is_album_deleted(
                album_id, space_id=space_id
            ):
                continue
            owner = str(r.get("owner_user_id") or "")
            if not _bound_here(GALLERY_ALBUM_KIND, album_id, space_id, owner):
                unbound += 1
                continue
            await self._gallery_repo.tombstone_album(
                album_id,
                space_id=space_id,
                owner_user_id=owner,
                created_at=str(r.get("created_at") or ""),
                deleted_by=deleted_by,
            )
        _log_tombstone_refusals(
            "gallery album", provider, space_id, cross_space, unbound
        )

    async def _persist_item_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
        held_back: HeldBack,
    ) -> None:
        """Apply streamed gallery-item deletes (``gallery_items_deleted``,
        migration 0085).

        An item held live here in an album of this space is tombstoned, its
        album's count lowered, its files unlinked unless another row still
        names them, and ``GalleryItemDeleted`` published — what a live
        ``SPACE_GALLERY_ITEM_DELETED`` does. A member household's records
        were admitted only under the live rule. From the **host**, an id
        never held here gets a stub tombstone when the id is owner-bound to
        its ``uploaded_by`` in THIS space and its album is held live here in
        this space (``album_id`` is a FK); under an album tombstoned here it
        needs nothing — the album's tombstone covers it. An album not held
        here at all yet (the chunks applied out of order — ``gallery`` after
        this one) holds the stub back for the next stream
        (:class:`HeldBack`) instead of dropping the delete.
        """
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            item_id = str(r.get("id") or r.get("item_id") or "")
            if not item_id:
                continue
            deleted_by = await self._vetted_actor(r, space_id, provider, from_host)
            held = await self._gallery_repo.get_item(item_id)
            if held is not None:
                if await self._gallery_repo.delete_item_in_space(
                    item_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await unlink_unreferenced(
                        self._media_dir,
                        self._media_refs,
                        [held.url, held.thumbnail_url],
                    )
                    await self._bus.publish(
                        GalleryItemDeleted(
                            item_id=item_id,
                            album_id=held.album_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                        )
                    )
                else:
                    cross_space.append(item_id)
                continue
            if not from_host or await self._gallery_repo.is_item_deleted(
                item_id, space_id=space_id
            ):
                continue
            uploader = str(r.get("uploaded_by") or "")
            album_id = str(r.get("album_id") or "")
            if not album_id or await self._gallery_repo.is_album_deleted(
                album_id, space_id=space_id
            ):
                continue  # gone with its album; the album tombstone wins
            if not _bound_here(GALLERY_ITEM_KIND, item_id, space_id, uploader):
                unbound += 1
                continue
            if await self._gallery_repo.tombstone_item(
                item_id,
                space_id=space_id,
                album_id=album_id,
                uploaded_by=uploader,
                created_at=str(r.get("created_at") or ""),
                deleted_by=deleted_by,
            ):
                continue
            if (
                await self._gallery_repo.get_album(album_id) is None
                and album_id not in self._foreign_system_albums
            ):
                held_back.add()  # its album has not reached us yet
            else:
                unbound += 1  # its album is another space's
        _log_tombstone_refusals(
            "gallery item", provider, space_id, cross_space, unbound
        )

    def _legacy_album_deleted(self, space_id: str, album_id: str) -> bool:
        return self._legacy_album_deletes is not None and (
            self._legacy_album_deletes.is_deleted(space_id, album_id)
        )

    async def _persist_album(
        self,
        record: dict[str, Any],
        space_id: str,
        *,
        from_host: bool,
        held_back: HeldBack,
    ) -> bool:
        """Create a streamed album — or, held here already in this space for
        the same owner, take the **host's** edits of it (name, description,
        cover; the host's copy is the one the space converges on, as for
        every host record). ``True`` when the album is held here in this
        space afterwards, so its item count is recounted. A member
        household's record only ever adds an album (:meth:`_admit`)."""
        if self._legacy_album_deleted(
            space_id, str(record["id"])
        ) or await self._gallery_repo.is_album_deleted(
            str(record["id"]), space_id=space_id
        ):
            log.debug("sync: gallery album %s was deleted here — skipped", record["id"])
            return False
        if record.get("is_system"):
            ours = await self._gallery_repo.get_system_album(space_id)
            if ours is not None and ours.id != str(record["id"]):
                # Every household mints its own system ("Posts") album, so a
                # provider's is never stored here once we hold ours (the
                # partial unique index). Remember its id: an item or
                # tombstone naming it can never land — a refusal by rule,
                # never a hold.
                self._foreign_system_albums[str(record["id"])] = None
                while len(self._foreign_system_albums) > MAX_TRACKED_STREAMS:
                    self._foreign_system_albums.popitem(last=False)
                return False
        # The album lands in the space this sync stream was gated for —
        # never the record's own ``space_id`` (another space, or NULL =
        # the household gallery), which is just untrusted payload.
        album = _album_from_record(record, space_id)
        if (
            check_owner_bound_id(
                GALLERY_ALBUM_KIND,
                album.id,
                space_id=space_id,
                owner_user_id=album.owner_user_id,
            )
            is OwnerBinding.MISMATCH
        ):
            # An owner-bound id commits to its creator; a record naming
            # anyone else is a claim on another household's album.
            log.warning(
                "sync: gallery album id %s is not bound to %r in space %s — skipped",
                album.id,
                album.owner_user_id,
                space_id,
            )
            return False
        held = await self._gallery_repo.get_album(album.id)
        if held is not None:
            if held.space_id != space_id or held.owner_user_id != album.owner_user_id:
                log.warning(
                    "sync: gallery album %s is held for another space or owner "
                    "— refusing the write for %s",
                    album.id,
                    space_id,
                )
                return False
            if from_host and not held.is_system:
                # An idempotent re-apply changes no synced column, so it
                # stamps nothing (migration 0086) — no echo.
                await self._gallery_repo.update_album_in_space(
                    album.id,
                    {
                        "name": album.name,
                        "description": album.description,
                        "cover_item_id": album.cover_item_id,
                    },
                    space_id=space_id,
                )
            return True
        try:
            await self._gallery_repo.create_album(album)
        except Exception:
            # NOT a duplicate: ``create_album`` is already
            # ``ON CONFLICT DO NOTHING``, so a redelivered album is silent
            # at the SQL layer and never reaches here. The previous comment
            # claimed otherwise and the blanket ``pass`` hid the real
            # failure for as long as gallery sync has existed — a remotely
            # owned album violated ``owner_user_id REFERENCES users``, so a
            # member household got the image bytes and no rows (#650).
            # Anything landing here is worth seeing.
            log.warning(
                "sync: persisting gallery album %s (space=%s) failed",
                album.id,
                album.space_id,
                exc_info=True,
            )
            held_back.fail()  # a failed persist: the stream is not clean
            return False
        return True

    async def _take_host_item_edit(
        self, held: GalleryItem, streamed: GalleryItem, space_id: str
    ) -> None:
        """Apply the host's caption / day taken / album of an item held here
        (v_79), under the live ``SPACE_GALLERY_ITEM_UPDATED`` limits: a value
        the local rules refuse is skipped, never stored. The repo keeps the
        item, and any move, inside this space's live user albums."""
        patch: dict = {}
        caption = streamed.caption
        if caption is None or (
            isinstance(caption, str) and len(caption.strip()) <= CAPTION_MAX
        ):
            patch["caption"] = (caption or "").strip() or None
        try:
            patch["taken_at"] = valid_taken_day(streamed.taken_at)
        except ValueError:
            pass
        patch = {k: v for k, v in patch.items() if getattr(held, k) != v}
        to_album = streamed.album_id if streamed.album_id != held.album_id else None
        if not patch and to_album is None:
            return  # nothing the local rules take differs
        if (
            await self._gallery_repo.edit_item_in_space(
                held.id, space_id=space_id, patch=patch, to_album_id=to_album
            )
            is None
        ):
            log.debug(
                "sync: the host's edit of gallery item %s not applied in %s",
                held.id,
                space_id,
            )

    async def _persist_gallery_item(
        self,
        record: dict[str, Any],
        space_id: str,
        held_back: HeldBack,
        *,
        from_host: bool = False,
        provider: str = "",
    ) -> bool:
        """Store a streamed item — or, held here already in this space, take
        the **host's** caption, day taken and album (v_79: a space photo is
        edited in place, and the host's copy is the one the space converges
        on, as for albums). ``True`` when it landed in an album of this
        space (its count is then recounted)."""
        album_id = str(record.get("album_id") or "")
        if self._legacy_album_deleted(
            space_id, album_id
        ) or await self._gallery_repo.is_album_deleted(album_id, space_id=space_id):
            return False  # its album was deleted here; so was the item
        if await self._gallery_repo.is_item_deleted(
            str(record["id"]), space_id=space_id
        ):
            log.debug("sync: gallery item %s was deleted here — skipped", record["id"])
            return False
        item = GalleryItem(
            id=str(record["id"]),
            album_id=album_id,
            uploaded_by=str(record.get("uploaded_by") or record.get("uploader") or ""),
            item_type=str(record.get("item_type") or "photo"),
            # Only the canonical local ``api/media/<name>`` shape is stored.
            url=space_media_ref(record.get("url")) or "",
            thumbnail_url=space_media_ref(record.get("thumbnail_url")) or "",
            width=int(record.get("width") or 0),
            height=int(record.get("height") or 0),
            duration_s=record.get("duration_s"),
            caption=record.get("caption"),
            taken_at=record.get("taken_at") or record.get("day_taken"),
            sort_order=int(record.get("sort_order") or 0),
            created_at=record.get("created_at"),
        )
        held = (
            await self._gallery_repo.get_item(item.id)
            if from_host or self._gfs_media is not None
            else None
        )
        if held is not None and not from_host:
            # Held here already: a member's stream adds nothing to it (the
            # insert below is a no-op) — and never its own media_refs.
            held_elsewhere = held
            held = None
        else:
            held_elsewhere = None
        if held is not None:
            held_album = await self._gallery_repo.get_album(held.album_id)
            if held_album is None or held_album.space_id != space_id:
                # The id is held here for another space or the household
                # gallery: the record is not this space's to edit (nor does
                # it land — an id is never reused).
                log.warning(
                    "sync: gallery item %s is held outside space %s here "
                    "— refusing the write",
                    item.id,
                    space_id,
                )
                return False
            if _item_edited(held, item):
                await self._take_host_item_edit(held, item, space_id)
            return True
        # v_80 — a NEW item's files take this household's names for the
        # copies in the GFS media cache (bound to the streaming household).
        media = await self._localize_record(
            space_id,
            record,
            item.id,
            [u for u in (item.thumbnail_url, item.url) if u],
            provider=provider,
            takes_descriptor=held_elsewhere is None,
            from_host=from_host,
        )
        item = replace(
            item,
            url=media.url(item.url) or "",
            thumbnail_url=media.url(item.thumbnail_url) or "",
        )
        try:
            # ``bump_count=False``: the caller recounts the album once the
            # chunk is in. The album must belong to this sync's space.
            if await self._gallery_repo.create_item_in_space(
                item, space_id=space_id, bump_count=False
            ):
                if held_elsewhere is None:
                    await self._keep_media(media)
                return True
            if (
                await self._gallery_repo.get_album(album_id) is None
                and album_id not in self._foreign_system_albums
            ):
                # Its album is not here yet (a chunk applied out of order):
                # a later stream lands it — not a refusal.
                held_back.add()
                return False
            log.warning(
                "sync: gallery item %s names album %s outside space %s "
                "— refusing the write",
                item.id,
                item.album_id,
                space_id,
            )
        except Exception:
            # Same reasoning as ``_persist_album``: redelivery is handled in
            # SQL, so a failure here is real (a missing parent album, a bad
            # record) and must not be silent — nor count as stored.
            log.warning(
                "sync: persisting gallery item %s (album=%s) failed",
                item.id,
                item.album_id,
                exc_info=True,
            )
            held_back.fail()
        return False


def _item_edited(held: GalleryItem, streamed: GalleryItem) -> bool:
    """The host's record of a held item differs in an editable field."""
    return (
        held.album_id != streamed.album_id
        or held.caption != streamed.caption
        or held.taken_at != streamed.taken_at
    )


def _album_from_record(record: dict[str, Any], space_id: str) -> GalleryAlbum:
    """The album a sync record describes, filed under ``space_id``."""
    return GalleryAlbum(
        id=str(record["id"]),
        space_id=space_id,
        owner_user_id=(
            # System albums have no human owner; carry NULL.
            None
            if record.get("is_system")
            else (
                str(record.get("owner_user_id") or record.get("owner_id") or "") or None
            )
        ),
        name=str(record.get("name") or ""),
        description=record.get("description"),
        cover_item_id=record.get("cover_item_id"),
        item_count=int(record.get("item_count") or 0),
        retention_exempt=bool(record.get("retention_exempt", False)),
        is_system=bool(record.get("is_system", False)),
        created_at=record.get("created_at"),
    )
