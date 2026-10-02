"""Resolve @-mentions against a space's members (§23.42).

:class:`SpaceMentionResolver` builds the space's member view — local seats
(``space_members``) plus federated seats (``space_remote_members``) — as
:class:`~socialhome.domain.mention.MentionCandidate` rows and runs the pure
:class:`~socialhome.domain.mention.MentionParser` over post / comment
content. The lookup is the member list ONLY, so a mention can never resolve
to (or notify) someone outside the space.

Every household runs this against its own member view on the decrypted
content it stores — nothing about mentions travels on the wire, so there is
no protocol change. :meth:`tokens` hands the composer the exact token per
member (see :func:`~socialhome.domain.mention.mention_tokens`).
"""

from __future__ import annotations

import logging

from ..domain.mention import (
    Mention,
    MentionCandidate,
    MentionParser,
    MentionType,
    candidate_lookup,
    mention_tokens,
    mentions_added,
)
from ..domain.space import SETTINGS_AUTHORITY_ROLES, SpaceRole
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: Local roles that may use ``@here`` (when the space allows it at all).
_HERE_ROLES = SETTINGS_AUTHORITY_ROLES


def _names(*names: str | None) -> tuple[str, ...]:
    out: list[str] = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return tuple(out)


class SpaceMentionResolver:
    """Space-member-scoped mention resolution. Stateless; cheap to build."""

    __slots__ = ("_spaces", "_users", "_remote_members")

    def __init__(
        self,
        space_repo: AbstractSpaceRepo,
        user_repo: AbstractUserRepo,
        remote_member_repo: AbstractSpaceRemoteMemberRepo | None = None,
    ) -> None:
        self._spaces = space_repo
        self._users = user_repo
        self._remote_members = remote_member_repo

    async def candidates(self, space_id: str) -> list[MentionCandidate]:
        """Every mentionable member of *space_id* (local + remote seats).

        Names are the public ``handle`` first, then the login / remote
        username. Inactive local users and deprovisioned remote users are
        skipped; a seat with no user row carries no name and is skipped.
        """
        members = await self._spaces.list_members(space_id)
        remote_seats = (
            await self._remote_members.list_for_space(space_id)
            if self._remote_members is not None
            else []
        )
        ids = {m.user_id for m in members}
        local = (
            {u.user_id: u for u in await self._users.list_by_ids(ids)} if ids else {}
        )
        out: list[MentionCandidate] = []
        seen: set[str] = set()
        for user_id in [m.user_id for m in members] + [
            rm.user_id for rm in remote_seats
        ]:
            if user_id in seen:
                continue
            seen.add(user_id)
            user = local.get(user_id)
            if user is not None:
                if user.is_active():
                    out.append(
                        MentionCandidate(
                            user_id=user_id,
                            handles=_names(user.handle, user.username),
                        )
                    )
                continue
            remote = await self._users.get_remote(user_id)
            if remote is None or remote.deprovisioned_at:
                continue
            out.append(
                MentionCandidate(
                    user_id=user_id,
                    handles=_names(remote.handle, remote.remote_username),
                )
            )
        return out

    async def tokens(self, space_id: str) -> dict[str, str | None]:
        """user_id → token (without ``@``) a composer should insert."""
        return mention_tokens(await self.candidates(space_id))

    async def may_use_here(self, space_id: str, author_id: str | None) -> bool:
        """May *author_id* page everyone with ``@here`` in *space_id*?

        Only when the space's ``allow_here_mention`` toggle is on AND the
        author is an owner or admin **by this household's own roster** —
        never by anything the post's payload claims:

        * a local author → their ``space_members`` role (owner / admin);
        * a remote author → a live ``admin`` seat in ``space_remote_members``,
          or the host's owner: seated on the space's ``owner_instance_id``
          under the space's ``owner_username`` (a remote seat can't hold
          ``owner`` — the host's owner mirrors as a plain seat).

        Members, subscribers, bots and unknown authors → ``False``.
        """
        if not author_id:
            return False
        space = await self._spaces.get(space_id)
        if space is None or not space.allow_here_mention:
            return False
        if await self._users.get_by_user_id(author_id) is not None:
            member = await self._spaces.get_member(space_id, author_id)
            return member is not None and member.role in _HERE_ROLES
        if self._remote_members is None:
            return False
        seats = [
            rm
            for rm in await self._remote_members.list_for_space(space_id)
            if rm.user_id == author_id
        ]
        if any(rm.role == SpaceRole.ADMIN.value for rm in seats):
            return True
        if not any(rm.instance_id == space.owner_instance_id for rm in seats):
            return False
        remote = await self._users.get_remote(author_id)
        return (
            remote is not None
            and remote.instance_id == space.owner_instance_id
            and bool(space.owner_username)
            and remote.remote_username == space.owner_username
        )

    async def resolve(
        self,
        space_id: str,
        content: str | None,
        *,
        author_id: str | None,
    ) -> tuple[Mention, ...]:
        """Parse *content* against the space's members.

        A ``@here`` survives only when :meth:`may_use_here` allows it for
        *author_id* (``None`` → dropped); user mentions are unaffected.

        Fail-soft: a lookup failure logs and yields no mentions — a broken
        roster read must never block the post it was parsing.
        """
        if not content or "@" not in content:
            return ()
        try:
            lookup = candidate_lookup(await self.candidates(space_id))
            mentions = MentionParser(lookup_member=lookup).parse(content, space_id)
            if any(m.type is MentionType.HERE for m in mentions) and not (
                await self.may_use_here(space_id, author_id)
            ):
                mentions = tuple(m for m in mentions if m.type is not MentionType.HERE)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("mention resolution failed for space %s: %s", space_id, exc)
            return ()
        return mentions

    async def added(
        self,
        space_id: str,
        before: str | None,
        after: str | None,
        *,
        author_id: str | None,
    ) -> tuple[Mention, ...]:
        """Mentions an edit from *before* to *after* newly adds — the only
        people an edit may notify
        (:func:`~socialhome.domain.mention.mentions_added`).

        *after* is resolved like a new post (so a ``@here`` survives only
        when :meth:`may_use_here` allows it); *before* is parsed without
        that filter, so a ``@here`` already written — allowed or not — is
        never "new". Fail-soft like :meth:`resolve`.
        """
        if not after or "@" not in after:
            return ()
        new = await self.resolve(space_id, after, author_id=author_id)
        if not new:
            return ()
        if not before or "@" not in before:
            return new
        try:
            lookup = candidate_lookup(await self.candidates(space_id))
            old = MentionParser(lookup_member=lookup).parse(before, space_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("mention diff failed for space %s: %s", space_id, exc)
            return ()
        return mentions_added(old, new)
