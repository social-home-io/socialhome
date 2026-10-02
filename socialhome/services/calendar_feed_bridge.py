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

The bridge writes the post itself (not through
:meth:`SpaceService.create_post`): it runs on every household, for local
and federated events alike, and mints that household's own feed card. The
card IS a post, though, so it honours the space's ``posts`` access level
(§4.3): an event is mirrored only when ``posts``
:meth:`~socialhome.domain.space.SpaceFeatures.access_decision` would let
its creator create a post right away (PROCEED) — under ``ADMIN_ONLY`` an
owner / admin, under ``MODERATED`` content authority (the mirror never
queues). Otherwise the event stays in the Calendar tab and no card is made
— silently, it is policy. The creator's role is their local seat, or the
mirrored ``space_remote_members`` seat for a remote creator; a writer
seated on the space's HOST household passes, because the host's owner is
mirrored as a plain ``member`` and the host already drops a plain member's
announcement at the source (``SpaceCalendarService.create_event``) — the
same rule as :meth:`SpaceAuthorship._seated_as`.

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
from ..domain.space import (
    WRITER_ROLES,
    AccessDecision,
    ContentAction,
    Space,
    SpaceFeatureAccess,
    SpaceRole,
)
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class CalendarFeedBridge:
    """Mirror calendar event lifecycle into the space feed."""

    __slots__ = (
        "_bus",
        "_post_repo",
        "_calendar_repo",
        "_space_repo",
        "_remote_members",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        post_repo: "AbstractSpacePostRepo",
        calendar_repo: "AbstractSpaceCalendarRepo",
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
    ) -> None:
        self._bus = bus
        self._post_repo = post_repo
        self._calendar_repo = calendar_repo
        # The space row: its ``posts`` access level, and whether this
        # household holds the space at all (the ``space_posts`` FK).
        self._space_repo = space_repo
        # A remote creator's mirrored seat (their role).
        self._remote_members = remote_member_repo

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
        space = await self._space_repo.get(space_id)
        if space is None:
            return
        if not await self._creator_may_post(space, event.created_by):
            log.debug(
                "calendar-feed-bridge: %s's posts level keeps %s's event %s "
                "out of the feed",
                space_id,
                event.created_by,
                event.id,
            )
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

    async def _creator_may_post(self, space: Space, user_id: str) -> bool:
        """Would ``space``'s ``posts`` level let ``user_id`` post right now?"""
        if space.features.access_level("posts") is SpaceFeatureAccess.OPEN:
            return True
        return (
            space.features.access_decision(
                "posts",
                role=await self._creator_role(space, user_id),
                action=ContentAction.CREATE,
                owns_target=True,
            )
            is AccessDecision.PROCEED
        )

    async def _creator_role(self, space: Space, user_id: str) -> str | None:
        """The creator's local seat, else their live mirrored remote seat —
        a writer seat on the host household counting as the host's
        settings authority (its owner is mirrored as a ``member``)."""
        member = await self._space_repo.get_member(space.id, user_id)
        if member is not None:
            return member.role
        seat = await self._remote_members.get_including_tombstones(
            space.id, "", user_id
        )
        if seat is None or seat.tombstoned:
            return None
        if seat.instance_id == space.owner_instance_id and seat.role in {
            r.value for r in WRITER_ROLES
        }:
            return SpaceRole.ADMIN.value
        return seat.role

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
        # and its own ``space_id`` is the gated scope.
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
