"""Federation inbound service — land remote DM/space/user events locally (§24).

The §24.11 validation pipeline (``federation/inbound_validator.py``) has
already verified signature, replay cache, ban list, and decrypted the
payload by the time an event reaches the event registry. Handlers
attached by this service persist the effect locally and publish the
matching :class:`DomainEvent` on the bus so
:class:`~social_home.services.realtime_service.RealtimeService` can
fan out to WebSocket clients.

Events without a concrete subscriber fall through to a debug log — the
event dispatch registry never raises, so silent drops are observable.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.conversation import (
    ConversationMessage,
    MESSAGE_TYPES,
)
from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    DmMessageCreated,
    PostDeleted,
    SpaceMemberProfileUpdated,
    SpacePostCreated,
    UserStatusChanged,
)
from ..domain.post import Comment, CommentType, Post, PostType
from ..domain.space import SpaceMember
from ..domain.user import RemoteUser, UserStatus
from ..infrastructure.event_bus import EventBus
from ..media.image_processor import ImageProcessor
from ..repositories.profile_picture_repo import compute_picture_hash
from ..services.user_service import PROFILE_PICTURE_MAX_DIMENSION
from ..utils.datetime import parse_iso8601_lenient

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..repositories.conversation_repo import AbstractConversationRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FederationInboundService:
    """Apply decrypted inbound federation events to local state.

    Registers handlers for the event families backed by a concrete repo:
    DM messages, space posts/comments, space membership, user status.
    Handlers call the injected repos to persist the row and publish a
    local :class:`DomainEvent` so the realtime layer picks it up.
    """

    __slots__ = (
        "_bus",
        "_conversation_repo",
        "_space_post_repo",
        "_space_repo",
        "_user_repo",
        "_profile_picture_repo",
        "_report_service",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        conversation_repo: "AbstractConversationRepo",
        space_post_repo: "AbstractSpacePostRepo",
        space_repo: "AbstractSpaceRepo",
        user_repo: "AbstractUserRepo",
        profile_picture_repo=None,
        report_service=None,
    ) -> None:
        self._bus = bus
        self._conversation_repo = conversation_repo
        self._space_post_repo = space_post_repo
        self._space_repo = space_repo
        self._user_repo = user_repo
        self._profile_picture_repo = profile_picture_repo
        self._report_service = report_service

    def attach_to(self, federation_service) -> None:
        """Register inbound handlers on the federation event registry."""
        from ..domain.federation import FederationEventType as FET

        registry = federation_service._event_registry
        registry.register(FET.DM_MESSAGE, self._on_dm_message)
        registry.register(FET.DM_MESSAGE_DELETED, self._on_dm_deleted)
        registry.register(FET.DM_MESSAGE_REACTION, self._on_dm_reaction)

        registry.register(FET.SPACE_POST_CREATED, self._on_space_post_created)
        registry.register(FET.SPACE_POST_UPDATED, self._on_space_post_updated)
        registry.register(FET.SPACE_POST_DELETED, self._on_space_post_deleted)
        registry.register(FET.SPACE_COMMENT_CREATED, self._on_space_comment_added)
        registry.register(FET.SPACE_COMMENT_UPDATED, self._on_space_comment_updated)
        registry.register(FET.SPACE_COMMENT_DELETED, self._on_space_comment_deleted)

        registry.register(FET.SPACE_MEMBER_JOINED, self._on_space_member_joined)
        registry.register(FET.SPACE_MEMBER_LEFT, self._on_space_member_left)
        registry.register(
            FET.SPACE_MEMBER_PROFILE_UPDATED,
            self._on_space_member_profile_updated,
        )

        registry.register(FET.USERS_SYNC, self._on_users_sync)
        registry.register(FET.USER_UPDATED, self._on_user_updated)
        registry.register(FET.USER_REMOVED, self._on_user_removed)
        registry.register(FET.USER_STATUS_UPDATED, self._on_user_status_updated)

        registry.register(FET.SPACE_REPORT, self._on_space_report)

    # ── DM handlers ────────────────────────────────────────────────────

    async def _on_dm_message(self, event: "FederationEvent") -> None:
        p = event.payload
        conv_id = str(p.get("conversation_id") or "")
        message_id = str(p.get("message_id") or "")
        sender_user_id = str(p.get("sender_user_id") or "")
        content = str(p.get("content") or "")
        msg_type = str(p.get("type") or "text")
        if not conv_id or not message_id or not sender_user_id:
            log.debug("DM_MESSAGE missing required field: %s", p)
            return
        if msg_type not in MESSAGE_TYPES:
            msg_type = "text"

        msg = ConversationMessage(
            id=message_id,
            conversation_id=conv_id,
            sender_user_id=sender_user_id,
            content=content,
            created_at=parse_iso8601_lenient(p.get("occurred_at")),
            type=msg_type,
            media_url=p.get("media_url"),
        )
        await self._conversation_repo.save_message(msg)

        recipients = tuple(p.get("recipient_user_ids") or ())
        await self._bus.publish(
            DmMessageCreated(
                conversation_id=conv_id,
                message_id=message_id,
                sender_user_id=sender_user_id,
                sender_display_name=str(p.get("sender_display_name") or sender_user_id),
                recipient_user_ids=tuple(str(r) for r in recipients),
                content=content,
            )
        )

    async def _on_dm_deleted(self, event: "FederationEvent") -> None:
        message_id = str(event.payload.get("message_id") or "")
        if not message_id:
            return
        await self._conversation_repo.soft_delete_message(message_id)

    async def _on_dm_reaction(self, event: "FederationEvent") -> None:
        p = event.payload
        message_id = str(p.get("message_id") or "")
        user_id = str(p.get("user_id") or "")
        emoji = str(p.get("emoji") or "")
        action = str(p.get("action") or "add")
        if not message_id or not user_id or not emoji:
            return
        if action == "remove":
            await self._conversation_repo.remove_reaction(message_id, user_id, emoji)
        else:
            await self._conversation_repo.add_reaction(message_id, user_id, emoji)

    # ── Space content handlers ─────────────────────────────────────────

    async def _on_space_post_created(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        post = self._post_from_payload(event.payload)
        if post is None:
            return
        await self._space_post_repo.save(space_id, post)
        await self._bus.publish(SpacePostCreated(post=post, space_id=space_id))

    async def _on_space_post_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        post_id = str(p.get("id") or p.get("post_id") or "")
        new_content = str(p.get("content") or "")
        if not post_id:
            return
        await self._space_post_repo.edit(post_id, new_content)

    async def _on_space_post_deleted(self, event: "FederationEvent") -> None:
        post_id = str(event.payload.get("post_id") or event.payload.get("id") or "")
        if not post_id:
            return
        moderated_by = event.payload.get("moderated_by")
        await self._space_post_repo.soft_delete(
            post_id,
            moderated_by=str(moderated_by) if moderated_by else None,
        )
        await self._bus.publish(PostDeleted(post_id=post_id))

    async def _on_space_comment_added(self, event: "FederationEvent") -> None:
        p = event.payload
        post_id = str(p.get("post_id") or "")
        comment_id = str(p.get("comment_id") or p.get("id") or "")
        author = str(p.get("author") or "")
        if not post_id or not comment_id or not author:
            return
        comment_type_str = str(p.get("type") or "text")
        try:
            comment_type = CommentType(comment_type_str)
        except ValueError:
            comment_type = CommentType.TEXT
        comment = Comment(
            id=comment_id,
            post_id=post_id,
            author=author,
            type=comment_type,
            created_at=parse_iso8601_lenient(p.get("occurred_at")),
            parent_id=p.get("parent_id"),
            content=p.get("content") or "",
            media_url=p.get("media_url"),
        )
        await self._space_post_repo.add_comment(comment)
        await self._space_post_repo.increment_comment_count(post_id)
        await self._bus.publish(
            CommentAdded(
                post_id=post_id,
                comment=comment,
                space_id=str(p.get("space_id") or event.space_id or "") or None,
            ),
        )

    async def _on_space_comment_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        comment_id = str(p.get("id") or p.get("comment_id") or "")
        content = p.get("content")
        if not comment_id or content is None:
            return
        await self._space_post_repo.edit_comment(comment_id, str(content))
        refreshed = await self._space_post_repo.get_comment(comment_id)
        if refreshed is None:
            return
        await self._bus.publish(
            CommentUpdated(
                post_id=refreshed.post_id,
                comment=refreshed,
                space_id=str(p.get("space_id") or event.space_id or "") or None,
            ),
        )

    async def _on_space_comment_deleted(self, event: "FederationEvent") -> None:
        p = event.payload
        comment_id = str(p.get("comment_id") or p.get("id") or "")
        post_id = str(p.get("post_id") or "")
        if not comment_id or not post_id:
            return
        await self._space_post_repo.soft_delete_comment(comment_id)
        await self._space_post_repo.decrement_comment_count(post_id)
        await self._bus.publish(
            CommentDeleted(
                post_id=post_id,
                comment_id=comment_id,
                space_id=str(p.get("space_id") or event.space_id or "") or None,
            ),
        )

    # ── Report handler ─────────────────────────────────────────────────

    async def _on_space_report(self, event: "FederationEvent") -> None:
        """A peer's member reported content we host — persist locally."""
        if self._report_service is None:
            log.debug("SPACE_REPORT received but no ReportService attached")
            return
        p = event.payload
        await self._report_service.create_report_from_remote(
            reporter_user_id=str(p.get("reporter_user_id") or ""),
            reporter_instance_id=event.from_instance,
            target_type=str(p.get("target_type") or ""),
            target_id=str(p.get("target_id") or ""),
            category=str(p.get("category") or ""),
            notes=p.get("notes"),
        )

    # ── Space membership handlers ──────────────────────────────────────

    async def _on_space_member_joined(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        if not space_id or not user_id:
            return
        role = str(event.payload.get("role") or "member")
        joined_at = event.payload.get("occurred_at") or _now_iso()
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=role,
            joined_at=str(joined_at),
        )
        await self._space_repo.save_member(member)

    async def _on_space_member_left(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        if not space_id or not user_id:
            return
        await self._space_repo.delete_member(space_id, user_id)

    async def _on_space_member_profile_updated(
        self,
        event: "FederationEvent",
    ) -> None:
        p = event.payload
        space_id = event.space_id or str(p.get("space_id") or "")
        user_id = str(p.get("user_id") or "")
        if not space_id or not user_id:
            return
        member = await self._space_repo.get_member(space_id, user_id)
        if member is None:
            # Unknown member on this side — skip silently; a membership
            # event will catch up eventually.
            return
        picture_hash = p.get("picture_hash")
        bytes_b64 = p.get("picture_webp_base64")
        if bytes_b64 and self._profile_picture_repo is not None:
            try:
                raw = base64.b64decode(bytes_b64)
                webp = await ImageProcessor().generate_thumbnail(
                    raw,
                    size=PROFILE_PICTURE_MAX_DIMENSION,
                )
                local_hash = compute_picture_hash(webp)
                await self._profile_picture_repo.set_member_picture(
                    space_id,
                    user_id,
                    bytes_webp=webp,
                    hash=local_hash,
                    width=PROFILE_PICTURE_MAX_DIMENSION,
                    height=PROFILE_PICTURE_MAX_DIMENSION,
                )
                picture_hash = local_hash
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "SPACE_MEMBER_PROFILE_UPDATED: bad blob for %s in %s: %s",
                    user_id,
                    space_id,
                    exc,
                )
        await self._space_repo.set_member_profile(
            space_id,
            user_id,
            space_display_name=p.get("space_display_name"),
            picture_hash=picture_hash,
        )
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=p.get("space_display_name"),
                picture_hash=picture_hash,
            )
        )

    # ── User-profile handlers ──────────────────────────────────────────

    async def _on_users_sync(self, event: "FederationEvent") -> None:
        users = event.payload.get("users") or []
        if not isinstance(users, list):
            return
        for u in users:
            await self._upsert_remote_user(event.from_instance, u)

    async def _on_user_updated(self, event: "FederationEvent") -> None:
        await self._upsert_remote_user(event.from_instance, event.payload)

    async def _on_user_removed(self, event: "FederationEvent") -> None:
        """Mark a remote user as deprovisioned locally.

        The row stays in ``remote_users`` so historical posts / comments
        keep resolving to a display name, but member-list and
        autocomplete queries filter it out via
        ``list_remote_for_instance``.
        """
        user_id = str(event.payload.get("user_id") or "")
        if not user_id:
            return
        log.info("USER_REMOVED: flagging remote user %s as deprovisioned", user_id)
        await self._user_repo.mark_remote_deprovisioned(user_id)

    async def _on_user_status_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        user_id = str(p.get("user_id") or "")
        if not user_id:
            return
        status: UserStatus | None
        if p.get("status_cleared"):
            status = None
        else:
            emoji = p.get("emoji")
            text = p.get("text")
            if emoji is None and text is None:
                status = None
            else:
                status = UserStatus(
                    emoji=str(emoji) if emoji else None,
                    text=str(text) if text else None,
                    expires_at=str(p["expires_at"]) if p.get("expires_at") else None,
                )
        await self._bus.publish(UserStatusChanged(user_id=user_id, status=status))

    # ── Helpers ────────────────────────────────────────────────────────

    async def _upsert_remote_user(self, instance_id: str, payload: dict) -> None:
        user_id = str(payload.get("user_id") or "")
        username = str(payload.get("username") or payload.get("remote_username") or "")
        if not user_id or not username:
            return
        picture_hash = payload.get("picture_hash")

        # If the peer shipped fresh picture bytes, revalidate and store
        # locally. We trust the signature on the envelope (§24.11) but
        # still re-run the image through ImageProcessor so a malicious
        # peer can't plant arbitrary bytes in the blob table.
        bytes_b64 = payload.get("picture_webp_base64")
        if bytes_b64 and self._profile_picture_repo is not None:
            try:
                raw = base64.b64decode(bytes_b64)
                webp = await ImageProcessor().generate_thumbnail(
                    raw,
                    size=PROFILE_PICTURE_MAX_DIMENSION,
                )
                local_hash = compute_picture_hash(webp)
                await self._profile_picture_repo.set_user_picture(
                    user_id,
                    bytes_webp=webp,
                    hash=local_hash,
                    width=PROFILE_PICTURE_MAX_DIMENSION,
                    height=PROFILE_PICTURE_MAX_DIMENSION,
                )
                picture_hash = local_hash
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "USER_UPDATED: rejected remote picture for %s: %s",
                    user_id,
                    exc,
                )

        remote = RemoteUser(
            user_id=user_id,
            instance_id=instance_id,
            remote_username=username,
            display_name=str(payload.get("display_name") or username),
            picture_hash=picture_hash,
            bio=payload.get("bio"),
            public_key=payload.get("public_key"),
            synced_at=_now_iso(),
        )
        await self._user_repo.upsert_remote(remote)

    def _post_from_payload(self, payload: dict) -> Post | None:
        post_id = str(payload.get("id") or payload.get("post_id") or "")
        author = str(payload.get("author") or "")
        if not post_id or not author:
            return None
        type_str = str(payload.get("type") or "text")
        try:
            post_type = PostType(type_str)
        except ValueError:
            post_type = PostType.TEXT
        return Post(
            id=post_id,
            author=author,
            type=post_type,
            content=payload.get("content"),
            media_url=payload.get("media_url"),
            file_meta=None,
            created_at=parse_iso8601_lenient(payload.get("occurred_at")),
        )
