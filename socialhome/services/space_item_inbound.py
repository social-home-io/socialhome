"""Apply member-published comments, deletes and reactions (v_49).

:class:`~socialhome.services.space_public_inbound.SpacePublicInbound`
receives every ``space_item`` frame, decrypts it and runs the checks every
item type shares — the author signature over the generic inner
(:mod:`socialhome.services.space_item_author`), the origin household, the
writer cert at the scope the real type needs
(:func:`socialhome.domain.space_item.required_scope`), epoch freshness and
the v2 user binding. It hands the generic types here, which applies them
with the same rules as the federated ``SPACE_COMMENT_*`` /
``SPACE_POST_DELETED`` handlers — never more permissive:

* **Only the author edits or deletes.** The stored row's author must be the
  item's signed author, and the row must live in this space. Moderation of
  someone else's content stays on the host path (``SPACE_*`` events).
* **Owner-bound ids** (v_36): a new comment's id must commit to its author
  and space; so must the id of a delete for a row not held yet.
* **Member households also check the roster** — the author's live seat on
  the origin household (:meth:`SpaceAuthorship.item_seat_admits`; a
  follower seat only where the space lets followers comment / react, or for
  a change to its own row); a post delete runs the ``posts`` access level
  (:meth:`SpaceAuthorship.item_access_admits`), matching the ``write`` scope
  the cert already proves. Followers hold no roster and rely on the cert
  scope plus the user binding — and accept a ``comment``-scope reaction only
  while their copy of the space lets followers react.

**Ordering independence** (hard requirement 5 of the member-publish docs):
the GFS delivers one space's items in order, but the host relay, the
federated copy, the retry queues and space sync interleave with it.

* A **delete before its create** leaves a soft-deleted row under the
  item's id (the "tombstone" — the same ``deleted=1`` row a normal delete
  leaves), so the later create is a duplicate on every path: this relay
  and the host relay dedupe by id, the federated post create keeps a
  deleted row deleted, the federated comment create refuses a held id, and
  space sync skips a post deleted here.
* An **edit** is the author's full signed snapshot plus its signed time
  stamp; it lands only over an older stored stamp (last writer wins). An
  edit that arrives before its create IS the create, at its newest
  content, under the create's rules; the later create is a duplicate.
  An edit never revives a deleted row.
* **Reactions** are ordered per ``(post, user, emoji)`` by their signed
  stamp, persisted next to the reactions in the same transaction
  (``space_posts.reaction_stamps_json``; local writes stamp now), so a
  duplicate ``reaction_add`` from a second connection server, a queued
  copy, or one arriving after a restart cannot undo a later remove.

A comment, reaction or edit whose post is not held here yet is dropped:
the federated copy (members) or a later space sync carries it.

**Authority removals** (:meth:`SpaceItemInbound.apply_authority_removal`)
come over the host relay, not ``space_item``: a seed holder's notice that a
post or comment was removed (by anyone the host path allowed). Not
author-bound — the space authority may remove any item — but a tombstone
for an item not held yet needs an id owner-bound to the notice's author in
this space, so a removal that overtakes its create keeps the item gone and
can never claim another space's id.
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    PostDeleted,
    PostReactionChanged,
)
from ..domain.post import Comment, CommentType, Post, PostType
from ..domain.space_item import (
    ITEM_TYPE_COMMENT,
    ITEM_TYPE_COMMENT_DELETE,
    ITEM_TYPE_COMMENT_EDIT,
    ITEM_TYPE_POST_DELETE,
    ITEM_TYPE_REACTION_ADD,
    ITEM_TYPE_REACTION_REMOVE,
    MAX_REACTION_EMOJI_CHARS,
    REMOVAL_TARGET_POST,
    AuthorityRemoval,
    StaleItemStamp,
    item_stamp,
    stamp_to_db,
)
from ..domain.writer_cert import WRITER_SCOPE_WRITE
from ..federation.owner_bound_id import (
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    OwnerBinding,
    check_owner_bound_id,
)
from ..infrastructure.event_bus import EventBus
from ..utils.datetime import parse_iso8601_lenient
from .inbound_media_store import local_media_ref

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..federation.space_authorship import SpaceAuthorship
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .space_mentions import SpaceMentionResolver

log = logging.getLogger(__name__)


class _Item:
    """One verified item, as the handlers below read it."""

    __slots__ = (
        "space",
        "inner",
        "origin",
        "author",
        "target",
        "post_id",
        "stamp",
        "cert_scope",
        "member",
        "authorship",
    )

    def __init__(
        self,
        *,
        space: "Space",
        inner: dict[str, Any],
        origin: str,
        stamp: datetime,
        cert_scope: str,
        member: bool,
        authorship: "SpaceAuthorship | None",
    ) -> None:
        self.space = space
        self.inner = inner
        self.origin = origin
        self.author = str(inner.get("author_user_id") or "")
        self.target = str(inner.get("item_target") or "")
        self.post_id = str(inner.get("post_id") or "")
        self.stamp = stamp
        self.cert_scope = cert_scope
        self.member = member
        self.authorship = authorship


class SpaceItemInbound:
    """Persist member-published generic items (see the module docstring)."""

    __slots__ = ("_bus", "_spaces", "_posts", "_mentions")

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        space_post_repo: "AbstractSpacePostRepo",
        mention_resolver: "SpaceMentionResolver | None" = None,
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._posts = space_post_repo
        self._mentions = mention_resolver

    async def apply(
        self,
        *,
        space: "Space",
        item_type: str,
        inner: dict[str, Any],
        origin_instance_id: str,
        cert_scope: str,
        member_household: bool,
        authorship: "SpaceAuthorship | None",
    ) -> bool:
        """Apply one verified generic item. ``True`` when it changed
        something here; every refusal is logged."""
        stamp = item_stamp(inner.get("ts"))
        if stamp is None:
            log.warning(
                "space_item: %s with a missing or future stamp — dropped", item_type
            )
            return False
        item = _Item(
            space=space,
            inner=inner,
            origin=origin_instance_id,
            stamp=stamp,
            cert_scope=cert_scope,
            member=member_household,
            authorship=authorship,
        )
        if not item.target:
            return False
        if item_type == ITEM_TYPE_COMMENT:
            return await self._comment(item)
        if item_type == ITEM_TYPE_COMMENT_EDIT:
            return await self._comment_edit(item)
        if item_type == ITEM_TYPE_COMMENT_DELETE:
            return await self._comment_delete(item)
        if item_type == ITEM_TYPE_POST_DELETE:
            return await self._post_delete(item)
        if item_type in (ITEM_TYPE_REACTION_ADD, ITEM_TYPE_REACTION_REMOVE):
            return await self._reaction(item, added=item_type == ITEM_TYPE_REACTION_ADD)
        log.warning("space_item: unsupported item type %r — dropped", item_type)
        return False

    # ── Shared checks ────────────────────────────────────────────────────

    async def _seat_ok(self, item: _Item, *, subscriber_ok: bool) -> bool:
        if item.authorship is None or not item.member:
            return True
        return await item.authorship.item_seat_admits(
            origin_instance_id=item.origin,
            space_id=item.space.id,
            author_user_id=item.author,
            subscriber_ok=subscriber_ok,
        )

    def _bound_to_author(self, kind: str, row_id: str, item: _Item) -> bool:
        return (
            check_owner_bound_id(
                kind, row_id, space_id=item.space.id, owner_user_id=item.author
            )
            is OwnerBinding.VALID
        )

    async def _post_here(self, post_id: str, item: _Item) -> Post | None:
        """The post ``post_id`` iff it lives in this space."""
        got = await self._posts.get(post_id)
        if got is None or got[0] != item.space.id:
            return None
        return got[1]

    async def _comment_here(self, comment_id: str, item: _Item) -> Comment | None:
        """``comment_id`` iff its post lives in this space — a held comment
        of another space is refused (logged) as ``None``."""
        comment = await self._posts.get_comment(comment_id)
        if comment is None:
            return None
        if await self._post_here(comment.post_id, item) is None:
            log.warning(
                "space_item: comment %s belongs to another space — dropped", comment_id
            )
            return None
        return comment

    # ── Comments ─────────────────────────────────────────────────────────

    async def _comment(self, item: _Item, *, edited: bool = False) -> bool:
        """A new comment — also an edit that arrived before its create
        (``edited``), stored at the edited content with its stamp."""
        cid = item.target
        if not self._bound_to_author(SPACE_COMMENT_KIND, cid, item):
            log.warning(
                "space_item: comment %s id is not bound to its author — dropped", cid
            )
            return False
        try:
            ctype = CommentType(str(item.inner.get("comment_type") or ""))
        except ValueError:
            return False
        content = item.inner.get("content")
        media_url = item.inner.get("media_url")
        if ctype is CommentType.TEXT and not (
            isinstance(content, str) and content.strip()
        ):
            return False
        if ctype is CommentType.IMAGE and not (
            isinstance(media_url, str) and media_url
        ):
            return False
        if edited and ctype is not CommentType.TEXT:
            return False
        post = await self._post_here(item.post_id, item)
        if post is None or post.deleted:
            log.info(
                "space_item: comment %s on a post not held here (or deleted) — dropped",
                cid,
            )
            return False
        if await self._posts.get_comment(cid) is not None:
            log.debug("space_item: duplicate comment %s — dropped", cid)
            return False
        if not await self._seat_ok(
            item, subscriber_ok=item.space.features.allow_subscriber_comment
        ):
            log.warning(
                "space_item: comment %s author holds no commenting seat here — dropped",
                cid,
            )
            return False
        parent = item.inner.get("parent_id")
        comment = Comment(
            id=cid,
            post_id=post.id,
            author=item.author,
            type=ctype,
            created_at=parse_iso8601_lenient(item.inner.get("created_at")),
            parent_id=str(parent) if isinstance(parent, str) and parent else None,
            content=content if isinstance(content, str) else None,
            media_url=local_media_ref(media_url),
            edited_at=item.stamp if edited else None,
        )
        if not await self._posts.add_comment(comment, space_id=item.space.id):
            log.warning(
                "space_item: comment %s refused (its parent is not on the post)", cid
            )
            return False
        await self._posts.increment_comment_count(post.id, space_id=item.space.id)
        await self._bus.publish(
            CommentAdded(
                post_id=post.id,
                comment=comment,
                space_id=item.space.id,
                origin_instance_id=item.origin,
                mentions=(
                    await self._mentions.resolve(
                        item.space.id, comment.content, author_id=item.author
                    )
                    if self._mentions is not None
                    else ()
                ),
            )
        )
        return True

    async def _comment_edit(self, item: _Item) -> bool:
        cid = item.target
        content = item.inner.get("content")
        if not (isinstance(content, str) and content.strip()):
            return False
        comment = await self._comment_here(cid, item)
        if comment is None:
            if await self._posts.get_comment(cid) is not None:
                return False  # another space's comment (logged)
            # An edit before its create: the signed snapshot is the comment
            # at its newest content, under the create's rules.
            return await self._comment(item, edited=True)
        if comment.author != item.author:
            log.warning(
                "space_item: comment %s edit by someone other than its author — "
                "dropped",
                cid,
            )
            return False
        if comment.deleted or comment.type is not CommentType.TEXT:
            log.debug("space_item: comment %s deleted or not text — edit dropped", cid)
            return False
        if not await self._seat_ok(item, subscriber_ok=True):
            return False
        if not await self._posts.edit_comment(
            cid,
            content,
            space_id=item.space.id,
            edited_at=stamp_to_db(item.stamp),
        ):
            log.debug("space_item: comment %s holds a newer edit — dropped", cid)
            return False
        refreshed = await self._posts.get_comment(cid)
        if refreshed is None:
            return False
        await self._bus.publish(
            CommentUpdated(
                post_id=refreshed.post_id,
                comment=refreshed,
                space_id=item.space.id,
                origin_instance_id=item.origin,
                new_mentions=(
                    await self._mentions.added(
                        item.space.id,
                        comment.content,
                        refreshed.content,
                        author_id=item.author,
                    )
                    if self._mentions is not None
                    else ()
                ),
            )
        )
        return True

    async def _comment_delete(self, item: _Item) -> bool:
        cid = item.target
        comment = await self._comment_here(cid, item)
        if comment is None:
            if await self._posts.get_comment(cid) is not None:
                return False  # another space's comment (logged)
            return await self._comment_tombstone(item)
        if comment.author != item.author:
            log.warning(
                "space_item: comment %s delete by someone other than its author — "
                "dropped",
                cid,
            )
            return False
        if comment.deleted:
            return False
        if not await self._seat_ok(item, subscriber_ok=True):
            return False
        if not await self._posts.soft_delete_comment(cid, space_id=item.space.id):
            return False
        await self._posts.decrement_comment_count(
            comment.post_id, space_id=item.space.id
        )
        await self._bus.publish(
            CommentDeleted(
                post_id=comment.post_id,
                comment_id=cid,
                space_id=item.space.id,
                origin_instance_id=item.origin,
            )
        )
        return True

    async def _comment_tombstone(self, item: _Item) -> bool:
        """A comment delete that arrived before its create: a soft-deleted
        row under the id, so the create is a duplicate when it lands. Only
        for an id bound to the deleter (else a household could pre-empt
        someone else's comment) on a post held here."""
        cid = item.target
        if not self._bound_to_author(SPACE_COMMENT_KIND, cid, item):
            log.warning(
                "space_item: early delete of comment %s, whose id is not bound to "
                "the deleter — dropped",
                cid,
            )
            return False
        post = await self._post_here(item.post_id, item)
        if post is None:
            log.info(
                "space_item: early delete of comment %s on a post not held here — "
                "dropped",
                cid,
            )
            return False
        if not await self._seat_ok(item, subscriber_ok=True):
            return False
        tombstone = Comment(
            id=cid,
            post_id=post.id,
            author=item.author,
            type=CommentType.TEXT,
            created_at=item.stamp,
            deleted=True,
        )
        return await self._posts.add_comment(tombstone, space_id=item.space.id)

    # ── Post delete ──────────────────────────────────────────────────────

    async def _post_access_ok(self, item: _Item) -> bool:
        if item.authorship is None or not item.member:
            return True
        return await item.authorship.item_access_admits(
            origin_instance_id=item.origin,
            space_id=item.space.id,
            feature="posts",
            author_user_id=item.author,
        )

    async def _post_delete(self, item: _Item) -> bool:
        pid = item.target
        if item.post_id != pid:
            return False
        got = await self._posts.get(pid)
        if got is not None and got[0] != item.space.id:
            log.warning("space_item: post %s belongs to another space — dropped", pid)
            return False
        if got is not None and got[1].author != item.author:
            log.warning(
                "space_item: post %s delete by someone other than its author — dropped",
                pid,
            )
            return False
        if got is not None and got[1].deleted:
            return False
        if got is None and not self._bound_to_author(SPACE_POST_KIND, pid, item):
            log.warning(
                "space_item: early delete of post %s, whose id is not bound to the "
                "deleter — dropped",
                pid,
            )
            return False
        if not await self._post_access_ok(item):
            log.warning(
                "space_item: post %s delete refused by the posts access level here",
                pid,
            )
            return False
        if got is None:
            # Delete before create: the soft-deleted row is the tombstone.
            tombstone = Post(
                id=pid,
                author=item.author,
                type=PostType.TEXT,
                created_at=item.stamp,
                deleted=True,
            )
            return await self._posts.save(item.space.id, tombstone) is not None
        if not await self._posts.soft_delete(pid, space_id=item.space.id):
            return False
        await self._bus.publish(
            PostDeleted(
                post_id=pid,
                space_id=item.space.id,
                origin_instance_id=item.origin,
                author_user_id=item.author,
            )
        )
        return True

    # ── Authority removals (host relay) ─────────────────────────────────

    async def apply_authority_removal(
        self, *, space: "Space", removal: AuthorityRemoval
    ) -> bool:
        """Apply a space-authority removal notice (its authority signature
        and space already verified by the caller). Unlike a member's
        ``post_delete`` / ``comment_delete`` it is not author-bound: the
        space authority may remove anyone's item, as a moderator may on the
        host path. Same ordering rules as a member delete — a removal for an
        item not held yet leaves the soft-deleted tombstone, so the later
        create is a duplicate on every path; a removal is final, so a
        duplicate changes nothing. The events it publishes carry the space's
        owner household as their origin: never fanned back out, never
        credited to the remover (whom the notice does not name)."""
        if removal.target == REMOVAL_TARGET_POST:
            return await self._remove_post(space, removal)
        return await self._remove_comment(space, removal)

    async def _remove_post(self, space: "Space", removal: AuthorityRemoval) -> bool:
        pid = removal.item_id
        got = await self._posts.get(pid)
        if got is not None and got[0] != space.id:
            log.warning(
                "space_item: removal of post %s of another space — dropped", pid
            )
            return False
        if got is None:
            if not _bound_here(SPACE_POST_KIND, pid, space, removal):
                return False
            tombstone = Post(
                id=pid,
                author=removal.author_user_id,
                type=PostType.TEXT,
                created_at=datetime.now(timezone.utc),
                deleted=True,
            )
            return await self._posts.save(space.id, tombstone) is not None
        row = got[1]
        if row.deleted:
            return False
        if not await self._posts.soft_delete(pid, space_id=space.id):
            return False
        await self._bus.publish(
            PostDeleted(
                post_id=pid,
                space_id=space.id,
                origin_instance_id=space.owner_instance_id,
                author_user_id=row.author,
            )
        )
        return True

    async def _remove_comment(self, space: "Space", removal: AuthorityRemoval) -> bool:
        cid = removal.item_id
        got = await self._posts.get(removal.post_id)
        if got is None or got[0] != space.id:
            log.info(
                "space_item: removal of comment %s on a post not held here — dropped",
                cid,
            )
            return False
        comment = await self._posts.get_comment(cid)
        if comment is None:
            if not _bound_here(SPACE_COMMENT_KIND, cid, space, removal):
                return False
            tombstone = Comment(
                id=cid,
                post_id=removal.post_id,
                author=removal.author_user_id,
                type=CommentType.TEXT,
                created_at=datetime.now(timezone.utc),
                deleted=True,
            )
            return await self._posts.add_comment(tombstone, space_id=space.id)
        if comment.post_id != removal.post_id:
            log.warning(
                "space_item: removal of comment %s names another post — dropped", cid
            )
            return False
        if comment.deleted:
            return False
        if not await self._posts.soft_delete_comment(cid, space_id=space.id):
            return False
        await self._posts.decrement_comment_count(comment.post_id, space_id=space.id)
        await self._bus.publish(
            CommentDeleted(
                post_id=comment.post_id,
                comment_id=cid,
                space_id=space.id,
                origin_instance_id=space.owner_instance_id,
                author_user_id=comment.author,
            )
        )
        return True

    # ── Reactions ────────────────────────────────────────────────────────

    async def _reaction(self, item: _Item, *, added: bool) -> bool:
        if item.post_id != item.target:
            return False
        emoji = item.inner.get("emoji")
        if (
            not isinstance(emoji, str)
            or not emoji
            or len(emoji) > MAX_REACTION_EMOJI_CHARS
            or unicodedata.normalize("NFC", emoji.strip()) != emoji
        ):
            return False
        features = item.space.features
        # A follower can't tell a comment-only member from a follower: a
        # comment-scope reaction counts there only while followers may react.
        if (
            not item.member
            and item.cert_scope != WRITER_SCOPE_WRITE
            and not features.allow_subscriber_react
        ):
            log.info(
                "space_item: comment-scope reaction where followers may not react "
                "— dropped"
            )
            return False
        if not await self._seat_ok(item, subscriber_ok=features.allow_subscriber_react):
            log.warning("space_item: reactor holds no reacting seat here — dropped")
            return False
        try:
            if added:
                post = await self._posts.add_reaction(
                    item.post_id,
                    emoji,
                    item.author,
                    space_id=item.space.id,
                    stamp=stamp_to_db(item.stamp),
                )
            else:
                post = await self._posts.remove_reaction(
                    item.post_id,
                    emoji,
                    item.author,
                    space_id=item.space.id,
                    stamp=stamp_to_db(item.stamp),
                )
        except StaleItemStamp:
            log.debug("space_item: stale reaction on %s — dropped", item.post_id)
            return False
        except KeyError, ValueError:
            log.info(
                "space_item: reaction on post %s not applied (unknown, deleted or "
                "full)",
                item.post_id,
            )
            return False
        await self._bus.publish(
            PostReactionChanged(
                post=post,
                space_id=item.space.id,
                origin_instance_id=item.origin,
                reactor_user_id=item.author,
                emoji=emoji,
                added=added,
            )
        )
        return True


def _bound_here(
    kind: str, row_id: str, space: "Space", removal: AuthorityRemoval
) -> bool:
    """A removal may leave a tombstone for an id not held here only when the
    id is owner-bound to the named author in THIS space — otherwise a seed
    holder of one space could pre-empt another space's row on a household
    that follows both. (Logged.)"""
    if (
        removal.author_user_id
        and check_owner_bound_id(
            kind, row_id, space_id=space.id, owner_user_id=removal.author_user_id
        )
        is OwnerBinding.VALID
    ):
        return True
    log.info(
        "space_item: removal of %s %s, not held here and not bound to this "
        "space — no tombstone",
        kind,
        row_id,
    )
    return False
