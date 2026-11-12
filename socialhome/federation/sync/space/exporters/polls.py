"""Polls exporter — polls attached to space posts.

Walks posts for the space, calls :class:`AbstractPollRepo.get_meta` /
``list_options_with_counts`` for each post that has a poll, emits
one record per poll.

v_92: the record also carries ``votes`` (``{option_id, voter_user_id}``
— every member household sees who voted for what live, through
``SPACE_POLL_VOTE_CAST``), and a v_92 receiver converges the poll on the
host's record: it creates the poll and its options when missing (it missed
the live ``SPACE_POLL_CREATED``), takes the votes of everybody but its own
people and closes a closed poll. A requester below v_92 gets the record
without ``votes`` (:meth:`PollsExporter.without_votes`) — it drops the
resource anyway.

v_94: toward a v_94 requester ``votes`` is replaced by ``vote_stamps``
(``{voter: [stamp, [option_id, …]]}`` — each voter's selection that counts
and the stamp it was made at, a retraction included) and ``meta`` carries
``closed_at``: the receiver merges per voter by the one rule (the newest
stamp wins, :mod:`socialhome.domain.item_merge`) instead of replacing the
list. A v_92 / v_93 requester keeps getting ``votes``
(:meth:`PollsExporter.unstamped_votes`).

A record never splits across chunks, so its votes are bounded:
:data:`MAX_VOTES_PER_POLL` (votes, or voters of the stamp map), the most
recent voters' first and each voter's votes whole; ``votes_partial`` marks
a cut list, and the receiver then converges only the voters it names
(``docs/protocol/sync.md``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ..exporter import PagedExporterMixin
from ..window import SyncWindows
from .posts import iter_post_pages

if TYPE_CHECKING:
    from .....repositories.space_poll_repo import AbstractSpacePollRepo
    from .....repositories.space_post_repo import AbstractSpacePostRepo


#: The most votes one ``polls`` record carries (~100 B each: ≤ ~100 KB).
MAX_VOTES_PER_POLL: int = 1000


class PollsExporter(PagedExporterMixin):
    resource = "polls"

    __slots__ = ("_poll_repo", "_post_repo", "_stamps", "_votes", "_windows")

    def __init__(
        self,
        poll_repo: AbstractSpacePollRepo,
        space_post_repo: AbstractSpacePostRepo,
        windows: SyncWindows,
        *,
        votes: bool = True,
        stamps: bool = True,
    ) -> None:
        self._poll_repo = poll_repo
        self._post_repo = space_post_repo
        self._windows = windows
        self._votes = votes
        self._stamps = stamps

    def without_votes(self) -> PollsExporter:
        """The record set of a requester below v_92: no ``votes``."""
        return PollsExporter(
            self._poll_repo, self._post_repo, self._windows, votes=False
        )

    def unstamped_votes(self) -> PollsExporter:
        """The record set of a v_92 / v_93 requester: the ``votes`` list,
        no ``vote_stamps``."""
        return PollsExporter(
            self._poll_repo, self._post_repo, self._windows, stamps=False
        )

    def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, None)

    def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The changed posts' records — a poll / schedule change (or a
        vote) touches its post (migration 0086)."""
        return self._pages(space_id, since)

    async def _pages(
        self, space_id: str, since: int | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        window = await self._windows.for_space(space_id)
        async for posts in iter_post_pages(
            self._post_repo, space_id, window, since=since
        ):
            yield await self._records(posts)

    async def _records(self, posts) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in posts:
            meta = await self._poll_repo.get_meta(p.id)
            if meta is None:
                continue
            options = await self._poll_repo.list_options_with_counts(p.id)
            record: dict[str, Any] = {
                "post_id": p.id,
                "meta": meta,
                "options": options,
            }
            if self._votes and self._stamps:
                stamps, cut = await self._poll_repo.list_vote_stamps(
                    p.id, limit=MAX_VOTES_PER_POLL
                )
                if cut:
                    record["votes_partial"] = True
                record["vote_stamps"] = stamps
            elif self._votes:
                votes = await self._poll_repo.list_votes(
                    p.id, limit=MAX_VOTES_PER_POLL + 1
                )
                if len(votes) > MAX_VOTES_PER_POLL:
                    # Never half a voter: the one the cut runs through goes.
                    cut = votes[MAX_VOTES_PER_POLL]["voter_user_id"]
                    votes = [
                        v
                        for v in votes[:MAX_VOTES_PER_POLL]
                        if v["voter_user_id"] != cut
                    ]
                    record["votes_partial"] = True
                record["votes"] = votes
            out.append(record)
        return out
