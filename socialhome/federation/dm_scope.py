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
  created here or first arrives from its creator; for a group
  conversation (v_37) written only by its authority household's
  ``DM_GROUP_ROSTER``, which also names the seat's ``user_id``;
* for a group, the authority itself — the household its owner-bound
  conversation id commits to.

No new key or table.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .owner_bound_id import (
    GROUP_CONVERSATION_KIND,
    OwnerBinding,
    check_owner_bound_id,
)

if TYPE_CHECKING:
    from ..domain.conversation import RemoteConversationMember
    from ..domain.federation import FederationEvent
    from ..repositories.conversation_repo import AbstractConversationRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: Hold-buffer scope (``PendingSeatBuffer``) for DM events waiting on a
#: user's sync — a message from a sender not synced yet, or a group roster
#: naming an authority user not synced yet. Not a space id (those are
#: 32-char key fingerprints), so the keys never mix.
DM_HOLD_SCOPE = "dm"


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

    async def seat_of(
        self,
        event: "FederationEvent",
        conversation_id: str,
        user_id: str,
    ) -> "RemoteConversationMember | None":
        """The seat ``user_id`` holds in ``conversation_id`` on the sender.

        A seat is on the sending household (``instance_id ==
        from_instance``) and names the user either by the ``user_id`` a
        group roster wrote into it, or — for 1:1 seats — by the username
        this household's ``remote_users`` row holds for them. A user known
        here as homed anywhere else (a local member, a third household's
        user) never matches, whatever a seat says.

        The ``user_id`` form is what lets a group member on a household we
        never paired with speak at all: we hold no ``remote_users`` row for
        them, only the seat the authority's roster wrote, and the envelope
        — direct or mesh-routed, origin-signed — is signed by that seat's
        household.
        """
        sender = str(event.from_instance or "")
        if not conversation_id or not user_id or not sender:
            return None
        home = await self._users.get_instance_for_user(user_id)
        if home is not None and home != sender:
            return None
        members = await self._conversations.list_remote_members(conversation_id)
        for m in members:
            if m.instance_id == sender and m.user_id == user_id:
                return m
        if home is None:
            return None
        remote = await self._users.get_remote(user_id)
        if remote is None:
            return None
        for m in members:
            if (
                m.instance_id == sender
                and m.remote_username == remote.remote_username
                and m.user_id in (None, user_id)
            ):
                return m
        return None

    async def speaks_for(
        self,
        event: "FederationEvent",
        conversation_id: str,
        user_id: str,
    ) -> bool:
        """``user_id`` belongs to the sender AND is seated in ``conversation_id``."""
        return await self.seat_of(event, conversation_id, user_id) is not None

    async def authored_by_sender(
        self,
        event: "FederationEvent",
        conversation_id: str,
        user_id: str,
    ) -> bool:
        """``user_id`` is the sender's own: a user homed on it, or seated on it.

        For a change to a row that user already wrote (a delete): the author
        may since have left the conversation, but it is still theirs.
        """
        if await self.sent_from(event, user_id):
            return True
        return await self.seat_of(event, conversation_id, user_id) is not None

    async def relayed_by_authority(
        self,
        event: "FederationEvent",
        conversation_id: str,
        user_id: str,
    ) -> bool:
        """The sender is the group's authority, relaying a remote member's row.

        Only for catch-up history: a household newly added to a group pulls
        the backlog from the authority, whose copy holds every member's
        messages. The authority already decides who sits in the group, so it
        may hand over rows of any member seated on another household — never
        a row claimed for one of *our* users, whose words only ever
        originate here.
        """
        sender = str(event.from_instance or "")
        if not sender or not conversation_id or not user_id:
            return False
        if (
            check_owner_bound_id(
                GROUP_CONVERSATION_KIND,
                conversation_id,
                space_id="",
                owner_user_id=sender,
            )
            is not OwnerBinding.VALID
        ):
            return False
        if await self._users.get_by_user_id(user_id) is not None:
            return False
        home = await self._users.get_instance_for_user(user_id)
        remote = await self._users.get_remote(user_id) if home is not None else None
        for m in await self._conversations.list_remote_members(conversation_id):
            if m.user_id == user_id and (home is None or home == m.instance_id):
                return True
            if (
                remote is not None
                and m.instance_id == remote.instance_id
                and m.remote_username == remote.remote_username
            ):
                return True
        return False


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
