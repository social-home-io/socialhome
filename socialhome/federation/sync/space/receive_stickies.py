"""§25.6 sync of a space's stickies and their tombstones."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ....domain.federation import FederationEvent
from ....domain.sticky import MAX_STICKY_CONTENT_LENGTH, Sticky, coerce_peer_sticky
from ...owner_bound_id import SPACE_STICKY_KIND
from .receiver_base import (
    ReceiverBase,
    _bound_here,
    _log_sync_refusal,
    _log_tombstone_refusals,
)
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)


class StickiesMixin(ReceiverBase):
    """The sticky resources of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_stickies(
        self, space_id: str, records: list[dict[str, Any]]
    ) -> None:
        for r in records:
            sticky = _sticky_from_record(r, space_id)
            if sticky is None:
                continue
            if await self._sticky_repo.is_deleted(sticky.id, space_id=space_id):
                # Deleted here — a provider that missed the delete still
                # streams it; the tombstone wins (its id is never
                # reused), and its own tombstone stream will tell it.
                log.debug("sync: sticky %s was deleted here — skipped", sticky.id)
                continue
            if not await self._sticky_repo.save(sticky, space_id=space_id):
                _log_sync_refusal("sticky", sticky.id, space_id)

    async def _authored_sticky_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_STICKY_DELETED rule
        # (``_collaborative_write_allowed``, feature ``stickies``)
        # for a sticky held live here IN THIS SPACE.
        held_sticky = await self._sticky_repo.get(rid) if rid else None
        if held_sticky is None or held_sticky.space_id != space_id:
            return False
        return await self._writer_delete_admits(
            event, space_id, "stickies", r, row_owner=held_sticky.author
        )

    async def _authored_sticky(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        if not rid or await self._sticky_repo.get(rid) is not None:
            return False
        if await self._sticky_repo.is_deleted(rid, space_id=space_id):
            return False  # deleted here; the tombstone wins
        return await self._may_author(
            held_back,
            event,
            space_id,
            str(r.get("author") or r.get("created_by") or ""),
        )

    async def _persist_sticky_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed sticky deletes (``stickies_deleted``, migration
        0085).

        A sticky held live here in this space is tombstoned (``deleted_by``
        from the record's ``actor_user_id``), as a live
        ``SPACE_STICKY_DELETED`` does. A member household's records were
        admitted only under the live rule (:meth:`_authored_record`). From
        the **host**, an id never held here gets a stub tombstone — only
        when the id is owner-bound to its ``author`` in THIS space (sticky
        ids are global: a stub for another space's id would block that
        space's real sticky here). Refusals are summarised once per chunk.
        """
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            sticky_id = str(r.get("id") or r.get("sticky_id") or "")
            if not sticky_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._sticky_repo.get(sticky_id)
            if held is not None:
                if held.space_id != space_id:
                    cross_space.append(sticky_id)
                else:
                    await self._sticky_repo.delete(
                        sticky_id, space_id=space_id, deleted_by=deleted_by
                    )
                continue
            if not from_host or await self._sticky_repo.is_deleted(
                sticky_id, space_id=space_id
            ):
                continue
            author = str(r.get("author") or "")
            if not _bound_here(SPACE_STICKY_KIND, sticky_id, space_id, author):
                unbound += 1
                continue
            await self._sticky_repo.tombstone(
                sticky_id,
                space_id=space_id,
                author=author,
                created_at=str(r.get("created_at") or ""),
                deleted_by=deleted_by,
            )
        _log_tombstone_refusals("sticky", provider, space_id, cross_space, unbound)


def _sticky_from_record(r: dict[str, Any], space_id: str) -> Sticky | None:
    if not r.get("id") or not r.get("author"):
        return None
    # Shared sticky field rules: a non-hex colour (rendered as CSS) is
    # replaced, coordinates clamped, content sanitised + capped.
    fields = coerce_peer_sticky(
        content=r.get("content"),
        color=r.get("color"),
        position_x=r.get("position_x"),
        position_y=r.get("position_y"),
    )
    if not fields.content:
        return None
    if fields.truncated:
        log.warning(
            "space sync: sticky %s in space %s — content over %d characters, truncated",
            r["id"],
            space_id,
            MAX_STICKY_CONTENT_LENGTH,
        )
    return Sticky(
        id=str(r["id"]),
        author=str(r["author"]),
        content=fields.content,
        color=fields.color,
        position_x=fields.position_x,
        position_y=fields.position_y,
        created_at=str(r.get("created_at") or ""),
        updated_at=str(r.get("updated_at") or ""),
        space_id=space_id or r.get("space_id"),
    )
