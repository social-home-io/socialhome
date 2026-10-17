"""Mirror "going" space-event RSVPs onto the user's personal calendar.

When a user accepts an RSVP on a space calendar event ("going"), the
event lands on their own personal calendar — alongside household
events — without flipping back to the space surface to see what's on
their plate. The mirror tracks the source event: edits flow through,
the source being deleted drops the mirror, and changing the RSVP back
to maybe / declined / waitlist removes the mirror too.

Recurring events are mirrored as the seed row (rrule preserved) — v1
limitation: per-occurrence partial attendance isn't reflected, the
personal-calendar list expansion shows the whole series. Acceptable
trade-off: most household members RSVP "going" to the whole series
once, not per-occurrence.

Decoupled via the event bus: :class:`SpaceCalendarService` publishes
:class:`SpaceRsvpChanged` / :class:`CalendarEventUpdated` /
:class:`CalendarEventDeleted` and this bridge reacts. Keeps the
calendar service unaware of the personal-mirror feature — the mirror
is an additive surface, not a baked-in part of the RSVP flow.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from ..domain.calendar import CalendarEvent
from ..domain.events import (
    CalendarEventDeleted,
    CalendarEventUpdated,
    SpaceRsvpChanged,
)
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..repositories.calendar_repo import (
        AbstractCalendarRepo,
        AbstractSpaceCalendarRepo,
    )
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

# Statuses that warrant a personal-calendar mirror. "Going" is the
# obvious case; "requested" is included so a member's pending request
# on a capped event also lands on their calendar (otherwise they'd
# look at their personal calendar, see nothing, and assume the request
# vanished). Approval flips it to going (no mirror change); denial
# fires SpaceRsvpChanged with status removed → mirror drops.
_MIRRORED_STATUSES: frozenset[str] = frozenset({"going", "requested"})


class SpaceRsvpMirrorBridge:
    """Personal-calendar mirror for space event RSVPs.

    Idempotent on every event — a redelivered RSVP change collapses
    onto the same deterministic row id, never duplicates.
    """

    __slots__ = (
        "_bus",
        "_calendar_repo",
        "_space_calendar_repo",
        "_user_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        calendar_repo: AbstractCalendarRepo,
        space_calendar_repo: AbstractSpaceCalendarRepo,
        user_repo: AbstractUserRepo,
    ) -> None:
        self._bus = bus
        self._calendar_repo = calendar_repo
        self._space_calendar_repo = space_calendar_repo
        self._user_repo = user_repo

    def wire(self) -> None:
        """Subscribe to the bus. Idempotent — the bus de-duplicates
        subscribers internally so calling twice is safe."""
        self._bus.subscribe(SpaceRsvpChanged, self._on_rsvp_changed)
        self._bus.subscribe(CalendarEventUpdated, self._on_event_updated)
        self._bus.subscribe(CalendarEventDeleted, self._on_event_deleted)

    # ─── Subscribers ────────────────────────────────────────────────────

    async def _on_rsvp_changed(self, event: SpaceRsvpChanged) -> None:
        if event.status in _MIRRORED_STATUSES:
            await self._upsert_mirror(
                user_id=event.user_id,
                source_event_id=event.event_id,
            )
        else:
            await self._delete_mirror(
                user_id=event.user_id,
                source_event_id=event.event_id,
            )

    async def _on_event_updated(self, event: CalendarEventUpdated) -> None:
        """When a space event changes, refresh every mirror of it.

        Personal events firing this same event have no mirrors pointing
        at them (mirrors are only created for space events), so the
        ``list_mirrors_of`` lookup returns empty for those — no
        recursion, no extra writes.
        """
        mirrors = await self._calendar_repo.list_mirrors_of(event.event.id)
        if not mirrors:
            return
        for mirror in mirrors:
            refreshed = replace(
                mirror,
                summary=event.event.summary,
                start=event.event.start,
                end=event.event.end,
                description=event.event.description,
                all_day=event.event.all_day,
                rrule=event.event.rrule,
                cover_url=event.event.cover_url,
            )
            await self._calendar_repo.save_event(refreshed)

    async def _on_event_deleted(self, event: CalendarEventDeleted) -> None:
        mirrors = await self._calendar_repo.list_mirrors_of(event.event_id)
        for mirror in mirrors:
            await self._calendar_repo.delete_event(mirror.id)

    # ─── Mirror ops ─────────────────────────────────────────────────────

    async def _upsert_mirror(
        self,
        *,
        user_id: str,
        source_event_id: str,
    ) -> None:
        """Create or refresh the user's mirror of ``source_event_id``.

        No-op if the source event is gone, the user has no personal
        calendar yet, or the user isn't a local household member
        (remote users can RSVP via federation but their mirrors live on
        their own instance — we only mirror onto the local user's
        calendar)."""
        result = await self._space_calendar_repo.get_event(source_event_id)
        if result is None:
            return
        space_id, source = result
        local_user = await self._user_repo.get_by_user_id(user_id)
        if local_user is None:
            # Remote user RSVPing on our hosted space — their mirror
            # would land on the wrong instance. Skip; their own
            # instance handles the mirror locally.
            return
        cals = await self._calendar_repo.list_calendars_for_user(
            local_user.username,
        )
        if not cals:
            log.info(
                "space-rsvp mirror dropped — %s has no personal calendar",
                local_user.username,
            )
            return
        mirror_id = _mint_mirror_id(user_id, source_event_id)
        existing = await self._calendar_repo.get_event(mirror_id)
        mirror = CalendarEvent(
            id=mirror_id,
            calendar_id=existing.calendar_id if existing else cals[0].id,
            summary=source.summary,
            start=source.start,
            end=source.end,
            created_by=user_id,
            description=source.description,
            all_day=source.all_day,
            attendees=(),
            mirrored_from=source.id,
            rrule=source.rrule,
            rsvp_enabled=False,
            cover_url=source.cover_url,
            origin="local",
        )
        await self._calendar_repo.save_event(mirror)
        log.debug(
            "space-rsvp mirror saved: user=%s source=%s space=%s",
            user_id,
            source_event_id,
            space_id,
        )

    async def _delete_mirror(
        self,
        *,
        user_id: str,
        source_event_id: str,
    ) -> None:
        mirror_id = _mint_mirror_id(user_id, source_event_id)
        existing = await self._calendar_repo.get_event(mirror_id)
        if existing is None:
            return
        await self._calendar_repo.delete_event(mirror_id)


def _mint_mirror_id(user_id: str, source_event_id: str) -> str:
    """Deterministic id for a (user, source) mirror.

    Same RSVP redelivered, same id — collapses onto the existing row.
    Two components keep it unique across users sharing the same source.
    """
    return f"sm_{user_id[:16]}_{source_event_id[:24]}"
