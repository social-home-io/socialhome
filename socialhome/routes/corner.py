"""My Corner — personal dashboard bundle.

``GET /api/me/corner`` returns a single payload that the frontend
:mod:`DashboardPage` renders without per-widget round-trips. The
endpoint is cheap to call and safe to refetch on WS updates —
individual slice failures degrade gracefully to empty lists / zero
counts rather than returning a 5xx.
"""

from __future__ import annotations

import dataclasses

from aiohttp import web

from ..app_keys import corner_service_key
from ..security import sanitise_for_api
from .base import BaseView


class CornerView(BaseView):
    """``GET /api/me/corner`` — bundle everything the dashboard needs."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(corner_service_key)
        bundle = await svc.build(
            user_id=ctx.user_id,
            username=ctx.username,
        )
        return web.json_response(_bundle_to_dict(bundle))


def _bundle_to_dict(bundle) -> dict:
    d = dataclasses.asdict(bundle)
    # Coerce dataclass tuples → JSON-friendly lists + datetime → ISO.
    d["upcoming_events"] = [_event_to_dict(e) for e in bundle.upcoming_events]
    d["presence"] = [_presence_to_dict(p) for p in bundle.presence]
    d["tasks_due_today"] = [_task_to_dict(t) for t in bundle.tasks_due_today]
    d["bazaar"] = dataclasses.asdict(bundle.bazaar)
    d["followed_space_ids"] = list(bundle.followed_space_ids)
    d["followed_spaces_feed"] = [
        dataclasses.asdict(p) for p in bundle.followed_spaces_feed
    ]
    return sanitise_for_api(d)


def _event_to_dict(e) -> dict:
    return {
        "id": e.id,
        "calendar_id": e.calendar_id,
        "summary": e.summary,
        "description": e.description,
        "start": e.start.isoformat() if hasattr(e.start, "isoformat") else e.start,
        "end": e.end.isoformat() if hasattr(e.end, "isoformat") else e.end,
        "all_day": bool(getattr(e, "all_day", False)),
        "attendees": list(getattr(e, "attendees", []) or []),
        "created_by": getattr(e, "created_by", None),
    }


def _presence_to_dict(p) -> dict:
    return {
        "username": p.username,
        "user_id": p.user_id,
        "display_name": p.display_name,
        "state": p.state,
        "picture_url": p.picture_url,
        "zone_name": p.zone_name,
        "latitude": getattr(p, "latitude", None),
        "longitude": getattr(p, "longitude", None),
        "gps_accuracy_m": getattr(p, "gps_accuracy_m", None),
    }


def _task_to_dict(t) -> dict:
    return {
        "id": t.id,
        "list_id": t.list_id,
        "title": t.title,
        "status": t.status,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "position": t.position,
        "assignees": list(t.assignees) if t.assignees else [],
    }
