"""Conversation (DM) routes — /api/conversations/* (section 23.47)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import dm_service_key
from ..security import sanitise_for_api
from .base import BaseView


class ConversationCollectionView(BaseView):
    """GET /api/conversations — list conversations for the current user."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        convos = await svc.list_conversations(ctx.username)
        return web.json_response(
            [
                {
                    "id": c.id,
                    "type": c.type.value,
                    "name": c.name,
                    "last_message_at": c.last_message_at.isoformat()
                    if c.last_message_at
                    else None,
                }
                for c in convos
            ]
        )


class ConversationDmView(BaseView):
    """POST /api/conversations/dm — create a 1:1 DM."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        body = await self.body()
        conv = await svc.create_dm(
            creator_username=ctx.username,
            other_username=body["username"],
        )
        return web.json_response({"id": conv.id, "type": conv.type.value}, status=201)


class ConversationGroupView(BaseView):
    """POST /api/conversations/group — create a group DM."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        body = await self.body()
        conv = await svc.create_group_dm(
            creator_username=ctx.username,
            member_usernames=body.get("members", []),
            name=body.get("name"),
        )
        return web.json_response({"id": conv.id, "type": conv.type.value}, status=201)


class ConversationMessageView(BaseView):
    """GET/POST /api/conversations/{id}/messages — list or send messages."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        before = self.request.query.get("before")
        limit = min(max(int(self.request.query.get("limit", 50)), 1), 100)
        msgs = await svc.list_messages(
            conv_id,
            reader_username=ctx.username,
            before=before,
            limit=limit,
        )
        return web.json_response(
            [
                sanitise_for_api(
                    {
                        "id": m.id,
                        "sender_user_id": m.sender_user_id,
                        "content": m.content,
                        "type": m.type,
                        "media_url": m.media_url,
                        "reply_to_id": m.reply_to_id,
                        "deleted": m.deleted,
                        "created_at": m.created_at.isoformat()
                        if m.created_at
                        else None,
                        "edited_at": m.edited_at.isoformat() if m.edited_at else None,
                    }
                )
                for m in msgs
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        body = await self.body()
        msg = await svc.send_message(
            conv_id,
            sender_username=ctx.username,
            content=body.get("content", ""),
            type=body.get("type", "text"),
            media_url=body.get("media_url"),
            reply_to_id=body.get("reply_to_id"),
        )
        return web.json_response({"id": msg.id}, status=201)


class ConversationReadView(BaseView):
    """POST /api/conversations/{id}/read — mark conversation as read."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        await svc.mark_read(conv_id, username=ctx.username)
        return web.json_response({"ok": True})


class ConversationUnreadView(BaseView):
    """GET /api/conversations/{id}/unread — unread message count."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        count = await svc.count_unread(conv_id, username=ctx.username)
        return web.json_response({"unread": count})
