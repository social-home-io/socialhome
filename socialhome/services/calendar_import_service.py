"""Calendar import service (§5.2).

Users get three ways to populate a calendar:

1. **Upload an ``.ics``/``.vcs`` file** — parsed directly with
   :mod:`icalendar`. No AI involved; available in every deployment mode.
2. **AI from a photo** — the image bytes are inlined as a base64 data
   URL in the prompt and the configured platform ``ai_task`` is asked to
   emit a RFC 5545 ``VCALENDAR`` block, which is then parsed as in (1).
3. **AI from a free-text prompt** — same pipeline, text only.

Both AI paths go through the platform adapter's ``generate_ai_data``
method (Home Assistant routes it to ``ai_task.generate_data``; the
standalone adapter raises ``NotImplementedError``). The service is
intentionally tolerant of prose around the VCALENDAR block so a slightly
wonky agent reply still imports.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import date, datetime, time, timezone
from typing import Any

from icalendar import Calendar

from ..domain.calendar import CalendarEventCreate

log = logging.getLogger(__name__)


_VCAL_BLOCK_RE = re.compile(
    r"BEGIN:VCALENDAR.*?END:VCALENDAR",
    re.DOTALL | re.IGNORECASE,
)


# ─── Errors ──────────────────────────────────────────────────────────────


class AICalendarImportError(Exception):
    """Base class for calendar import errors."""


class AICalendarImportUnavailable(AICalendarImportError):
    """The platform adapter does not support AI data generation."""


class AICalendarImportParseError(AICalendarImportError):
    """The input (ICS file or agent reply) could not be parsed."""


# ─── Service ─────────────────────────────────────────────────────────────


class CalendarImportService:
    """Turn files / photos / prompts into draft calendar events."""

    __slots__ = ("_adapter", "_max_image_bytes", "_max_ics_bytes")

    def __init__(
        self,
        platform_adapter: Any,
        *,
        max_image_bytes: int = 8 * 1024 * 1024,
        max_ics_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._adapter = platform_adapter
        self._max_image_bytes = max_image_bytes
        self._max_ics_bytes = max_ics_bytes

    # ── Public entry points ──────────────────────────────────────────────

    async def import_ics(
        self,
        *,
        ics_bytes: bytes,
    ) -> list[CalendarEventCreate]:
        """Parse a raw ICS / VCS file. No AI involved."""
        if not ics_bytes:
            raise AICalendarImportError("ics_bytes is empty")
        if len(ics_bytes) > self._max_ics_bytes:
            raise AICalendarImportError(
                f"ICS file is too large: {len(ics_bytes)} bytes "
                f"(max {self._max_ics_bytes})"
            )
        return _parse_ics(ics_bytes)

    async def import_from_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        locale: str = "en",
        caption: str | None = None,
    ) -> list[CalendarEventCreate]:
        """Ask the AI to build a VCALENDAR from an image, then import it."""
        if not image_bytes:
            raise AICalendarImportError("image_bytes is empty")
        if len(image_bytes) > self._max_image_bytes:
            raise AICalendarImportError(
                f"image is too large: {len(image_bytes)} bytes "
                f"(max {self._max_image_bytes})"
            )

        data_url = f"data:{mime_type};base64," + base64.b64encode(image_bytes).decode(
            "ascii"
        )
        instructions = _build_image_prompt(
            locale=locale,
            caption=caption,
            data_url=data_url,
        )
        reply = await self._call_ai(
            task_name="socialhome_calendar_import_image",
            instructions=instructions,
        )
        return _parse_ai_reply(reply)

    async def import_from_prompt(
        self,
        *,
        prompt: str,
        locale: str = "en",
    ) -> list[CalendarEventCreate]:
        """Ask the AI to build a VCALENDAR from a text prompt, then import it."""
        if not prompt or not prompt.strip():
            raise AICalendarImportError("prompt is empty")

        instructions = _build_prompt_prompt(locale=locale, prompt=prompt)
        reply = await self._call_ai(
            task_name="socialhome_calendar_import_prompt",
            instructions=instructions,
        )
        return _parse_ai_reply(reply)

    # ── Internals ────────────────────────────────────────────────────────

    async def _call_ai(self, *, task_name: str, instructions: str) -> str:
        if not hasattr(self._adapter, "generate_ai_data"):
            raise AICalendarImportUnavailable(
                "Platform adapter does not implement generate_ai_data",
            )
        try:
            reply = await self._adapter.generate_ai_data(
                task_name=task_name,
                instructions=instructions,
            )
        except NotImplementedError as exc:
            raise AICalendarImportUnavailable(
                "Adapter raised NotImplementedError on generate_ai_data",
            ) from exc

        if not reply:
            raise AICalendarImportParseError("Agent returned an empty reply")
        return reply


# ─── Prompt construction ─────────────────────────────────────────────────

_RFC5545_INSTRUCTIONS = (
    "Output a single RFC 5545 VCALENDAR block containing one or more "
    "VEVENT components. Use UTC timestamps (suffix Z) or explicit "
    "VALUE=DATE for all-day events. Do not include prose, code fences, "
    "or Markdown — only the VCALENDAR text. If a detail is missing, "
    "infer a reasonable default rather than omitting a required field."
)


def _build_image_prompt(
    *,
    locale: str,
    caption: str | None,
    data_url: str,
) -> str:
    tail = f"\n\nUser caption: {caption}" if caption else ""
    return (
        "You are an event-extraction assistant. Look at the attached "
        "image (provided below as a data URL) and produce a calendar "
        "entry for every event you can see.\n\n"
        f"{_RFC5545_INSTRUCTIONS}\n\n"
        f"User locale: {locale}."
        f"{tail}\n\n"
        f"Attached image:\n{data_url}"
    )


def _build_prompt_prompt(*, locale: str, prompt: str) -> str:
    return (
        "You are an event-extraction assistant. Build calendar entries "
        "from the user's description.\n\n"
        f"{_RFC5545_INSTRUCTIONS}\n\n"
        f"User locale: {locale}.\n\n"
        f"User description:\n{prompt}"
    )


# ─── ICS parsing ─────────────────────────────────────────────────────────


def _parse_ai_reply(reply: str) -> list[CalendarEventCreate]:
    """Extract the first VCALENDAR block from ``reply`` and parse it."""
    match = _VCAL_BLOCK_RE.search(reply)
    if not match:
        raise AICalendarImportParseError(
            f"No VCALENDAR block found in agent reply: {reply[:200]!r}"
        )
    return _parse_ics(match.group(0).encode("utf-8"))


def _parse_ics(ics_bytes: bytes) -> list[CalendarEventCreate]:
    try:
        cal = Calendar.from_ical(ics_bytes)
    except (ValueError, KeyError) as exc:
        raise AICalendarImportParseError(f"Could not parse VCALENDAR: {exc}") from exc

    events: list[CalendarEventCreate] = []
    for component in cal.walk("VEVENT"):
        events.append(_vevent_to_create(component))
    if not events:
        raise AICalendarImportParseError("VCALENDAR contained no VEVENT components")
    return events


def _vevent_to_create(component: Any) -> CalendarEventCreate:
    summary = str(component.get("summary") or "").strip()
    if not summary:
        raise AICalendarImportParseError("VEVENT missing SUMMARY")

    dtstart = component.get("dtstart")
    dtend = component.get("dtend")
    if dtstart is None:
        raise AICalendarImportParseError("VEVENT missing DTSTART")

    start_val = dtstart.dt
    end_val = dtend.dt if dtend is not None else None
    all_day = isinstance(start_val, date) and not isinstance(start_val, datetime)

    start_dt = _as_datetime(start_val)
    end_dt = _as_datetime(end_val) if end_val is not None else start_dt

    description = str(component.get("description") or "").strip() or None

    rrule_field = component.get("rrule")
    rrule_str: str | None = None
    if rrule_field is not None:
        # icalendar's ``rrule`` field is a ``vRecur`` — str() renders it
        # back to the RFC 5545 form we can round-trip.
        rrule_str = str(rrule_field).strip() or None

    return CalendarEventCreate(
        summary=summary,
        start=start_dt,
        end=end_dt,
        all_day=all_day,
        description=description,
        rrule=rrule_str,
    )


def _as_datetime(value: date | datetime) -> datetime:
    """Promote a bare :class:`date` to a UTC midnight datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)
