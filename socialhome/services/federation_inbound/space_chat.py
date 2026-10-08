"""Inbound federation handlers for a space's chat (v_55).

Registers ``SPACE_CHAT_MESSAGE_CREATED`` / ``_UPDATED`` / ``_DELETED`` and
``SPACE_CHAT_REACTION`` on the event registry. By the time a handler runs,
the §24.11 pipeline has verified the envelope (signature, timestamp,
replay, ban, peer class — a link-joined household may send these —, the
writer gate — a follower-only household's writes die there — and the
archive gate, which still lets the delete through). Each handler then
re-decides everything about the PEOPLE and ROWS the payload names, on this
household's own facts:

* **The space and its chat here.** The payload's space must be the
  envelope's (:func:`resolve_space_id`), held here, not dissolved, with
  ``features.chat`` on, and — so a household whose people only follow the
  space never stores chat content — at least one local WRITER seat.
  Otherwise the event is dropped. Each household keeps its own chat for
  the space: the wire never names a conversation.
* **Create** — the message id must be owner-bound to the author in this
  space (:data:`SPACE_CHAT_MESSAGE_KIND`; a legacy-shaped id is refused,
  the kind is bound from its first release), and the author must be a
  writer the sender speaks for (:meth:`SpaceAuthorship.may_author_writer`:
  their live writer seat on the sender, or the host relaying a remote
  writer; never a local user, a follower, a banned user or the bot).
  Idempotent: an id held already is a no-op. Text only (v1); a reply
  names a message of the same chat, or is dropped.
* **Update** — the author's own edit only (:meth:`SpaceAuthorship.acts_for`
  with a writer seat, the row's stored sender).
* **Delete** — the author (any live seat), or content authority: a
  moderator / admin / the host acting as a named ``actor_user_id``
  (:meth:`SpaceAuthorship.moderates_as`).
* **Reaction** — the reactor's own, a writer seat on the sender.

Accepted events land in the space's conversation like local ones and
publish the same bus events (WS frames, notifications — mute, level and
mentions respected), stamped with ``origin_instance_id`` so the outbound
never echoes them. A guardian-blocked sender is withheld from the
protected reader (§CP.F2), as in every system chat.

The §25.6 catch-up (``chat_messages``) runs its records through
:meth:`SpaceChatInboundHandlers.apply_sync_records` — the create rule
above, with the providing household as the sender.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ...domain.conversation import ConversationMessage, SystemChatScope
from ...domain.events import (
    DmMessageCreated,
    DmMessageDeleted,
    DmMessageReactionChanged,
    DmMessageUpdated,
)
from ...domain.federation import FederationEvent, FederationEventType
from ...domain.space import WRITER_ROLES
from ...federation.owner_bound_id import (
    SPACE_CHAT_MESSAGE_KIND,
    OwnerBinding,
    check_owner_bound_id,
)
from ...federation.space_scope import log_not_applied, resolve_space_id
from ...infrastructure.event_bus import EventBus
from ...repositories.conversation_repo import AbstractConversationRepo
from ...repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ...repositories.space_repo import AbstractSpaceRepo
from ...repositories.user_repo import AbstractUserRepo
from ...utils.datetime import parse_iso8601_lenient
from ..dm_audience import local_audience
from ..dm_mentions import DmMentionResolver
from ..dm_service import MAX_DM_LENGTH
from ..protection_gate import ProtectionGateMixin
from ..space_chat_service import SpaceChatService
from ..system_chat_policy import SystemChatPolicy

if TYPE_CHECKING:
    from ...domain.conversation import Conversation
    from ...federation.federation_service import FederationService
    from ...federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)

_WRITER_ROLE_VALUES: frozenset[str] = frozenset(r.value for r in WRITER_ROLES)

#: Longest emoji glyph a reaction may carry (as :meth:`DmService.add_reaction`).
_MAX_EMOJI_LEN = 32


def _refuse(event: FederationEvent, what: str, row_id: str, reason: str) -> None:
    """WARNING for a refused write — never benign delivery noise."""
    log.warning(
        "%s from %s: %s %s refused — %s",
        event.event_type,
        event.from_instance,
        what,
        row_id,
        reason,
    )


def _bound_to(message_id: str, space_id: str, author: str) -> bool:
    """``message_id`` is owner-bound to ``author`` in ``space_id`` (the
    space chat binds every id from its first release — no legacy window)."""
    return (
        check_owner_bound_id(
            SPACE_CHAT_MESSAGE_KIND,
            message_id,
            space_id=space_id,
            owner_user_id=author,
        )
        is OwnerBinding.VALID
    )


def _sync_event(
    event_type: FederationEventType, space_id: str, payload: dict, *, provider: str
) -> FederationEvent:
    """A catch-up record dressed as the live event, the provider as sender."""
    return FederationEvent(
        msg_id=f"sync:{space_id}:{event_type.value}",
        event_type=event_type,
        from_instance=provider,
        to_instance="",
        timestamp="",
        payload=payload,
        space_id=space_id,
    )


def _str(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


class SpaceChatInboundHandlers(ProtectionGateMixin):
    """Register and run the ``SPACE_CHAT_*`` inbound handlers."""

    __slots__ = (
        "_bus",
        "_authorship",
        "_spaces",
        "_seats",
        "_convos",
        "_users",
        "_chats",
        "_policy",
        "_child_protection",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        authorship: "SpaceAuthorship | None",
        space_repo: AbstractSpaceRepo,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        chat_service: SpaceChatService,
        policy: SystemChatPolicy,
    ) -> None:
        self._bus = bus
        #: §24.11 authorship. ``None`` refuses every chat write (fail
        #: closed): without the roster mirror nothing binds an author to
        #: the sending household.
        self._authorship = authorship
        self._spaces = space_repo
        self._seats = remote_member_repo
        self._convos = conversation_repo
        self._users = user_repo
        self._chats = chat_service
        self._policy = policy
        self._child_protection = None

    def attach_to(self, federation_service: "FederationService") -> None:
        """Register the four handlers on the event registry."""
        registry = federation_service._event_registry
        registry.register(
            FederationEventType.SPACE_CHAT_MESSAGE_CREATED, self._on_created
        )
        registry.register(
            FederationEventType.SPACE_CHAT_MESSAGE_UPDATED, self._on_updated
        )
        registry.register(
            FederationEventType.SPACE_CHAT_MESSAGE_DELETED, self._on_deleted
        )
        registry.register(FederationEventType.SPACE_CHAT_REACTION, self._on_reaction)

    # ── Shared gates ───────────────────────────────────────────────────

    async def _chat_for(
        self,
        event: FederationEvent,
        *,
        what: str,
        row_id: str,
        removal: bool = False,
    ) -> "tuple[str, Conversation | None, SpaceAuthorship] | None":
        """The space id, this household's chat for it (``None`` before it
        was first created) and the authorship binder — or ``None`` (logged)
        when the event must be dropped. Nothing is created here: a refused
        event leaves no trace.

        A ``removal`` (a delete) only needs the space held here: a delete
        must reach every copy that may hold the message, even while the chat
        is off here or nobody here writes any more."""
        space_id = resolve_space_id(event)
        if not space_id:
            return None
        authorship = self._authorship
        if authorship is None:
            _refuse(event, what, row_id, "no space roster mirror wired")
            return None
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            log_not_applied(event, what=what, row_id=row_id, reason="space not held")
            return None
        if removal:
            return space_id, await self._chats.get_chat(space_id), authorship
        if not space.features.chat:
            log_not_applied(event, what=what, row_id=row_id, reason="chat is off here")
            return None
        if not await self._has_local_writer(space_id):
            # A household whose people only follow the space (or that
            # holds no seat at all) never stores chat content.
            _refuse(event, what, row_id, "no writer of this space lives here")
            return None
        return space_id, await self._chats.get_chat(space_id), authorship

    async def _has_local_writer(self, space_id: str) -> bool:
        return any(
            str(m.role) in _WRITER_ROLE_VALUES
            for m in await self._spaces.list_members(space_id)
        )

    async def _message_in(
        self,
        event: FederationEvent,
        chat: "Conversation | None",
        message_id: str,
        *,
        what: str,
    ) -> ConversationMessage | None:
        """The held, live message ``message_id`` of ``chat`` — else ``None``."""
        msg = await self._convos.get_message(message_id) if message_id else None
        if msg is None or chat is None:
            log_not_applied(event, what=what, row_id=message_id, reason="unknown here")
            return None
        if msg.conversation_id != chat.id:
            _refuse(event, what, message_id, "not a message of this space's chat")
            return None
        if msg.deleted:
            log_not_applied(
                event, what=what, row_id=message_id, reason="already deleted"
            )
            return None
        return msg

    async def _display_name(self, space_id: str, user_id: str) -> str:
        remote = await self._users.get_remote(user_id)
        if remote is not None and remote.display_name:
            return remote.display_name
        seat = await self._seats.get_including_tombstones(space_id, "", user_id)
        if seat is not None and seat.display_name:
            return seat.display_name
        return "Someone"

    async def _audience(
        self, chat_id: str, *, actor: str, withheld: frozenset[str]
    ) -> tuple[str, ...]:
        return await local_audience(
            self._convos,
            self._users,
            chat_id,
            actor_user_id=actor,
            withheld=withheld,
            policy=self._policy,
        )

    # ── Create ─────────────────────────────────────────────────────────

    async def _on_created(
        self, event: FederationEvent, *, from_sync: bool = False
    ) -> None:
        """A new message. ``from_sync`` (the §25.6 catch-up): stored
        quietly — no bell, no mention, no WS frame per historical message —
        and a chat it creates seats its readers at *now*, so nobody inherits
        the backlog as unread."""
        p = event.payload if isinstance(event.payload, dict) else {}
        message_id = _str(p, "message_id")
        author = _str(p, "author_user_id")
        content = _str(p, "content")
        if not message_id or not author or not content:
            log_not_applied(
                event, what="chat message", row_id=message_id, reason="incomplete"
            )
            return
        if len(content) > MAX_DM_LENGTH:
            _refuse(event, "chat message", message_id, "content too long")
            return
        got = await self._chat_for(event, what="chat message", row_id=message_id)
        if got is None:
            return
        space_id, chat, authorship = got
        if not _bound_to(message_id, space_id, author):
            # Bound from its first release: any other id — another
            # author's, another space's, a legacy uuid — is refused.
            _refuse(event, "chat message", message_id, f"id not bound to {author!r}")
            return
        if not await authorship.may_author_writer(event, space_id, author):
            await authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="chat message",
                row_id=message_id,
                user_id=author,
            )
            return
        if await self._convos.get_message(message_id) is not None:
            # Held already — or deleted before it arrived here (a
            # tombstone): either way a create never brings it back.
            log_not_applied(
                event, what="chat message", row_id=message_id, reason="already held"
            )
            return
        created_at = parse_iso8601_lenient(p.get("created_at"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        # A message from the future would sit atop the chat forever.
        created_at = min(created_at, datetime.now(timezone.utc))
        if chat is None:
            # The first message for the space here: create the chat with
            # every local reader seated just before it, so it reads as new.
            chat = await self._chats.reconcile(
                space_id,
                seat_at=(
                    None
                    if from_sync
                    else (created_at - timedelta(microseconds=1)).isoformat()
                ),
            )
        reply_to_id: str | None = _str(p, "reply_to_id") or None
        if reply_to_id is not None:
            target = await self._convos.get_message(reply_to_id)
            if target is None or target.conversation_id != chat.id:
                reply_to_id = None
        msg = ConversationMessage(
            id=message_id,
            conversation_id=chat.id,
            sender_user_id=author,
            content=content,
            created_at=created_at,
            reply_to_id=reply_to_id,
        )
        if not await self._convos.insert_message_if_absent(msg) or from_sync:
            return
        withheld = await self._guardian_block_counterparts(author)
        await self._bus.publish(
            DmMessageCreated(
                conversation_id=chat.id,
                message_id=msg.id,
                sender_user_id=author,
                sender_display_name=await self._display_name(space_id, author),
                recipient_user_ids=await self._audience(
                    chat.id, actor=author, withheld=withheld
                ),
                content=content,
                reply_to_id=reply_to_id,
                occurred_at=msg.created_at,
                mentions=await DmMentionResolver(self._convos, self._users).resolve(
                    chat.id, content
                ),
                system_scope=SystemChatScope.SPACE.value,
                origin_instance_id=event.from_instance,
            )
        )

    async def apply_sync_records(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        """§25.6 catch-up: each ``chat_messages`` record goes through the
        live create rule, ``provider`` standing in as the sender."""
        for r in records:
            payload = {
                "space_id": space_id,
                "message_id": r.get("message_id") or r.get("id"),
                "author_user_id": r.get("author_user_id"),
                "content": r.get("content"),
                "reply_to_id": r.get("reply_to_id"),
                "created_at": r.get("created_at"),
            }
            await self._on_created(
                _sync_event(
                    FederationEventType.SPACE_CHAT_MESSAGE_CREATED,
                    space_id,
                    payload,
                    provider=provider,
                ),
                from_sync=True,
            )

    async def apply_sync_tombstones(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        """§25.6 catch-up deletions (``chat_messages_deleted``), applied
        before the messages: each id must be owner-bound to the record's
        ``author_user_id`` in this space, and the provider must speak for
        that author (any seat) or hold content authority (the host, an
        admin or moderator household). A held message is deleted; one never
        held is recorded as a tombstone, so no later create or stream brings
        it back."""
        authorship = self._authorship
        if authorship is None:
            return
        for r in records:
            message_id = str(r.get("message_id") or r.get("id") or "")
            author = str(r.get("author_user_id") or "")
            event = _sync_event(
                FederationEventType.SPACE_CHAT_MESSAGE_DELETED,
                space_id,
                {"space_id": space_id, "message_id": message_id},
                provider=provider,
            )
            if (
                not message_id
                or not author
                or not _bound_to(message_id, space_id, author)
            ):
                _refuse(
                    event, "chat deletion", message_id, "id not bound to its author"
                )
                continue
            if not await authorship.acts_for(
                event, space_id, author, any_role=True
            ) and not await authorship.has_content_authority(event, space_id):
                _refuse(
                    event, "chat deletion", message_id, "provider may not delete it"
                )
                continue
            got = await self._chat_for(
                event, what="chat deletion", row_id=message_id, removal=True
            )
            if got is None:
                continue
            _space, chat, _auth = got
            await self._remove(
                event, space_id, chat, message_id, author=author, actor=author
            )

    # ── Update ─────────────────────────────────────────────────────────

    async def _on_updated(self, event: FederationEvent) -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        message_id = _str(p, "message_id")
        content = _str(p, "content")
        if not message_id or not content or len(content) > MAX_DM_LENGTH:
            log_not_applied(
                event, what="chat edit", row_id=message_id, reason="malformed"
            )
            return
        got = await self._chat_for(event, what="chat edit", row_id=message_id)
        if got is None:
            return
        space_id, chat, authorship = got
        msg = await self._message_in(event, chat, message_id, what="chat edit")
        if msg is None:
            return
        claimed = _str(p, "author_user_id")
        if (
            (claimed and claimed != msg.sender_user_id)
            or await self._spaces.is_banned(space_id, msg.sender_user_id)
            or not await authorship.acts_for(event, space_id, msg.sender_user_id)
        ):
            _refuse(event, "chat edit", message_id, "not the author's household")
            return
        await self._convos.edit_message(message_id, content)
        withheld = await self._guardian_block_counterparts(msg.sender_user_id)
        await self._bus.publish(
            DmMessageUpdated(
                conversation_id=msg.conversation_id,
                message_id=message_id,
                sender_user_id=msg.sender_user_id,
                recipient_user_ids=await self._audience(
                    msg.conversation_id, actor=msg.sender_user_id, withheld=withheld
                ),
                content=content,
                edited_at=parse_iso8601_lenient(p.get("edited_at")),
                new_mentions=await DmMentionResolver(self._convos, self._users).added(
                    msg.conversation_id, msg.content, content
                ),
                sender_display_name=await self._display_name(
                    space_id, msg.sender_user_id
                ),
                origin_instance_id=event.from_instance,
                is_edit=True,
            )
        )

    # ── Delete ─────────────────────────────────────────────────────────

    async def _on_deleted(self, event: FederationEvent) -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        message_id = _str(p, "message_id")
        actor = _str(p, "actor_user_id")
        got = await self._chat_for(
            event, what="chat delete", row_id=message_id, removal=True
        )
        if got is None:
            return
        space_id, chat, authorship = got
        held = await self._convos.get_message(message_id) if message_id else None
        if held is not None:
            if chat is None or held.conversation_id != chat.id:
                _refuse(event, "chat delete", message_id, "not a message of this chat")
                return
            author = held.sender_user_id
        else:
            # Never held here: the delete overtook its create (or we were
            # offline for it). The owner-bound id says whose it was.
            author = _str(p, "author_user_id")
            if (
                not message_id
                or not author
                or not _bound_to(message_id, space_id, author)
            ):
                _refuse(event, "chat delete", message_id, "id not bound to its author")
                return
        own = actor == author and await authorship.acts_for(
            event, space_id, actor, any_role=True
        )
        if not own and not (
            actor and await authorship.moderates_as(event, space_id, actor)
        ):
            _refuse(
                event,
                "chat delete",
                message_id,
                "neither the author nor a moderator the sender speaks for",
            )
            return
        await self._remove(
            event, space_id, chat, message_id, author=author, actor=actor
        )

    async def _remove(
        self,
        event: FederationEvent,
        space_id: str,
        chat: "Conversation | None",
        message_id: str,
        *,
        author: str,
        actor: str,
    ) -> None:
        """Apply an authorised deletion: soft-delete the held message (and
        tell open threads), or — never held — record a tombstone so a late
        create or catch-up of the id is refused. Nothing is recorded where
        nobody may read the chat and no chat exists."""
        held = await self._convos.get_message(message_id)
        if held is not None:
            if chat is None or held.conversation_id != chat.id:
                _refuse(event, "chat delete", message_id, "not a message of this chat")
                return
            if held.deleted:
                log_not_applied(
                    event,
                    what="chat delete",
                    row_id=message_id,
                    reason="already deleted",
                )
                return
            await self._convos.soft_delete_message(message_id)
            await self._bus.publish(
                DmMessageDeleted(
                    conversation_id=held.conversation_id,
                    message_id=message_id,
                    sender_user_id=held.sender_user_id,
                    actor_user_id=actor,
                    system_scope=SystemChatScope.SPACE.value,
                    origin_instance_id=event.from_instance,
                    recipient_user_ids=await local_audience(
                        self._convos,
                        self._users,
                        held.conversation_id,
                        actor_user_id=actor,
                        include_actor=True,
                        policy=self._policy,
                    ),
                )
            )
            return
        if chat is None:
            if not await self._has_local_writer(space_id):
                log_not_applied(
                    event, what="chat delete", row_id=message_id, reason="no chat here"
                )
                return
            chat = await self._chats.reconcile(space_id)
        await self._convos.insert_tombstone(chat.id, message_id, sender_user_id=author)

    # ── Reaction ───────────────────────────────────────────────────────

    async def _on_reaction(self, event: FederationEvent) -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        message_id = _str(p, "message_id")
        reactor = _str(p, "user_id")
        emoji = _str(p, "emoji").strip()
        action = _str(p, "action")
        if (
            not message_id
            or not reactor
            or not emoji
            or len(emoji) > _MAX_EMOJI_LEN
            or action not in ("add", "remove")
        ):
            log_not_applied(
                event, what="chat reaction", row_id=message_id, reason="malformed"
            )
            return
        got = await self._chat_for(event, what="chat reaction", row_id=message_id)
        if got is None:
            return
        space_id, chat, authorship = got
        msg = await self._message_in(event, chat, message_id, what="chat reaction")
        if msg is None:
            return
        if await self._spaces.is_banned(
            space_id, reactor
        ) or not await authorship.acts_for(event, space_id, reactor):
            _refuse(
                event, "chat reaction", message_id, f"{reactor!r} is not its writer"
            )
            return
        if action == "add":
            await self._convos.add_reaction(message_id, reactor, emoji)
        else:
            await self._convos.remove_reaction(message_id, reactor, emoji)
        withheld = await self._guardian_block_counterparts(
            reactor
        ) | await self._guardian_block_counterparts(msg.sender_user_id)
        await self._bus.publish(
            DmMessageReactionChanged(
                conversation_id=msg.conversation_id,
                message_id=message_id,
                user_id=reactor,
                emoji=emoji,
                action=action,
                recipient_user_ids=await self._audience(
                    msg.conversation_id, actor=reactor, withheld=withheld
                ),
                origin_instance_id=event.from_instance,
            )
        )
