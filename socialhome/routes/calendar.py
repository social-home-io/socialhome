"""Calendar routes — /api/calendars/* (§5.2)."""

from __future__ import annotations

import base64

from aiohttp import web

from .. import app_keys as K
from ..app_keys import calendar_service_key
from ..security import error_response
from ..services.calendar_import_service import (
    AICalendarImportError,
    AICalendarImportUnavailable,
)
from .base import BaseView


def _cal_dict(cal) -> dict:
    return {
        "id": cal.id,
        "name": cal.name,
        "color": cal.color,
        "owner_username": cal.owner_username,
        "calendar_type": cal.calendar_type,
    }


def _event_dict(event) -> dict:
    return {
        "id": event.id,
        "calendar_id": event.calendar_id,
        "summary": event.summary,
        "start": event.start.isoformat() if event.start else None,
        "end": event.end.isoformat() if event.end else None,
        "all_day": event.all_day,
        "description": event.description,
        "attendees": list(event.attendees),
        "created_by": event.created_by,
        "rrule": event.rrule,
        "capacity": getattr(event, "capacity", None),
    }


class CalendarCollectionView(BaseView):
    """``GET /api/calendars`` — list calendars.

    ``POST /api/calendars`` — create a calendar.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(calendar_service_key)
        cals = await svc.list_calendars(ctx.username)
        return web.json_response([_cal_dict(c) for c in cals])

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        svc = self.svc(calendar_service_key)
        cal = await svc.create_calendar(
            name=body.get("name", ""),
            owner_username=ctx.username,
            color=body.get("color"),
        )
        return web.json_response(_cal_dict(cal), status=201)


class CalendarEventsView(BaseView):
    """``GET /api/calendars/{id}/events`` — list events in a calendar.

    ``POST /api/calendars/{id}/events`` — create an event in a calendar.
    """

    async def get(self) -> web.Response:
        self.user  # auth check
        calendar_id = self.match("id")
        start = self.request.query.get("start")
        end = self.request.query.get("end")
        if not start or not end:
            return error_response(
                422, "UNPROCESSABLE", "Query params 'start' and 'end' are required."
            )
        svc = self.svc(calendar_service_key)
        events = await svc.list_events_in_range(calendar_id, start=start, end=end)
        return web.json_response([_event_dict(e) for e in events])

    async def post(self) -> web.Response:
        ctx = self.user
        calendar_id = self.match("id")
        body = await self.body()
        svc = self.svc(calendar_service_key)
        event = await svc.create_event(
            calendar_id=calendar_id,
            summary=body.get("summary", ""),
            start=body.get("start", ""),
            end=body.get("end", ""),
            created_by=ctx.user_id,
            all_day=bool(body.get("all_day", False)),
            description=body.get("description"),
            attendees=body.get("attendees"),
            rrule=body.get("rrule"),
        )
        return web.json_response(_event_dict(event), status=201)


class CalendarEventDeleteView(BaseView):
    """``PATCH /api/calendars/events/{id}`` — edit;
    ``DELETE /api/calendars/events/{id}`` — delete an event."""

    async def delete(self) -> web.Response:
        self.user  # auth check
        event_id = self.match("id")
        svc = self.svc(calendar_service_key)
        await svc.delete_event(event_id)
        return web.json_response({"ok": True})

    async def patch(self) -> web.Response:
        self.user
        event_id = self.match("id")
        body = await self.body()
        svc = self.svc(calendar_service_key)
        event = await svc.update_event(
            event_id,
            summary=body.get("summary"),
            start=body.get("start"),
            end=body.get("end"),
            all_day=body.get("all_day"),
            description=body.get("description"),
            attendees=body.get("attendees"),
            rrule=body.get("rrule"),
        )
        return web.json_response(_event_dict(event))


async def _persist_imported_events(view, calendar_id, created_by, events):
    svc = view.svc(calendar_service_key)
    persisted = []
    for ev in events:
        persisted.append(
            await svc.create_event(
                calendar_id=calendar_id,
                summary=ev.summary,
                start=ev.start.isoformat(),
                end=ev.end.isoformat(),
                created_by=created_by,
                all_day=ev.all_day,
                description=ev.description,
                rrule=ev.rrule,
            )
        )
    return web.json_response(
        {"events": [_event_dict(e) for e in persisted]},
        status=201,
    )


class CalendarImportIcsView(BaseView):
    """``POST /api/calendars/{id}/import_ics`` — import events from an ICS file.

    Accepts raw ``text/calendar`` bytes OR a JSON body ``{"ics": "..."}``.
    Does not require AI support.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        calendar_id = self.match("id")
        import_svc = self.request.app[K.calendar_import_service_key]

        content_type = self.request.headers.get("Content-Type", "")
        if "application/json" in content_type:
            payload = await self.body()
            ics_text = str(payload.get("ics") or "")
            if not ics_text:
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    "Missing 'ics' in JSON body",
                )
            ics_bytes = ics_text.encode("utf-8")
        else:
            ics_bytes = await self.request.read()

        try:
            events = await import_svc.import_ics(ics_bytes=ics_bytes)
        except AICalendarImportError as exc:
            return error_response(422, "ICS_PARSE_ERROR", str(exc))

        return await _persist_imported_events(
            self,
            calendar_id,
            ctx.user_id,
            events,
        )


class CalendarImportImageView(BaseView):
    """``POST /api/calendars/{id}/import_image`` — AI-generate events from a photo.

    Accepts raw ``image/*`` bytes OR JSON ``{"image_data_url", "caption"}``.
    Returns 503 when the adapter has no AI backend.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        calendar_id = self.match("id")
        import_svc = self.request.app[K.calendar_import_service_key]

        content_type = self.request.headers.get("Content-Type", "")
        caption: str | None = self.request.query.get("caption")
        if content_type.startswith("image/"):
            body = await self.request.read()
            mime = content_type
        else:
            payload = await self.body()
            data_url: str = str(payload.get("image_data_url") or "")
            caption = caption or payload.get("caption")
            if not data_url.startswith("data:"):
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    "image_data_url must start with 'data:'",
                )
            try:
                header, b64 = data_url.split(",", 1)
                mime = header.split(":", 1)[1].split(";", 1)[0]
                body = base64.b64decode(b64)
            except Exception as exc:
                return error_response(
                    422,
                    "UNPROCESSABLE",
                    f"Could not decode image_data_url: {exc}",
                )

        try:
            events = await import_svc.import_from_image(
                image_bytes=body,
                mime_type=mime or "image/jpeg",
                locale=getattr(ctx, "metadata", {}).get("locale") or "en",
                caption=caption,
            )
        except AICalendarImportUnavailable as exc:
            return error_response(503, "AI_AGENT_UNAVAILABLE", str(exc))
        except AICalendarImportError as exc:
            return error_response(422, "AI_PARSE_ERROR", str(exc))

        return await _persist_imported_events(
            self,
            calendar_id,
            ctx.user_id,
            events,
        )


class CalendarImportPromptView(BaseView):
    """``POST /api/calendars/{id}/import_prompt`` — AI-generate events from text.

    Body is JSON ``{"prompt": "..."}``. Returns 503 when the adapter has no
    AI backend.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        calendar_id = self.match("id")
        import_svc = self.request.app[K.calendar_import_service_key]

        payload = await self.body()
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return error_response(
                422,
                "UNPROCESSABLE",
                "Missing 'prompt' in JSON body",
            )

        try:
            events = await import_svc.import_from_prompt(
                prompt=prompt,
                locale=getattr(ctx, "metadata", {}).get("locale") or "en",
            )
        except AICalendarImportUnavailable as exc:
            return error_response(503, "AI_AGENT_UNAVAILABLE", str(exc))
        except AICalendarImportError as exc:
            return error_response(422, "AI_PARSE_ERROR", str(exc))

        return await _persist_imported_events(
            self,
            calendar_id,
            ctx.user_id,
            events,
        )


# ── Space-scoped calendar + RSVPs (§23.7) ─────────────────────────────


class SpaceCalendarEventsView(BaseView):
    """``GET /api/spaces/{id}/calendar/events`` — list events in a space calendar.

    ``POST /api/spaces/{id}/calendar/events`` — create an event in a space calendar.
    """

    async def get(self) -> web.Response:
        self.user  # auth check
        space_id = self.match("id")
        start = self.request.query.get("start")
        end = self.request.query.get("end")
        if not start or not end:
            return error_response(
                422,
                "UNPROCESSABLE",
                "Query params 'start' and 'end' are required.",
            )
        space_cal_svc = self.svc(K.space_cal_service_key)
        events = await space_cal_svc.list_events_in_range(
            space_id,
            start=start,
            end=end,
        )
        return web.json_response([_event_dict(e) for e in events])

    async def _require_member(self, space_id: str, user_id: str) -> bool:
        space_repo = self.svc(K.space_repo_key)
        return await space_repo.get_member(space_id, user_id) is not None

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        space_cal_svc = self.svc(K.space_cal_service_key)
        try:
            event = await space_cal_svc.create_event(
                space_id=space_id,
                summary=str(body.get("summary") or body.get("title") or ""),
                start=str(body.get("start") or body.get("start_at") or ""),
                end=str(body.get("end") or body.get("end_at") or ""),
                created_by=ctx.user_id,
                description=body.get("description"),
                all_day=bool(body.get("all_day", False)),
                attendees=tuple(body.get("attendees") or ()),
                rrule=body.get("rrule"),
                capacity=body.get("capacity"),
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        # Service publishes CalendarEventCreated internally — don't
        # double-publish (previous route called bus.publish again,
        # double-firing HA bridge + WS).
        return web.json_response(_event_dict(event), status=201)


class SpaceCalendarEventDetailView(BaseView):
    """``PATCH`` / ``DELETE /api/spaces/{id}/calendar/events/{eid}``."""

    async def _require_member(self, space_id: str, user_id: str) -> bool:
        space_repo = self.svc(K.space_repo_key)
        return await space_repo.get_member(space_id, user_id) is not None

    async def patch(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        event_id = self.match("eid")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        space_cal_svc = self.svc(K.space_cal_service_key)
        body = await self.body()
        try:
            event = await space_cal_svc.update_event(
                event_id,
                summary=body.get("summary") or body.get("title"),
                start=body.get("start") or body.get("start_at"),
                end=body.get("end") or body.get("end_at"),
                all_day=body.get("all_day"),
                description=body.get("description"),
                attendees=(
                    tuple(body["attendees"])
                    if body.get("attendees") is not None
                    else None
                ),
                rrule=body.get("rrule"),
                capacity=body.get("capacity"),
                clear_capacity=bool(body.get("clear_capacity", False)),
            )
        except KeyError:
            return error_response(404, "NOT_FOUND", "Event not found.")
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(_event_dict(event))

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        event_id = self.match("eid")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        space_cal_svc = self.svc(K.space_cal_service_key)
        await space_cal_svc.delete_event(event_id)
        return web.json_response({"ok": True})


# Back-compat alias — the old name is still used in some tests/imports.
SpaceCalendarEventDeleteView = SpaceCalendarEventDetailView


class CalendarEventRsvpView(BaseView):
    """``POST`` / ``DELETE /api/calendars/events/{id}/rsvp`` — RSVP to / clear-RSVP from an event.

    Space-scoped: the event's ``calendar_id`` doubles as the owning
    ``space_id`` (see :class:`SpaceCalendarService.create_event`). We
    reject non-members so a user who only knows the event id can't
    vote on a private space, and scope the fan-out to space members
    so RSVP counts don't leak to unrelated households.

    For recurring events, ``occurrence_at`` is required (body field on
    POST, query string on DELETE) and must match a real occurrence
    under the event's RRULE. For non-recurring events it may be
    omitted; the service defaults to ``event.start``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        event_id = self.match("id")
        body = await self.body()
        status = str(body.get("status") or "")
        occurrence_at = body.get("occurrence_at")
        space_cal_svc = self.svc(K.space_cal_service_key)

        space_id = await _resolve_space_id_for_event(self, event_id)
        if space_id is None:
            return error_response(404, "NOT_FOUND", "Event not found.")
        space_repo = self.svc(K.space_repo_key)
        member = await space_repo.get_member(space_id, ctx.user_id)
        if member is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")

        try:
            await space_cal_svc.rsvp(
                event_id=event_id,
                user_id=ctx.user_id,
                status=status,
                occurrence_at=occurrence_at,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        except KeyError:
            return error_response(404, "NOT_FOUND", "Event not found.")

        return await _broadcast_rsvp_counts(
            self,
            event_id=event_id,
            space_id=space_id,
            occurrence_at=occurrence_at,
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        event_id = self.match("id")
        occurrence_at = self.request.query.get("occurrence_at")
        space_cal_svc = self.svc(K.space_cal_service_key)

        space_id = await _resolve_space_id_for_event(self, event_id)
        if space_id is None:
            return error_response(404, "NOT_FOUND", "Event not found.")
        space_repo = self.svc(K.space_repo_key)
        member = await space_repo.get_member(space_id, ctx.user_id)
        if member is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")

        try:
            await space_cal_svc.remove_rsvp(
                event_id=event_id,
                user_id=ctx.user_id,
                occurrence_at=occurrence_at,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))

        return await _broadcast_rsvp_counts(
            self,
            event_id=event_id,
            space_id=space_id,
            occurrence_at=occurrence_at,
        )


async def _broadcast_rsvp_counts(
    view: BaseView,
    *,
    event_id: str,
    space_id: str,
    occurrence_at: str | None,
    extra: dict | None = None,
) -> web.Response:
    """Aggregate RSVP counts (per-occurrence when given), broadcast a
    ``calendar.rsvp_updated`` WS frame, return the JSON response."""
    space_cal_svc = view.svc(K.space_cal_service_key)
    space_repo = view.svc(K.space_repo_key)
    rsvps = await space_cal_svc.list_rsvps(
        event_id, occurrence_at=occurrence_at,
    )
    counts: dict[str, int] = {
        "going": 0,
        "maybe": 0,
        "declined": 0,
        "requested": 0,
        "waitlist": 0,
    }
    for r in rsvps:
        counts[r.status] = counts.get(r.status, 0) + 1
    ws = view.svc(K.ws_manager_key)
    member_ids = await space_repo.list_local_member_user_ids(space_id)
    frame: dict = {
        "type": "calendar.rsvp_updated",
        "event_id": event_id,
        "space_id": space_id,
        "counts": counts,
    }
    if occurrence_at is not None:
        frame["occurrence_at"] = occurrence_at
    await ws.broadcast_to_users(member_ids, frame)
    body: dict = {"ok": True, "counts": counts}
    if extra:
        body.update(extra)
    return web.json_response(body)


async def _resolve_space_id_for_event(view, event_id: str) -> str | None:
    """Return the event's owning space_id, or None if the event is
    missing. Delegates to :class:`SpaceCalendarService.resolve_space_id`.
    """
    svc = view.svc(K.space_cal_service_key)
    return await svc.resolve_space_id(event_id)


class CalendarEventApprovalView(BaseView):
    """``POST /api/calendars/events/{id}/approve`` — host approval action.

    Phase C: approve or deny a member's pending request-to-join on a
    capped event. Body: ``{"user_id": str, "occurrence_at"?: str,
    "action": "approve" | "deny"}``. Approver gate: caller must be the
    event creator OR a space admin/owner.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        event_id = self.match("id")
        body = await self.body()
        target_user = str(body.get("user_id") or "").strip()
        action = str(body.get("action") or "").strip()
        occurrence_at = body.get("occurrence_at")
        if not target_user or action not in ("approve", "deny"):
            return error_response(
                422, "UNPROCESSABLE",
                "user_id and action ('approve' | 'deny') required.",
            )

        space_cal_svc = self.svc(K.space_cal_service_key)
        space_id = await _resolve_space_id_for_event(self, event_id)
        if space_id is None:
            return error_response(404, "NOT_FOUND", "Event not found.")
        # Approver = event creator OR space admin/owner.
        space_repo = self.svc(K.space_repo_key)
        member = await space_repo.get_member(space_id, ctx.user_id)
        if member is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")
        result = await space_cal_svc._repo.get_event(event_id)
        if result is None:
            return error_response(404, "NOT_FOUND", "Event not found.")
        _sid, event = result
        is_creator = event.created_by == ctx.user_id
        is_admin = member.role in ("owner", "admin")
        if not (is_creator or is_admin):
            return error_response(
                403, "FORBIDDEN",
                "Only the event creator or a space admin can approve.",
            )

        try:
            if action == "approve":
                new_status = await space_cal_svc.approve_rsvp(
                    event_id=event_id,
                    user_id=target_user,
                    occurrence_at=occurrence_at,
                )
            else:
                await space_cal_svc.deny_rsvp(
                    event_id=event_id,
                    user_id=target_user,
                    occurrence_at=occurrence_at,
                )
                new_status = None
        except KeyError as exc:
            return error_response(404, "NOT_FOUND", str(exc))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))

        return await _broadcast_rsvp_counts(
            self,
            event_id=event_id,
            space_id=space_id,
            occurrence_at=occurrence_at,
            extra={"action": action, "new_status": new_status},
        )


class CalendarEventPendingView(BaseView):
    """``GET /api/calendars/events/{id}/pending`` — list pending requests.

    Phase C: returns ``requested`` RSVPs for the host UI. Approver-only.
    Optional ``?occurrence_at=`` query string to scope to one occurrence.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        event_id = self.match("id")
        occurrence_at = self.request.query.get("occurrence_at")
        space_cal_svc = self.svc(K.space_cal_service_key)
        space_id = await _resolve_space_id_for_event(self, event_id)
        if space_id is None:
            return error_response(404, "NOT_FOUND", "Event not found.")
        space_repo = self.svc(K.space_repo_key)
        member = await space_repo.get_member(space_id, ctx.user_id)
        if member is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")
        result = await space_cal_svc._repo.get_event(event_id)
        if result is None:
            return error_response(404, "NOT_FOUND", "Event not found.")
        _sid, event = result
        if event.created_by != ctx.user_id and member.role not in ("owner", "admin"):
            return error_response(
                403, "FORBIDDEN",
                "Only the event creator or a space admin can list pending requests.",
            )
        pending = await space_cal_svc.list_pending(
            event_id, occurrence_at=occurrence_at,
        )
        return web.json_response(
            {
                "pending": [
                    {
                        "user_id": r.user_id,
                        "occurrence_at": r.occurrence_at,
                        "updated_at": r.updated_at,
                    }
                    for r in pending
                ],
            }
        )


class CalendarEventRsvpsView(BaseView):
    """``GET /api/calendars/events/{id}/rsvps`` — list RSVPs for an event.

    Accepts ``?occurrence_at=<iso>`` to scope to a single occurrence
    of a recurring event. Without it, returns RSVPs across all
    occurrences (each row carries its own ``occurrence_at`` so callers
    can group client-side).
    """

    async def get(self) -> web.Response:
        self.user  # auth check
        event_id = self.match("id")
        occurrence_at = self.request.query.get("occurrence_at")
        space_cal_svc = self.svc(K.space_cal_service_key)
        rsvps = await space_cal_svc.list_rsvps(
            event_id, occurrence_at=occurrence_at,
        )
        return web.json_response(
            {
                "rsvps": [
                    {
                        "user_id": r.user_id,
                        "status": r.status,
                        "updated_at": r.updated_at,
                        "occurrence_at": r.occurrence_at,
                    }
                    for r in rsvps
                ],
            }
        )
