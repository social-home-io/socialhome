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
* ``SPACE_TASK_CREATED``         — task list rows.
* ``SPACE_PAGE_CREATED``         — wiki-style pages.
* ``SPACE_STICKY_CREATED``       — corkboard notes.
* ``SPACE_CALENDAR_EVENT_CREATED`` — calendar events (RRULEs included).
* ``SPACE_GALLERY_ITEM_CREATED`` — gallery items, joined via
  ``gallery_items.album_id`` → ``gallery_albums.space_id``. Albums
  themselves still ride the chunked initial sync (§4.2.3) — they're
  structural, rare, and a per-event push would race with the album
  pre-sync.

The replay payload for every type matches what its corresponding
``federation_inbound_*`` handler reads, so the receiver applies a
re-emitted event with no special-case logic and dedups by primary key.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ....domain.federation import FederationEventType

if TYPE_CHECKING:
    from ....domain.calendar import CalendarEvent
    from ....domain.federation import FederationEvent
    from ....domain.gallery import GalleryItem
    from ....domain.page import Page
    from ....domain.post import Comment, Post
    from ....domain.sticky import Sticky
    from ....domain.task import Task
    from ....repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ....repositories.gallery_repo import AbstractGalleryRepo
    from ....repositories.page_repo import AbstractPageRepo
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.sticky_repo import AbstractStickyRepo
    from ....repositories.task_repo import AbstractSpaceTaskRepo
    from ...federation_service import FederationService


log = logging.getLogger(__name__)


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
    ) -> None:
        self._federation = federation_service
        self._space_repo = space_repo
        self._space_post_repo = space_post_repo
        self._space_task_repo = space_task_repo
        self._page_repo = page_repo
        self._sticky_repo = sticky_repo
        self._space_calendar_repo = space_calendar_repo
        self._gallery_repo = gallery_repo

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
        peers = await self._space_repo.list_member_instances(space_id)
        if event.from_instance not in peers:
            return 0

        sent = 0
        sent += await self._replay_posts(
            space_id,
            since,
            to=event.from_instance,
        )
        sent += await self._replay_comments(
            space_id,
            since,
            to=event.from_instance,
        )
        sent += await self._replay_tasks(
            space_id,
            since,
            to=event.from_instance,
        )
        sent += await self._replay_pages(
            space_id,
            since,
            to=event.from_instance,
        )
        sent += await self._replay_stickies(
            space_id,
            since,
            to=event.from_instance,
        )
        sent += await self._replay_calendar(
            space_id,
            since,
            to=event.from_instance,
        )
        sent += await self._replay_gallery_items(
            space_id,
            since,
            to=event.from_instance,
        )
        return sent

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
            _task_to_payload,
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
        pages = await self._page_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            pages,
            FederationEventType.SPACE_PAGE_CREATED,
            _page_to_payload,
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
        """Replay missed ``SPACE_GALLERY_ITEM_CREATED`` events.

        Albums themselves still ride the chunked initial sync path —
        they're rare and structural — so resume only re-emits items.
        Receivers FK back to the album row already mirrored on
        first-pair sync; an item whose album is unknown locally drops
        cleanly via the inbound handler's broad-except.
        """
        if self._gallery_repo is None:
            return 0
        items = await self._gallery_repo.list_items_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
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
    return {
        "id": post.id,
        "author": post.author,
        "type": post.type.value,
        "content": post.content,
        "media_url": post.media_url,
        "occurred_at": _iso(post.created_at),
    }


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


def _task_to_payload(task: "Task") -> dict:
    return {
        "id": task.id,
        "list_id": task.list_id,
        "title": task.title,
        "status": task.status.value,
        "position": task.position,
        "created_by": task.created_by,
        "description": task.description,
        "assignees": list(task.assignees),
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
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
    }


def _gallery_item_to_payload(item: "GalleryItem") -> dict:
    """§S-9 thumbnail-only projection — full file fetched on demand.

    Mirrors ``GalleryItem.to_thumbnail_dict`` so receivers see the
    same shape on resume replay as on the live per-event push from
    ``GalleryFederationOutbound``.
    """
    return item.to_thumbnail_dict()


def _iso(value) -> str:
    """ISO-format helper that tolerates ``datetime`` and ``str`` inputs."""
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
