"""Conversation (DM) routes — /api/conversations/* (section 23.47)."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import unquote

from aiohttp import web

from ..app_keys import (
    conversation_repo_key,
    dm_service_key,
    federation_repo_key,
    media_signer_key,
    media_transcode_repo_key,
    notification_service_key,
    online_status_service_key,
    user_repo_key,
)
from ..domain.conversation import ConversationType, mute_active
from ..domain.user import _picture_url
from ..media_signer import sign_media_urls_in, strip_signature_query
from ..security import error_response, sanitise_for_api
from .base import BaseView
from .media_status import READY, media_filename, video_poster_path


class ConversationCollectionView(BaseView):
    """GET /api/conversations — list conversations for the current user."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        repo = self.svc(conversation_repo_key)
        user_repo = self.svc(user_repo_key)
        convos = await svc.list_conversations(ctx.username)

        # Fold a small member preview into each row so the inbox can
        # render avatar stacks + a peer-name fallback ("Anna, Bob")
        # without N+1 follow-up fetches. Self is filtered out so the
        # preview reads as "the others".
        rows: list[dict] = []
        now = datetime.now(timezone.utc)
        for c in convos:
            members = await repo.list_members(c.id)
            # Cross-household DMs / group DMs may seat a federated peer
            # as a :class:`RemoteConversationMember`. The two member
            # tables share no schema, so this endpoint has to fold both
            # rosters into a single preview the SPA can render — without
            # this, a 1:1 DM with a remote peer surfaces as "Direct
            # message" with no avatar because the local-members list
            # contains only the caller (filtered out as "self" below).
            remote_members = await repo.list_remote_members(c.id)
            preview: list[dict] = []
            own_last_read_at: str | None = None
            own_muted_until: str | None = None
            own_notif_level = "all"
            for m in members:
                if m.username == ctx.username:
                    own_notif_level = m.notif_level
                    # The caller's own mute, when still on (a past
                    # ``muted_until`` reads as unmuted).
                    if mute_active(m.muted_until, now=now):
                        own_muted_until = m.muted_until
                    # Stash the caller's own read watermark so the SPA can
                    # render a "New messages since you last looked" divider
                    # without a second round-trip.
                    own_last_read_at = m.last_read_at
                    continue
                u = await user_repo.get(m.username)
                if u is None:
                    continue
                preview.append(
                    {
                        "user_id": u.user_id,
                        "username": u.username,
                        "display_name": u.display_name,
                        "picture_url": _picture_url(u.user_id, u.picture_hash),
                    }
                )
            for rm in remote_members:
                ru = await user_repo.get_remote_by_member(
                    rm.instance_id,
                    rm.remote_username,
                )
                if ru is None:
                    if rm.user_id is None:
                        # Member row exists but the peer-directory snapshot
                        # hasn't landed yet — skip the preview entry (the
                        # SPA falls back to ``member_count`` for the avatar
                        # stub) rather than synthesising a fake name.
                        continue
                    # A group seat on a household we never paired with:
                    # the authority's roster named them.
                    preview.append(
                        {
                            "user_id": rm.user_id,
                            "username": rm.remote_username,
                            "display_name": rm.display_name or rm.remote_username,
                            "picture_url": None,
                        }
                    )
                    continue
                preview.append(
                    {
                        "user_id": ru.user_id,
                        "username": ru.remote_username,
                        "display_name": ru.display_name,
                        "picture_url": _picture_url(ru.user_id, ru.picture_hash),
                    }
                )
            unread = await svc.count_unread(c.id, username=ctx.username)
            rows.append(
                {
                    "id": c.id,
                    "type": c.type.value,
                    "name": c.name,
                    "last_message_at": c.last_message_at.isoformat()
                    if c.last_message_at
                    else None,
                    "members": preview,
                    "member_count": len(
                        [
                            m
                            for m in members
                            if m.deleted_at is None
                            or c.type is not ConversationType.GROUP_DM
                        ]
                    )
                    + len(remote_members),
                    # Group conversations: whether this household keeps the
                    # member list (it created the group) — the SPA offers
                    # add / remove / rename only then; anyone can leave.
                    "managed_here": svc.groups.is_authority_here(c.id),
                    "unread": unread,
                    # ISO 8601 timestamp the caller last marked-as-read on
                    # this conversation. ``null`` for brand-new threads.
                    # The SPA uses this to find the first-unread message
                    # in the loaded window and scroll to the "New
                    # messages" divider on entry.
                    "last_read_at": own_last_read_at,
                    # The caller muted this conversation until then (UTC
                    # ISO 8601; ``9999-…`` = until they unmute). ``null``
                    # when not muted. Unread still counts; no bell / push.
                    "muted_until": own_muted_until,
                    # Groups: the caller's own level — ``all`` or
                    # ``mentions`` (only @-mentions ring). Always ``all``
                    # for a 1:1.
                    "notif_level": own_notif_level,
                }
            )
        return web.json_response(rows)


class ConversationDmView(BaseView):
    """POST /api/conversations/dm — create a 1:1 DM.

    Body shape: either ``{"username": "..."}`` (local-only DM) or
    ``{"user_id": "..."}`` (cross-household DM with a remote user
    already mirrored from a paired peer's directory).
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        body = await self.body()
        username = body.get("username")
        user_id = body.get("user_id")
        if (username is None) == (user_id is None):
            return error_response(
                422,
                "UNPROCESSABLE",
                "exactly one of username / user_id is required",
            )
        conv = await svc.create_dm(
            creator_username=ctx.username,
            other_username=username,
            other_user_id=user_id,
        )
        return web.json_response({"id": conv.id, "type": conv.type.value}, status=201)


def _str_list(value: object) -> list[str]:
    """A JSON list of strings, or ``[]`` for anything else."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v]


class ConversationGroupView(BaseView):
    """POST /api/conversations/group — create a group DM.

    Body: ``{"members": [local usernames], "member_user_ids": [user_ids],
    "name": str?}``. ``member_user_ids`` may name people from directly
    paired households (v_37+); this household becomes the group's
    authority. A person who can't join is refused with 422
    ``GROUP_MEMBER_UNSUPPORTED`` and a message naming them.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        body = await self.body()
        conv = await svc.create_group_dm(
            creator_username=ctx.username,
            member_usernames=_str_list(body.get("members", [])),
            member_user_ids=_str_list(body.get("member_user_ids", [])),
            name=body.get("name"),
        )
        return web.json_response({"id": conv.id, "type": conv.type.value}, status=201)


class ConversationItemView(BaseView):
    """PATCH /api/conversations/{id} — rename a group (``{"name": str|null}``).

    Only a member on the group's authority household (403 otherwise).
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        body = await self.body()
        await svc.rename_group(
            self.match("id"),
            actor_username=ctx.username,
            name=body.get("name"),
        )
        return web.json_response({"ok": True})


class ConversationLeaveView(BaseView):
    """POST /api/conversations/{id}/leave — the caller leaves the conversation.

    A 1:1 is hidden for the caller; a group is left for good (the
    authority household is told when it is another one).
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        await svc.leave(self.match("id"), username=ctx.username)
        return web.json_response({"ok": True})


class ConversationMuteView(BaseView):
    """PUT / DELETE /api/conversations/{id}/mute — the caller's own mute.

    ``PUT`` body ``{"duration": "1h" | "8h" | "1w" | "forever"}``; returns
    ``{"muted_until": iso}``. ``DELETE`` unmutes (``{"muted_until": null}``).
    Members only (403). Local only — a mute is never federated.
    """

    async def put(self) -> web.Response:
        body = await self.body()
        duration = body.get("duration")
        if not isinstance(duration, str):
            return error_response(422, "UNPROCESSABLE", "duration is required")
        until = await self.svc(dm_service_key).mute(
            self.match("id"),
            username=self.user.username,
            duration=duration,
        )
        return web.json_response({"muted_until": until})

    async def delete(self) -> web.Response:
        await self.svc(dm_service_key).unmute(
            self.match("id"),
            username=self.user.username,
        )
        return web.json_response({"muted_until": None})


class ConversationNotifPrefsView(BaseView):
    """GET / PUT /api/conversations/{id}/notif-prefs — the caller's own level.

    Group conversations only. ``PUT`` body ``{"level": "all" | "mentions"}``
    (422 for another value or a 1:1); both return ``{"level"}``. Members
    only (403). An active mute (``/mute``) still wins. Local only.
    """

    async def get(self) -> web.Response:
        conv_id = self.match("id")
        members = await self.svc(conversation_repo_key).list_members(conv_id)
        for m in members:
            if m.username == self.user.username and m.deleted_at is None:
                return web.json_response({"level": m.notif_level})
        raise PermissionError("not a member of this conversation")

    async def put(self) -> web.Response:
        body = await self.body()
        level = body.get("level")
        if not isinstance(level, str):
            return error_response(422, "UNPROCESSABLE", "level is required")
        saved = await self.svc(dm_service_key).set_notif_level(
            self.match("id"),
            username=self.user.username,
            level=level,
        )
        return web.json_response({"level": saved})


class ConversationMemberView(BaseView):
    """DELETE /api/conversations/{id}/members/{user_id} — remove from a group.

    Only a member on the group's authority household (403 otherwise).
    """

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        await svc.remove_group_member(
            self.match("id"),
            actor_username=ctx.username,
            user_id=unquote(self.match("user_id")),
        )
        return web.json_response({"ok": True})


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
        repo = self.svc(conversation_repo_key)
        # Video messages transcode in the background — surface a live
        # ``media_status`` so the SPA shows a "Processing…" placeholder
        # until the ``.webm`` exists. One batched repo read per request.
        video_fns = [
            fn
            for m in msgs
            if m.type == "video" and (fn := media_filename(m.media_url)) is not None
        ]
        statuses = await self.svc(media_transcode_repo_key).status_for(video_fns)
        payload = []
        for m in msgs:
            # ``list_reactions`` is a small per-message read; the page
            # size is capped at 100 so the worst case is 100 queries —
            # cheap on SQLite WAL. A bulk-by-conversation read can come
            # later if the cost shows up in profiling.
            rxs = await repo.list_reactions(m.id) if not m.deleted else []
            reactions = [{"user_id": r.user_id, "emoji": r.emoji} for r in rxs]
            row = {
                "id": m.id,
                "sender_user_id": m.sender_user_id,
                "content": m.content,
                "type": m.type,
                "media_url": m.media_url,
                "file_name": m.file_name,
                "mime_type": m.mime_type,
                "file_size_bytes": m.file_size_bytes,
                "media_sync_status": m.media_sync_status,
                "reply_to_id": m.reply_to_id,
                "reactions": reactions,
                "deleted": m.deleted,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "edited_at": m.edited_at.isoformat() if m.edited_at else None,
            }
            if m.type == "video":
                fn = media_filename(m.media_url)
                row["media_status"] = statuses.get(fn, READY) if fn else READY
                # Signed poster — set the unsigned ``.webp`` sibling path
                # so the ``sign_media_urls_in`` pass below signs it
                # alongside ``media_url``.
                poster = video_poster_path(m.media_url)
                if poster is not None:
                    row["media_thumbnail_url"] = poster
            payload.append(sanitise_for_api(row))
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return web.json_response(payload)

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        conv_id = self.match("id")
        body = await self.body()
        # File-size is taken from the body if provided, but the SPA can
        # also leave it ``None`` and let the receiver read it via the
        # signed URL's ``content-length``. The cap is enforced by
        # ``MediaUploadView`` before we ever reach this handler.
        raw_size = body.get("file_size_bytes")
        try:
            file_size_bytes = int(raw_size) if raw_size is not None else None
        except TypeError, ValueError:
            file_size_bytes = None
        msg = await svc.send_message(
            conv_id,
            sender_username=ctx.username,
            content=body.get("content", ""),
            type=body.get("type", "text"),
            media_url=strip_signature_query(body.get("media_url")),
            file_name=body.get("file_name"),
            mime_type=body.get("mime_type"),
            file_size_bytes=file_size_bytes,
            reply_to_id=body.get("reply_to_id"),
        )
        return web.json_response({"id": msg.id}, status=201)


class ConversationMembersView(BaseView):
    """``GET /api/conversations/{id}/members`` — roster for one DM.

    Each row carries ``user_id``, ``username``, ``display_name``,
    plus the same session-presence triple as ``GET /api/presence``
    (``is_online`` / ``is_idle`` / ``last_seen_at``) so the DM thread
    header can render a WhatsApp-style "Online" / "Last seen 2 h ago"
    status without a follow-up fetch.
    """

    async def post(self) -> web.Response:
        """Add people to a group: ``{"usernames": [...], "user_ids": [...]}``.

        Only a member on the group's authority household (403 otherwise);
        a person who can't join is refused with 422
        ``GROUP_MEMBER_UNSUPPORTED``.
        """
        ctx = self.user
        svc = self.svc(dm_service_key)
        body = await self.body()
        await svc.add_group_members(
            self.match("id"),
            actor_username=ctx.username,
            usernames=_str_list(body.get("usernames")),
            user_ids=_str_list(body.get("user_ids")),
        )
        return web.json_response({"ok": True}, status=201)

    async def get(self) -> web.Response:
        ctx = self.user
        conv_id = self.match("id")
        repo = self.svc(conversation_repo_key)
        members = await repo.list_members(conv_id)
        # Members only: the roster names people on other households.
        if not any(
            m.username == ctx.username and m.deleted_at is None for m in members
        ):
            raise PermissionError("not a member of this conversation")
        conv = await repo.get(conv_id)
        if conv is not None and conv.type is ConversationType.GROUP_DM:
            # Somebody who left a group is no longer on its roster.
            members = [m for m in members if m.deleted_at is None]
        fed_repo = self.request.app.get(federation_repo_key)
        household_names: dict[str, str | None] = {}

        async def _household(instance_id: str) -> str | None:
            if instance_id not in household_names:
                inst = (
                    await fed_repo.get_instance(instance_id)
                    if fed_repo is not None
                    else None
                )
                household_names[instance_id] = (
                    inst.effective_display_name if inst is not None else None
                )
            return household_names[instance_id]

        # See :class:`ConversationCollectionView` for why the remote
        # member roster has to be folded in alongside the local one —
        # the thread header reads its title + avatar from this endpoint,
        # and without the remote rows a cross-household DM renders with
        # no peer name and a placeholder avatar.
        remote_members = await repo.list_remote_members(conv_id)
        # Member rows hold ``username``; the user_repo lookup gives us
        # display_name + user_id (and the persisted last_seen_at fallback
        # for offline users).
        user_repo = self.svc(user_repo_key)
        online_svc = self.request.app.get(online_status_service_key)
        # The exact @-token per member (``None`` → can't be mentioned), so
        # the composer inserts what this household resolves back to them.
        tokens = await self.svc(dm_service_key).mention_tokens(conv_id)
        rows: list[dict] = []
        for m in members:
            u = await user_repo.get(m.username)
            if u is None:
                continue
            is_online = bool(online_svc and online_svc.is_online(u.user_id))
            is_idle = bool(online_svc and online_svc.is_idle(u.user_id))
            if is_online and online_svc is not None:
                last_dt = online_svc.last_seen(u.user_id)
                last_seen = last_dt.isoformat() if last_dt is not None else None
            else:
                last_seen = u.last_seen_at
            # Same shape ``DmInboxPage`` already consumes — a relative
            # signed URL (no leading slash) so the SPA's ``<img src>``
            # resolves it against ``document.baseURI`` and works under
            # the HAOS Supervisor ingress prefix without any URL surgery.
            rows.append(
                {
                    "user_id": u.user_id,
                    "username": u.username,
                    "display_name": u.display_name,
                    "picture_url": _picture_url(u.user_id, u.picture_hash),
                    "is_self": ctx is not None and ctx.user_id == u.user_id,
                    "is_online": is_online,
                    "is_idle": is_idle,
                    "last_seen_at": last_seen,
                    "instance_id": None,
                    "household_name": None,
                    "mention": tokens.get(u.user_id),
                }
            )
        for rm in remote_members:
            ru = await user_repo.get_remote_by_member(
                rm.instance_id,
                rm.remote_username,
            )
            if ru is None:
                if rm.user_id is None:
                    continue
                # A group seat on a household we never paired with: only
                # what the authority's roster said about them.
                rows.append(
                    {
                        "user_id": rm.user_id,
                        "username": rm.remote_username,
                        "display_name": rm.display_name or rm.remote_username,
                        "picture_url": None,
                        "is_self": False,
                        "is_online": False,
                        "is_idle": False,
                        "last_seen_at": None,
                        "instance_id": rm.instance_id,
                        "household_name": await _household(rm.instance_id),
                        "mention": tokens.get(rm.user_id),
                    }
                )
                continue
            # Presence for remote users is tracked through the same
            # OnlineStatusService — federation USER_ONLINE / USER_OFFLINE
            # envelopes populate its remote cache, keyed on the global
            # ``user_id``. ``remote_users`` has no persisted
            # ``last_seen_at`` column, so we fall through to the
            # presence cache for both the online and offline branches.
            is_online = bool(online_svc and online_svc.is_online(ru.user_id))
            is_idle = bool(online_svc and online_svc.is_idle(ru.user_id))
            last_dt = online_svc.last_seen(ru.user_id) if online_svc else None
            last_seen = last_dt.isoformat() if last_dt is not None else None
            rows.append(
                {
                    "user_id": ru.user_id,
                    "username": ru.remote_username,
                    "display_name": ru.display_name,
                    "picture_url": _picture_url(ru.user_id, ru.picture_hash),
                    # A remote member is by definition not ``self`` —
                    # ctx is always a local user.
                    "is_self": False,
                    "is_online": is_online,
                    "is_idle": is_idle,
                    "last_seen_at": last_seen,
                    "instance_id": rm.instance_id,
                    "household_name": await _household(rm.instance_id),
                    "mention": tokens.get(ru.user_id),
                }
            )
        return self._json(rows)


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
        # Clear bell badges for this conversation in lockstep with the
        # read-receipt update — opening a thread is the natural "I've
        # seen these" signal, no separate UI gesture needed.
        notif_svc = self.request.app.get(notification_service_key)
        if notif_svc is not None and ctx is not None:
            try:
                await notif_svc.mark_read_for_dm(ctx.user_id, conv_id)
            except Exception:
                # Best-effort — never block the read receipt on this.
                pass
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


class ConversationMessageReactionView(BaseView):
    """``PUT|DELETE /api/conversations/{id}/messages/{mid}/reactions/{emoji}``.

    Add or remove a single emoji reaction for the caller on a DM
    message. ``{emoji}`` is URL-encoded so multi-byte glyphs survive
    the routing layer. The actor is always the caller — there's no
    "react on behalf of" path.
    """

    async def put(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        message_id = self.match("mid")
        emoji = unquote(self.match("emoji"))
        await svc.add_reaction(
            message_id,
            username=ctx.username,
            emoji=emoji,
        )
        return web.json_response({"ok": True})

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        message_id = self.match("mid")
        emoji = unquote(self.match("emoji"))
        await svc.remove_reaction(
            message_id,
            username=ctx.username,
            emoji=emoji,
        )
        return web.json_response({"ok": True})


class ConversationMessageReactionListView(BaseView):
    """``GET /api/conversations/{id}/messages/{mid}/reactions`` —
    the full reaction roster for one message. Membership-gated.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(dm_service_key)
        message_id = self.match("mid")
        reactions = await svc.list_reactions(
            message_id,
            username=ctx.username,
        )
        return web.json_response(
            {"reactions": [{"user_id": r.user_id, "emoji": r.emoji} for r in reactions]}
        )


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
