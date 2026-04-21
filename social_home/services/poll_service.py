"""Poll + schedule-poll service (§9).

Thin read/write over :table:`polls` / :table:`poll_options` / :table:`poll_votes`
and :table:`schedule_poll_meta` / :table:`schedule_slots` /
:table:`schedule_responses`. The embedded-in-post shape lives in
:mod:`social_home.domain.post`; this service only needs DB IO.
"""

from __future__ import annotations

import logging
import uuid

from ..domain.events import (
    PollClosed,
    PollCreated,
    PollVoted,
    SchedulePollFinalized,
    SchedulePollResponded,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.poll_repo import AbstractPollRepo

log = logging.getLogger(__name__)


_SCHEDULE_RESPONSES = frozenset({"yes", "maybe", "no"})


class PollServiceError(Exception):
    """Base class for poll-service errors."""


class PollNotFoundError(PollServiceError):
    """No poll exists for the given post_id."""


class PollClosedError(PollServiceError):
    """Votes are rejected once the poll is closed."""


class PollService:
    """One shared service for both reply polls and schedule polls."""

    __slots__ = ("_repo", "_bus")

    def __init__(
        self,
        repo: AbstractPollRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = repo
        self._bus = bus

    # ─── Reply polls ──────────────────────────────────────────────────────

    async def create_poll(
        self,
        *,
        post_id: str,
        question: str,
        options: list[str],
        allow_multiple: bool = False,
        closes_at: str | None = None,
        space_id: str | None = None,
    ) -> dict:
        """Attach a new reply poll to ``post_id``. Returns the summary."""
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")
        cleaned = [str(o).strip() for o in options if str(o).strip()]
        if len(cleaned) < 2:
            raise ValueError("a poll needs at least two options")
        option_rows = [
            {"id": uuid.uuid4().hex, "text": t, "position": i}
            for i, t in enumerate(cleaned)
        ]
        await self._repo.create_poll(
            post_id=post_id,
            question=question,
            closes_at=closes_at,
            allow_multiple=allow_multiple,
            options=option_rows,
        )
        if self._bus is not None:
            await self._bus.publish(
                PollCreated(
                    post_id=post_id,
                    question=question,
                    allow_multiple=allow_multiple,
                    space_id=space_id,
                )
            )
        return await self.summary(post_id, space_id=space_id)

    async def cast_vote(
        self,
        *,
        post_id: str,
        option_id: str,
        voter_user_id: str,
        space_id: str | None = None,
    ) -> None:
        """Toggle the voter's choice for *post_id*.

        For a single-choice poll, replaces any prior vote. For
        ``allow_multiple=True``, toggles the option — voting on an
        already-selected option retracts it, preserving the rest.
        """
        meta = await self._repo.get_meta(post_id)
        if meta is None:
            raise PollNotFoundError(post_id)
        if meta["closed"]:
            raise PollClosedError(post_id)
        if not await self._repo.option_belongs_to_post(
            option_id=option_id,
            post_id=post_id,
        ):
            raise ValueError(f"Unknown poll option {option_id!r}")
        current = set(
            await self._repo.list_user_votes(post_id, voter_user_id),
        )
        if meta["allow_multiple"]:
            if option_id in current:
                # Toggle off — clear all + reinsert the rest.
                await self._repo.clear_user_votes(
                    post_id=post_id,
                    voter_user_id=voter_user_id,
                )
                for oid in current - {option_id}:
                    await self._repo.insert_vote(
                        option_id=oid,
                        voter_user_id=voter_user_id,
                    )
            else:
                await self._repo.insert_vote(
                    option_id=option_id,
                    voter_user_id=voter_user_id,
                )
        else:
            await self._repo.clear_user_votes(
                post_id=post_id,
                voter_user_id=voter_user_id,
            )
            await self._repo.insert_vote(
                option_id=option_id,
                voter_user_id=voter_user_id,
            )
        if self._bus is not None:
            new_votes = tuple(
                await self._repo.list_user_votes(post_id, voter_user_id),
            )
            await self._bus.publish(
                PollVoted(
                    post_id=post_id,
                    voter_user_id=voter_user_id,
                    option_ids=new_votes,
                    space_id=space_id,
                )
            )

    async def retract_vote(
        self,
        *,
        post_id: str,
        voter_user_id: str,
        space_id: str | None = None,
    ) -> None:
        if await self._repo.get_meta(post_id) is None:
            raise PollNotFoundError(post_id)
        await self._repo.clear_user_votes(
            post_id=post_id,
            voter_user_id=voter_user_id,
        )
        if self._bus is not None:
            await self._bus.publish(
                PollVoted(
                    post_id=post_id,
                    voter_user_id=voter_user_id,
                    option_ids=(),
                    space_id=space_id,
                )
            )

    async def close_poll(
        self,
        *,
        post_id: str,
        actor_user_id: str,
        space_id: str | None = None,
    ) -> None:
        """Close the poll. Only the post author may close it."""
        meta = await self._repo.get_meta(post_id)
        if meta is None:
            raise PollNotFoundError(post_id)
        author = await self._repo.get_post_author(post_id)
        if author is None or author != actor_user_id:
            raise PermissionError("Only the post author may close this poll")
        await self._repo.close(post_id)
        if self._bus is not None:
            await self._bus.publish(
                PollClosed(
                    post_id=post_id,
                    space_id=space_id,
                )
            )

    async def summary(
        self,
        post_id: str,
        *,
        voter_user_id: str | None = None,
        space_id: str | None = None,
    ) -> dict:
        """Full poll payload matching ``PollData`` on the client.

        ``{post_id, question, options: [{id, text, vote_count}],
        allow_multiple, closed, closes_at, total_votes, user_vote,
        space_id}``.
        """
        meta = await self._repo.get_meta(post_id)
        if meta is None:
            raise PollNotFoundError(post_id)
        options = await self._repo.list_options_with_counts(post_id)
        options = [
            {"id": o["id"], "text": o["text"], "vote_count": int(o["count"])}
            for o in options
        ]
        total = sum(o["vote_count"] for o in options)
        user_vote: list[str] = []
        if voter_user_id:
            user_vote = await self._repo.list_user_votes(
                post_id,
                voter_user_id,
            )
        return {
            "post_id": post_id,
            "question": meta["question"],
            "options": options,
            "allow_multiple": bool(meta["allow_multiple"]),
            "closed": bool(meta["closed"]),
            "closes_at": meta["closes_at"],
            "total_votes": total,
            "user_vote": user_vote,
            "space_id": space_id,
        }

    # ─── Schedule polls ──────────────────────────────────────────────────

    async def create_schedule_poll(
        self,
        *,
        post_id: str,
        title: str,
        deadline: str | None = None,
        slots: list[dict],
        space_id: str | None = None,
    ) -> dict:
        """Create a new schedule poll bound to ``post_id``.

        Each ``slots`` entry needs ``slot_date`` and optionally
        ``start_time`` + ``end_time``. Slot ids are minted here so
        callers can render the poll immediately.
        """
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        if not slots:
            raise ValueError("at least one slot is required")
        minted = []
        for i, s in enumerate(slots):
            sd = str(s.get("slot_date") or "").strip()
            if not sd:
                raise ValueError("each slot needs a slot_date")
            minted.append(
                {
                    "id": s.get("id") or uuid.uuid4().hex,
                    "slot_date": sd,
                    "start_time": s.get("start_time") or None,
                    "end_time": s.get("end_time") or None,
                    "position": int(s.get("position", i)),
                }
            )
        await self._repo.create_schedule_poll(
            post_id=post_id,
            title=title,
            deadline=deadline,
            slots=minted,
        )
        return await self.schedule_summary(post_id, space_id=space_id)

    async def respond_schedule(
        self,
        *,
        poll_id: str,
        slot_id: str,
        user_id: str,
        response: str,
        space_id: str | None = None,
    ) -> None:
        if response not in _SCHEDULE_RESPONSES:
            raise ValueError(f"response must be one of {sorted(_SCHEDULE_RESPONSES)}")
        await self._repo.upsert_schedule_response(
            slot_id=slot_id,
            user_id=user_id,
            response=response,
        )
        if self._bus is not None:
            await self._bus.publish(
                SchedulePollResponded(
                    post_id=poll_id,
                    slot_id=slot_id,
                    user_id=user_id,
                    response=response,
                    space_id=space_id,
                )
            )

    async def retract_schedule(
        self,
        *,
        poll_id: str,
        slot_id: str,
        user_id: str,
        space_id: str | None = None,
    ) -> None:
        await self._repo.delete_schedule_response(
            slot_id=slot_id,
            user_id=user_id,
        )
        if self._bus is not None:
            await self._bus.publish(
                SchedulePollResponded(
                    post_id=poll_id,
                    slot_id=slot_id,
                    user_id=user_id,
                    response="retracted",
                    space_id=space_id,
                )
            )

    async def finalize_schedule_poll(
        self,
        *,
        post_id: str,
        slot_id: str,
        actor_user_id: str,
        space_id: str | None = None,
    ) -> dict:
        """Author locks in the winning slot. Emits SchedulePollFinalized
        so the space calendar auto-create can hook in."""
        author = await self._repo.get_post_author(post_id)
        if author is None or author != actor_user_id:
            raise PermissionError(
                "Only the post author may finalize this schedule poll",
            )
        slot = await self._repo.finalize_schedule_poll(
            post_id=post_id,
            slot_id=slot_id,
        )
        if slot is None:
            raise ValueError(f"slot {slot_id!r} not in poll {post_id!r}")
        summary = await self.schedule_summary(post_id, space_id=space_id)
        if self._bus is not None:
            await self._bus.publish(
                SchedulePollFinalized(
                    post_id=post_id,
                    slot_id=slot_id,
                    slot_date=slot["slot_date"],
                    start_time=slot["start_time"],
                    end_time=slot["end_time"],
                    title=summary.get("title") or "",
                    finalized_by=actor_user_id,
                    space_id=space_id,
                )
            )
        return summary

    async def schedule_summary(
        self,
        poll_id: str,
        *,
        space_id: str | None = None,
    ) -> dict:
        """Return the full poll payload matching ``ScheduleUI.ScheduleData``.

        Shape:

        ``{post_id, title, deadline, slots: [...], responses: [...],
        finalized_slot_id, closed}``.
        """
        meta = await self._repo.get_schedule_meta(poll_id)
        if meta is None:
            # Caller can treat as "not a schedule poll" — return empty shell.
            return {
                "post_id": poll_id,
                "title": "",
                "deadline": None,
                "slots": [],
                "responses": [],
                "finalized_slot_id": None,
                "closed": False,
                "space_id": space_id,
            }
        slots = await self._repo.list_schedule_slots(poll_id)
        responses = await self._repo.list_schedule_responses(poll_id)
        return {
            "post_id": poll_id,
            "title": meta["title"],
            "deadline": meta["deadline"],
            "slots": slots,
            "responses": responses,
            "finalized_slot_id": meta["finalized_slot_id"],
            "closed": meta["closed"],
            "space_id": space_id,
        }
