"""Auto-create a feed post when a space calendar event lands (Phase B).

Subscribes to :class:`CalendarEventCreated` / :class:`CalendarEventUpdated`
/ :class:`CalendarEventDeleted` on the bus and produces a corresponding
:class:`PostType.EVENT` post in the space feed:

* **Created** — insert one ``Post(type=EVENT, linked_event_id=event.id)``
  via the post repo. Body is the event summary; the post's comment thread
  becomes the event's discussion.
* **Updated** — edit the post body if the title changed; emit
  :class:`SpacePostEdited` so feed clients re-render.
* **Deleted** — soft-delete the post (preserves comment thread for
  history). The schema's ``ON DELETE SET NULL`` on
  ``space_posts.linked_event_id`` keeps the row even when the calendar
  event is hard-deleted from a peer instance — the body becomes "(event
  removed)" via the renderer.

The bridge bypasses :meth:`SpaceService.create_post` (no moderation
queue, no per-feature access gate) — by the time a calendar event has
been persisted, the calendar's own access level has already gated the
write. Adding a second gate would block events from spaces with
``posts_access=admin_only`` that still allow calendar.

One post per *event series* — recurring events do **not** generate a new
post per occurrence (would flood the feed). The card surfaces the next
occurrence; clients RSVP to specific occurrences via the existing
``POST /api/calendars/events/{id}/rsvp`` endpoint with ``occurrence_at``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    SpacePostCreated,
)
from ..domain.post import Post, PostType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class CalendarFeedBridge:
    """Mirror calendar event lifecycle into the space feed."""

    __slots__ = ("_bus", "_post_repo", "_calendar_repo", "_space_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        post_repo: "AbstractSpacePostRepo",
        calendar_repo: "AbstractSpaceCalendarRepo",
        space_repo: "AbstractSpaceRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._post_repo = post_repo
        self._calendar_repo = calendar_repo
        # Optional — when wired, the bridge skips peer households'
        # cross-household calendar mirrors (their ``space_posts`` FK
        # would fail without a local ``spaces`` row). Tests that
        # exercise the bridge in isolation can omit it.
        self._space_repo = space_repo

    def wire(self) -> None:
        self._bus.subscribe(CalendarEventCreated, self._on_created)
        self._bus.subscribe(CalendarEventUpdated, self._on_updated)
        self._bus.subscribe(CalendarEventDeleted, self._on_deleted)

    async def _on_created(self, evt: CalendarEventCreated) -> None:
        result = await self._calendar_repo.get_event(evt.event.id)
        if result is None:
            return
        space_id, event = result
        # §23.15 — the feed mirror is opt-in. An event that wasn't flagged
        # to announce lives only in the Calendar tab; skip the post. (The
        # event still federates + renders on every member's calendar.)
        if not event.announce_in_feed:
            return
        # Cross-household calendar events arrive on peer households
        # that mirror the calendar row but don't own the parent
        # ``spaces`` row (the §D1b invitee-side mirror). The
        # ``space_posts`` FK requires a local space row, so the
        # feed-bridge has nothing useful to do for those — skip
        # rather than crash with FOREIGN KEY constraint failed.
        # The peer's calendar UI still renders the event; only the
        # feed-mirror is host-local.
        if (
            self._space_repo is not None
            and await self._space_repo.get(space_id) is None
        ):
            return
        # Idempotency guard — a peer event arriving twice (initial sync +
        # live federation) shouldn't create two posts. Keyed on the
        # linked_event_id; lookup is a single indexed read.
        existing = await self._find_existing_post(event.id)
        if existing is not None:
            return
        post = Post(
            id=uuid.uuid4().hex,
            author=event.created_by,
            type=PostType.EVENT,
            created_at=datetime.now(timezone.utc),
            content=event.summary,
            linked_event_id=event.id,
        )
        if await self._post_repo.save(space_id, post) is None:
            log.warning(
                "calendar-feed-bridge: post id %s already exists in another "
                "space — skipping the feed mirror",
                post.id,
            )
            return
        await self._bus.publish(
            SpacePostCreated(space_id=space_id, post=post),
        )

    async def _on_updated(self, evt: CalendarEventUpdated) -> None:
        post = await self._find_existing_post(evt.event.id)
        if post is None:
            return
        space_id, existing = post
        # Only republish when the user-visible body actually changed.
        new_body = evt.event.summary
        if existing.content == new_body:
            return
        # Local-only bridge: the post was located by ``linked_event_id``
        # and its own ``space_id`` is the gated scope (#693).
        await self._post_repo.edit(existing.id, new_body, space_id=space_id)

    async def _on_deleted(self, evt: CalendarEventDeleted) -> None:
        post = await self._find_existing_post(evt.event_id)
        if post is None:
            return
        space_id, existing = post
        if existing.deleted:
            return
        await self._post_repo.soft_delete(
            existing.id, space_id=space_id, moderated_by=None
        )

    async def _find_existing_post(
        self,
        event_id: str,
    ) -> tuple[str, Post] | None:
        """Locate the auto-created event post by ``linked_event_id``.

        The post repo doesn't (yet) expose a generic find-by-column
        helper, but the bridge is the only writer of
        ``linked_event_id`` so a direct query against the table is fine.
        """
        try:
            row = await self._post_repo.get_by_linked_event_id(event_id)
        except AttributeError:
            return None
        return row
