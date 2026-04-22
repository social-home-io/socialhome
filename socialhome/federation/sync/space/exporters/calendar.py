"""Calendar exporter — space calendar events in a wide window.

v1 scope: the repo exposes ``list_events_in_range`` only. For sync we
pass a very wide window (10 years each side) — enough for any
reasonable household calendar.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.calendar_repo import AbstractSpaceCalendarRepo


_WIDE_WINDOW = timedelta(days=3652)  # ~10 years either side


class CalendarExporter:
    resource = "calendar"

    __slots__ = ("_repo",)

    def __init__(self, space_calendar_repo: "AbstractSpaceCalendarRepo") -> None:
        self._repo = space_calendar_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        events = await self._repo.list_events_in_range(
            space_id,
            start=now - _WIDE_WINDOW,
            end=now + _WIDE_WINDOW,
        )
        return [_event_to_dict(e) for e in events]


def _event_to_dict(event) -> dict[str, Any]:
    d = asdict(event)
    for field in ("start", "end"):
        v = d.get(field)
        if v is not None and not isinstance(v, str):
            d[field] = v.isoformat()
    d["attendees"] = list(d.get("attendees") or ())
    return d
