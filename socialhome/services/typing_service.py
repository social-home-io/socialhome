"""TypingService — fan typing indicators across local + remote conversation members.

When a client sends a ``typing`` WS frame:

1. WS handler calls :meth:`TypingService.user_started_typing`.
2. Service looks up the conversation members (local + remote).
3. Local members get a ``conversation.user_typing`` WS event.
4. For each remote instance with members in the conversation, we ship a
   ``DM_USER_TYPING`` federation event so its WS clients can do the same.

Indicators auto-expire after 6 seconds with no further activity — the
sender is expected to re-emit ``typing`` while typing continues. The
caller treats absence-of-event as "stopped typing".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.federation import FederationEventType
from ..federation.dm_scope import DmScope, refuse
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo
from .dm_audience import local_audience
from .peer_outbound import SingleTargetSender
from .protection_gate import ProtectionGateMixin
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..domain.conversation import Conversation
    from .system_chat_policy import SystemChatPolicy
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


#: How long since last keystroke before the indicator counts as expired.
TYPING_TTL_SECONDS: float = 6.0


@dataclass(slots=True)
class _TypingState:
    """Per-(conversation, user) timestamp of last typing event."""

    last_seen_at: float


class TypingService(VisibilityMixin, SingleTargetSender, ProtectionGateMixin):
    """Relay + dedup typing indicators across conversation members.

    Never across a guardian block (§CP.F2): a blocked person's typing isn't
    shown to the protected account, nor the other way round — locally, from
    another household, or to the blocked person's household.
    """

    __slots__ = (
        "_child_protection",
        "_convo_repo",
        "_user_repo",
        "_dm_scope",
        "_space_repo",
        "_ws",
        "_federation",
        "_own_instance_id",
        "_active",
        "_active_comments",
        "_system_chats",
    )

    def __init__(
        self,
        *,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        ws_manager,
        federation_service=None,
        own_instance_id: str = "",
        space_repo: "AbstractSpaceRepo | None" = None,
        visibility_repo: "AbstractPeerUserVisibilityRepo | None" = None,
    ) -> None:
        self._convo_repo = conversation_repo
        self._user_repo = user_repo
        self._dm_scope = DmScope(
            conversation_repo=conversation_repo,
            user_repo=user_repo,
        )
        self._space_repo = space_repo
        self._ws = ws_manager
        self._federation = federation_service
        self._own_instance_id = own_instance_id
        self._visibility_repo = visibility_repo
        self._child_protection = None
        # (conversation_id, user_id) → _TypingState
        self._active: dict[tuple[str, str], _TypingState] = {}
        # (post_id, user_id) → _TypingState  for comment-thread typing
        # (a separate keyspace from conversation typing so the two
        # never alias on a stray collision between an id pair).
        self._active_comments: dict[tuple[str, str], _TypingState] = {}
        #: Live read access to system chats (household / space chat);
        #: without it nobody types into one.
        self._system_chats: "SystemChatPolicy | None" = None

    def attach_system_chats(self, policy: "SystemChatPolicy") -> None:
        """Wire the system-chat policy: typing in the household (or a
        space's) chat is shown only while the typist may read it."""
        self._system_chats = policy

    def attach_federation(self, federation_service, own_instance_id: str) -> None:
        self._federation = federation_service
        self._own_instance_id = own_instance_id

    def attach_space_repo(self, space_repo: "AbstractSpaceRepo") -> None:
        """Wire :class:`AbstractSpaceRepo` post-construction.

        Tests may build a bare service first and attach later. The
        comment-typing fan-out needs the space-member list to scope
        ``comment.user_typing`` to people who can actually see the
        space post.
        """
        self._space_repo = space_repo

    # ─── Local entry point: from WS handler ───────────────────────────────

    async def user_started_typing(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        sender_username: str,
        now: float | None = None,
    ) -> int:
        """Record + fan out a typing event. Returns count of WS deliveries."""
        now = now if now is not None else time.monotonic()
        conv = await self._convo_repo.get(conversation_id)
        if conv is not None and conv.is_system:
            return await self._system_chat_typing(
                conv, sender_user_id=sender_user_id, sender_username=sender_username
            )
        key = (conversation_id, sender_user_id)
        # Throttle: ignore duplicates within 1 second.
        existing = self._active.get(key)
        if existing is not None and (now - existing.last_seen_at) < 1.0:
            return 0
        self._active[key] = _TypingState(last_seen_at=now)
        self._gc(now)

        # Fan-out to local conversation members (excluding the sender).
        # ConversationMember stores ``username``; resolve to ``user_id``
        # for WS routing (some test fakes attach ``user_id`` directly,
        # which we honour as a fast path).
        members = await self._convo_repo.list_members(conversation_id)
        blocked = await self._guardian_block_counterparts(sender_user_id)
        local_targets: list[str] = []
        for m in members:
            uid = await self._resolve_user_id(m)
            if uid and uid != sender_user_id and uid not in blocked:
                local_targets.append(uid)
        delivered = await self._ws.broadcast_to_users(
            local_targets,
            {
                "type": "conversation.user_typing",
                "conversation_id": conversation_id,
                "sender_user_id": sender_user_id,
                "sender_username": sender_username,
            },
        )

        # Fan-out to remote instances that have members in this conversation.
        await self._fan_to_remote_members(
            conversation_id=conversation_id,
            sender_user_id=sender_user_id,
            sender_username=sender_username,
        )
        return delivered

    async def _system_chat_typing(
        self,
        conv: "Conversation",
        *,
        sender_user_id: str,
        sender_username: str,
    ) -> int:
        """Typing in a system chat: only from someone the policy lets read
        it right now (chat on, still a member), only to its live audience,
        never federated (household chat) — a space chat's typing would ride
        its own events."""
        if self._system_chats is None or not await self._system_chats.can_read(
            conv, sender_user_id
        ):
            return 0
        targets = await local_audience(
            self._convo_repo,
            self._user_repo,
            conv.id,
            actor_user_id=sender_user_id,
            withheld=await self._guardian_block_counterparts(sender_user_id),
        )
        if not targets:
            return 0
        return await self._ws.broadcast_to_users(
            list(targets),
            {
                "type": "conversation.user_typing",
                "conversation_id": conv.id,
                "sender_user_id": sender_user_id,
                "sender_username": sender_username,
            },
        )

    # ─── Comment-thread typing ────────────────────────────────────────────

    async def user_typing_on_comment(
        self,
        *,
        post_id: str,
        space_id: str | None,
        sender_user_id: str,
        sender_username: str,
        now: float | None = None,
    ) -> int:
        """Record + fan out a typing event on a comment thread.

        Scope:

        * ``space_id is None`` (household feed) → broadcast to every
          local connected user. Household-feed posts are visible to
          every household member, so a typing indicator reasonably
          reaches anyone watching the comment thread.
        * ``space_id is not None`` (space feed) → broadcast only to the
          space's local members, so the indicator never leaks past the
          space's audience.

        Returns the count of WS deliveries. Inbound throttle: ignore
        duplicate emits within 1 s. Cross-instance fan-out (federating
        typing on remote-instance space comments) is a follow-up; this
        first cut keeps it local-only since household + same-instance
        space members are the common case.
        """
        now = now if now is not None else time.monotonic()
        key = (post_id, sender_user_id)
        existing = self._active_comments.get(key)
        if existing is not None and (now - existing.last_seen_at) < 1.0:
            return 0
        self._active_comments[key] = _TypingState(last_seen_at=now)
        self._gc_comments(now)

        targets: list[str] = []
        if space_id is None:
            # Household scope — every local user is allowed to see.
            try:
                local_users = await self._user_repo.list_active()
            except Exception:
                local_users = []
            for u in local_users:
                if u.user_id and u.user_id != sender_user_id:
                    targets.append(u.user_id)
        elif self._space_repo is not None:
            try:
                members = await self._space_repo.list_members(space_id)
            except Exception:
                members = []
            for m in members:
                uid = getattr(m, "user_id", None)
                if uid and uid != sender_user_id:
                    targets.append(uid)
        # space_id set but space_repo unwired → no fan-out (defensive;
        # production wiring is in app.py and unconditional).

        return await self._ws.broadcast_to_users(
            targets,
            {
                "type": "comment.user_typing",
                "post_id": post_id,
                "space_id": space_id,
                "sender_user_id": sender_user_id,
                "sender_username": sender_username,
            },
        )

    def is_typing_on_comment(
        self,
        post_id: str,
        user_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        now = now if now is not None else time.monotonic()
        state = self._active_comments.get((post_id, user_id))
        if state is None:
            return False
        return (now - state.last_seen_at) <= TYPING_TTL_SECONDS

    def _gc_comments(self, now: float) -> None:
        cutoff = now - TYPING_TTL_SECONDS
        stale = [k for k, v in self._active_comments.items() if v.last_seen_at < cutoff]
        for k in stale:
            self._active_comments.pop(k, None)

    # ─── Internal: shared helpers ─────────────────────────────────────────

    async def _resolve_user_id(self, member) -> str | None:
        """Map a conversation member to a ``user_id``.

        The domain :class:`ConversationMember` holds ``username``; some
        test fakes attach ``user_id`` directly. Try the direct field
        first, then fall back to a user_repo lookup.
        """
        direct = getattr(member, "user_id", None)
        if direct:
            return direct
        username = getattr(member, "username", None)
        if not username:
            return None
        try:
            user = await self._user_repo.get(username)
        except Exception:
            return None
        return user.user_id if user else None

    # ─── Federation entry point: from FederationService dispatch ─────────

    async def handle_remote_typing(self, event) -> int:
        """Inbound DM_USER_TYPING from a remote instance.

        Forward to local members of the conversation. Returns local
        delivery count. Only a user of the sending household who is seated
        in the conversation can be shown typing; the frame carries the
        username this household holds for them, not the payload's.
        """
        payload = event.payload or {}
        cid = str(payload.get("conversation_id") or "")
        sender_uid = str(payload.get("sender_user_id") or "")
        if not cid or not sender_uid:
            return 0
        seat = await self._dm_scope.seat_of(event, cid, sender_uid)
        if seat is None:
            refuse(
                event,
                "typist is not a seated user of the sending household",
                conversation=cid,
                user=sender_uid,
            )
            return 0
        sender_username = seat.remote_username
        members = await self._convo_repo.list_members(cid)
        blocked = await self._guardian_block_counterparts(sender_uid)
        local_targets: list[str] = []
        for m in members:
            uid = await self._resolve_user_id(m)
            if uid and uid != sender_uid and uid not in blocked:
                local_targets.append(uid)
        return await self._ws.broadcast_to_users(
            local_targets,
            {
                "type": "conversation.user_typing",
                "conversation_id": cid,
                "sender_user_id": sender_uid,
                "sender_username": sender_username,
                "from_instance": event.from_instance,
            },
        )

    # ─── Inspection ───────────────────────────────────────────────────────

    def is_typing(
        self,
        conversation_id: str,
        user_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        now = now if now is not None else time.monotonic()
        state = self._active.get((conversation_id, user_id))
        if state is None:
            return False
        return (now - state.last_seen_at) <= TYPING_TTL_SECONDS

    def active_typers(
        self,
        conversation_id: str,
        *,
        now: float | None = None,
    ) -> list[str]:
        now = now if now is not None else time.monotonic()
        return [
            uid
            for (cid, uid), state in self._active.items()
            if cid == conversation_id
            and (now - state.last_seen_at) <= TYPING_TTL_SECONDS
        ]

    # ─── Internals ────────────────────────────────────────────────────────

    def _gc(self, now: float) -> None:
        """Purge entries older than the TTL."""
        cutoff = now - TYPING_TTL_SECONDS
        stale = [k for k, v in self._active.items() if v.last_seen_at < cutoff]
        for k in stale:
            self._active.pop(k, None)

    async def _fan_to_remote_members(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        sender_username: str,
    ) -> None:
        if self._federation is None:
            return
        try:
            remote_members = await self._convo_repo.list_remote_members(
                conversation_id,
            )
        except Exception:
            return
        blocked = await self._guardian_block_counterparts(sender_user_id)
        # A household whose every seat here is someone a guardian block
        # separates from the typist gets nothing (§CP.F2).
        wanted: set[str | None] = set()
        if blocked:
            for rm in remote_members:
                seat_uid = getattr(rm, "user_id", None)
                if seat_uid is None:
                    ru = await self._user_repo.get_remote_by_member(
                        rm.instance_id, rm.remote_username
                    )
                    seat_uid = ru.user_id if ru is not None else None
                if seat_uid not in blocked:
                    wanted.add(rm.instance_id)
        seen_instances: set[str] = set()
        for rm in remote_members:
            inst = getattr(rm, "instance_id", None)
            if not inst or inst == self._own_instance_id or inst in seen_instances:
                continue
            seen_instances.add(inst)
            if blocked and inst not in wanted:
                continue
            hidden = await self.hidden_for_peer(inst)
            if sender_user_id in hidden:
                continue
            await self.send_to_instance(
                inst,
                FederationEventType.DM_USER_TYPING,
                {
                    "conversation_id": conversation_id,
                    "sender_user_id": sender_user_id,
                    "sender_username": sender_username,
                },
            )
