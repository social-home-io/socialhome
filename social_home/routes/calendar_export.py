"""iCal export route — GET /api/calendar/{calendar_id}/export.ics (§5.2)."""

from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web

from ..app_keys import calendar_service_key
from .base import BaseView


def _ical_dt(dt: datetime) -> str:
    """Format a datetime as iCalendar YYYYMMDDTHHMMSSZ (UTC)."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def _ical_escape(text: str) -> str:
    """Escape special characters per RFC 5545 §3.3.11."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


class CalendarExportView(BaseView):
    """``GET /api/calendar/{calendar_id}/export.ics`` — iCal export."""

    async def get(self) -> web.Response:
        self.user  # auth check
        calendar_id = self.match("calendar_id")
        svc = self.svc(calendar_service_key)

        # Use a very wide range to fetch all events for export.
        far_past = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
        far_future = datetime(2099, 12, 31, tzinfo=timezone.utc).isoformat()
        events = await svc.list_events_in_range(
            calendar_id,
            start=far_past,
            end=far_future,
        )

        lines: list[str] = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Social Home//EN",
        ]
        for ev in events:
            lines.append("BEGIN:VEVENT")
            lines.append(f"UID:{ev.id}@social-home")
            if ev.start:
                lines.append(f"DTSTART:{_ical_dt(ev.start)}")
            if ev.end:
                lines.append(f"DTEND:{_ical_dt(ev.end)}")
            lines.append(f"SUMMARY:{_ical_escape(ev.summary)}")
            lines.append(f"DESCRIPTION:{_ical_escape(ev.description or '')}")
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")

        body = "\r\n".join(lines) + "\r\n"
        return web.Response(
            text=body,
            content_type="text/calendar",
            charset="utf-8",
        )
