"""Gallery favourites — each person's own ♡ (owner decision D12).

Anyone who may see a photo or video can mark it a favourite. Favourites
are **private**: nobody else learns who marked what — no event on the bus,
no notification, no count on the photo, and nothing leaves the household
(the §25.6 sync exporter and the federation dicts carry no favourite
field). There are no comments or reactions on gallery photos; talking
happens in the feed and the chats.

The favourites list spans every album the person can still see: household
albums open to them (a private album only for its chosen people —
:meth:`AbstractGalleryRepo.album_visible_to`) and the albums of the spaces
they hold a seat in whose gallery is on. A photo in the trash is hidden
until it comes back; the trash purge takes its favourites with it.
"""

from __future__ import annotations

import logging

from ..domain.gallery import GalleryAlbum, GalleryFavourite, GalleryItem
from ..domain.preferences import FeatureDisabledError
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.space_repo import AbstractSpaceRepo
from .gallery_service import GalleryNotFoundError, GalleryService

log = logging.getLogger(__name__)

#: ``POST /api/gallery/bulk`` actions this service answers.
FAVOURITE_ACTIONS: frozenset[str] = frozenset({"favourite", "unfavourite"})
#: The most photos one bulk request marks — the selection's own cap.
FAVOURITE_BULK_MAX: int = 500


class GalleryFavouritesService:
    """Marks, unmarks and lists one person's favourites."""

    __slots__ = ("_gallery", "_repo", "_space_repo")

    def __init__(
        self,
        repo: AbstractGalleryRepo,
        space_repo: AbstractSpaceRepo,
        gallery: GalleryService,
    ) -> None:
        self._repo = repo
        self._space_repo = space_repo
        # Who may see an album (a space seat, the space's gallery switch).
        self._gallery = gallery

    async def mark(self, item_id: str, *, user_id: str) -> None:
        """Make *item_id* one of *user_id*'s favourites (idempotent). An
        item they can't see answers like an unknown one (404), so marking
        is no oracle."""
        await self._mark(item_id, user_id, {})

    async def unmark(self, item_id: str, *, user_id: str) -> None:
        """No longer a favourite. Always succeeds — removing a mark tells
        nothing about the item, and a photo that has since become hidden
        can still be let go."""
        await self._repo.remove_favourite(user_id, item_id)

    async def ids_among(self, user_id: str, item_ids: list[str]) -> frozenset[str]:
        """Which of *item_ids* are *user_id*'s favourites — the ♡ on a page
        of items, for that viewer only."""
        return await self._repo.favourite_ids_among(user_id, item_ids)

    async def page(
        self,
        *,
        user_id: str,
        limit: int = 60,
        before: str | None = None,
        before_id: str | None = None,
    ) -> tuple[list[GalleryFavourite], dict[str, GalleryAlbum]]:
        """One page of the person's favourites, last marked first, and the
        albums they live in (as the person sees them). Only what they may
        still see — filtered in SQL, so a page is full until the last."""
        favourites = await self._repo.list_favourite_items(
            user_id,
            space_ids=await self._gallery_spaces(user_id),
            limit=limit,
            before=before,
            before_id=before_id,
        )
        albums = await self._gallery.albums_as_seen(
            [f.item.album_id for f in favourites], actor_user_id=user_id
        )
        return favourites, albums

    async def apply(
        self, action: str, item_ids: list[str], *, user_id: str
    ) -> tuple[list[str], list[str]]:
        """``favourite`` / ``unfavourite`` a selection. ``(done, refused)``:
        an item the person can't see is refused, like the single route's
        404."""
        if action not in FAVOURITE_ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        ids = list(dict.fromkeys(str(i) for i in item_ids if i))
        if not ids or len(ids) > FAVOURITE_BULK_MAX:
            raise ValueError(f"1–{FAVOURITE_BULK_MAX} items per request")
        if action == "unfavourite":
            for iid in ids:
                await self._repo.remove_favourite(user_id, iid)
            return ids, []
        done: list[str] = []
        refused: list[str] = []
        # One look per album, not per photo.
        seen: dict[str, bool] = {}
        for iid in ids:
            try:
                await self._mark(iid, user_id, seen)
            except GalleryNotFoundError, FeatureDisabledError:
                refused.append(iid)
            else:
                done.append(iid)
        return done, refused

    async def _mark(self, item_id: str, user_id: str, seen: dict[str, bool]) -> None:
        item = await self._repo.get_item(item_id)
        if item is None or not await self._may_see(item, user_id, seen):
            raise GalleryNotFoundError(f"item {item_id!r} not found")
        if not await self._repo.add_favourite(user_id, item_id):
            raise GalleryNotFoundError(f"item {item_id!r} not found")

    async def _may_see(
        self, item: GalleryItem, user_id: str, seen: dict[str, bool]
    ) -> bool:
        """May *user_id* see the album *item* lives in? A space album: a
        seat and the space's gallery on (:meth:`GalleryService.visible_album`
        — a switched-off gallery is its 403). A household album: open to
        everyone at home, theirs, or a private one they were chosen for."""
        cached = seen.get(item.album_id)
        if cached is not None:
            return cached
        try:
            album = await self._gallery.visible_album(
                item.album_id, actor_user_id=user_id
            )
        except GalleryNotFoundError:
            seen[item.album_id] = False
            return False
        ok = album.space_id is not None or await self._repo.album_visible_to(
            album.id, user_id
        )
        seen[item.album_id] = ok
        return ok

    async def _gallery_spaces(self, user_id: str) -> frozenset[str]:
        """The spaces whose albums the person sees: a seat (not a banned
        user's leftover) in a space whose gallery is on."""
        out: set[str] = set()
        for space in await self._space_repo.list_for_user(user_id):
            if not space.features.gallery:
                continue
            if await self._space_repo.is_banned(space.id, user_id):
                continue
            out.add(space.id)
        return frozenset(out)
