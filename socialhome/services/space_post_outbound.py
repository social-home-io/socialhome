"""Outbound federation bridge for space-feed posts.

Subscribes to :class:`SpacePostCreated` / :class:`PostEdited` /
:class:`PostDeleted` (when scoped to a space) and broadcasts the
corresponding ``SPACE_POST_*`` federation event to every household
that has a member in the space — direct delivery when the peer is
paired CONFIRMED, mesh-routed under ``SPACE_ROUTED`` when not.

Before this service, ``SpacePostCreated`` was only consumed by
local bridges (HA, realtime WS, search indexer, system-album); the
federation outbound for the post never fired, so a remote member
on another household never saw posts in spaces they belonged to.
:mod:`socialhome.services.federation_inbound_service` already
had the inbound half wired (``_on_space_post_created`` saves to
``space_posts`` on receive); the missing outbound is what this
module supplies.

Membership-gating is handled by
:meth:`FederationService.broadcast_to_space_members` — it queries
``space_instances`` for the broadcast set, so non-member relays
never receive the envelope directly. Mesh-routed delivery to a
non-paired member wraps in ``SPACE_ROUTED`` so the intermediate
relay sees only opaque ciphertext (§D1b transport rule, see
CLAUDE.md "Encryption-First Rule").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    PostDeleted,
    PostEdited,
    PostReactionChanged,
    SpacePostCreated,
)
from ..domain.federation import FederationEventType
from ..domain.link_preview import link_preview_to_dict
from ..domain.space import PUBLIC_SPACE_TIERS
from ..domain.space_item import (
    ITEM_TYPE_COMMENT,
    ITEM_TYPE_COMMENT_DELETE,
    ITEM_TYPE_COMMENT_EDIT,
    ITEM_TYPE_POST,
    ITEM_TYPE_POST_DELETE,
    ITEM_TYPE_POST_EDIT,
    ITEM_TYPE_REACTION_ADD,
    ITEM_TYPE_REACTION_REMOVE,
)
from ..infrastructure.event_bus import EventBus
from .moderation_release import current_release, with_release
from .space_item_author import build_signed_item_inner
from .space_public_author import build_signed_author_inner

if TYPE_CHECKING:
    from ..domain.post import Post
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .gfs_member_publish_service import GfsMemberPublishService
    from .space_media_sync_service import SpaceMediaSyncService
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)


class SpacePostOutbound:
    """Bus-event → federation broadcaster for space posts."""

    __slots__ = (
        "_bus",
        "_federation",
        "_media_sync",
        "_federation_repo",
        "_spaces",
        "_users",
        "_own_instance_id",
        "_own_instance_pk",
        "_own_identity_seed",
        "_writer_certs",
        "_member_gfs",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
        user_repo: "AbstractUserRepo",
        media_sync: "SpaceMediaSyncService | None" = None,
        federation_repo=None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._spaces = space_repo
        self._users = user_repo
        #: Identity for the per-author relay-hint signature. Empty until
        #: :meth:`attach_identity` wires the real instance id / pubkey /
        #: seed (mirrors :class:`SpacePublicOutbound`). Without it the
        #: ``public_relay`` hint is omitted and the normal broadcast still
        #: fires.
        self._own_instance_id: str = ""
        self._own_instance_pk: bytes = b""
        self._own_identity_seed: bytes = b""
        #: v_49 — when wired, the relay hint carries this household's writer
        #: cert for the current epoch (a seed holder re-checks + re-stamps it).
        self._writer_certs: "SpaceWriterCertService | None" = None
        #: v_49 — when wired, a non-seed-holding writer household publishes
        #: its own public/global post to the GFS itself (trusted mode).
        self._member_gfs: "GfsMemberPublishService | None" = None
        #: Optional — when wired, ``SPACE_POST_CREATED`` broadcasts
        #: are followed by per-peer outbox enqueues for every
        #: referenced media URL. The sync service's scheduler reads
        #: rows lazily, chunks files > 1 MiB, retries with
        #: exponential backoff. Without it the receiver's
        #: ``<img src>`` 404s because only the URL string federates.
        self._media_sync = media_sync
        #: Needed to enumerate the broadcast set (every member
        #: household) for the media outbox. Optional — when ``None``
        #: the media fan-out is a no-op.
        self._federation_repo = federation_repo
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)
        self._bus.subscribe(PostEdited, self._on_post_edited)
        self._bus.subscribe(PostDeleted, self._on_post_deleted)
        # #117 followup — same gap on comments. ``CommentAdded`` /
        # ``CommentUpdated`` / ``CommentDeleted`` already carry
        # ``space_id`` (unlike the post-level events), so we can
        # gate-and-federate cleanly here.
        self._bus.subscribe(CommentAdded, self._on_comment_added)
        self._bus.subscribe(CommentUpdated, self._on_comment_updated)
        self._bus.subscribe(CommentDeleted, self._on_comment_deleted)
        # v_49 — a local user's space reaction rides only the member relay
        # (space reactions are not federated events).
        self._bus.subscribe(PostReactionChanged, self._on_reaction_changed)

    def attach_writer_certs(self, writer_certs: "SpaceWriterCertService") -> None:
        """Wire the v_49 writer-cert holder for the relay hint."""
        self._writer_certs = writer_certs

    def attach_member_gfs(self, member_gfs: "GfsMemberPublishService") -> None:
        """Wire the v_49 trusted-mode member publisher."""
        self._member_gfs = member_gfs

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        own_instance_public_key: bytes,
        own_identity_seed: bytes,
    ) -> None:
        """Wire this household's identity so the member broadcast can carry a
        pre-signed ``public_relay`` author hint (mirrors
        :meth:`SpacePublicOutbound.attach_identity`)."""
        self._own_instance_id = own_instance_id
        self._own_instance_pk = own_instance_public_key
        self._own_identity_seed = own_identity_seed

    async def _schedule_gfs_publish(self, space_id: str, post: Post) -> None:
        """Build the GFS inner of our own post and hand it to the member
        publisher's background queue (v_49 trusted mode). Never raises."""
        assert self._member_gfs is not None
        try:
            author = await self._users.get_by_user_id(post.author)
            if author is None:
                return
            inner = build_signed_author_inner(
                post=post,
                space_id=space_id,
                author_username=author.username,
                author_pk=self._own_instance_pk,
                author_identity_seed=self._own_identity_seed,
                origin_instance_id=self._own_instance_id,
                author_identity_anchor=author.identity_anchor,
                item_type=ITEM_TYPE_POST,
                item_target=post.id,
            )
            self._member_gfs.schedule_post(space_id, post.author, inner)
        except Exception:
            log.exception(
                "member GFS publish could not be scheduled for space=%s post=%s",
                space_id,
                post.id,
            )

    # ── v_49 member publish of comments, reactions, own edits / deletes ──

    async def _member_publishable(self, space_id: str | None) -> bool:
        """A local write in ``space_id`` may go over the member relay: a
        PUBLIC/GLOBAL space that admits followers, with our identity and
        the member publisher wired. The publisher then decides by our cert
        whether THIS author may publish THIS item (else the host path)."""
        if (
            not space_id
            or self._member_gfs is None
            or not (
                self._own_instance_id
                and self._own_instance_pk
                and self._own_identity_seed
            )
        ):
            return False
        space = await self._spaces.get(space_id)
        if space is None:
            return False
        if space.space_type in PUBLIC_SPACE_TIERS and bool(
            space.features.allow_subscribers
        ):
            return True
        # v_51 — a private space with an opaque connection-server channel.
        return await self._member_gfs.channel_space(space_id)

    async def _schedule_item(
        self,
        space_id: str,
        author_user_id: str,
        item_type: str,
        *,
        target: str,
        post_id: str,
        **fields: str | None,
    ) -> None:
        """Sign a generic item inner for our own user and hand it to the
        member publisher's background queue. Never raises."""
        assert self._member_gfs is not None
        try:
            author = await self._users.get_by_user_id(author_user_id)
            if author is None:
                return
            inner = build_signed_item_inner(
                item_type=item_type,
                item_target=target,
                space_id=space_id,
                post_id=post_id,
                author_user_id=author_user_id,
                author_username=author.username,
                author_pk=self._own_instance_pk,
                author_identity_seed=self._own_identity_seed,
                origin_instance_id=self._own_instance_id,
                author_identity_anchor=author.identity_anchor,
                ts=datetime.now(timezone.utc).isoformat(),
                **fields,
            )
            self._member_gfs.schedule_item(space_id, author_user_id, item_type, inner)
        except Exception:
            log.exception(
                "member GFS %s could not be scheduled for space=%s target=%s",
                item_type,
                space_id,
                target,
            )

    async def _schedule_post_edit(self, space_id: str, post: Post) -> None:
        """The author's edit as a signed snapshot of the edited post plus its
        signed ``edited_at`` (receivers keep the newest). Never raises."""
        assert self._member_gfs is not None
        try:
            author = await self._users.get_by_user_id(post.author)
            if author is None:
                return
            inner = build_signed_author_inner(
                post=post,
                space_id=space_id,
                author_username=author.username,
                author_pk=self._own_instance_pk,
                author_identity_seed=self._own_identity_seed,
                origin_instance_id=self._own_instance_id,
                author_identity_anchor=author.identity_anchor,
                item_type=ITEM_TYPE_POST_EDIT,
                item_target=post.id,
                edited_at=datetime.now(timezone.utc).isoformat(),
            )
            self._member_gfs.schedule_item(
                space_id, post.author, ITEM_TYPE_POST_EDIT, inner
            )
        except Exception:
            log.exception(
                "member GFS post edit could not be scheduled for space=%s post=%s",
                space_id,
                post.id,
            )

    async def _on_reaction_changed(self, event: PostReactionChanged) -> None:
        if event.origin_instance_id is not None or not event.reactor_user_id:
            return
        if not await self._member_publishable(event.space_id):
            return
        assert event.space_id is not None
        await self._schedule_item(
            event.space_id,
            event.reactor_user_id,
            ITEM_TYPE_REACTION_ADD if event.added else ITEM_TYPE_REACTION_REMOVE,
            target=event.post.id,
            post_id=event.post.id,
            emoji=event.emoji,
        )

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        """Fan ``SPACE_POST_CREATED`` to every member household.

        The payload mirrors what
        :meth:`FederationInboundService._post_from_payload` expects —
        adding fields here without a matching receiver-side change
        is harmless (older peers ignore extras).
        """
        post = event.post
        # Local source-of-truth fires bus events for both household
        # feed posts (no space_id) AND space posts. Federation here is
        # only for space-scoped rows.
        if not event.space_id:
            return
        # ``origin_instance_id`` is None when the bus event came from
        # a local POST (the SPA's ``/api/spaces/{id}/posts``);
        # populated when ``federation_inbound_service`` re-published
        # after receiving SPACE_POST_CREATED. Re-broadcasting an
        # inbound-driven event would create a federation loop:
        # peer → us → peer → us → … Skip the publish.
        if event.origin_instance_id is not None:
            return
        # Calendar-event posts are derived locally on every household by
        # :class:`CalendarFeedBridge` from the federated
        # ``SPACE_CALENDAR_EVENT_CREATED`` envelope. Federating the
        # bridge's ``SpacePostCreated`` would deliver a *second* post id
        # to peers (their bridge already minted one with the
        # deterministic ``linked_event_id`` and the peer's outbound
        # then re-federates that one back to us), so the originator
        # ends up with two feed cards for one calendar event. Skip the
        # broadcast — the calendar event is the source of truth on the
        # wire and the bridge is the source of truth in each peer's DB.
        #
        # The one linked post that IS federated: an announce card released
        # from the moderation queue (v_43). No household's bridge mints that
        # card (its creator could not post straight away), so the approval
        # is the only source of it — it carries the event link. A bridge
        # card minted while an approved EVENT is applied stays local like
        # any other (it has no ``approved_by``).
        release = current_release()
        if post.linked_event_id is not None and (
            release is None or event.approved_by is None
        ):
            return
        payload: dict = {
            "id": post.id,
            "space_id": event.space_id,
            "author": post.author,
            # v_42: who made the write — receivers check it against the
            # space's ``posts`` access level. A create's actor is its author
            # — a release from the moderation queue too (v_43: the approver
            # is named in the approval block instead); a v_42 host's release
            # named the approver here.
            "actor_user_id": (
                post.author if release is not None else event.approved_by or post.author
            ),
            "type": post.type.value,
            "content": post.content,
            "media_url": post.media_url,
            "image_urls": list(post.image_urls),
            "occurred_at": post.created_at.isoformat() if post.created_at else None,
            # Mirror feed visibility on member households: a Bazaar listing's
            # anchor post is hidden from the feed unless the seller announced
            # it. Omitted-on-older-sender → receiver defaults to visible.
            "hidden_from_feed": post.hidden_from_feed,
        }
        if post.location is not None:
            payload["location"] = {
                "lat": post.location.lat,
                "lon": post.location.lon,
                "label": post.location.label,
            }
        # The author-built link card rides inside the encrypted payload like
        # the text; receivers validate it and never fetch the URL. Older
        # receivers ignore the key and show the post without a card.
        if post.link_preview is not None:
            payload["link_preview"] = link_preview_to_dict(post.link_preview)
        if post.linked_event_id is not None:
            payload["linked_event_id"] = post.linked_event_id
        if post.file_meta is not None:
            payload["file_meta"] = {
                "url": post.file_meta.url,
                "mime_type": post.file_meta.mime_type,
                "original_name": post.file_meta.original_name,
                "size_bytes": post.file_meta.size_bytes,
            }
        # Attach a pre-signed relay hint so a seed-holding member can forward this
        # public/global post to the GFS subscribers even when the owner is offline
        # (remote-author relay). Built only for a public/global space + a local
        # author we can sign for; private spaces never relay to the GFS. A
        # public/global space that allows no subscribers is listed for
        # discovery but is NOT publicly readable, so its GFS relay is dead
        # (see ``space_public_outbound``) — don't sign or ship a hint nothing
        # may use. Members are unaffected: they are fanned out below over
        # ``space_instances`` by ``broadcast_to_space_members``, which never
        # consults the hint.
        space = await self._spaces.get(event.space_id)
        if (
            space is not None
            and space.space_type in PUBLIC_SPACE_TIERS
            and space.features.allow_subscribers
            and self._own_instance_id
            and self._own_instance_pk
            and self._own_identity_seed
        ):
            author = await self._users.get_by_user_id(post.author)
            if author is not None:
                payload["public_relay"] = build_signed_author_inner(
                    post=post,
                    space_id=event.space_id,
                    author_username=author.username,
                    author_pk=self._own_instance_pk,
                    author_identity_seed=self._own_identity_seed,
                    origin_instance_id=self._own_instance_id,
                    # Carry the immutable uuid anchor so a seed-holding relay's
                    # self-cert survives a later username change. Passed raw —
                    # the builder normalises a legacy username-anchored row
                    # (``anchor == username``, the 0041 backfill) to "absent"
                    # so its signed bytes stay v_25-compatible.
                    author_identity_anchor=author.identity_anchor,
                )
                # v_49 — our household's writer cert, outside the author
                # signature (it authenticates itself against the space key).
                # Absent → a seed holder relays it on its own authority, as
                # for a pre-v_49 author.
                if self._writer_certs is not None:
                    try:
                        cert = await self._writer_certs.current_own_cert_wire(
                            event.space_id
                        )
                    except Exception:
                        log.exception(
                            "public_relay: writer cert lookup failed for %s",
                            event.space_id,
                        )
                        cert = None
                    if cert is not None:
                        payload["public_relay"]["writer_cert"] = cert
        with_release(payload)
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_POST_CREATED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_POST_CREATED broadcast failed for space=%s post=%s",
                event.space_id,
                post.id,
            )
        # v_49 — trusted member publish: hand our own post to the member
        # publisher, which publishes it to each capable connection server in
        # the BACKGROUND (post creation never waits on a GFS). The host keeps
        # relaying the post as before — older followers only read the host's
        # copy — and receivers dedupe the two by post id. The GFS copy
        # carries its own inner, with the real item type and target bound
        # inside the author signature.
        if self._member_gfs is not None and (
            "public_relay" in payload
            # v_51 — a private space's member publishes through its channel.
            or await self._member_gfs.channel_space(event.space_id)
        ):
            await self._schedule_gfs_publish(event.space_id, post)
        # Hand off the bytes-federation to SpaceMediaSyncService.
        # SPACE_POST_CREATED carries only the URL strings; without
        # the outbox-driven SPACE_MEDIA_BLOB stream the receiver's
        # ``<img src>`` 404s because the file only lives on the
        # sender's media path. Outbox enqueues happen post-broadcast
        # so the receiver always gets the post metadata first.
        if self._media_sync is not None and self._federation_repo is not None:
            media_urls: list[str] = []
            if post.media_url:
                media_urls.append(post.media_url)
            media_urls.extend(post.image_urls or ())
            if post.file_meta is not None and post.file_meta.url:
                media_urls.append(post.file_meta.url)
            if post.link_preview is not None and post.link_preview.thumbnail_url:
                media_urls.append(post.link_preview.thumbnail_url)
            if media_urls:
                try:
                    targets = await self._federation_repo.list_member_instance_ids(
                        event.space_id,
                    )
                except Exception:
                    log.exception(
                        "SPACE_MEDIA_BLOB enqueue: list peers failed for "
                        "space=%s post=%s",
                        event.space_id,
                        post.id,
                    )
                    return
                own = getattr(self._federation, "_own_instance_id", "") or ""
                targets = [t for t in targets if t and t != own]
                if targets:
                    try:
                        await self._media_sync.enqueue_for_post(
                            post_id=post.id,
                            target_instance_ids=targets,
                            media_urls=media_urls,
                            space_id=event.space_id,
                        )
                    except Exception:
                        log.exception(
                            "SPACE_MEDIA_BLOB enqueue failed for space=%s post=%s",
                            event.space_id,
                            post.id,
                        )

    async def _on_post_edited(self, event: PostEdited) -> None:
        # ``space_id`` is set by the space-edit path in space_service;
        # household-feed edits leave it ``None`` and stay local.
        if not event.space_id:
            return
        if event.origin_instance_id is not None:
            return
        # Same source-of-truth rule as ``_on_space_post_created``:
        # calendar-derived posts are edited locally on every peer by
        # the :class:`CalendarFeedBridge` consuming
        # ``CalendarEventUpdated``; the bridge bumps the post body off
        # the federated calendar event, so a parallel
        # ``SPACE_POST_UPDATED`` would race / loop.
        if event.post.linked_event_id is not None:
            return
        post = event.post
        payload = {
            "id": post.id,
            "post_id": post.id,
            "space_id": event.space_id,
            "content": post.content,
        }
        if event.actor_user_id:
            # v_42: the editor (the author, or a moderator / admin).
            payload["actor_user_id"] = event.actor_user_id
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_POST_UPDATED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_POST_UPDATED broadcast failed for space=%s post=%s",
                event.space_id,
                post.id,
            )
        # v_49 — only the author's own edit goes over the member relay; a
        # moderator's edit of someone else's post stays on the host path.
        if event.actor_user_id == post.author and await self._member_publishable(
            event.space_id
        ):
            await self._schedule_post_edit(event.space_id, post)

    async def _on_post_deleted(self, event: PostDeleted) -> None:
        if not event.space_id:
            return
        if event.origin_instance_id is not None:
            return
        payload = {
            "id": event.post_id,
            "post_id": event.post_id,
            "space_id": event.space_id,
        }
        if event.actor_user_id:
            payload["actor_user_id"] = event.actor_user_id
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_POST_DELETED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_POST_DELETED broadcast failed for space=%s post=%s",
                event.space_id,
                event.post_id,
            )
        if (
            event.actor_user_id
            and event.actor_user_id == event.author_user_id
            and await self._member_publishable(event.space_id)
        ):
            await self._schedule_item(
                event.space_id,
                event.actor_user_id,
                ITEM_TYPE_POST_DELETE,
                target=event.post_id,
                post_id=event.post_id,
            )

    # ── Comments ─────────────────────────────────────────────────────────

    async def _on_comment_added(self, event: CommentAdded) -> None:
        if not event.space_id:
            return
        if event.origin_instance_id is not None:
            return
        c = event.comment
        payload: dict = {
            "id": c.id,
            "comment_id": c.id,
            "post_id": event.post_id,
            "space_id": event.space_id,
            "author": c.author,
            "type": c.type.value,
            "content": c.content,
            "media_url": c.media_url,
            "parent_id": c.parent_id,
            "occurred_at": c.created_at.isoformat() if c.created_at else None,
        }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_COMMENT_CREATED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_COMMENT_CREATED broadcast failed for space=%s comment=%s",
                event.space_id,
                c.id,
            )
        if await self._member_publishable(event.space_id):
            await self._schedule_item(
                event.space_id,
                c.author,
                ITEM_TYPE_COMMENT,
                target=c.id,
                post_id=event.post_id,
                comment_type=c.type.value,
                parent_id=c.parent_id,
                content=c.content,
                media_url=c.media_url,
                created_at=c.created_at.isoformat() if c.created_at else None,
            )

    async def _on_comment_updated(self, event: CommentUpdated) -> None:
        if not event.space_id:
            return
        if event.origin_instance_id is not None:
            return
        c = event.comment
        payload: dict = {
            "id": c.id,
            "comment_id": c.id,
            "post_id": event.post_id,
            "space_id": event.space_id,
            "content": c.content,
        }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_COMMENT_UPDATED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_COMMENT_UPDATED broadcast failed for space=%s comment=%s",
                event.space_id,
                c.id,
            )
        # The edit carries the whole comment, so a receiver that never saw
        # the comment stores it at its newest content.
        if event.actor_user_id == c.author and await self._member_publishable(
            event.space_id
        ):
            await self._schedule_item(
                event.space_id,
                c.author,
                ITEM_TYPE_COMMENT_EDIT,
                target=c.id,
                post_id=event.post_id,
                comment_type=c.type.value,
                parent_id=c.parent_id,
                content=c.content,
                created_at=c.created_at.isoformat() if c.created_at else None,
            )

    async def _on_comment_deleted(self, event: CommentDeleted) -> None:
        if not event.space_id:
            return
        if event.origin_instance_id is not None:
            return
        payload = {
            "comment_id": event.comment_id,
            "id": event.comment_id,
            "post_id": event.post_id,
            "space_id": event.space_id,
        }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_COMMENT_DELETED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_COMMENT_DELETED broadcast failed for space=%s comment=%s",
                event.space_id,
                event.comment_id,
            )
        if (
            event.actor_user_id
            and event.actor_user_id == event.author_user_id
            and await self._member_publishable(event.space_id)
        ):
            await self._schedule_item(
                event.space_id,
                event.actor_user_id,
                ITEM_TYPE_COMMENT_DELETE,
                target=event.comment_id,
                post_id=event.post_id,
            )
