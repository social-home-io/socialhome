"""The household chat — one system group chat of every local user.

Built on group-DM storage (``conversations`` with ``system_scope =
'household'``): messages, reactions, edit / delete, mentions, unread,
mute and notification level all go through the existing
``/api/conversations/{id}/...`` routes, authorised live by
:class:`~socialhome.services.system_chat_policy.SystemChatPolicy`. It is
never federated and never listed in the DM inbox or the DM badge.

This service owns the chat's lifecycle:

* :meth:`ensure_chat` creates the conversation once (idempotent through
  the migration-0082 unique index).
* :meth:`reconcile` makes the seat rows match the active local users —
  seats hold only per-user state (read watermark, mute, level), so a seat
  added late or removed late never changes who may read or write.
* The reconciler runs on :class:`UserProvisioned` /
  :class:`UserDeprovisioned` and lazily on every ``GET
  /api/household/chat`` (which also catches a reactivated account, for
  which no event is published).
* ``feat_household_chat`` off: :meth:`summary` reports ``enabled=False``
  and creates nothing; the policy refuses every read and write; the data
  is kept.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..domain.conversation import (
    Conversation,
    SystemChatScope,
    SystemChatSummary,
    mute_active,
)
from ..domain.events import UserDeprovisioned, UserProvisioned
from ..infrastructure.event_bus import EventBus
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo
from .dm_mentions import DmMentionResolver
from .system_chat_policy import HouseholdChatAccess

log = logging.getLogger(__name__)


class HouseholdChatService:
    """Create the household chat and keep its seats in step with the users."""

    __slots__ = ("_convos", "_users", "_access", "_bus")

    def __init__(
        self,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        access: HouseholdChatAccess,
        bus: EventBus | None = None,
    ) -> None:
        self._convos = conversation_repo
        self._users = user_repo
        self._access = access
        self._bus = bus

    def wire(self) -> None:
        """Reconcile seats on user provisioning / deprovisioning."""
        if self._bus is None:
            return
        self._bus.subscribe(UserProvisioned, self._on_user_provisioned)
        self._bus.subscribe(UserDeprovisioned, self._on_user_deprovisioned)

    async def ensure_chat(self) -> Conversation:
        """The household chat, created on first use."""
        chat = await self._convos.get_household_chat()
        if chat is not None:
            return chat
        return await self._convos.create_system_chat(SystemChatScope.HOUSEHOLD)

    async def reconcile(self) -> Conversation:
        """Seat every active local user, take out everyone else; return the chat."""
        chat = await self.ensure_chat()
        active = {u.username for u in await self._users.list_active()}
        seats = {
            m.username: m.deleted_at is None
            for m in await self._convos.list_members(chat.id)
        }
        for username in sorted(active):
            if not seats.get(username, False):
                await self._convos.upsert_seat(
                    chat.id,
                    username,
                    notif_level=self._access.default_notif_level,
                )
        for username, seated in seats.items():
            if seated and username not in active:
                await self._convos.remove_seat(chat.id, username)
        return chat

    async def summary(self, username: str) -> SystemChatSummary:
        """What the feed's Chat tab needs for ``username``.

        ``enabled=False`` (nothing created) while the household turned the
        chat off. :class:`PermissionError` for an account that is not an
        active local user.
        """
        user = await self._users.get(username)
        if user is None or user.state != "active" or user.deleted_at is not None:
            raise PermissionError("not a member of the household chat")
        if not await self._access.enabled():
            return SystemChatSummary(enabled=False)
        chat = await self.reconcile()
        seat = next(
            (
                m
                for m in await self._convos.list_members(chat.id)
                if m.username == username and m.deleted_at is None
            ),
            None,
        )
        muted_until = (
            seat.muted_until
            if seat is not None
            and mute_active(seat.muted_until, now=datetime.now(timezone.utc))
            else None
        )
        return SystemChatSummary(
            enabled=True,
            conversation_id=chat.id,
            # At "Only @mentions" only the unread messages that mention the
            # viewer count — the badge never shows chatter they muted.
            unread=await DmMentionResolver(self._convos, self._users).unread_for(
                chat.id,
                username,
                user.user_id,
                seat.notif_level if seat is not None else None,
            ),
            notif_level=seat.notif_level if seat is not None else None,
            muted_until=muted_until,
            last_read_at=seat.last_read_at if seat is not None else None,
        )

    async def _on_user_provisioned(self, event: UserProvisioned) -> None:
        chat = await self._convos.get_household_chat()
        if chat is None:
            # Created — with every user seated — on first open.
            return
        await self._convos.upsert_seat(
            chat.id,
            event.username,
            notif_level=self._access.default_notif_level,
        )

    async def _on_user_deprovisioned(self, event: UserDeprovisioned) -> None:
        chat = await self._convos.get_household_chat()
        if chat is None:
            return
        await self._convos.remove_seat(chat.id, event.username)
