"""Gallery search — find photos by what people wrote and did (owner
decision D11).

A search matches a photo's caption, its album's name or description, the
name of who added it ("von Lena"), and the month and year it was taken
(else uploaded) — see :mod:`socialhome.domain.gallery_search`. No faces,
objects or places, now or later.

The words go through the app's one full-text index (``search_index``,
FTS5): migration 0141 keeps captions and album names in it. Everything a
person may not see is left out **in the repository's SQL** (so a page is
full and a count is honest), by the rules the rest of the gallery applies:

* household albums open to everyone at home, the person's own, and the
  private ones they were chosen for (``album_visible_to``);
* the albums of the spaces they hold a seat in whose gallery is on (a
  banned person's leftover seat is none);
* nothing in the trash;
* nothing added by someone they blocked, or across a guardian block.

Snippets are built here, after that filter, from the rows that passed it.
"""

from __future__ import annotations

import logging

from ..domain.gallery import GalleryAlbum
from ..domain.gallery_search import (
    GALLERY_SEARCH_ALBUMS_MAX,
    GALLERY_SEARCH_MIN_CHARS,
    GALLERY_SEARCH_PAGE,
    GALLERY_SEARCH_PAGE_MAX,
    GalleryQuery,
    GallerySearchHit,
    GallerySearchResult,
    GallerySearchSpec,
    GallerySearchWord,
    fts_prefix_query,
    name_matches,
    parse_gallery_query,
    snippet,
)
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .gallery_service import GalleryService
from .preferences_service import PreferencesService

log = logging.getLogger(__name__)

_GALLERY_SECTION = "gallery"


class GallerySearchService:
    """Runs gallery searches for one person at a time."""

    __slots__ = ("_gallery", "_household_features", "_repo", "_space_repo", "_users")

    def __init__(
        self,
        repo: AbstractGalleryRepo,
        space_repo: AbstractSpaceRepo,
        user_repo: AbstractUserRepo,
        gallery: GalleryService,
        *,
        household_features: PreferencesService | None = None,
    ) -> None:
        self._repo = repo
        self._space_repo = space_repo
        self._users = user_repo
        # Album covers and names as the viewer sees them.
        self._gallery = gallery
        # The household's gallery switch — only :meth:`count` reads it
        # (the routes answer 403 ``FEATURE_DISABLED`` themselves).
        self._household_features = household_features

    async def search(
        self,
        raw: str,
        *,
        user_id: str,
        space_id: str | None = None,
        limit: int = GALLERY_SEARCH_PAGE,
        offset: int = 0,
    ) -> GallerySearchResult:
        """One page of photos and videos matching *raw* that *user_id* may
        see — in one space's albums when *space_id* is given (a space they
        have no seat in, or whose gallery is off, finds nothing), else in
        every album they see: the household's and their spaces'. The first
        page of a search of words alone also names the matching albums."""
        query = _parse(raw)
        if query is None:
            return GallerySearchResult()
        spec = await self._spec(query, user_id=user_id, space_id=space_id)
        if spec is None:
            return GallerySearchResult()
        limit = max(1, min(limit, GALLERY_SEARCH_PAGE_MAX))
        offset = max(0, offset)
        rows = await self._repo.search_items(spec, limit=limit + 1, offset=offset)
        more = len(rows) > limit
        rows = rows[:limit]
        album_rows: list[GalleryAlbum] = []
        if offset == 0 and query.terms and not query.people and not query.has_date:
            album_rows = await self._repo.search_albums(
                spec, limit=GALLERY_SEARCH_ALBUMS_MAX
            )
        seen = await self._gallery.albums_as_seen(
            [i.album_id for i in rows] + [a.id for a in album_rows],
            actor_user_id=user_id,
        )
        hits = tuple(
            GallerySearchHit(
                item=item,
                album=seen[item.album_id],
                snippet=snippet(item.caption, query.terms),
            )
            for item in rows
            if item.album_id in seen
        )
        albums = tuple(seen[a.id] for a in album_rows if a.id in seen)
        return GallerySearchResult(items=hits, albums=albums, more=more)

    async def count(self, raw: str, *, user_id: str) -> int:
        """How many photos and videos a search across everything the person
        sees finds — the "Photos" count of the app's global search. ``0``
        while the household's gallery is switched off."""
        query = _parse(raw)
        if query is None:
            return 0
        if self._household_features is not None:
            prefs = await self._household_features.get_household()
            if not prefs.is_enabled(_GALLERY_SECTION):
                return 0
        spec = await self._spec(query, user_id=user_id, space_id=None)
        if spec is None:
            return 0
        return await self._repo.count_search_items(spec)

    # ─── Internals ────────────────────────────────────────────────────────

    async def _spec(
        self, query: GalleryQuery, *, user_id: str, space_id: str | None
    ) -> GallerySearchSpec | None:
        spaces = await self._gallery_spaces(user_id)
        if space_id is not None:
            if space_id not in spaces:
                return None
            household, in_scope = False, frozenset({space_id})
        else:
            household, in_scope = True, spaces
        names = await self._names(in_scope) if query.terms or query.people else {}
        words = tuple(
            GallerySearchWord(
                fts=fts_prefix_query(term),
                uploader_ids=_matching(names, term),
            )
            for term in query.terms
        )
        uploader_ids: frozenset[str] | None = None
        if query.people:
            uploader_ids = frozenset().union(
                *(_matching(names, p) for p in query.people)
            )
        return GallerySearchSpec(
            viewer_user_id=user_id,
            household=household,
            space_ids=in_scope,
            words=words,
            uploader_ids=uploader_ids,
            year=query.year,
            month=query.month,
        )

    async def _gallery_spaces(self, user_id: str) -> frozenset[str]:
        """The spaces whose albums the person sees: a seat (not a banned
        user's leftover) in a space whose gallery is on — the favourites'
        rule."""
        out: set[str] = set()
        for space in await self._space_repo.list_for_user(user_id):
            if not space.features.gallery:
                continue
            if await self._space_repo.is_banned(space.id, user_id):
                continue
            out.add(space.id)
        return frozenset(out)

    async def _names(self, space_ids: frozenset[str]) -> dict[str, list[str]]:
        """``user_id → names`` of everyone whose photos the search can
        reach: the household's people (their display name and username)
        and the members of the spaces searched (their name in the space,
        and a remote member's display name)."""
        names: dict[str, list[str]] = {}
        for user in await self._users.list_active():
            names[user.user_id] = [user.display_name, user.username]
        local = frozenset(names)
        remote: set[str] = set()
        for sid in sorted(space_ids):
            for member in await self._space_repo.list_members(sid):
                if member.space_display_name:
                    names.setdefault(member.user_id, []).append(
                        member.space_display_name
                    )
                if member.user_id not in local:
                    remote.add(member.user_id)
        if remote:
            for ru in await self._users.list_remote_by_ids(remote):
                names.setdefault(ru.user_id, []).append(ru.display_name)
        return names


def _parse(raw: str) -> GalleryQuery | None:
    text = (raw or "").strip()
    if len(text) < GALLERY_SEARCH_MIN_CHARS:
        return None
    query = parse_gallery_query(text)
    return None if query.is_empty else query


def _matching(names: dict[str, list[str]], term: str) -> frozenset[str]:
    return frozenset(
        uid
        for uid, labels in names.items()
        if any(name_matches(n, term) for n in labels)
    )
