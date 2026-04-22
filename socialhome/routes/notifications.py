"""Notification routes — /api/notifications/* (§17.2)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import event_bus_key, notification_repo_key
from ..domain.events import NotificationReadChanged
from .base import BaseView


class NotificationCollectionView(BaseView):
    """``GET /api/notifications`` — paginated notification list."""

    async def get(self) -> web.Response:
        ctx = self.user
        before = self.request.query.get("before")
        limit = min(max(int(self.request.query.get("limit", 50)), 1), 50)
        repo = self.svc(notification_repo_key)
        notes = await repo.list(ctx.user_id, before=before, limit=limit)
        return web.json_response(
            [
                {
                    "id": n.id,
                    "type": n.type,
                    "title": n.title,
                    "body": n.body,
                    "link_url": n.link_url,
                    "read_at": n.read_at,
                    "created_at": n.created_at,
                }
                for n in notes
            ]
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
        repo = self.svc(notification_repo_key)
        bus = self.svc(event_bus_key)
        await repo.mark_read(nid, ctx.user_id)
        await bus.publish(
            NotificationReadChanged(
                user_id=ctx.user_id,
                unread_count=await repo.count_unread(ctx.user_id),
            )
        )
        return web.json_response({"ok": True})


class NotificationReadAllView(BaseView):
    """``POST /api/notifications/read-all`` — mark all notifications read."""

    async def post(self) -> web.Response:
        ctx = self.user
        repo = self.svc(notification_repo_key)
        bus = self.svc(event_bus_key)
        await repo.mark_all_read(ctx.user_id)
        await bus.publish(
            NotificationReadChanged(
                user_id=ctx.user_id,
                unread_count=0,
            )
        )
        return web.json_response({"ok": True})
