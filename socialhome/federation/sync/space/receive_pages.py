"""§25.6 sync of a space's pages: ``pages`` (v_48 versions) and
``pages_deleted``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ....domain.events import PageDeleted
from ....domain.federation import FederationEvent
from ....domain.page import Page
from ....domain.relay_media_frame import bounded_id, bounded_stamp
from ....domain.space_media_ref import MEDIA_REFS_FIELD, page_media_urls
from ....services.page_conflict_service import (
    PageMode,
    canonical_from_wire,
    log_page_not_stored,
    page_too_large,
)
from ...owner_bound_id import SPACE_PAGE_KIND, OwnerBinding, check_owner_bound_id
from .receiver_base import ReceiverBase, _log_sync_refusal, _sync_event
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)


class PagesMixin(ReceiverBase):
    """The page resources of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_pages(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        for r in records:
            page = _page_from_record(r, space_id)
            if page is None:
                continue
            if await self._page_repo.is_page_deleted(
                page.id, space_id=space_id
            ) and not await self._is_host_version(space_id, provider, r):
                # Deleted here — a provider that missed the delete still
                # streams it; the tombstone wins (its id is never
                # reused), and its own tombstone stream will tell it.
                # Only the host's version may revive a tombstone, and
                # only one the host never confirmed (the engine's call).
                log.debug("sync: page %s was deleted here — skipped", page.id)
                continue
            if await self._apply_page_version(page, r, space_id, provider):
                continue
            # The one receiver rule (a host version's is the engine's):
            # over the limit and larger than our copy → not stored. The
            # stream goes on; the watermark moves.
            held_page = await self._page_repo.get_space_page(page.id, space_id=space_id)
            if page_too_large(
                page, (), was=(held_page, ()) if held_page is not None else None
            ):
                log_page_not_stored("space sync", page, (), provider)
                continue
            if not await self._page_repo.save(page, space_id=space_id):
                _log_sync_refusal("page", page.id, space_id)

    async def _authored_page_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_PAGE_DELETED rule: a writer household
        # removes a page held live here IN THIS SPACE, if the
        # space's ``pages`` level admits the delete for the user who
        # made it (``actor_user_id``), who must be seated on the
        # provider. Refusals are counted in :meth:`_admit`.
        rid = rid or str(r.get("page_id") or "")
        held_page = await self._page_repo.get(rid) if rid else None
        if held_page is None or held_page.space_id != space_id:
            return False
        return await self._writer_delete_admits(
            event, space_id, "pages", r, row_owner=held_page.created_by
        )

    async def _authored_page(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        if not rid or await self._page_repo.get(rid) is not None:
            return False
        if await self._page_repo.is_page_deleted(rid, space_id=space_id):
            return False  # deleted here; the tombstone wins
        creator = str(r.get("created_by") or "")
        if creator:
            return await self._may_author(held_back, event, space_id, creator)
        return await auth.writes_here(event, space_id)

    async def _persist_page_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed page deletes (``pages_deleted``).

        A page held live here in this space is tombstoned (``deleted_by``
        from the record's ``actor_user_id``) exactly as a live
        ``SPACE_PAGE_DELETED`` does — confirmed (final) when the host
        streams it, or when we are the host (which then re-broadcasts the
        delete); unconfirmed from another member. The host's record also
        confirms a tombstone of our own. A member household's records were
        admitted only for pages held live here (:meth:`_authored_record`).

        From the **host**, an id we never held gets a stub tombstone, so a
        stale copy another household streams or replays later cannot
        create it — but only when the id is owner-bound to its
        ``created_by`` in THIS space (page ids are global: a stub for
        another space's id would block that space's real page here).
        Refusals are summarised once per chunk.
        """
        space = await self._space_repo.get(space_id)
        from_host = await self._acts_as_host(space, provider)
        is_host = (
            space is not None
            and self._page_conflicts is not None
            and space.owner_instance_id == self._page_conflicts.own_instance_id
        )
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            page_id = str(r.get("id") or r.get("page_id") or "")
            if not page_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._page_repo.get(page_id)
            if held is not None:
                if held.space_id != space_id:
                    cross_space.append(page_id)
                elif self._page_conflicts is not None:
                    deleted = await self._page_conflicts.delete_page(
                        space_id, page_id, deleted_by=deleted_by, from_instance=provider
                    )
                    if deleted and space is not None and is_host:
                        # The host accepted a member's delete it had missed:
                        # it decides, so it tells every member household.
                        await self._bus.publish(
                            PageDeleted(
                                page_id=page_id,
                                space_id=space_id,
                                actor_user_id=deleted_by,
                            )
                        )
                else:
                    await self._page_repo.delete(
                        page_id,
                        space_id=space_id,
                        deleted_by=deleted_by,
                        confirmed=from_host,
                    )
                continue
            if await self._page_repo.is_page_deleted(page_id, space_id=space_id):
                if from_host:
                    # The host stands behind our own (unconfirmed) delete.
                    await self._page_repo.confirm_delete(page_id, space_id=space_id)
                continue
            if not from_host:
                continue
            created_by = str(r.get("created_by") or "")
            if (
                check_owner_bound_id(
                    SPACE_PAGE_KIND,
                    page_id,
                    space_id=space_id,
                    owner_user_id=created_by,
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            await self._page_repo.tombstone(
                page_id, space_id=space_id, created_by=created_by, deleted_by=deleted_by
            )
        if cross_space:
            log.warning(
                "space sync: %d page tombstone(s) from %s for %s name a page "
                "held in another space — refused: %s",
                len(cross_space),
                provider,
                space_id,
                ", ".join(cross_space[:5]),
            )
        if unbound:
            log.info(
                "space sync: %d page tombstone(s) from %s for %s name a page "
                "never held here whose id is not bound to this space — no "
                "stub recorded",
                unbound,
                provider,
                space_id,
            )

    async def _page_seq_hints(
        self, records: list[dict[str, Any]], space_id: str, *, provider: str
    ) -> None:
        """On the **host**: a member household's ``pages`` records carry
        the ``seq`` it mirrors. A host restored from a backup raises its seq
        floor to it, so its next own edit lands above every version members
        hold — they take it. Only the number is read (the highest per page
        in the chunk), only for live pages held here, and only from a
        **writer** household seated in the space; the content is never
        taken this way (and the admission drops the records right after).
        :meth:`PageConflictService.raise_floor` bounds each raise and rate
        limits them per provider, page and day."""
        engine = self._page_conflicts
        if engine is None or not records or self._authorship is None:
            return
        mode, host = await engine.mode(space_id)
        if mode is not PageMode.HOST or not provider or provider == host:
            return
        highest: dict[str, int] = {}
        for r in records:
            page_id = str(r.get("id") or r.get("page_id") or "")
            seq = r.get("seq")
            if (
                not page_id
                or not isinstance(seq, int)
                or isinstance(seq, bool)
                or seq <= 0
            ):
                continue
            highest[page_id] = max(seq, highest.get(page_id, 0))
        if not highest:
            return
        event = _sync_event(space_id, "pages", provider)
        if not await self._authorship.writes_here(event, space_id):
            return
        for page_id, seq in highest.items():
            await engine.raise_floor(space_id, page_id, seq, provider=provider)

    async def _is_host_version(
        self, space_id: str, provider: str, r: dict[str, Any]
    ) -> bool:
        """Is ``r`` a host version (``seq``) streamed by the space's host?"""
        if "seq" not in r or not provider:
            return False
        space = await self._space_repo.get(space_id)
        return await self._acts_as_host(space, provider)

    async def _apply_page_version(
        self, page: Page, r: dict[str, Any], space_id: str, provider: str
    ) -> bool:
        """v_48 host-sequenced pages. ``True`` when handled here:

        * on the **host** every page record is ignored — it is the pages'
          sequencer; a member's new page reaches it as a create proposal,
          never as an unsequenced row it would hold but never broadcast;
        * a record streamed by the space's host carrying ``seq`` is the
          host's version: mirrored by ``seq`` (newer applies, older never
          reverts) — even when we have not seen the host's v_48
          capabilities yet;
        * any other record never updates a page held here and lands
          unsequenced (``seq`` 0 — a member cannot forge the host's order);
        * under a pre-v_48 host a record without ``seq`` is taken as before.
        """
        engine = self._page_conflicts
        if engine is None:
            return False
        mode, host = await engine.mode(space_id)
        if mode is PageMode.HOST:
            return True
        if host and provider == host and "seq" in r:
            version = canonical_from_wire(r)
            if version is None:
                log.warning(
                    "space sync: page %s in %s — malformed host version; skipped",
                    page.id,
                    space_id,
                )
                return True
            await engine.mirror(space_id=space_id, page_id=page.id, version=version)
            if self._gfs_media is not None and version.has_state:
                # v_82: a link-joined requester keeps the page's files
                # under the names the page shows (sync line 82).
                await self._keep_media(
                    self._gfs_media.localize_as_shown(
                        space_id,
                        r.get(MEDIA_REFS_FIELD),
                        [
                            *page_media_urls(version.cover_image_url, version.content),
                            *(
                                u
                                for side in version.conflict
                                for u in page_media_urls(
                                    side.cover_image_url, side.content
                                )
                            ),
                        ],
                        item_id=page.id,
                        origin=provider,
                    )
                )
            return True
        if mode is PageMode.LEGACY:
            return False
        held = await self._page_repo.get_space_page(page.id, space_id=space_id)
        return held is not None


def _page_from_record(r: dict[str, Any], space_id: str) -> Page | None:
    page_id = bounded_id(r.get("id"))
    if not page_id or not r.get("title"):
        return None
    # Ids and stamps bounded where they are read (the size rule measures
    # them at their widest).
    return Page(
        id=page_id,
        title=str(r["title"]),
        content=str(r.get("content") or ""),
        created_by=bounded_id(r.get("created_by")),
        created_at=bounded_stamp(r.get("created_at")),
        updated_at=bounded_stamp(r.get("updated_at")),
        space_id=space_id or r.get("space_id"),
        cover_image_url=r.get("cover_image_url"),
    )
