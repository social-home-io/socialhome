"""Inbound federation handlers for space-scoped content (§13).

Mirrors tasks, pages, stickies, calendar events, and poll votes from
paired peers into local repos so the UI shows a coherent space view.
Post/comment events are handled elsewhere (federation_inbound_service).

Handlers are lenient: malformed payloads log + return rather than
raise, because §24.11 has already verified the signature + replay
cache, and a peer sending a malformed body shouldn't take the inbound
pipeline down.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...domain.calendar import CalendarEvent
from ...domain.federation import FederationEventType
from ...domain.page import Page
from ...domain.sticky import Sticky
from ...domain.task import Task, TaskStatus
from ...infrastructure.event_bus import EventBus
from ...utils.datetime import parse_iso8601_lenient, parse_iso8601_optional

if TYPE_CHECKING:
    from ...domain.federation import FederationEvent
    from ...federation.federation_service import FederationService
    from ...repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ...repositories.page_repo import AbstractPageRepo
    from ...repositories.poll_repo import AbstractPollRepo
    from ...repositories.sticky_repo import AbstractStickyRepo
    from ...repositories.task_repo import AbstractSpaceTaskRepo

log = logging.getLogger(__name__)


class SpaceContentInboundHandlers:
    """Register space-content inbound handlers."""

    __slots__ = (
        "_bus",
        "_page_repo",
        "_sticky_repo",
        "_task_repo",
        "_calendar_repo",
        "_poll_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        page_repo: "AbstractPageRepo",
        sticky_repo: "AbstractStickyRepo",
        task_repo: "AbstractSpaceTaskRepo",
        calendar_repo: "AbstractSpaceCalendarRepo",
        poll_repo: "AbstractPollRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._page_repo = page_repo
        self._sticky_repo = sticky_repo
        self._task_repo = task_repo
        self._calendar_repo = calendar_repo
        self._poll_repo = poll_repo

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry

        # Tasks
        registry.register(FederationEventType.SPACE_TASK_CREATED, self._on_task_saved)
        registry.register(FederationEventType.SPACE_TASK_UPDATED, self._on_task_saved)
        registry.register(FederationEventType.SPACE_TASK_DELETED, self._on_task_deleted)

        # Pages
        registry.register(FederationEventType.SPACE_PAGE_CREATED, self._on_page_saved)
        registry.register(FederationEventType.SPACE_PAGE_UPDATED, self._on_page_saved)
        registry.register(FederationEventType.SPACE_PAGE_DELETED, self._on_page_deleted)

        # Stickies
        registry.register(
            FederationEventType.SPACE_STICKY_CREATED, self._on_sticky_saved
        )
        registry.register(
            FederationEventType.SPACE_STICKY_UPDATED, self._on_sticky_saved
        )
        registry.register(
            FederationEventType.SPACE_STICKY_DELETED, self._on_sticky_deleted
        )

        # Calendar events
        registry.register(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            self._on_calendar_saved,
        )
        registry.register(
            FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
            self._on_calendar_saved,
        )
        registry.register(
            FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
            self._on_calendar_deleted,
        )

        # Polls — only registered when a poll_repo is attached (deployments
        # without polls skip it entirely, classical behaviour).
        if self._poll_repo is not None:
            registry.register(
                FederationEventType.SPACE_POLL_CREATED,
                self._on_poll_created,
            )
            registry.register(
                FederationEventType.SPACE_POLL_VOTE_CAST,
                self._on_poll_vote,
            )
            registry.register(
                FederationEventType.SPACE_POLL_CLOSED,
                self._on_poll_closed,
            )
            # Schedule polls piggy-back on the poll repo — the
            # response / finalized rows live in the same SQLite module.
            registry.register(
                FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED,
                self._on_schedule_response_updated,
            )
            registry.register(
                FederationEventType.SPACE_SCHEDULE_FINALIZED,
                self._on_schedule_finalized,
            )

    # ─── Tasks ───────────────────────────────────────────────────────────

    async def _on_task_saved(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        p = event.payload
        task_id = str(p.get("id") or p.get("task_id") or "")
        list_id = str(p.get("list_id") or "")
        title = str(p.get("title") or "")
        if not space_id or not task_id or not list_id or not title:
            log.debug("SPACE_TASK_* missing required field")
            return
        try:
            status = TaskStatus(str(p.get("status") or "todo"))
        except ValueError:
            status = TaskStatus.TODO
        assignees = p.get("assignees") or ()
        task = Task(
            id=task_id,
            list_id=list_id,
            title=title,
            status=status,
            position=int(p.get("position") or 0),
            created_by=str(p.get("created_by") or ""),
            created_at=parse_iso8601_lenient(p.get("created_at")),
            updated_at=parse_iso8601_lenient(
                p.get("updated_at") or p.get("occurred_at")
            ),
            description=p.get("description"),
            due_date=None,  # due_date is a ``date`` — parsing lives in the service
            assignees=tuple(str(a) for a in assignees),
        )
        await self._task_repo.save(space_id, task)

    async def _on_task_deleted(self, event: "FederationEvent") -> None:
        task_id = str(event.payload.get("id") or event.payload.get("task_id") or "")
        if not task_id:
            return
        await self._task_repo.delete(task_id)

    # ─── Pages ───────────────────────────────────────────────────────────

    async def _on_page_saved(self, event: "FederationEvent") -> None:
        """Mirror a remote page into the local ``space_pages`` table.

        Page timestamps are ISO strings (matches the domain type —
        `Page.created_at`/`updated_at` are `str`).
        """
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        p = event.payload
        page_id = str(p.get("id") or p.get("page_id") or "")
        title = str(p.get("title") or "")
        if not page_id or not title:
            log.debug("SPACE_PAGE_* missing required field")
            return
        page = Page(
            id=page_id,
            title=title,
            content=str(p.get("content") or ""),
            created_by=str(p.get("created_by") or ""),
            created_at=str(p.get("created_at") or p.get("occurred_at") or ""),
            updated_at=str(p.get("updated_at") or p.get("occurred_at") or ""),
            space_id=space_id or None,
            cover_image_url=p.get("cover_image_url"),
        )
        await self._page_repo.save(page)

    async def _on_page_deleted(self, event: "FederationEvent") -> None:
        page_id = str(event.payload.get("id") or event.payload.get("page_id") or "")
        if not page_id:
            return
        await self._page_repo.delete(page_id)

    # ─── Stickies ────────────────────────────────────────────────────────

    async def _on_sticky_saved(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        p = event.payload
        sticky_id = str(p.get("id") or p.get("sticky_id") or "")
        author = str(p.get("author") or p.get("created_by") or "")
        content = str(p.get("content") or p.get("text") or "")
        if not sticky_id or not author or not content:
            log.debug("SPACE_STICKY_* missing required field")
            return
        now_iso = str(
            p.get("updated_at") or p.get("created_at") or p.get("occurred_at") or "",
        )
        sticky = Sticky(
            id=sticky_id,
            author=author,
            content=content,
            color=str(p.get("color") or p.get("colour") or "yellow"),
            position_x=float(p.get("position_x") or 0.0),
            position_y=float(p.get("position_y") or 0.0),
            created_at=str(p.get("created_at") or p.get("occurred_at") or ""),
            updated_at=now_iso,
            space_id=space_id or None,
        )
        await self._sticky_repo.save(sticky)

    async def _on_sticky_deleted(self, event: "FederationEvent") -> None:
        sticky_id = str(event.payload.get("id") or event.payload.get("sticky_id") or "")
        if not sticky_id:
            return
        await self._sticky_repo.delete(sticky_id)

    # ─── Calendar events ─────────────────────────────────────────────────

    async def _on_calendar_saved(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        p = event.payload
        event_id = str(p.get("id") or p.get("event_id") or "")
        calendar_id = str(p.get("calendar_id") or "")
        summary = str(p.get("summary") or p.get("title") or "")
        created_by = str(p.get("created_by") or "")
        start = parse_iso8601_optional(p.get("start"))
        end = parse_iso8601_optional(p.get("end"))
        if (
            not space_id
            or not event_id
            or not calendar_id
            or not summary
            or not created_by
            or start is None
            or end is None
        ):
            log.debug("SPACE_CALENDAR_EVENT_* missing required field")
            return
        ev = CalendarEvent(
            id=event_id,
            calendar_id=calendar_id,
            summary=summary,
            start=start,
            end=end,
            created_by=created_by,
            description=p.get("description"),
            all_day=bool(p.get("all_day", False)),
            attendees=tuple(str(a) for a in (p.get("attendees") or ())),
            mirrored_from=p.get("mirrored_from"),
        )
        await self._calendar_repo.save_event(space_id, ev)

    async def _on_calendar_deleted(self, event: "FederationEvent") -> None:
        event_id = str(event.payload.get("id") or event.payload.get("event_id") or "")
        if not event_id:
            return
        await self._calendar_repo.delete_event(event_id)

    # ─── Polls ──────────────────────────────────────────────────────────

    async def _on_poll_created(self, event: "FederationEvent") -> None:
        """Poll creation is already carried inline on ``SPACE_POST_CREATED``
        (posts with ``type=poll`` have a ``poll`` field). The dedicated
        ``SPACE_POLL_CREATED`` event is a signal hook for subscribers —
        we log + publish for future UI wiring, no persistence side-effect.
        """
        post_id = str(event.payload.get("post_id") or event.payload.get("id") or "")
        if not post_id:
            return
        log.debug(
            "SPACE_POLL_CREATED post=%s space=%s from=%s",
            post_id,
            event.space_id,
            event.from_instance,
        )

    async def _on_poll_vote(self, event: "FederationEvent") -> None:
        """Mirror a remote poll vote. Enforces the single-choice invariant
        (the old vote is cleared first) matching the local poll service."""
        if self._poll_repo is None:
            return
        p = event.payload
        post_id = str(p.get("post_id") or "")
        option_id = str(p.get("option_id") or "")
        voter = str(p.get("voter_user_id") or p.get("user_id") or "")
        if not post_id or not option_id or not voter:
            log.debug("SPACE_POLL_VOTE_CAST missing required field")
            return
        # Guard against posting a vote for an option that doesn't
        # actually belong to this post on our side — would corrupt
        # the tally.
        belongs = await self._poll_repo.option_belongs_to_post(
            option_id=option_id,
            post_id=post_id,
        )
        if not belongs:
            log.debug(
                "SPACE_POLL_VOTE_CAST option %s not in post %s",
                option_id,
                post_id,
            )
            return
        await self._poll_repo.clear_user_votes(
            post_id=post_id,
            voter_user_id=voter,
        )
        await self._poll_repo.insert_vote(
            option_id=option_id,
            voter_user_id=voter,
        )

    async def _on_poll_closed(self, event: "FederationEvent") -> None:
        if self._poll_repo is None:
            return
        post_id = str(event.payload.get("post_id") or "")
        if not post_id:
            return
        await self._poll_repo.close(post_id)

    async def _on_schedule_response_updated(
        self,
        event: "FederationEvent",
    ) -> None:
        """Mirror a peer's schedule-poll vote / retraction locally."""
        if self._poll_repo is None:
            return
        p = event.payload
        slot_id = str(p.get("slot_id") or "")
        user_id = str(p.get("user_id") or "")
        response = str(p.get("response") or "")
        if not slot_id or not user_id:
            log.debug("SPACE_SCHEDULE_RESPONSE_UPDATED missing field")
            return
        if response == "retracted" or not response:
            await self._poll_repo.delete_schedule_response(
                slot_id=slot_id,
                user_id=user_id,
            )
        else:
            await self._poll_repo.upsert_schedule_response(
                slot_id=slot_id,
                user_id=user_id,
                response=response,
            )

    async def _on_schedule_finalized(
        self,
        event: "FederationEvent",
    ) -> None:
        """Mirror a peer's schedule-poll finalisation. The matching
        local calendar entry is produced by
        :class:`ScheduleCalendarBridge` if the household enables it.
        """
        if self._poll_repo is None:
            return
        post_id = str(event.payload.get("post_id") or "")
        slot_id = str(event.payload.get("slot_id") or "")
        if not post_id or not slot_id:
            return
        await self._poll_repo.finalize_schedule_poll(
            post_id=post_id,
            slot_id=slot_id,
        )
