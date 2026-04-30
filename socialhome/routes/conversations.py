"""Conversation (DM) routes — /api/conversations/* (section 23.47)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import dm_service_key, media_signer_key
from ..media_signer import sign_media_urls_in, strip_signature_query
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
        signer = self.request.app.get(media_signer_key)
        payload = [
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
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return web.json_response(payload)

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
            media_url=strip_signature_query(body.get("media_url")),
            reply_to_id=body.get("reply_to_id"),
        )
        return web.json_response({"id": msg.id}, status=201)


class ConversationReadView(BaseView):
    """POST /api/conversations/{id}/read — mark conversation as read.

    Updates the caller's watermark AND bulk-upserts
    ``conversation_delivery_state`` rows so other participants see the
    read-receipt tick. Returns ``{marked}`` — count of messages whose
    state flipped to ``read``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        marked = await svc.mark_read(conv_id, username=ctx.username)
        return web.json_response({"ok": True, "marked": int(marked or 0)})


class ConversationUnreadView(BaseView):
    """GET /api/conversations/{id}/unread — unread message count."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        count = await svc.count_unread(conv_id, username=ctx.username)
        return web.json_response({"unread": count})


class ConversationMessageDeliveryView(BaseView):
    """``POST /api/conversations/{id}/messages/{mid}/delivered`` — stamp
    the caller's delivery state for one message.

    Called by the client when a DM_MESSAGE WebSocket frame lands or the
    message first appears in a list response. Idempotent; a later
    ``mark_read`` of the whole conversation supersedes.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        message_id = self.match("mid")
        await svc.mark_delivered(
            conv_id,
            message_id=message_id,
            username=ctx.username,
        )
        return web.json_response({"ok": True})


class ConversationDeliveryStatesView(BaseView):
    """``GET /api/conversations/{id}/delivery-states`` — bulk read.

    Returns one row per (message, user) so the client can render
    checkmarks. Optional ``?message_ids=a,b,c`` narrows the query.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        raw_ids = self.request.query.get("message_ids") or ""
        ids = [x for x in raw_ids.split(",") if x] or None
        states = await svc.list_delivery_states(
            conv_id,
            username=ctx.username,
            message_ids=ids,
        )
        return web.json_response({"states": states})


class ConversationGapsView(BaseView):
    """``GET /api/conversations/{id}/gaps`` — open sequence holes.

    Returns one row per (sender, expected_seq) pair the inbound
    validator flagged as missing. Used by the client to surface a
    "messages may be missing" banner.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        gaps = await svc.list_open_gaps(conv_id, username=ctx.username)
        return web.json_response({"gaps": gaps})
