"""Auto-create a space calendar event when a schedule poll finalises.

Spec §23.53: "once the organiser picks the winning slot, the chosen
time should land on the space calendar so members can see it alongside
other events." We subscribe to :class:`SchedulePollFinalized` and call
:class:`SpaceCalendarService.create_event` — but only if the owning
household has the ``calendar`` feature enabled. Household-scoped
polls (``space_id is None``) fall through: they're turned into a
personal calendar entry by other code paths (calendar import etc.).

The bridge is deliberately small — one handler, one clear side effect
— so the finalize code path stays readable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import SchedulePollFinalized
from ..infrastructure.event_bus import EventBus
from ..services.household_features_service import (
    FeatureDisabledError,
    HouseholdFeaturesService,
)

if TYPE_CHECKING:
    from ..services.calendar_service import SpaceCalendarService

log = logging.getLogger(__name__)


class ScheduleCalendarBridge:
    """Create a space calendar event whenever a space-scoped schedule
    poll finalises and the space has the ``calendar`` feature on."""

    __slots__ = ("_bus", "_calendar", "_features")

    def __init__(
        self,
        *,
        bus: EventBus,
        space_calendar_service: "SpaceCalendarService",
        household_features: HouseholdFeaturesService,
    ) -> None:
        self._bus = bus
        self._calendar = space_calendar_service
        self._features = household_features

    def wire(self) -> None:
        self._bus.subscribe(
            SchedulePollFinalized,
            self._on_finalized,
        )

    async def _on_finalized(self, event: SchedulePollFinalized) -> None:
        if event.space_id is None:
            return
        # Only fire if the household has calendar turned on. A
        # FeatureDisabledError bubbles up as a silent no-op — the
        # poll itself still finalises, we just don't populate the
        # calendar.
        try:
            await self._features.require_enabled("calendar")
        except FeatureDisabledError:
            log.debug(
                "schedule→calendar: calendar feature off, skipping %s",
                event.post_id,
            )
            return
        start, end = _event_window(event)
        try:
            await self._calendar.create_event(
                space_id=event.space_id,
                summary=event.title or "Scheduled event",
                start=start,
                end=end,
                created_by=event.finalized_by,
                description=f"From schedule poll {event.post_id}",
                all_day=event.start_time is None,
                attendees=(),
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "schedule→calendar: create_event failed for %s: %s",
                event.post_id,
                exc,
            )


def _event_window(event: SchedulePollFinalized) -> tuple[str, str]:
    """Compute ISO start/end for the calendar event.

    All-day slots → ``[YYYY-MM-DDT00:00, +1 day]``. Timed slots →
    explicit ``start_time`` + ``end_time`` (falls back to +60 min when
    the poll only carries a start time).
    """
    if not event.start_time:
        start_dt = datetime.fromisoformat(f"{event.slot_date}T00:00:00+00:00")
        end_dt = start_dt.replace(hour=23, minute=59)
        return start_dt.isoformat(), end_dt.isoformat()
    start_dt = datetime.fromisoformat(
        f"{event.slot_date}T{event.start_time}+00:00",
    )
    if event.end_time:
        end_dt = datetime.fromisoformat(
            f"{event.slot_date}T{event.end_time}+00:00",
        )
    else:
        end_dt = start_dt.replace(
            hour=(start_dt.hour + 1) % 24,
        )
        if end_dt <= start_dt:
            end_dt = start_dt.replace(tzinfo=timezone.utc)
    return start_dt.isoformat(), end_dt.isoformat()
