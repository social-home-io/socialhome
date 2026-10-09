"""A space's chat — one system group chat per space, for its writers.

Built on group-DM storage (``conversations`` with ``system_scope =
'space'`` and ``space_id``, migration 0082): messages, reactions, edit /
delete, mentions, unread, mute and notification level all go through the
existing ``/api/conversations/{id}/...`` routes, authorised live by
:class:`~socialhome.services.system_chat_policy.SpaceChatAccess` (a local
writer seat — never a follower —, ``features.chat`` on, not banned, the
space not dissolved; posting also needs it not archived). The chat travels
between member households as the v_55 ``SPACE_CHAT_*`` events
(:class:`~socialhome.services.space_chat_outbound.SpaceChatOutbound`,
:mod:`~socialhome.services.federation_inbound.space_chat`); each household
keeps its own conversation for the space and never names it on the wire.

This service owns the chat's lifecycle on this household:

* :meth:`ensure_chat` creates the conversation once (idempotent through
  the migration-0082 one-chat-per-space index).
* :meth:`reconcile` makes the local seat rows match who may read the chat
  right now. Seats hold only per-user state (read watermark, mute, level —
  ``mentions`` by default); access is the policy's, so a seat a
  reconciliation has not caught up on yet never grants anything.
* The reconciler runs on :class:`SpaceMemberJoined` /
  :class:`SpaceMemberLeft`, on :class:`SpaceConfigChanged` (bans, role
  changes, feature toggles) and :class:`SpaceFeaturesApplied` (the host's
  config arriving), on :class:`RemoteSpaceMemberBanned`, and lazily on every
  ``GET /api/spaces/{id}/chat``. A purged space takes its chat with it
  (``ON DELETE CASCADE``).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

from ..domain.conversation import (
    Conversation,
    SpaceChatSeatUnread,
    SystemChatScope,
    SystemChatSummary,
    mute_active,
)
from ..domain.events import (
    RemoteSpaceMemberBanned,
    SpaceConfigChanged,
    SpaceFeaturesApplied,
    SpaceMemberJoined,
    SpaceMemberLeft,
)
from ..domain.space import WRITER_ROLES, SpaceRole
from ..infrastructure.event_bus import EventBus
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .dm_mentions import DmMentionResolver
from .system_chat_policy import SpaceChatAccess, SystemChatPolicy

log = logging.getLogger(__name__)


class SpaceChatService:
    """Create a space's chat and keep its seats in step with who may read it."""

    __slots__ = ("_convos", "_users", "_spaces", "_access", "_policy", "_bus")

    def __init__(
        self,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        space_repo: AbstractSpaceRepo,
        access: SpaceChatAccess,
        policy: SystemChatPolicy,
        bus: EventBus | None = None,
    ) -> None:
        self._convos = conversation_repo
        self._users = user_repo
        self._spaces = space_repo
        self._access = access
        self._policy = policy
        self._bus = bus

    def wire(self) -> None:
        """Reconcile seats whenever who may read a space's chat can change."""
        if self._bus is None:
            return
        self._bus.subscribe(SpaceMemberJoined, self._on_space_event)
        self._bus.subscribe(SpaceMemberLeft, self._on_space_event)
        self._bus.subscribe(SpaceConfigChanged, self._on_space_event)
        self._bus.subscribe(SpaceFeaturesApplied, self._on_space_event)
        self._bus.subscribe(RemoteSpaceMemberBanned, self._on_space_event)

    async def get_chat(self, space_id: str) -> Conversation | None:
        """The space's chat, or ``None`` before it was first created."""
        return await self._convos.get_space_chat(space_id)

    async def ensure_chat(self, space_id: str) -> Conversation:
        """The space's chat, created on first use. :class:`KeyError` for a
        space this household does not hold (or that was dissolved)."""
        chat = await self._convos.get_space_chat(space_id)
        if chat is not None:
            return chat
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        return await self._convos.create_system_chat(
            SystemChatScope.SPACE, space_id=space_id
        )

    async def reconcile(
        self, space_id: str, *, seat_at: str | None = None
    ) -> Conversation:
        """Seat every local user who may read the chat right now, take out
        everyone else; return the chat.

        ``seat_at`` (UTC ISO 8601) is the read watermark a NEW seat starts
        at — default now, so a newcomer inherits no backlog. The first
        message that arrives for a space passes a stamp just before itself,
        so the chat it creates shows it as unread.
        """
        chat = await self.ensure_chat(space_id)
        await self._reconcile_seats(chat, seat_at=seat_at)
        return chat

    async def _reconcile_seats(
        self, chat: Conversation, *, seat_at: str | None = None
    ) -> None:
        allowed: set[str] = set()
        for member in await self._spaces.list_members(chat.space_id or ""):
            user = await self._users.get_by_user_id(member.user_id)
            if user is not None and await self._policy.can_read(chat, user.user_id):
                allowed.add(user.username)
        seats = {
            m.username: m.deleted_at is None
            for m in await self._convos.list_members(chat.id)
        }
        for username in sorted(allowed):
            if not seats.get(username, False):
                await self._convos.upsert_seat(
                    chat.id,
                    username,
                    notif_level=self._access.default_notif_level,
                    at=seat_at,
                )
        for username, seated in seats.items():
            if seated and username not in allowed:
                await self._convos.remove_seat(chat.id, username)

    async def summary(self, space_id: str, username: str) -> SystemChatSummary:
        """What the space's Chat switch needs for ``username``.

        :class:`KeyError` (404) when the space is unknown or dissolved here,
        or ``username`` holds no seat in it (or is banned) — the chat of a
        space you are not in does not exist for you. A **follower** gets
        ``enabled=False`` (followers neither read nor post the chat), as
        does everyone while the space's admins turned the chat off; nothing
        is created then.
        """
        user = await self._users.get(username)
        space = await self._spaces.get(space_id)
        if user is None or space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        member = await self._spaces.get_member(space_id, user.user_id)
        if member is None or await self._spaces.is_banned(space_id, user.user_id):
            raise KeyError(f"space {space_id!r} not found")
        if str(member.role) == SpaceRole.SUBSCRIBER.value or not space.features.chat:
            return SystemChatSummary(enabled=False)
        chat = await self.reconcile(space_id)
        seat = next(
            (
                m
                for m in await self._convos.list_members(chat.id)
                if m.username == username and m.deleted_at is None
            ),
            None,
        )
        if seat is None:
            # The policy refused this reader (e.g. an inactive account).
            return SystemChatSummary(enabled=False)
        muted_until = (
            seat.muted_until
            if mute_active(seat.muted_until, now=datetime.now(timezone.utc))
            else None
        )
        return SystemChatSummary(
            enabled=True,
            conversation_id=chat.id,
            # At "Only @mentions" only the unread messages that mention the
            # viewer count — the badge never shows chatter they muted.
            unread=await DmMentionResolver(
                self._convos, self._users, self._policy
            ).unread_for(chat.id, username, user.user_id, seat.notif_level),
            notif_level=seat.notif_level,
            muted_until=muted_until,
            last_read_at=seat.last_read_at,
        )

    async def unread_by_space(self, username: str) -> dict[str, SpaceChatSeatUnread]:
        """``username``'s space chats that have something to show, keyed by
        space — the spaces list's per-space unread dot.

        One repo query reads every live seat with its raw unread count
        (writer role, not banned, chat on, space not dissolved — the
        :class:`SpaceChatAccess` read rules). Each count is then narrowed
        to what the seat hears, the summary's rule: ``0`` while muted, and
        at ``mentions`` only the unread messages that @-mention the viewer.
        The mention count parses message text, so it runs only for a
        ``mentions`` seat that has unread messages at all (each bounded by
        ``list_unread_contents``' 500-message cap, its roster read in two
        batched queries — no per-member lookups). Seats with nothing
        unread are still returned (with ``0``) so the SPA knows their
        level and mute for live updates; a chat never opened yet has no
        seat and is absent.
        """
        user = await self._users.get(username)
        if user is None or not user.is_active():
            return {}
        rows = await self._convos.list_space_chat_unread(
            username, writer_roles=frozenset(r.value for r in WRITER_ROLES)
        )
        now = datetime.now(timezone.utc)
        resolver = DmMentionResolver(self._convos, self._users, self._policy)
        out: dict[str, SpaceChatSeatUnread] = {}
        for row in rows:
            muted = mute_active(row.muted_until, now=now)
            unread = row.unread
            if muted:
                unread = 0
            elif unread and row.notif_level == "mentions":
                unread = await resolver.unread_for(
                    row.conversation_id, username, user.user_id, row.notif_level
                )
            out[row.space_id] = replace(
                row,
                unread=unread,
                muted_until=row.muted_until if muted else None,
            )
        return out

    async def _on_space_event(
        self,
        event: SpaceMemberJoined
        | SpaceMemberLeft
        | SpaceConfigChanged
        | SpaceFeaturesApplied
        | RemoteSpaceMemberBanned,
    ) -> None:
        chat = await self._convos.get_space_chat(event.space_id)
        if chat is None:
            # Created — with every reader seated — on first open or on the
            # first message that arrives for it.
            return
        await self._reconcile_seats(chat)
