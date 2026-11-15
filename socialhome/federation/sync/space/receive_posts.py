"""§25.6 sync of a space's feed: ``posts``, ``comments``, ``polls`` and
their tombstones.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ....domain.events import (
    CommentDeleted,
    CommentUpdated,
    PollCreated,
    PostDeleted,
    SpaceCommentSynced,
    SpacePostSynced,
)
from ....domain.federation import FederationEvent
from ....domain.file_hashes import parse_file_hashes
from ....domain.post import (
    FEED_POST_MAX_IMAGES,
    MAX_POLL_OPTIONS,
    Comment,
    CommentType,
    FileMeta,
    Post,
    PostType,
)
from ....domain.space import ContentAction
from ....domain.space_item import reaction_emoji_ok, stamp_to_db, stored_stamp
from ....domain.space_media_ref import post_media_urls
from ....services.inbound_media_store import (
    local_media_ref,
    space_media_ref,
    space_media_refs,
)
from ....services.link_preview_service import space_wire_link_preview
from ....services.space_media_names import comment_media_ref
from ...owner_bound_id import (
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    OwnerBinding,
    check_owner_bound_id,
)
from .receiver_base import (
    ReceiverBase,
    _log_sync_refusal,
    _log_tombstone_refusals,
    _parse_iso,
)
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)


def _edit_stamp_of(raw: object) -> str | None:
    """A streamed row's ``edited_at`` in the stored stamp shape
    (:func:`stamp_to_db`), or ``None`` (none, unparseable, or too far ahead
    of our clock — it would block every later edit). A naive stamp is UTC,
    as every stored one is."""
    stamp = stored_stamp(raw)
    return stamp_to_db(stamp) if stamp is not None else None


def _synced_reaction_stamps(raw: object) -> dict[str, list]:
    """The host's ``reaction_stamps`` that pass the live reaction rules:
    a stamp no more than the live skew ahead of our clock (one years ahead
    would win every later removal — last writer wins), re-shaped like a
    stored one, and an emoji the live path would take. Anything else is
    dropped — the reaction sets still carry what it said."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, list) or len(value) != 2:
            continue
        user_id, sep, emoji = key.partition("\x00")
        stamp = stored_stamp(value[0])
        if (
            not sep
            or not user_id
            or stamp is None
            or not reaction_emoji_ok(emoji, legacy=True)
        ):
            continue
        out[key] = [stamp_to_db(stamp), value[1] is True]
    return out


def _synced_vote_stamps(raw: object) -> dict[str, list] | None:
    """The host's v_94 ``vote_stamps`` (``{voter: [stamp, [option_id,
    …]]}``) with only the entries the live vote rule would take — a stamp
    no more than the live skew ahead of our clock, re-shaped like a stored
    one, a list of option ids no longer than a poll has options. ``None``
    when the record carries none (a host below v_94)."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, list] = {}
    for voter, value in raw.items():
        if not isinstance(voter, str) or not voter:
            continue
        if not isinstance(value, list) or len(value) != 2:
            continue
        stamp = stored_stamp(value[0])
        picked = value[1]
        if (
            stamp is None
            or not isinstance(picked, list)
            or len(picked) > MAX_POLL_OPTIONS
            or not all(isinstance(o, str) and o for o in picked)
        ):
            continue
        out[voter] = [stamp_to_db(stamp), picked]
    return out


def _synced_reactions(raw: object) -> dict[str, list[str]] | None:
    """The host's reaction sets (``emoji → users``) with only emoji the live
    path would take; ``None`` when the record carries none."""
    if not isinstance(raw, dict):
        return None
    return {
        k: [u for u in v if isinstance(u, str) and u]
        for k, v in raw.items()
        if reaction_emoji_ok(k, legacy=True) and isinstance(v, list)
    }


class PostsMixin(ReceiverBase):
    """The feed resources of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_posts(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        from_host = await self._is_host(space_id, provider)
        for r in records:
            post = _post_from_record(r)
            if post is None:
                continue
            # A post deleted here stays deleted: the provider may have
            # missed the delete, or the delete overtook the create (a
            # v_49 member-published delete leaves a soft-deleted row
            # under the id). ``save`` would upsert it live again.
            held_post = await self._space_post_repo.get(post.id)
            if held_post is not None and held_post[1].deleted:
                log.debug("sync: post %s was deleted here — skipped", post.id)
                continue
            if held_post is not None:
                # Keep what this household holds of the row's shared
                # state: its reactions (ordered by the stamps written
                # with them — a provider's snapshot would wipe relayed
                # ones and the stamps would then refuse the copy that
                # could restore them) and its comment count.
                post = replace(
                    post,
                    reactions=held_post[1].reactions,
                    comment_count=held_post[1].comment_count,
                )
            media = await self._localize_record(
                space_id,
                r,
                post.id,
                post_media_urls(post),
                provider=provider,
                takes_descriptor=held_post is None or from_host,
                held=(post_media_urls(held_post[1]) if held_post is not None else None),
                from_host=from_host,
            )
            post = media.post(post)
            if await self._space_post_repo.save(space_id, post) is None:
                log.warning(
                    "space sync: post %s already exists in another space "
                    "— refusing the write for %s",
                    post.id,
                    space_id,
                )
                continue
            stamps = (
                _synced_reaction_stamps(r.get("reaction_stamps")) if from_host else {}
            )
            if from_host and held_post is not None:
                # v_92 — the host's reactions, merged last writer wins
                # per user and emoji by their stamps: a reaction (or a
                # removal) this household missed lands, a newer one it
                # holds stays. A removal travels only as a stamp: the
                # host's sets settle just the reactions NO stamp orders
                # here — one whose add we applied stays even when the
                # host lacks it (it may never have received it).
                # Our own people's reactions are authored here.
                await self._space_post_repo.merge_reaction_stamps(
                    post.id,
                    stamps,
                    space_id=space_id,
                    reactions=_synced_reactions(r.get("reactions")),
                    keep_users=await self._local_people(space_id),
                )
            elif stamps:
                # A post new here took the host's reactions: keep their
                # stamps too, so a stale relayed copy cannot undo them.
                await self._space_post_repo.merge_reaction_stamps(
                    post.id, stamps, space_id=space_id
                )
            await self._keep_media(media)
            # Derived local state only (the system album's mirror of its
            # media) — no notification, no fan-out.
            await self._bus.publish(SpacePostSynced(post=post, space_id=space_id))

    async def _persist_comments(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        # An older provider streamed its deleted comments as comment
        # records (content cleared, ``deleted: true``); stored, one read
        # as a live empty comment. From the host such a record is the
        # delete it stands for — applied as a ``comments_deleted``
        # tombstone; from anyone else it is dropped (a member's deletes
        # travel as tombstones, judged by the live rule).
        gone = [r for r in records if r.get("deleted")]
        if gone and await self._is_host(space_id, provider):
            await self._persist_comment_tombstones(gone, space_id, provider=provider)
        comments_from_host = await self._is_host(space_id, provider)
        for r in records:
            if r.get("deleted"):
                continue
            comment = _comment_from_record(r)
            if comment is None:
                continue
            # v_85 — a hashed picture takes its content name (the
            # host's hashes only); a raw content name never lands.
            comment = replace(
                comment,
                media_url=comment_media_ref(
                    space_id,
                    comment.media_url,
                    parse_file_hashes(r) if comments_from_host else None,
                ),
            )
            if await self._space_post_repo.add_comment(comment, space_id=space_id):
                # History, not news: only replies held for this comment
                # (the parent-wait buffer) follow it.
                await self._bus.publish(
                    SpaceCommentSynced(comment=comment, space_id=space_id)
                )
                continue
            # Not inserted: an id held here already (a member-relayed
            # copy, or a delete's tombstone) stays as it is — but for
            # the host's edit of it (v_92): an edit this household
            # missed lands, last writer wins on ``edited_at`` like the
            # live ``SPACE_COMMENT_UPDATED`` (the repo refuses a
            # deleted comment, another space's and an older stamp) …
            held_comment = await self._space_post_repo.get_comment(comment.id)
            if held_comment is not None:
                stamp = _edit_stamp_of(r.get("edited_at"))
                if (
                    comments_from_host
                    and stamp is not None
                    and comment.content is not None
                    and held_comment.content != comment.content
                    and await self._space_post_repo.edit_comment(
                        comment.id,
                        comment.content,
                        space_id=space_id,
                        edited_at=stamp,
                    )
                ):
                    refreshed = await self._space_post_repo.get_comment(comment.id)
                    if refreshed is not None:
                        await self._bus.publish(
                            # History, not news: the open views refresh
                            # (no mentions notify), and the provider
                            # as origin keeps the outbound from
                            # federating it again.
                            CommentUpdated(
                                post_id=refreshed.post_id,
                                comment=refreshed,
                                space_id=space_id,
                                origin_instance_id=provider,
                            )
                        )
                    continue
                log.debug("sync: comment %s is held here — skipped", comment.id)
            else:  # … anything else targets a post outside this space.
                log.warning(
                    "space sync: comment %s targets post %s outside space "
                    "%s — refusing the write",
                    comment.id,
                    comment.post_id,
                    space_id,
                )

    async def _authored_post(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        if not rid or await self._space_post_repo.get(rid) is not None:
            return False
        return await self._may_author(
            held_back, event, space_id, str(r.get("author") or "")
        )

    async def _authored_post_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_POST_DELETED rule
        # (``FederationInboundService._owned_post_mutation_allowed``):
        # a post held live here IN THIS SPACE, removed by its
        # author's household or one with content authority (host /
        # admin / moderator seat — ``may_mutate``), then the space's
        # ``posts`` level for the delete, for the user who made it:
        # a moderator removal names its moderator
        # (``actor_user_id``, migration 0084) — who must be seated
        # on the provider and pass ``moderates_as`` /
        # ``admin_as`` there, as the live event's actor does; the
        # author's own delete names nobody and counts as the
        # author's when the provider speaks for the author. A
        # removal recorded before 0084 names nobody: from a
        # moderator household it passes only an ``OPEN`` level (the
        # host's own stream carries it regardless).
        rid = rid or str(r.get("post_id") or "")
        held_post = await self._space_post_repo.get(rid) if rid else None
        if held_post is None or held_post[0] != space_id or held_post[1].deleted:
            return False
        author = held_post[1].author
        if not await auth.may_mutate(event, space_id, author):
            return False
        actor = (
            str(r.get("actor_user_id") or "") or None if r.get("moderated") else None
        )
        if actor is None and await auth.acts_for(
            event, space_id, author, any_role=True
        ):
            actor = author
        return await auth.access_admits(
            event,
            space_id,
            "posts",
            ContentAction.DELETE,
            actor=actor,
            row_owner=author,
            quiet=True,
        )

    async def _authored_comment_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_COMMENT_DELETED rule
        # (``_owned_comment_mutation_allowed``): a comment held live
        # here on a post of THIS SPACE, removed by its author's
        # household or one with content authority.
        rid = rid or str(r.get("comment_id") or "")
        held_comment = await self._space_post_repo.get_comment(rid) if rid else None
        if held_comment is None or held_comment.deleted:
            return False
        parent = await self._space_post_repo.get(held_comment.post_id)
        if parent is None or parent[0] != space_id:
            return False
        return await auth.may_mutate(event, space_id, held_comment.author)

    async def _authored_comment(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        if not rid or await self._space_post_repo.get_comment(rid) is not None:
            return False
        return await self._may_author(
            held_back,
            event,
            space_id,
            str(r.get("author") or ""),
            subscriber_comment=True,
        )

    async def _persist_polls(
        self, records: list[dict[str, Any]], space_id: str, *, provider: str
    ) -> None:
        """Reply polls (v_92) — only the host's records get here (a member
        household's are refused in :meth:`_authored_record`). Each converges
        the poll on the host's copy: created with its options when missing
        (it missed the live ``SPACE_POLL_CREATED``), the host's
        question / deadline, closed when the host's is, and everybody's
        votes but our own people's — from a v_94 host's ``vote_stamps``:
        merged per voter by stamp while the poll is open (the one merge
        rule), replaced by the host's map once it is closed there (its
        result is the one every household shows); from a v_92 / v_93
        host's ``votes`` list as before v_94. An older host's record
        (neither) leaves the votes."""
        if self._poll_repo is None:
            log.debug(
                "received %d poll records — no poll_repo wired, skipping",
                len(records),
            )
            return
        keep = await self._local_people(space_id)
        for r in records:
            post_id = str(r.get("post_id") or "")
            meta = r.get("meta")
            options = r.get("options")
            if (
                not post_id
                or not isinstance(meta, dict)
                or not isinstance(options, list)
            ):
                continue
            question = str(meta.get("question") or "")
            shaped = [
                {"id": str(o.get("id") or ""), "text": str(o.get("text") or "")}
                for o in options
                if isinstance(o, dict) and o.get("id") and o.get("text")
            ]
            if not question or not shaped:
                continue
            raw_votes = r.get("votes")
            votes = (
                [
                    (str(v.get("option_id") or ""), str(v.get("voter_user_id") or ""))
                    for v in raw_votes
                    if isinstance(v, dict)
                ]
                if isinstance(raw_votes, list)
                else None
            )
            closes_at = meta.get("closes_at")
            lacked = await self._poll_repo.get_meta(post_id) is None
            if not await self._poll_repo.apply_synced_poll(
                space_id=space_id,
                post_id=post_id,
                question=question,
                closes_at=str(closes_at) if closes_at else None,
                allow_multiple=bool(meta.get("allow_multiple")),
                closed=bool(meta.get("closed")),
                options=shaped,
                votes=votes,
                keep_voters=keep,
                votes_partial=r.get("votes_partial") is True,
                vote_stamps=_synced_vote_stamps(r.get("vote_stamps")),
            ):
                _log_sync_refusal("poll", post_id, space_id)
                continue
            if lacked and await self._poll_repo.get_meta(post_id) is not None:
                # The stream created a poll this household lacked: the SPA
                # shows it, and a vote or a close held for it
                # (``PendingParentBuffer``) replays now. Never re-broadcast
                # (it names the household it came from).
                await self._bus.publish(
                    PollCreated(
                        post_id=post_id,
                        question=question,
                        allow_multiple=bool(meta.get("allow_multiple")),
                        space_id=space_id,
                        options=tuple(shaped),
                        closes_at=str(closes_at) if closes_at else None,
                        origin_instance_id=provider or space_id,
                    )
                )

    async def _persist_post_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed post deletes (``posts_deleted``).

        A post held live here in this space is soft-deleted — content
        cleared, the row kept as its tombstone — and ``PostDeleted`` is
        published as for a live ``SPACE_POST_DELETED`` (its origin the
        provider, so nothing is re-broadcast). A member household's records
        were admitted only under the live rule (:meth:`_authored_record`).

        From the **host**, an id we never held gets the soft-deleted row a
        delete that overtook its create leaves (v_49), so a stale copy
        another household streams later cannot create it — but only when
        the id is owner-bound to its ``author`` in THIS space (post ids are
        global: a stub for another space's id would block that space's real
        post here). A legacy (unbound) or mismatched id is skipped.
        Refusals are summarised once per chunk.
        """
        space = await self._space_repo.get(space_id)
        from_host = await self._acts_as_host(space, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            post_id = str(r.get("id") or r.get("post_id") or "")
            if not post_id:
                continue
            # A moderator removal names its moderator (migration 0084); the
            # author's own delete names nobody.
            moderator = (
                str(r.get("actor_user_id") or "") or None
                if r.get("moderated")
                else None
            )
            held = await self._space_post_repo.get(post_id)
            if held is not None:
                if held[0] != space_id:
                    cross_space.append(post_id)
                elif not held[1].deleted and await self._space_post_repo.soft_delete(
                    post_id, space_id=space_id, moderated_by=moderator
                ):
                    await self._bus.publish(
                        PostDeleted(
                            post_id=post_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                            author_user_id=held[1].author,
                        )
                    )
                continue
            if not from_host:
                continue
            author = str(r.get("author") or "")
            if (
                check_owner_bound_id(
                    SPACE_POST_KIND, post_id, space_id=space_id, owner_user_id=author
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            try:
                # The post's own type: a stub of a retention-exempt type
                # stays exempt here.
                post_type = PostType(str(r.get("type") or "text"))
            except ValueError:
                post_type = PostType.TEXT
            saved = await self._space_post_repo.save(
                space_id,
                Post(
                    id=post_id,
                    author=author,
                    type=post_type,
                    created_at=_parse_iso(r.get("created_at")),
                    deleted=True,
                    moderated=bool(r.get("moderated")),
                ),
            )
            if saved is not None and moderator is not None:
                await self._space_post_repo.soft_delete(
                    post_id, space_id=space_id, moderated_by=moderator
                )
        _log_tombstone_refusals("post", provider, space_id, cross_space, unbound)

    async def _persist_comment_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed comment deletes (``comments_deleted``).

        A comment held live here on a post of this space is soft-deleted,
        its post's comment count lowered and ``CommentDeleted`` published —
        what a live ``SPACE_COMMENT_DELETED`` does. A member household's
        records were admitted only under the live rule
        (:meth:`_authored_record`).

        From the **host**, an id we never held gets a soft-deleted row on
        its post — only when the id is owner-bound to its ``author`` in THIS
        space and the post is held here in this space (the insert checks
        it), so a stale copy streamed later cannot create the comment.
        """
        space = await self._space_repo.get(space_id)
        from_host = await self._acts_as_host(space, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            comment_id = str(r.get("id") or r.get("comment_id") or "")
            if not comment_id:
                continue
            held = await self._space_post_repo.get_comment(comment_id)
            if held is not None:
                parent = await self._space_post_repo.get(held.post_id)
                if parent is None or parent[0] != space_id:
                    cross_space.append(comment_id)
                elif (
                    not held.deleted
                    and await self._space_post_repo.soft_delete_comment(
                        comment_id, space_id=space_id
                    )
                ):
                    await self._space_post_repo.decrement_comment_count(
                        held.post_id, space_id=space_id
                    )
                    await self._bus.publish(
                        CommentDeleted(
                            post_id=held.post_id,
                            comment_id=comment_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                            author_user_id=held.author,
                        )
                    )
                continue
            if not from_host:
                continue
            author = str(r.get("author") or "")
            post_id = str(r.get("post_id") or "")
            if (
                not post_id
                or check_owner_bound_id(
                    SPACE_COMMENT_KIND,
                    comment_id,
                    space_id=space_id,
                    owner_user_id=author,
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            if not await self._space_post_repo.add_comment(
                Comment(
                    id=comment_id,
                    post_id=post_id,
                    author=author,
                    type=CommentType.TEXT,
                    created_at=_parse_iso(r.get("created_at")),
                    deleted=True,
                ),
                space_id=space_id,
            ):
                unbound += 1  # its post is not held here in this space
        _log_tombstone_refusals("comment", provider, space_id, cross_space, unbound)


def _post_from_record(r: dict[str, Any]) -> Post | None:
    post_id = r.get("id")
    author = r.get("author")
    if not post_id or not author:
        return None
    try:
        post_type = PostType(str(r.get("type") or "text"))
    except ValueError:
        post_type = PostType.TEXT
    file_meta_dict = r.get("file_meta")
    file_meta = None
    if isinstance(file_meta_dict, dict):
        try:
            file_meta = FileMeta(**file_meta_dict)
        except TypeError:
            file_meta = None
    return Post(
        id=str(post_id),
        author=str(author),
        type=post_type,
        created_at=_parse_iso(r.get("created_at")),
        content=r.get("content"),
        # Media references only in the local-upload shape.
        media_url=space_media_ref(r.get("media_url")),
        comment_count=int(r.get("comment_count") or 0),
        pinned=bool(r.get("pinned", False)),
        deleted=bool(r.get("deleted", False)),
        edited_at=_parse_iso(r.get("edited_at")) if r.get("edited_at") else None,
        moderated=bool(r.get("moderated", False)),
        file_meta=file_meta,
        # An unannounced listing / event anchor must stay out of the
        # joiner's feed exactly as it is out of the provider's.
        hidden_from_feed=bool(r.get("hidden_from_feed", False)),
        # The post's image gallery — the media bytes that follow are
        # matched against these names. Local-upload references, feed-capped.
        image_urls=space_media_refs(r.get("image_urls"), limit=FEED_POST_MAX_IMAGES),
        # The author-built link card (never re-fetched here), re-validated.
        link_preview=space_wire_link_preview(r.get("link_preview")),
        # A joiner sees the reactions the provider holds (a held post keeps
        # its own — see the ``posts`` branch).
        reactions=_reactions_from_record(r.get("reactions")),
    )


def _reactions_from_record(raw: object) -> dict[str, frozenset[str]]:
    """The exporter's ``{emoji: [user_id, …]}``, malformed entries dropped.
    No post-wide emoji cap (v_93): a joiner shows what the provider shows."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, frozenset[str]] = {}
    for emoji, users in raw.items():
        if not isinstance(emoji, str) or not emoji or not isinstance(users, list):
            continue
        ids = frozenset(u for u in users if isinstance(u, str) and u)
        if ids:
            out[emoji] = ids
    return out


def _comment_from_record(r: dict[str, Any]) -> Comment | None:
    if not r.get("id") or not r.get("post_id") or not r.get("author"):
        return None
    try:
        comment_type = CommentType(str(r.get("type") or "text"))
    except ValueError:
        comment_type = CommentType.TEXT
    return Comment(
        id=str(r["id"]),
        post_id=str(r["post_id"]),
        author=str(r["author"]),
        type=comment_type,
        created_at=_parse_iso(r.get("created_at")),
        parent_id=r.get("parent_id"),
        content=r.get("content"),
        media_url=local_media_ref(r.get("media_url")),
    )
