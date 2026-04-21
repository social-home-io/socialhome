"""SearchService — keeps the FTS5 index in sync + serves queries.

Subscribes to domain events on the bus to mirror new/edited content
into ``search_index``. Search queries use the repo directly, with an
access-control filter applied on top so callers only see content they
are allowed to see (spec §23.2.6).

The service exposes the spec's five filter types (``posts``,
``people``, ``spaces``, ``pages``, ``messages``) via :meth:`search`;
internally these expand to one-or-more underlying scopes indexed by
:mod:`social_home.repositories.search_repo`.
"""

from __future__ import annotations

import logging

from ..domain.events import (
    CommentAdded,
    DmMessageCreated,
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

#: Minimum query length (§23.2.5). Anything shorter returns an empty
#: result set so the UI shows "Keep typing…".
MIN_QUERY_CHARS: int = 2


class SearchService:
    """Domain-event-driven indexing + query API."""

    __slots__ = ("_bus", "_repo", "_space_repo", "_user_repo", "_conv_repo")

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
        if not body:
            return
        await self._repo.upsert(
            scope=SCOPE_MESSAGE,
            ref_id=event.message_id,
            space_id=None,
            title=event.sender_display_name or "",
            body=body,
        )

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
        ``posts`` → {post, space_post}, ``people`` → {user}, etc. Pass
        the caller's identity so hits the caller can't see are filtered
        out (§23.2.6). ``offset`` enables pagination — the client fires a
        second request with ``offset=limit`` for "Load more".
        """
        q = (query or "").strip()
        if len(q) < MIN_QUERY_CHARS:
            return {"hits": [], "counts": {}}

        if type_ in SCOPE_TYPE_GROUPS:
            scopes: frozenset[str] | None = SCOPE_TYPE_GROUPS[type_]
        elif scope:
            scopes = frozenset({scope})
        else:
            scopes = None

        # Fetch more than ``limit`` so access filtering doesn't leave
        # the response short — 2× is a fair heuristic.
        raw = await self._repo.search(
            q,
            scopes=scopes,
            space_id=space_id,
            limit=limit * 2,
            offset=offset,
        )
        filtered = await self._apply_access_filter(
            raw,
            caller_user_id=caller_user_id,
            caller_username=caller_username,
        )
        counts = await self._repo.count_by_scope(q, space_id=space_id)
        return {"hits": filtered[:limit], "counts": counts}

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

    # ─── Access filtering (§23.2.6) ───────────────────────────────────────

    async def _apply_access_filter(
        self,
        hits: list[SearchHit],
        *,
        caller_user_id: str | None,
        caller_username: str | None,
    ) -> list[SearchHit]:
        """Drop hits the caller isn't allowed to see.

        * ``SCOPE_SPACE_POST`` + ``SCOPE_PAGE`` (in a space): require
          membership in ``space_id`` when known.
        * ``SCOPE_MESSAGE``: require membership in the DM conversation.
          Today the index doesn't carry the conversation id, so we
          only surface DM hits to the caller who authored them — a
          conservative default that never leaks other households'
          DMs; refining this is follow-up work.
        * ``SCOPE_POST`` / ``SCOPE_USER`` / ``SCOPE_SPACE``: visible to
          every authenticated household member.

        If the access repos haven't been attached, filtering is a no-op
        (tests that don't care about visibility keep working).
        """
        if not hits:
            return hits
        # Early exit when we have no machinery to filter with.
        if self._space_repo is None and self._conv_repo is None:
            return hits

        space_member_cache: dict[str, bool] = {}

        async def _in_space(space_id: str) -> bool:
            if self._space_repo is None or not caller_username:
                return True
            if space_id in space_member_cache:
                return space_member_cache[space_id]
            try:
                uids = await self._space_repo.list_local_member_user_ids(space_id)
            except Exception:
                space_member_cache[space_id] = True
                return True
            allowed = caller_user_id is not None and caller_user_id in uids
            space_member_cache[space_id] = allowed
            return allowed

        kept: list[SearchHit] = []
        for hit in hits:
            if hit.scope in (SCOPE_SPACE_POST, SCOPE_PAGE) and hit.space_id:
                if not await _in_space(hit.space_id):
                    continue
            if hit.scope == SCOPE_MESSAGE:
                # Without a conversation id in the index row, restrict
                # DM hits to the caller's own messages (conservative).
                # ``title`` is the sender display name on write, so we
                # don't have ownership info here; skip all DM hits for
                # callers who haven't opted into DM search specifically.
                # Safer to err on the side of privacy (§26 + §12.5).
                continue
            kept.append(hit)
        return kept
