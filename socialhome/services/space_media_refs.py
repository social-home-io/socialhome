"""Shared sender behaviour for the ``media_refs`` descriptor (v_80).

Space posts, calendar events and gallery items all do the same three things
for households that read a space through a GFS: ask the uploader for a plan
(:class:`~.space_media_gfs_uploader.SpaceMediaGfsUploader`), hand the
descriptor to exactly the link-joined v_80 members (a ``per_peer`` hook of
:meth:`FederationService.broadcast_to_space_members` — never on the shared
payload a paired member gets) and queue the upload. Fail-soft: a planning
error is a WARNING and the item goes out without the descriptor.

Behaviour-only mixin (``__slots__ = ()``): the consumer declares the
``_media_gfs`` slot and sets it to ``None`` in its ``__init__``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING

from ..domain.space_media_ref import MEDIA_REFS_FIELD

if TYPE_CHECKING:
    from .space_media_gfs_uploader import SpaceMediaGfsUploader, SpaceMediaPlan

log = logging.getLogger(__name__)


class SpaceMediaRefsMixin:
    """Plan, address and upload an item's pictures for GFS readers."""

    __slots__ = ()

    _media_gfs: SpaceMediaGfsUploader | None

    def attach_media_gfs(self, media_gfs: SpaceMediaGfsUploader) -> None:
        """Wire the GFS media uploader (v_80)."""
        # The slot is the consumer's (this mixin is behaviour-only, see the
        # module docstring); mypy only sees the mixin's empty ``__slots__``.
        self._media_gfs = media_gfs  # type: ignore[misc]

    async def _plan_media_refs(
        self,
        space_id: str,
        media_urls: Iterable[str | None],
        *,
        item_id: str,
        followers: bool,
    ) -> SpaceMediaPlan | None:
        """The upload plan for item *item_id*'s *media_urls* (each blob is
        sealed for that item), or ``None`` (nothing to send, nobody reading
        through a GFS, not wired, or an error)."""
        urls = [u for u in media_urls if u]
        if self._media_gfs is None or not urls:
            return None
        try:
            return await self._media_gfs.plan(
                space_id, urls, item_id=item_id, followers=followers
            )
        except Exception:
            log.warning(
                "space media for GFS readers of space %s could not be prepared",
                space_id,
                exc_info=True,
            )
            return None

    def _media_refs_per_peer(
        self, plan: SpaceMediaPlan | None
    ) -> Callable[[str, dict], Awaitable[dict]] | None:
        """A ``per_peer`` hook adding the descriptor for link-joined v_80
        members only (a new dict; the shared payload is never touched)."""
        media_gfs = self._media_gfs
        if plan is None or media_gfs is None:
            return None
        wire = plan.wire()

        async def _per_peer(instance_id: str, payload: dict) -> dict:
            if await media_gfs.is_relay_reader(instance_id):
                return {**payload, MEDIA_REFS_FIELD: wire}
            return payload

        return _per_peer

    def _upload_media(self, plan: SpaceMediaPlan | None) -> None:
        """Queue *plan*'s uploads (after the item went out)."""
        if plan is not None and self._media_gfs is not None:
            self._media_gfs.upload(plan)
