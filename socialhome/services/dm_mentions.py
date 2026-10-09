"""Resolve @-mentions against a conversation's seated members (§23.42).

:class:`DmMentionResolver` is the DM / group-DM sibling of
:class:`~socialhome.services.space_mentions.SpaceMentionResolver`. Its
member view is the conversation's seats on THIS household — local
``conversation_members`` plus ``conversation_remote_members`` (a paired
peer's ``remote_users`` row, or the ``user_id`` a group roster seat names)
— so a mention can never resolve to someone outside the conversation.

A system chat keeps no remote seat rows: its members on other households
come from the access policy (:meth:`SystemChatPolicy.remote_seats` — a
space chat's writers on the space roster), so a space chat's composer can
@-mention a member of another household too.

Each household parses the decrypted message against its own seat view;
nothing about mentions travels on the wire (no protocol change). ``@here``
has no meaning in a chat and is always dropped.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.conversation import (
    Conversation,
    ConversationType,
    RemoteConversationMember,
)
from ..domain.mention import (
    Mention,
    MentionCandidate,
    MentionParser,
    MentionType,
    candidate_lookup,
    mention_tokens,
    mentions_added,
)
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from .system_chat_policy import SystemChatPolicy

log = logging.getLogger(__name__)

#: Message types whose ``content`` is member-written text (a caption for
#: media). ``location`` is structured JSON and ``audio`` a machine
#: transcript — neither is parsed for mentions.
MENTIONABLE_TYPES: frozenset[str] = frozenset({"text", "image", "video", "file"})


def _names(*names: str | None) -> tuple[str, ...]:
    out: list[str] = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return tuple(out)


class DmMentionResolver:
    """Conversation-seat-scoped mention resolution. Stateless; cheap to build."""

    __slots__ = ("_convos", "_users", "_system_chats")

    def __init__(
        self,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        system_chats: "SystemChatPolicy | None" = None,
    ) -> None:
        self._convos = conversation_repo
        self._users = user_repo
        self._system_chats = system_chats

    async def remote_seats(self, conv: Conversation) -> list[RemoteConversationMember]:
        """*conv*'s seats on other households: the stored rows, or — for a
        system chat — the policy's computed roster (none without a policy)."""
        if conv.system_scope is None:
            return await self._convos.list_remote_members(conv.id)
        if self._system_chats is None:
            return []
        return await self._system_chats.remote_seats(conv)

    async def candidates(self, conversation_id: str) -> list[MentionCandidate]:
        """Every mentionable seat of *conversation_id* (local + remote).

        Names are the public ``handle`` first, then the login / remote
        username. A group member who left is no longer seated; inactive
        local users and deprovisioned remote users are skipped, as is a
        remote seat that names no ``user_id`` at all.
        """
        conv = await self._convos.get(conversation_id)
        if conv is None:
            return []
        group = conv.type is ConversationType.GROUP_DM
        out: list[MentionCandidate] = []
        seen: set[str] = set()
        # One read each for the local and the remote side, never one lookup
        # per seat — a chat badge counts mentions for many busy chats.
        active = {u.username: u for u in await self._users.list_active()}
        for m in await self._convos.list_members(conversation_id):
            if group and m.deleted_at is not None:
                continue
            user = active.get(m.username)
            if user is None or user.user_id in seen:
                continue
            seen.add(user.user_id)
            out.append(
                MentionCandidate(
                    user_id=user.user_id,
                    handles=_names(user.handle, user.username),
                )
            )
        remote = await self.remote_seats(conv)
        by_id = {
            ru.user_id: ru
            for ru in await self._users.list_remote_by_ids(
                {rm.user_id for rm in remote if not rm.remote_username and rm.user_id}
            )
        }
        for rm in remote:
            ru = (
                await self._users.get_remote_by_member(
                    rm.instance_id, rm.remote_username
                )
                if rm.remote_username
                else by_id.get(rm.user_id or "")
            )
            if ru is not None:
                if ru.deprovisioned_at or ru.user_id in seen:
                    continue
                seen.add(ru.user_id)
                out.append(
                    MentionCandidate(
                        user_id=ru.user_id,
                        handles=_names(ru.handle, ru.remote_username),
                    )
                )
                continue
            if rm.user_id is None or rm.user_id in seen or not rm.remote_username:
                continue
            # A group seat on a household we never paired with: only the
            # username the authority's roster shipped.
            seen.add(rm.user_id)
            out.append(
                MentionCandidate(user_id=rm.user_id, handles=(rm.remote_username,))
            )
        return out

    async def tokens(self, conversation_id: str) -> dict[str, str | None]:
        """user_id → token (without ``@``) a composer should insert."""
        return mention_tokens(await self.candidates(conversation_id))

    async def _parse(self, conversation_id: str, contents: list[str]) -> list:
        lookup = candidate_lookup(await self.candidates(conversation_id))
        parser = MentionParser(lookup_member=lookup)
        return [
            tuple(
                m
                for m in parser.parse(c, conversation_id)
                if m.type is not MentionType.HERE
            )
            for c in contents
        ]

    async def resolve(
        self, conversation_id: str, content: str | None
    ) -> tuple[Mention, ...]:
        """User mentions in *content* resolved against the seats.

        Fail-soft: a lookup failure logs and yields nothing — a broken
        roster read must never block the message it was parsing.
        """
        if not content or "@" not in content:
            return ()
        try:
            (out,) = await self._parse(conversation_id, [content])
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "mention resolution failed for conversation %s: %s",
                conversation_id,
                exc,
            )
            return ()
        return out

    async def count_mentioning(
        self, conversation_id: str, contents: list[str], user_id: str
    ) -> int:
        """How many of ``contents`` @-mention ``user_id`` (one roster read
        for all of them). Fail-soft: a lookup failure counts nothing."""
        texts = [c for c in contents if c and "@" in c]
        if not texts:
            return 0
        try:
            parsed = await self._parse(conversation_id, texts)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "mention count failed for conversation %s: %s", conversation_id, exc
            )
            return 0
        return sum(1 for ms in parsed if any(m.user_id == user_id for m in ms))

    async def unread_for(
        self, conversation_id: str, username: str, user_id: str, notif_level: str | None
    ) -> int:
        """A member's unread count as their level hears it: every unread
        message at ``all``, only the unread ones that @-mention them at
        ``mentions`` — so a chat badge never shows chatter the member
        asked not to hear about."""
        if notif_level != "mentions":
            return await self._convos.count_unread(conversation_id, username)
        contents = await self._convos.list_unread_contents(
            conversation_id, username, types=MENTIONABLE_TYPES
        )
        return await self.count_mentioning(conversation_id, contents, user_id)

    async def added(
        self, conversation_id: str, before: str | None, after: str | None
    ) -> tuple[Mention, ...]:
        """Mentions an edit from *before* to *after* newly adds
        (:func:`~socialhome.domain.mention.mentions_added`)."""
        if not after or "@" not in after:
            return ()
        try:
            old, new = await self._parse(conversation_id, [before or "", after])
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "mention diff failed for conversation %s: %s", conversation_id, exc
            )
            return ()
        return mentions_added(old, new)
