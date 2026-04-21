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

from ..domain.federation import FederationEventType
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


#: How long since last keystroke before the indicator counts as expired.
TYPING_TTL_SECONDS: float = 6.0


@dataclass(slots=True)
class _TypingState:
    """Per-(conversation, user) timestamp of last typing event."""

    last_seen_at: float


class TypingService:
    """Relay + dedup typing indicators across conversation members."""

    __slots__ = (
        "_convo_repo",
        "_user_repo",
        "_ws",
        "_federation",
        "_own_instance_id",
        "_active",
    )

    def __init__(
        self,
        *,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        ws_manager,
        federation_service=None,
        own_instance_id: str = "",
    ) -> None:
        self._convo_repo = conversation_repo
        self._user_repo = user_repo
        self._ws = ws_manager
        self._federation = federation_service
        self._own_instance_id = own_instance_id
        # (conversation_id, user_id) → _TypingState
        self._active: dict[tuple[str, str], _TypingState] = {}

    def attach_federation(self, federation_service, own_instance_id: str) -> None:
        self._federation = federation_service
        self._own_instance_id = own_instance_id

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
        local_targets: list[str] = []
        for m in members:
            uid = await self._resolve_user_id(m)
            if uid and uid != sender_user_id:
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
        delivery count.
        """
        payload = event.payload or {}
        cid = payload.get("conversation_id") or ""
        sender_uid = payload.get("sender_user_id") or ""
        sender_username = payload.get("sender_username") or ""
        if not cid or not sender_uid:
            return 0
        members = await self._convo_repo.list_members(cid)
        local_targets: list[str] = []
        for m in members:
            uid = await self._resolve_user_id(m)
            if uid and uid != sender_uid:
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
        seen_instances: set[str] = set()
        for rm in remote_members:
            inst = getattr(rm, "instance_id", None)
            if not inst or inst == self._own_instance_id or inst in seen_instances:
                continue
            seen_instances.add(inst)
            try:
                await self._federation.send_event(
                    to_instance_id=inst,
                    event_type=FederationEventType.DM_USER_TYPING,
                    payload={
                        "conversation_id": conversation_id,
                        "sender_user_id": sender_user_id,
                        "sender_username": sender_username,
                    },
                )
            except Exception as exc:  # pragma: no cover
                log.debug("typing: failed to relay to %s: %s", inst, exc)
