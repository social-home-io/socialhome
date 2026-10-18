"""SearchService — keeps the FTS5 index in sync + serves queries.

Subscribes to domain events on the bus to mirror new/edited content
into ``search_index``. Search queries use the repo directly, with an
access-control filter applied on top so callers only see content they
are allowed to see (spec §23.2.6).

The service exposes the spec's five filter types (``posts``,
``people``, ``spaces``, ``pages``, ``messages``) via :meth:`search`;
internally these expand to one-or-more underlying scopes indexed by
:mod:`socialhome.repositories.search_repo`.
"""

from __future__ import annotations

import logging

from ..domain.dm_location import parse_location_content
from ..domain.events import (
    CommentAdded,
    DmMessageCreated,
    DmMessageDeleted,
    PageCreated,
    PageDeleted,
    PageUpdated,
    PostCreated,
    PostDeleted,
    PostEdited,
    SpacePostCreated,
    SpacePostModerated,
    UserDeprovisioned,
    UserProvisioned,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.search_repo import (
    SCOPE_MESSAGE,
    SCOPE_PAGE,
    SCOPE_POST,
    SCOPE_SPACE,
    SCOPE_SPACE_POST,
    SCOPE_TYPE_GROUPS,
    SCOPE_USER,
    AbstractSearchRepo,
    SearchHit,
)

log = logging.getLogger(__name__)

#: Scopes no search ever returns or counts: a DM message row carries no
#: conversation id, so membership can't be checked — never shown.
_NEVER_SEARCHABLE: frozenset[str] = frozenset({SCOPE_MESSAGE})

#: Minimum query length (§23.2.5). Anything shorter returns an empty
#: result set so the UI shows "Keep typing…".
MIN_QUERY_CHARS: int = 2


class SearchService:
    """Domain-event-driven indexing + query API."""

    __slots__ = ("_bus", "_conv_repo", "_repo", "_space_repo", "_user_repo")

    def __init__(self, bus: EventBus, repo: AbstractSearchRepo) -> None:
        self._bus = bus
        self._repo = repo
        self._space_repo = None
        self._user_repo = None
        self._conv_repo = None

    def attach_access_repos(
        self,
        *,
        space_repo=None,
        user_repo=None,
        conversation_repo=None,
    ) -> None:
        """Wire the repos used for access-control filtering (§23.2.6)."""
        self._space_repo = space_repo
        self._user_repo = user_repo
        self._conv_repo = conversation_repo

    def wire(self) -> None:
        """Subscribe handlers on the bus.  Idempotent."""
        self._bus.subscribe(PostCreated, self._on_post_created)
        self._bus.subscribe(PostEdited, self._on_post_edited)
        self._bus.subscribe(PostDeleted, self._on_post_deleted)
        self._bus.subscribe(CommentAdded, self._on_comment_added)
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)
        self._bus.subscribe(SpacePostModerated, self._on_space_post_moderated)
        self._bus.subscribe(PageCreated, self._on_page_created)
        self._bus.subscribe(PageUpdated, self._on_page_updated)
        self._bus.subscribe(PageDeleted, self._on_page_deleted)
        self._bus.subscribe(DmMessageCreated, self._on_dm_message)
        self._bus.subscribe(DmMessageDeleted, self._on_dm_message_deleted)
        self._bus.subscribe(UserProvisioned, self._on_user_provisioned)
        self._bus.subscribe(UserDeprovisioned, self._on_user_deprovisioned)

    # ─── Event handlers ───────────────────────────────────────────────────

    async def _on_post_created(self, event: PostCreated) -> None:
        await self._index_post(event.post, space_id=None)

    async def _on_post_edited(self, event: PostEdited) -> None:
        await self._index_post(event.post, space_id=None)

    async def _on_post_deleted(self, event: PostDeleted) -> None:
        await self._repo.delete(scope=SCOPE_POST, ref_id=event.post_id)

    async def _on_comment_added(self, event: CommentAdded) -> None:
        # Comments are part of the parent post's hit; we don't index
        # them separately to keep the index small. The post body still
        # surfaces the conversation.
        return

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        await self._index_post(
            event.post, space_id=event.space_id, scope=SCOPE_SPACE_POST
        )

    async def _on_space_post_moderated(self, event: SpacePostModerated) -> None:
        # Moderation deletes the post — drop the index entry too.
        await self._repo.delete(scope=SCOPE_SPACE_POST, ref_id=event.post.id)

    async def _on_page_created(self, event: PageCreated) -> None:
        await self._index_page(
            event.page_id, event.space_id, event.title, event.content
        )

    async def _on_page_updated(self, event: PageUpdated) -> None:
        await self._index_page(
            event.page_id, event.space_id, event.title, event.content
        )

    async def _on_page_deleted(self, event: PageDeleted) -> None:
        await self._repo.delete(scope=SCOPE_PAGE, ref_id=event.page_id)

    async def _on_user_provisioned(self, event: UserProvisioned) -> None:
        """Auto-index newly provisioned local users (§23.2)."""
        await self.index_user(
            user_id=event.user_id,
            username=event.username,
            display_name=event.username,  # display_name isn't in the event
        )

    async def _on_user_deprovisioned(self, event: UserDeprovisioned) -> None:
        await self.delete_user(event.user_id)

    async def _on_dm_message(self, event: DmMessageCreated) -> None:
        """Index DM plaintext into ``search_index`` with SCOPE_MESSAGE.

        Skips empty bodies so file-only DMs don't create empty hits.
        """
        body = (event.content or "").strip()
        if event.message_type == "location":
            # Index the pin's label only — the JSON (and its coordinates)
            # is not searchable text.
            try:
                body = parse_location_content(body).label or ""
            except ValueError:
                body = ""
        if not body:
            return
        await self._repo.upsert(
            scope=SCOPE_MESSAGE,
            ref_id=event.message_id,
            space_id=None,
            title=event.sender_display_name or "",
            body=body,
        )

    async def _on_dm_message_deleted(self, event: DmMessageDeleted) -> None:
        """A deleted message (DM or chat) leaves the index."""
        await self._repo.delete(scope=SCOPE_MESSAGE, ref_id=event.message_id)

    async def remove_messages(self, message_ids: list[str]) -> None:
        """Drop messages cleared without an event (space-chat retention)."""
        for message_id in message_ids:
            await self._repo.delete(scope=SCOPE_MESSAGE, ref_id=message_id)

    # ─── User / space indexing (§23.2) ────────────────────────────────────
    #
    # These are driven imperatively (no domain events today) from the
    # services that create / rename users + spaces.

    async def index_user(
        self,
        *,
        user_id: str,
        username: str,
        display_name: str = "",
        bio: str | None = None,
    ) -> None:
        """Upsert a user into the search index so "People" queries find them."""
        body_parts = [username, display_name or "", bio or ""]
        body = " ".join(p for p in body_parts if p).strip()
        if not body:
            return
        await self._repo.upsert(
            scope=SCOPE_USER,
            ref_id=user_id,
            space_id=None,
            title=display_name or username,
            body=body,
        )

    async def delete_user(self, user_id: str) -> None:
        await self._repo.delete(scope=SCOPE_USER, ref_id=user_id)

    async def index_space(
        self,
        *,
        space_id: str,
        name: str,
        description: str = "",
    ) -> None:
        """Upsert a space into the search index so "Spaces" queries find it."""
        body = (description or "").strip()
        if not name and not body:
            return
        await self._repo.upsert(
            scope=SCOPE_SPACE,
            ref_id=space_id,
            space_id=space_id,
            title=name,
            body=body,
        )

    async def delete_space(self, space_id: str) -> None:
        await self._repo.delete(scope=SCOPE_SPACE, ref_id=space_id)

    # ─── Public query API ─────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        scope: str | None = None,
        space_id: str | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """Run a user query and return raw hits (back-compat shape).

        Callers wanting the richer "hits + per-scope counts + access
        filter" shape should use :meth:`search_with_counts` instead.
        Queries under :data:`MIN_QUERY_CHARS` return ``[]`` per §23.2.5.
        """
        q = (query or "").strip()
        if len(q) < MIN_QUERY_CHARS:
            return []
        scopes = frozenset({scope}) if scope else None
        return await self._repo.search(
            q,
            scopes=scopes,
            space_id=space_id,
            limit=limit,
        )

    async def search_with_counts(
        self,
        query: str,
        *,
        scope: str | None = None,  # back-compat: single scope string
        type_: str | None = None,  # spec: "posts"/"people"/"spaces"/...
        space_id: str | None = None,
        caller_user_id: str | None = None,
        caller_username: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Run a user query and return hits + per-type counts.

        Return shape::

            {"hits": [SearchHit, ...], "counts": {"post": N, ...}}

        The ``type_`` filter groups underlying scopes per spec §23.2:
        ``posts`` → {post, space_post}, ``people`` → {user}, etc.
        ``offset`` enables pagination — the client fires a second request
        with ``offset=limit`` for "Load more".

        Access (§23.2.6), applied to the hits AND the counts, failing
        closed: a row tied to a space only for a caller with a seat there
        (any role, a follower included); no DM message (the index has no
        conversation id to check membership against). ``space_id`` the
        caller has no seat in answers exactly like a query with no
        matches. Without the space repo or a caller there are no seats —
        nothing space-bound is returned or counted.
        """
        del caller_username  # identity is the user id
        q = (query or "").strip()
        if len(q) < MIN_QUERY_CHARS:
            return {"hits": [], "counts": {}}

        if type_ in SCOPE_TYPE_GROUPS:
            scopes: frozenset[str] | None = SCOPE_TYPE_GROUPS[type_]
        elif scope:
            scopes = frozenset({scope})
        else:
            scopes = None

        seats = await self._seat_space_ids(caller_user_id)
        if space_id is not None and space_id not in seats:
            return {"hits": [], "counts": {}}
        raw = await self._repo.search(
            q,
            scopes=scopes,
            space_id=space_id,
            limit=limit,
            offset=offset,
            visible_space_ids=seats,
            exclude_scopes=_NEVER_SEARCHABLE,
        )
        # Defence in depth: the repo filtered in SQL; never trust one gate.
        hits = [
            h
            for h in raw
            if h.scope not in _NEVER_SEARCHABLE
            and (h.space_id is None or h.space_id in seats)
        ]
        counts = await self._repo.count_by_scope(
            q,
            space_id=space_id,
            visible_space_ids=seats,
            exclude_scopes=_NEVER_SEARCHABLE,
        )
        return {"hits": hits[:limit], "counts": counts}

    async def _seat_space_ids(self, caller_user_id: str | None) -> frozenset[str]:
        """The spaces the caller holds a seat in (any role). Fail closed:
        no space repo or no caller → none. A repo error propagates (the
        request fails) rather than widening what the caller sees."""
        if self._space_repo is None or not caller_user_id:
            return frozenset()
        spaces = await self._space_repo.list_for_user(caller_user_id)
        return frozenset(s.id for s in spaces)

    # ─── Internals ────────────────────────────────────────────────────────

    async def _index_post(
        self, post, *, space_id: str | None, scope: str = SCOPE_POST
    ) -> None:
        # Posts may not have content (file/media posts); skip those — the
        # caption-less file isn't useful in a text index.
        body = (getattr(post, "content", None) or "").strip()
        if not body:
            return
        await self._repo.upsert(
            scope=scope,
            ref_id=post.id,
            space_id=space_id,
            title="",
            body=body,
        )

    async def _index_page(
        self,
        page_id: str,
        space_id: str | None,
        title: str,
        content: str,
    ) -> None:
        body = (content or "").strip()
        if not body and not title:
            return
        await self._repo.upsert(
            scope=SCOPE_PAGE,
            ref_id=page_id,
            space_id=space_id,
            title=title or "",
            body=body,
        )
