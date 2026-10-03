"""``SPACE_SYNC_RESUME`` — long-offline catch-up (spec §4.4 / §11452).

When an instance reconnects after the 7-day outbox window, the
provider's queued events for it have expired. Spec §4.4.1 calls for
the receiver to ask each peer for the missed events via
``SPACE_SYNC_RESUME {space_id, since}``. The provider responds with a
**burst of individual federation events** — not a chunked sync.
Receivers dedup against existing rows by primary key, so re-deliveries
are harmless.

Resource types replayed today:

* ``SPACE_POST_CREATED``         — posts in the space.
* ``SPACE_COMMENT_CREATED``      — comments on those posts (joined
  via ``space_post_comments.post_id`` → ``space_posts.space_id``).
* ``SPACE_TASK_LIST_DELETED``    — the space's task lists deleted since
  ``since`` (their tombstones, migration 0069), so a household that
  missed a delete drops the list and its tasks.
* ``SPACE_TASK_LIST_CREATED``    — the space's task lists created *or
  renamed* since ``since`` (v_40), sent before the tasks filed under them.
  A held list's create applies as a rename, so a missed rename heals; a
  list unchanged since ``since`` is not re-sent, so a co-member that
  missed a rename cannot revert it with its old name.
* ``SPACE_TASK_DELETED``         — the space's tasks deleted since
  ``since`` (their tombstones, migration 0071), with the deleter as
  ``actor_user_id``, sent after the lists and before the live tasks.
* ``SPACE_TASK_CREATED``         — live task rows (tombstones excluded).
* ``SPACE_PAGE_DELETED``         — the space's pages deleted since
  ``since`` (their tombstones, migration 0073), with the deleter as
  ``actor_user_id``, sent before the live pages — to the host too.
* ``SPACE_PAGE_CREATED``         — live wiki-style pages (tombstones
  excluded).
* ``SPACE_STICKY_CREATED``       — corkboard notes.
* ``SPACE_CALENDAR_EVENT_CREATED`` — calendar events (RRULEs included).
* ``SPACE_GALLERY_ITEM_CREATED`` — gallery items, joined via
  ``gallery_items.album_id`` → ``gallery_albums.space_id``. Each
  replayed item's album goes out first as ``SPACE_GALLERY_ALBUM_CREATED``
  (v_33, once per album, never the system album): the receiver files an
  item only into an album it already holds, and an album created while it
  was offline is exactly the one it lacks.

The replay payload for every type matches what its corresponding
``federation_inbound_*`` handler reads, so the receiver applies a
re-emitted event with no special-case logic and dedups by primary key.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ....domain.federation import FederationEventType
from ....domain.federation_capabilities import FederationCapability
from ....domain.link_preview import link_preview_to_dict
from ....domain.page import page_tombstone_to_wire_dict
from ....domain.page_version import version_hash
from ....services.page_conflict_service import side_to_wire
from ....domain.task import (
    task_list_to_wire_dict,
    task_list_tombstone_to_wire_dict,
    task_to_wire_dict,
    task_tombstone_to_wire_dict,
)

if TYPE_CHECKING:
    from ....domain.calendar import CalendarEvent
    from ....domain.federation import FederationEvent
    from ....domain.gallery import GalleryAlbum, GalleryItem
    from ....domain.page import Page
    from ....domain.post import Comment, Post
    from ....domain.sticky import Sticky
    from ....repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ....repositories.gallery_repo import AbstractGalleryRepo
    from ....services.gallery_tombstones import GalleryAlbumTombstones
    from ....repositories.page_repo import AbstractPageRepo
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.sticky_repo import AbstractStickyRepo
    from ....repositories.task_repo import AbstractSpaceTaskRepo
    from ...federation_service import FederationService


log = logging.getLogger(__name__)


#: A space holds at most ``gallery_service.ALBUMS_PER_SPACE`` albums; the
#: repo caps a listing at the same 200.
MAX_ALBUMS_REPLAYED: int = 200

#: Hard cap on rows replayed per resource type per single
#: ``SPACE_SYNC_RESUME``. Receivers that need older events re-issue the
#: request with the new high-water mark. Matches the DM-history
#: equivalent so a household with many spaces doesn't burst-pin a
#: small HA instance.
MAX_PER_RESOURCE: int = 500


class SpaceSyncResumeProvider:
    """Receiver- and provider-side helper for ``SPACE_SYNC_RESUME``.

    Construct once per app and register :meth:`handle_request` for
    :data:`FederationEventType.SPACE_SYNC_RESUME`. The receiver-side
    sender is :meth:`send_request` — typically called by a reconnect
    scheduler when the federation link to a peer comes back up after
    the outbox-retention window.
    """

    __slots__ = (
        "_federation",
        "_space_repo",
        "_space_post_repo",
        "_space_task_repo",
        "_page_repo",
        "_sticky_repo",
        "_space_calendar_repo",
        "_gallery_repo",
        "_gallery_tombstones",
    )

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
        space_post_repo: "AbstractSpacePostRepo",
        space_task_repo: "AbstractSpaceTaskRepo | None" = None,
        page_repo: "AbstractPageRepo | None" = None,
        sticky_repo: "AbstractStickyRepo | None" = None,
        space_calendar_repo: "AbstractSpaceCalendarRepo | None" = None,
        gallery_repo: "AbstractGalleryRepo | None" = None,
        gallery_tombstones: "GalleryAlbumTombstones | None" = None,
    ) -> None:
        self._federation = federation_service
        self._space_repo = space_repo
        self._space_post_repo = space_post_repo
        self._space_task_repo = space_task_repo
        self._page_repo = page_repo
        self._sticky_repo = sticky_repo
        self._space_calendar_repo = space_calendar_repo
        self._gallery_repo = gallery_repo
        self._gallery_tombstones = gallery_tombstones

    # ── Outbound (requester side) ─────────────────────────────────────

    async def send_request(
        self,
        *,
        space_id: str,
        instance_id: str,
        since: str,
    ) -> None:
        """Ask ``instance_id`` to replay missed events since ``since``.

        ``since`` is an ISO-8601 timestamp — typically the receiver's
        local ``MAX(updated_at)`` for the space. Returns immediately;
        responses arrive as individual ``SPACE_*_CREATED`` events
        handled by ``federation_inbound_service``.
        """
        if not space_id or not instance_id or not since:
            return
        await self._federation.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.SPACE_SYNC_RESUME,
            payload={"space_id": space_id, "since": since},
            space_id=space_id,
        )

    # ── Inbound (provider side) ───────────────────────────────────────

    async def handle_request(self, event: "FederationEvent") -> int:
        """Replay missed events for one (space, peer) pair.

        Returns the total number of events sent across every resource
        type (0 if the peer isn't a member, the space is unknown, or
        there's nothing newer than ``since``). Membership is gated by
        ``list_member_instances`` — a peer that isn't in the space gets
        silently dropped, matching the §S-1 sync-begin guard.
        """
        payload = event.payload or {}
        space_id = str(
            event.space_id or payload.get("space_id") or "",
        )
        since = str(payload.get("since") or "")
        if not space_id or not since:
            return 0
        # Validate ISO-8601 — reject malformed input rather than letting
        # the SQL ``> ?`` comparison silently match nothing.
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            log.debug(
                "SPACE_SYNC_RESUME from %s: bad 'since' %r — dropping",
                event.from_instance,
                since,
            )
            return 0
        return await self.replay_space_to(
            space_id=space_id,
            instance_id=event.from_instance,
            since=since,
        )

    async def _is_member(self, space_id: str, instance_id: str) -> bool:
        """Membership gate — the §S-1 security boundary.

        A peer that isn't a member of ``space_id`` must never receive any
        of the space's content (posts, comments, calendar…). Both the
        ``SPACE_SYNC_RESUME`` path (:meth:`handle_request`) and the
        §319.6 resync paths (:meth:`replay_space_to` /
        :meth:`replay_calendar_to`) run this check before any
        ``_replay_*`` call so a resync request can't bypass it.
        """
        peers = await self._space_repo.list_member_instances(space_id)
        return instance_id in peers

    async def replay_space_to(
        self,
        *,
        space_id: str,
        instance_id: str,
        since: str,
    ) -> int:
        """Replay every space resource newer than ``since`` to one peer.

        Membership-gated: a non-member receives nothing (returns 0). This
        is the entry point for both ``SPACE_SYNC_RESUME`` and the §319.6
        ``space:<id>`` resync scope — the gate runs *before* any
        ``_replay_*`` call so neither path can leak space content to a
        non-member household.
        """
        if not await self._is_member(space_id, instance_id):
            return 0
        sent = 0
        sent += await self._replay_posts(space_id, since, to=instance_id)
        sent += await self._replay_comments(space_id, since, to=instance_id)
        # Lists before tasks: a task is only filed under a list held here.
        sent += await self._replay_task_list_deletes(space_id, since, to=instance_id)
        sent += await self._replay_task_lists(space_id, since, to=instance_id)
        sent += await self._replay_task_deletes(space_id, since, to=instance_id)
        sent += await self._replay_tasks(space_id, since, to=instance_id)
        sent += await self._replay_page_deletes(space_id, since, to=instance_id)
        sent += await self._replay_pages(space_id, since, to=instance_id)
        sent += await self._replay_stickies(space_id, since, to=instance_id)
        sent += await self._replay_calendar(space_id, since, to=instance_id)
        sent += await self._replay_gallery_items(space_id, since, to=instance_id)
        return sent

    async def replay_calendar_to(
        self,
        *,
        space_id: str,
        instance_id: str,
        since: str,
    ) -> int:
        """Replay only the space's calendar events newer than ``since``.

        Entry point for the §319.6 ``calendar:<id>`` resync scope.
        Membership-gated identically to :meth:`replay_space_to` — a
        non-member receives nothing (returns 0).
        """
        if not await self._is_member(space_id, instance_id):
            return 0
        return await self._replay_calendar(space_id, since, to=instance_id)

    # ── Per-resource replay ───────────────────────────────────────────

    async def _replay_posts(self, space_id: str, since: str, *, to: str) -> int:
        posts = await self._space_post_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            posts,
            FederationEventType.SPACE_POST_CREATED,
            _post_to_payload,
            space_id=space_id,
            to=to,
        )

    async def _replay_comments(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        rows = await self._space_post_repo.list_comments_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        sent = 0
        for post_id, comment in rows:
            try:
                await self._federation.send_event(
                    to_instance_id=to,
                    event_type=FederationEventType.SPACE_COMMENT_CREATED,
                    payload=_comment_to_payload(post_id, comment),
                    space_id=space_id,
                )
                sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "SPACE_SYNC_RESUME comment replay to %s failed: %s",
                    to,
                    exc,
                )
        return sent

    async def _replay_task_list_deletes(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        if self._space_task_repo is None:
            return 0
        tombstones = await self._space_task_repo.list_list_tombstones(
            space_id,
            since=since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            tombstones,
            FederationEventType.SPACE_TASK_LIST_DELETED,
            lambda tombstone: task_list_tombstone_to_wire_dict(tombstone, space_id),
            space_id=space_id,
            to=to,
        )

    async def _replay_task_lists(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        if self._space_task_repo is None:
            return 0
        lists = await self._space_task_repo.list_lists_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            lists,
            FederationEventType.SPACE_TASK_LIST_CREATED,
            lambda task_list: task_list_to_wire_dict(task_list, space_id),
            space_id=space_id,
            to=to,
        )

    async def _replay_task_deletes(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        if self._space_task_repo is None:
            return 0
        tombstones = await self._space_task_repo.list_task_tombstones(
            space_id,
            since=since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            tombstones,
            FederationEventType.SPACE_TASK_DELETED,
            lambda tombstone: task_tombstone_to_wire_dict(tombstone, space_id),
            space_id=space_id,
            to=to,
        )

    async def _replay_tasks(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        if self._space_task_repo is None:
            return 0
        tasks = await self._space_task_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            tasks,
            FederationEventType.SPACE_TASK_CREATED,
            lambda task: task_to_wire_dict(task, space_id),
            space_id=space_id,
            to=to,
        )

    async def _replay_page_deletes(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        if self._page_repo is None:
            return 0
        tombstones = await self._page_repo.list_page_tombstones(
            space_id,
            since=since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            tombstones,
            FederationEventType.SPACE_PAGE_DELETED,
            lambda tombstone: page_tombstone_to_wire_dict(tombstone, space_id),
            space_id=space_id,
            to=to,
        )

    async def _replay_pages(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        if self._page_repo is None:
            return 0
        sequenced = await self._federation.peer_supports(
            to, min_version=FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES
        )
        space = await self._space_repo.get(space_id)
        if sequenced and space is not None and space.owner_instance_id == to:
            # v_48: the host sequences the pages — a replay to it could only
            # roll it back. Our own drafts reach it as proposals (forwarder).
            return 0
        pages = await self._page_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        # v_48: each replayed page carries the host's ``seq`` (and, from the
        # host, its version hash + conflict list), so a member mirrors it by
        # sequence; a replay from anyone but the host never updates a page
        # the receiver holds. An unacknowledged local draft is never
        # replayed — the canonical version it was made from is (or nothing,
        # for an unsequenced create). An older peer gets the plain fields.
        payloads: dict[str, dict] = {}
        replayed = []
        for page in pages:
            if page.pending_base_seq is not None:
                base = await self._page_repo.get_draft_base(page.id, space_id=space_id)
                if base is None or not base.title:
                    continue
                page = replace(
                    page,
                    title=base.title,
                    content=base.content,
                    cover_image_url=base.cover_image_url,
                    seq=base.seq,
                )
            payload = _page_to_payload(page)
            # A replay says so: a v_48 host never takes it for a proposal
            # (it could roll the host back, or bring a deleted page back).
            payload["replay"] = True
            if sequenced:
                sides = await self._page_repo.list_conflict_sides(
                    page.id, space_id=space_id
                )
                payload.update(
                    seq=page.seq,
                    version_hash=version_hash(
                        page.title, page.content, page.cover_image_url
                    ),
                    last_editor_user_id=page.last_editor_user_id or page.created_by,
                    conflict=[side_to_wire(s) for s in sides],
                )
            payloads[page.id] = payload
            replayed.append(page)
        pages = replayed
        return await self._send_each(
            pages,
            FederationEventType.SPACE_PAGE_CREATED,
            lambda page: payloads[page.id],
            space_id=space_id,
            to=to,
        )

    async def _replay_stickies(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        if self._sticky_repo is None:
            return 0
        stickies = await self._sticky_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            stickies,
            FederationEventType.SPACE_STICKY_CREATED,
            _sticky_to_payload,
            space_id=space_id,
            to=to,
        )

    async def _replay_calendar(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        if self._space_calendar_repo is None:
            return 0
        events = await self._space_calendar_repo.list_events_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            events,
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            _calendar_to_payload,
            space_id=space_id,
            to=to,
        )

    async def _replay_gallery_items(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
    ) -> int:
        """Replay the space's gallery: albums first, then missed items.

        The receiver files an item only into an album it already holds for
        the space, and an album made, edited or deleted while it was away is
        state it simply lacks — whether or not anything was uploaded since.
        So, in order:

        * every album delete recorded since ``since`` as
          ``SPACE_GALLERY_ALBUM_DELETED`` (:class:`GalleryAlbumTombstones`);
        * every non-system album of the space as
          ``SPACE_GALLERY_ALBUM_CREATED`` — an idempotent no-op for one the
          receiver holds;
        * those edited since ``since`` as ``SPACE_GALLERY_ALBUM_UPDATED``;
        * the items newer than ``since`` as ``SPACE_GALLERY_ITEM_CREATED``.

        The system "Posts" album is skipped: every household rebuilds its
        own from the posts.
        """
        if self._gallery_repo is None:
            return 0
        sent = 0
        if self._gallery_tombstones is not None:
            sent += await self._send_each(
                [
                    {"id": album_id}
                    for album_id in self._gallery_tombstones.deleted_since(
                        space_id, since
                    )
                ],
                FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
                dict,
                space_id=space_id,
                to=to,
            )
        albums = [
            a
            for a in await self._gallery_repo.list_albums(
                space_id, limit=MAX_ALBUMS_REPLAYED
            )
            if a.space_id == space_id and not a.is_system
        ]
        sent += await self._send_each(
            albums,
            FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
            _gallery_album_to_payload,
            space_id=space_id,
            to=to,
        )
        sent += await self._send_each(
            [a for a in albums if _changed_since(a.updated_at, since)],
            FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
            _gallery_album_to_payload,
            space_id=space_id,
            to=to,
        )
        items = await self._gallery_repo.list_items_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return sent + await self._send_each(
            items,
            FederationEventType.SPACE_GALLERY_ITEM_CREATED,
            _gallery_item_to_payload,
            space_id=space_id,
            to=to,
        )

    async def _send_each(
        self,
        rows: list,
        event_type: FederationEventType,
        to_payload,
        *,
        space_id: str,
        to: str,
    ) -> int:
        sent = 0
        for row in rows:
            try:
                await self._federation.send_event(
                    to_instance_id=to,
                    event_type=event_type,
                    payload=to_payload(row),
                    space_id=space_id,
                )
                sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "SPACE_SYNC_RESUME %s replay to %s failed: %s",
                    event_type,
                    to,
                    exc,
                )
        return sent


# ─── Payload shapers ─────────────────────────────────────────────────────


def _post_to_payload(post: "Post") -> dict:
    payload: dict = {
        "id": post.id,
        "author": post.author,
        "type": post.type.value,
        "content": post.content,
        "media_url": post.media_url,
        "occurred_at": _iso(post.created_at),
    }
    # Location posts ride on the existing SPACE_POST_CREATED event —
    # no new event type. Coords are already 4dp-truncated by
    # feed_service / space_service before the post is persisted, so
    # the payload carries the canonical wire form.
    if post.location is not None:
        loc: dict = {"lat": post.location.lat, "lon": post.location.lon}
        if post.location.label is not None:
            loc["label"] = post.location.label
        payload["location"] = loc
    if post.link_preview is not None:
        payload["link_preview"] = link_preview_to_dict(post.link_preview)
    return payload


def _comment_to_payload(post_id: str, comment: "Comment") -> dict:
    return {
        "post_id": post_id,
        "comment_id": comment.id,
        "author": comment.author,
        "type": comment.type.value,
        "content": comment.content,
        "media_url": comment.media_url,
        "parent_id": comment.parent_id,
        "occurred_at": _iso(comment.created_at),
    }


def _page_to_payload(page: "Page") -> dict:
    return {
        "id": page.id,
        "title": page.title,
        "content": page.content,
        "created_by": page.created_by,
        "cover_image_url": page.cover_image_url,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
    }


def _sticky_to_payload(sticky: "Sticky") -> dict:
    return {
        "id": sticky.id,
        "author": sticky.author,
        "content": sticky.content,
        "color": sticky.color,
        "position_x": sticky.position_x,
        "position_y": sticky.position_y,
        "created_at": sticky.created_at,
        "updated_at": sticky.updated_at,
    }


def _calendar_to_payload(event: "CalendarEvent") -> dict:
    return {
        "id": event.id,
        "calendar_id": event.calendar_id,
        "summary": event.summary,
        "description": event.description,
        "start": _iso(event.start),
        "end": _iso(event.end),
        "all_day": event.all_day,
        "attendees": list(event.attendees),
        "created_by": event.created_by,
        "rrule": event.rrule,
        "cover_url": event.cover_url,
    }


def _gallery_album_to_payload(album: "GalleryAlbum") -> dict:
    """Same shape as the live ``GalleryFederationOutbound`` album push."""
    return album.to_federation_dict()


def _gallery_item_to_payload(item: "GalleryItem") -> dict:
    """Same shape as the live ``GalleryFederationOutbound`` item push —
    ``GalleryItem.to_federation_dict`` (thumbnail projection + full ``url``)."""
    return item.to_federation_dict()


def _iso(value) -> str:
    """ISO-format helper that tolerates ``datetime`` and ``str`` inputs."""
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _changed_since(value: str | None, since: str) -> bool:
    """``value`` is later than ``since`` — or either is unreadable, where
    re-sending an unchanged album is harmless and skipping a change is not."""
    if not value:
        return False
    try:
        a = datetime.fromisoformat(value)
        b = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return True
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return a > b
