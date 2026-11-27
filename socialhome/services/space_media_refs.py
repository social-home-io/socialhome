"""Shared sender behaviour for the ``media_refs`` descriptor (v_80).

Space posts, calendar events, gallery items and pages all do the same
three things in a GFS space (a space with a channel — the only kind a GFS
keeps media for since v_96): ask the uploader for a plan
(:class:`~.space_media_gfs_uploader.SpaceMediaGfsUploader`), put the
descriptor on the item's SHARED payload — owner 2026-11-27: every household
of a GFS space, paired, mesh, link-joined, member or follower, takes the
space's files from the GFS cache, so every copy carries it — and queue the
upload in the durable queue. Fail-soft: a planning error is a WARNING and
the item goes out without the descriptor.

Behaviour-only mixin (``__slots__ = ()``): the consumer declares the
``_media_gfs`` slot and sets it to ``None`` in its ``__init__``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
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
        author_user_id: str = "",
    ) -> SpaceMediaPlan | None:
        """The upload plan for item *item_id*'s *media_urls* (each blob is
        sealed for that item), or ``None`` (nothing to send, no GFS space,
        not wired, or an error). *author_user_id* is told about a file no
        GFS of the space can take."""
        urls = [u for u in media_urls if u]
        if self._media_gfs is None or not urls:
            return None
        try:
            return await self._media_gfs.plan(
                space_id,
                urls,
                item_id=item_id,
                followers=followers,
                author_user_id=author_user_id,
            )
        except Exception:
            log.warning(
                "space media for GFS readers of space %s could not be prepared",
                space_id,
                exc_info=True,
            )
            return None

    @staticmethod
    def _with_media_refs(payload: dict, plan: SpaceMediaPlan | None) -> dict:
        """*payload* with *plan*'s descriptor (a new dict) — the one copy
        every household of the GFS space reads, so the item still goes as
        ONE space item."""
        if plan is None:
            return payload
        return {**payload, MEDIA_REFS_FIELD: plan.wire()}

    async def _upload_media(
        self, plan: SpaceMediaPlan | None, *, author_user_id: str = ""
    ) -> None:
        """Queue *plan*'s uploads in the durable queue."""
        if plan is not None and self._media_gfs is not None:
            await self._media_gfs.upload(plan, author_user_id=author_user_id)
