"""iCalendar (.ics) routes — Phase F.

Two surfaces:

* Per-event download — :class:`CalendarEventIcsView` returns one VEVENT
  for ``GET /api/calendars/events/{id}.ics``. Member-only via the
  standard auth flow (session cookie or bearer token), so the URL is
  shared like any other API URL.

* Per-(user, space) subscribable feed — :class:`SpaceCalendarFeedView`
  returns a multi-VEVENT VCALENDAR for the next 90 days at
  ``GET /api/spaces/{space_id}/calendar.ics?token=...``. The token is
  embedded in the URL because most desktop calendar clients (Apple
  Calendar, Outlook, Thunderbird) refresh the feed daily *without* an
  OAuth round-trip — they need a stable URL with a secret. Tokens are
  per-(user, space), revocable; managed via
  :class:`SpaceCalendarFeedTokenView`.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from aiohttp import web

from .. import app_keys as K
from ..security import error_response
from ..serialization.ics import (
    feed_etag,
    serialize_event,
    serialize_feed,
)
from .base import BaseView


def _ics_response(payload: bytes, *, request: web.BaseRequest) -> web.Response:
    """Render an .ics body with proper headers + ETag conditional GET."""
    etag = feed_etag(payload)
    inm = request.headers.get("If-None-Match")
    if inm and inm == etag:
        return web.Response(status=304, headers={"ETag": etag})
    return web.Response(
        body=payload,
        content_type="text/calendar",
        charset="utf-8",
        headers={
            "ETag": etag,
            # 15 min — calendar clients honour this and skip refetches.
            "Cache-Control": "private, max-age=900",
        },
    )


class CalendarEventIcsView(BaseView):
    """``GET /api/calendars/events/{id}/export.ics`` — one event as VCALENDAR.

    Member-only. Used for the "Add to my calendar" button on the event
    detail page; rendering one VEVENT lets the user import the event
    into their native calendar.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        event_id = self.match("id")
        space_cal_svc = self.svc(K.space_cal_service_key)
        result = await space_cal_svc._repo.get_event(event_id)
        if result is None:
            return error_response(404, "NOT_FOUND", "Event not found.")
        space_id, event = result
        space_repo = self.svc(K.space_repo_key)
        member = await space_repo.get_member(space_id, ctx.user_id)
        if member is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")
        # Pull this user's reminders so VALARMs land in the export.
        reminders = await space_cal_svc.list_reminders(
            event_id=event_id,
            user_id=ctx.user_id,
        )
        payload = serialize_event(event, reminders=reminders)
        return _ics_response(payload, request=self.request)


class SpaceCalendarFeedView(BaseView):
    """``GET /api/spaces/{id}/calendar/export.ics?token=...`` — subscribable feed.

    Returns the next 90 days of events for the space. The ``token``
    is per-(user, space) and ties the feed to a specific user's
    reminder set. Revoked tokens return 401.
    """

    async def get(self) -> web.Response:
        space_id = self.match("id")
        token = self.request.query.get("token", "").strip()
        if not token:
            return error_response(401, "UNAUTHORIZED", "feed token required")
        space_cal_svc = self.svc(K.space_cal_service_key)
        repo = space_cal_svc._repo
        owner = await repo.get_user_for_feed_token(token)
        if owner is None:
            return error_response(401, "UNAUTHORIZED", "feed token invalid or revoked")
        token_user, token_space = owner
        if token_space != space_id:
            return error_response(401, "UNAUTHORIZED", "token / space mismatch")
        # Verify the user is still a member — leaving the space should
        # invalidate the feed regardless of token.
        space_repo = self.svc(K.space_repo_key)
        if await space_repo.get_member(space_id, token_user) is None:
            return error_response(401, "UNAUTHORIZED", "no longer a member")
        now = datetime.now(timezone.utc)
        events = await repo.list_events_in_range(
            space_id,
            start=now,
            end=now + timedelta(days=90),
        )
        # Attach this user's reminders per event for VALARM blocks.
        rby: dict = {}
        for ev in events:
            base_id = ev.id.split("@", 1)[0]
            if base_id in rby:
                continue
            rby[base_id] = await repo.list_reminders(
                event_id=base_id,
                user_id=token_user,
            )
        payload = serialize_feed(events, reminders_by_event=rby)
        return _ics_response(payload, request=self.request)


class SpaceCalendarFeedTokenView(BaseView):
    """``POST`` / ``DELETE /api/spaces/{id}/calendar/feed-token`` — token mgmt.

    POST generates a new token (or returns the existing one for this
    user/space pair), revokes any previous, and returns the full URL.
    DELETE revokes the current token; future fetches return 401 until a
    fresh POST.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        space_repo = self.svc(K.space_repo_key)
        if await space_repo.get_member(space_id, ctx.user_id) is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")
        space_cal_svc = self.svc(K.space_cal_service_key)
        repo = space_cal_svc._repo
        token = secrets.token_urlsafe(32)
        await repo.upsert_feed_token(
            user_id=ctx.user_id,
            space_id=space_id,
            token=token,
        )
        url = f"/api/spaces/{space_id}/calendar/export.ics?token={token}"
        return web.json_response({"token": token, "url": url}, status=201)

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        space_repo = self.svc(K.space_repo_key)
        if await space_repo.get_member(space_id, ctx.user_id) is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")
        space_cal_svc = self.svc(K.space_cal_service_key)
        repo = space_cal_svc._repo
        await repo.revoke_feed_token(user_id=ctx.user_id, space_id=space_id)
        return web.json_response({"ok": True})
