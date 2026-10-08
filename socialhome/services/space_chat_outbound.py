"""Outbound federation for a space's chat (v_55).

Subscribes to the conversation bus events — :class:`DmMessageCreated`,
:class:`DmMessageUpdated` (a sender's edit), :class:`DmMessageDeleted` and
:class:`DmMessageReactionChanged` — and, for a **space** chat only (never a
DM, never the household chat), ships the matching ``SPACE_CHAT_*`` event
to the space's other member households:

* **Encryption-first.** The whole payload is encrypted per peer by
  :meth:`FederationService.send_event` (the envelope's plaintext carries
  only the routing fields — event type, households, ``space_id``). The
  payload names the space, never a conversation id: each household keeps
  its own chat for the space and maps ``space_id`` to it.
* **Writers only.** :meth:`FederationService.broadcast_to_space_members`
  is narrowed with ``only_instances`` to the households holding at least
  one live WRITER seat (owner / admin / moderator / member) by this
  household's roster mirror (:meth:`SpaceChatAudience.writer_households`),
  so a household whose people only follow the space receives nothing. A
  household with no live roster row yet (gossip still on its way) is left
  out too — fail closed; the §25.6 catch-up fills it in later.
* **Mesh / relay.** Each per-peer send rides ``send_with_mesh_fallback``:
  a non-paired member is reached under ``SPACE_ROUTED`` (sealed end to end,
  so a relay household sees ciphertext only), a link-joined member through
  its ``space_session`` relay seat — exactly like posts and comments.
* **Version gate.** Each target must be at ``MIN_FOR_SPACE_CHAT`` by
  :meth:`FederationService.space_member_supports` — a paired household by
  its own advertisement, a mesh-only member by the version it claimed over
  the mesh — so a member household below v_55 (or one whose version we do
  not know) is skipped silently (no fallback).
* **No echo.** An event that arrived from another household carries
  ``origin_instance_id`` and is never re-broadcast.

Read receipts, delivery state and typing stay local — nothing here sends
them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.conversation import SystemChatScope
from ..domain.events import (
    DmMessageCreated,
    DmMessageDeleted,
    DmMessageReactionChanged,
    DmMessageUpdated,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import WRITER_ROLES
from ..infrastructure.event_bus import EventBus
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)

_WRITER_ROLE_VALUES: frozenset[str] = frozenset(r.value for r in WRITER_ROLES)


class SpaceChatAudience:
    """Which member households may receive a space's chat: those holding at
    least one live WRITER seat, by this household's roster mirror.

    One rule for the live fan-out (:class:`SpaceChatOutbound`) and the
    §25.6 catch-up (the ``chat_messages`` sync resource): a follower-only
    household never gets a chat message, live or streamed.
    """

    __slots__ = ("_seats", "_own_instance_id")

    def __init__(
        self,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        *,
        own_instance_id: str,
    ) -> None:
        self._seats = remote_member_repo
        self._own_instance_id = own_instance_id

    async def writer_households(self, space_id: str) -> frozenset[str]:
        """Every other household holding a live writer seat in ``space_id``."""
        ids = await self._seats.list_instances_with_roles(space_id, _WRITER_ROLE_VALUES)
        return frozenset(i for i in ids if i and i != self._own_instance_id)

    async def may_receive(self, space_id: str, instance_id: str) -> bool:
        """Whether household ``instance_id`` may receive ``space_id``'s chat."""
        return instance_id in await self.writer_households(space_id)


class SpaceChatOutbound:
    """Bus events of a space chat → ``SPACE_CHAT_*`` to its writer households."""

    __slots__ = ("_bus", "_federation", "_convos", "_audience")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        conversation_repo: AbstractConversationRepo,
        audience: SpaceChatAudience,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._convos = conversation_repo
        self._audience = audience

    def wire(self) -> None:
        self._bus.subscribe(DmMessageCreated, self._on_created)
        self._bus.subscribe(DmMessageUpdated, self._on_updated)
        self._bus.subscribe(DmMessageDeleted, self._on_deleted)
        self._bus.subscribe(DmMessageReactionChanged, self._on_reaction)

    async def _space_of(self, conversation_id: str) -> str | None:
        """The space a conversation is the chat of; ``None`` for any other."""
        conv = await self._convos.get(conversation_id)
        if conv is None or conv.system_scope is not SystemChatScope.SPACE:
            return None
        return conv.space_id

    async def _on_created(self, event: DmMessageCreated) -> None:
        if event.system_scope != SystemChatScope.SPACE.value:
            return
        if event.origin_instance_id is not None:
            return
        space_id = await self._space_of(event.conversation_id)
        if space_id is None:
            return
        await self._broadcast(
            space_id,
            FederationEventType.SPACE_CHAT_MESSAGE_CREATED,
            {
                "space_id": space_id,
                "message_id": event.message_id,
                "author_user_id": event.sender_user_id,
                "content": event.content,
                "reply_to_id": event.reply_to_id,
                "created_at": event.occurred_at.isoformat(),
            },
        )

    async def _on_updated(self, event: DmMessageUpdated) -> None:
        if not event.is_edit or event.origin_instance_id is not None:
            return
        space_id = await self._space_of(event.conversation_id)
        if space_id is None:
            return
        await self._broadcast(
            space_id,
            FederationEventType.SPACE_CHAT_MESSAGE_UPDATED,
            {
                "space_id": space_id,
                "message_id": event.message_id,
                "author_user_id": event.sender_user_id,
                "content": event.content,
                "edited_at": event.edited_at.isoformat(),
            },
        )

    async def _on_deleted(self, event: DmMessageDeleted) -> None:
        if event.system_scope != SystemChatScope.SPACE.value:
            return
        if event.origin_instance_id is not None:
            return
        space_id = await self._space_of(event.conversation_id)
        if space_id is None:
            return
        await self._broadcast(
            space_id,
            FederationEventType.SPACE_CHAT_MESSAGE_DELETED,
            {
                "space_id": space_id,
                "message_id": event.message_id,
                "author_user_id": event.sender_user_id,
                "actor_user_id": event.actor_user_id,
            },
        )

    async def _on_reaction(self, event: DmMessageReactionChanged) -> None:
        if event.origin_instance_id is not None:
            return
        space_id = await self._space_of(event.conversation_id)
        if space_id is None:
            return
        await self._broadcast(
            space_id,
            FederationEventType.SPACE_CHAT_REACTION,
            {
                "space_id": space_id,
                "message_id": event.message_id,
                "user_id": event.user_id,
                "emoji": event.emoji,
                "action": event.action,
            },
        )

    async def _broadcast(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        targets = {
            iid
            for iid in await self._audience.writer_households(space_id)
            if await self._federation.space_member_supports(
                iid, min_version=FederationCapability.MIN_FOR_SPACE_CHAT
            )
        }
        if not targets:
            return
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                event_type,
                payload,
                only_instances=targets,
            )
        except Exception:
            log.exception(
                "%s broadcast failed for space=%s message=%s",
                event_type.value,
                space_id,
                payload.get("message_id"),
            )
