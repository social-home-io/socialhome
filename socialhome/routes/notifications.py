"""Notification routes — /api/notifications/* (§17.2)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    notification_repo_key,
    notification_service_key,
)
from ..domain.notification import Notification
from ..domain.notification_kinds import kind_of
from ..security import error_response
from .base import BaseView

#: Pages one "I saw these" call may name (a page view names one or two).
MAX_READ_LINKS = 20
#: Longest page path accepted (a notification link is far shorter).
MAX_LINK_LENGTH = 512
#: Things one "I saw these" call may name.
MAX_READ_OBJECTS = 20
#: Longest object key accepted (``<kind>:<id>`` is far shorter).
MAX_OBJECT_KEY_LENGTH = 256


def notification_json(n: Notification) -> dict:
    """One row as the list returns it. ``object_key`` / ``space_id`` /
    ``params`` are ``null`` on a row from before 0129; ``new_count`` /
    ``total_count`` lift the params' counts (1 / 1 for such a row)."""
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "link_url": n.link_url,
        "read_at": n.read_at,
        "created_at": n.created_at,
        "object_key": n.object_key,
        "space_id": n.space_id,
        "params": n.params.to_dict() if n.params else None,
        "new_count": n.params.new if n.params else 1,
        "total_count": n.params.total if n.params else 1,
        # Settings → Notifications kind it falls under (D4) — the row "…"
        # menu offers to turn that kind down.
        "kind": kind_of(n.type, n.space_id),
    }


class NotificationCollectionView(BaseView):
    """``GET /api/notifications`` — paginated notification list."""

    async def get(self) -> web.Response:
        ctx = self.user
        before = self.request.query.get("before")
        limit = min(max(int(self.request.query.get("limit", 50)), 1), 50)
        unread_only = self.request.query.get("unread") in ("1", "true")
        repo = self.svc(notification_repo_key)
        notes = await repo.list(
            ctx.user_id, before=before, limit=limit, unread_only=unread_only
        )
        return web.json_response([notification_json(n) for n in notes])


class NotificationNeedsYouView(BaseView):
    """``GET /api/notifications/needs-you`` — the caller's "Needs you"
    items (D12): requests that wait for them, read or not, newest first,
    each checked against the request it asks about. ``{items, count}``;
    the page and the bell pin them on top and group them per space."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(notification_service_key)
        items = await svc.pending_actions(ctx.user_id)
        return web.json_response(
            {"items": [notification_json(n) for n in items], "count": len(items)}
        )


class NotificationUnreadCountView(BaseView):
    """``GET /api/notifications/unread-count``."""

    async def get(self) -> web.Response:
        ctx = self.user
        repo = self.svc(notification_repo_key)
        count = await repo.count_unread(ctx.user_id)
        return web.json_response({"unread": count})


class NotificationReadView(BaseView):
    """``POST /api/notifications/{id}/read`` — mark one notification read."""

    async def post(self) -> web.Response:
        ctx = self.user
        nid = self.match("id")
        await self.svc(notification_service_key).mark_read(ctx.user_id, nid)
        return web.json_response({"ok": True})


class NotificationReadAllView(BaseView):
    """``POST /api/notifications/read-all`` — mark all notifications read."""

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(notification_service_key).mark_all_read(ctx.user_id)
        return web.json_response({"ok": True})


class NotificationReadByLinkView(BaseView):
    """``POST /api/notifications/read-by-link`` — the caller saw these pages.

    Body ``{"links": ["/post/p1", "/spaces/s1?post=p2", …]}``: every unread
    notification of the caller that opens one of them reads as read (a
    ``?comment=`` / ``?reply=`` it picks out counts as the same page), and
    every device of theirs gets the new unread count. The SPA calls it once
    the page has shown the thing. Returns ``{ok, marked}``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        data = await self.body()
        links = data.get("links") if isinstance(data, dict) else None
        if (
            not isinstance(links, list)
            or not 1 <= len(links) <= MAX_READ_LINKS
            or not all(
                isinstance(link, str)
                and link.startswith("/")
                and len(link) <= MAX_LINK_LENGTH
                for link in links
            )
        ):
            return error_response(
                422,
                "UNPROCESSABLE",
                f"links must be 1-{MAX_READ_LINKS} paths starting with '/'.",
            )
        svc = self.svc(notification_service_key)
        marked = await svc.mark_read_by_links(ctx.user_id, links)
        return web.json_response({"ok": True, "marked": marked})


class NotificationReadByObjectView(BaseView):
    """``POST /api/notifications/read-by-object`` — the caller saw these things.

    Body ``{"object_keys": ["post:p1", "dm:c1", …]}`` (1–20 keys, each the
    ``object_key`` a row carries): every unread row of the caller about one
    of them reads as read, whatever its type, and every device of theirs
    gets the new unread count. Returns ``{ok, marked}``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        data = await self.body()
        keys = data.get("object_keys") if isinstance(data, dict) else None
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= MAX_READ_OBJECTS
            or not all(
                isinstance(key, str)
                and ":" in key
                and len(key) <= MAX_OBJECT_KEY_LENGTH
                for key in keys
            )
        ):
            return error_response(
                422,
                "UNPROCESSABLE",
                f"object_keys must be 1-{MAX_READ_OBJECTS} '<kind>:<id>' keys.",
            )
        svc = self.svc(notification_service_key)
        marked = await svc.mark_read_by_objects(ctx.user_id, keys)
        return web.json_response({"ok": True, "marked": marked})
