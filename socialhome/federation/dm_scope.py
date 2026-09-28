"""Who may write into a direct conversation on behalf of whom (§24.11).

The §24.11 pipeline authenticates the *household* that signed an
envelope (``from_instance``). A DM payload then names a conversation, a
message and a person — ``conversation_id``, ``message_id``,
``sender_user_id`` / ``user_id`` — all of which the sender writes. Left
unchecked, one paired household could speak as a local member, post into
a conversation it is not part of, or rewrite and delete another person's
message by its id.

:class:`DmScope` answers from facts the receiver already holds and the
sender cannot forge:

* a user's home household — ``get_instance_for_user`` (our own users
  resolve to this instance, remote users to the household their
  ``user_id`` was derived from);
* the conversation's remote seats — ``conversation_remote_members`` rows
  ``(instance_id, remote_username)``, written when a conversation is
  created here or first arrives from its creator.

No new key, table or field.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..repositories.conversation_repo import AbstractConversationRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class DmScope:
    """Bind the conversation and the people a DM payload names to its sender."""

    __slots__ = ("_conversations", "_users")

    def __init__(
        self,
        *,
        conversation_repo: "AbstractConversationRepo",
        user_repo: "AbstractUserRepo",
    ) -> None:
        self._conversations = conversation_repo
        self._users = user_repo

    async def sent_from(self, event: "FederationEvent", user_id: str) -> bool:
        """``user_id`` is a user of the household that signed ``event``.

        Unknown users and our own users are refused: a person's words only
        ever arrive from their own household.
        """
        sender = str(event.from_instance or "")
        if not user_id or not sender:
            return False
        return await self._users.get_instance_for_user(user_id) == sender

    async def seated(self, event: "FederationEvent", conversation_id: str) -> bool:
        """The sending household holds a seat in ``conversation_id``."""
        sender = str(event.from_instance or "")
        if not conversation_id or not sender:
            return False
        members = await self._conversations.list_remote_members(conversation_id)
        return any(m.instance_id == sender for m in members)

    async def speaks_for(
        self,
        event: "FederationEvent",
        conversation_id: str,
        user_id: str,
    ) -> bool:
        """``user_id`` belongs to the sender AND is seated in ``conversation_id``."""
        if not conversation_id or not await self.sent_from(event, user_id):
            return False
        remote = await self._users.get_remote(user_id)
        if remote is None:
            return False
        members = await self._conversations.list_remote_members(conversation_id)
        return any(
            m.instance_id == event.from_instance
            and m.remote_username == remote.remote_username
            for m in members
        )


def refuse(event: "FederationEvent", reason: str, **ids: object) -> None:
    """Log one refused DM write at WARNING (a peer bug or a misbehaving peer)."""
    detail = " ".join(f"{k}={v}" for k, v in ids.items())
    log.warning(
        "%s from %s: %s (%s) — refusing",
        event.event_type,
        event.from_instance,
        reason,
        detail,
    )
