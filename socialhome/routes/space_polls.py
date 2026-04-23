"""Space-scoped poll + schedule-poll routes (§9 / §13).

Mirrors the household :mod:`socialhome.routes.polls` surface but
scoped to a single space and gated by :meth:`AbstractSpaceRepo.get_member`.
All routes dispatch to the second :class:`PollService` instance
registered under :data:`space_poll_service_key`, which wraps
:class:`SqliteSpacePollRepo` + the ``space_*`` tables.

The ``space_id`` kwarg is always passed to the service so the
:class:`PollFederationOutbound` / :class:`ScheduleFederationOutbound`
subscribers can fan mutations out to paired peer instances.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import space_poll_service_key, space_repo_key
from ..security import error_response
from .base import BaseView


class _SpacePollBase(BaseView):
    """Shared member-check for every space-scoped poll view."""

    async def _require_member(self, space_id: str, user_id: str) -> bool:
        space_repo = self.svc(space_repo_key)
        member = await space_repo.get_member(space_id, user_id)
        return member is not None


# ─── Reply polls ────────────────────────────────────────────────────────


class SpacePollSummaryView(_SpacePollBase):
    """``GET /api/spaces/{id}/posts/{pid}/poll`` — poll results.

    ``POST /api/spaces/{id}/posts/{pid}/poll`` — attach a new reply
    poll to ``pid``. Body:
    ``{question, options: [text, ...], allow_multiple?, closes_at?}``.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        svc = self.svc(space_poll_service_key)
        return web.json_response(
            await svc.summary(
                post_id,
                voter_user_id=ctx.user_id,
                space_id=space_id,
            ),
        )

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        body = await self.body()
        svc = self.svc(space_poll_service_key)
        try:
            summary = await svc.create_poll(
                post_id=post_id,
                question=str(body.get("question") or ""),
                options=list(body.get("options") or []),
                allow_multiple=bool(body.get("allow_multiple", False)),
                closes_at=body.get("closes_at"),
                space_id=space_id,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(
            await svc.summary(
                post_id,
                voter_user_id=ctx.user_id,
                space_id=space_id,
            )
            if summary.get("question")
            else summary,
            status=201,
        )


class SpacePollVoteView(_SpacePollBase):
    """``POST /api/spaces/{id}/posts/{pid}/poll/vote`` — cast a vote.

    ``DELETE`` — retract all votes the caller cast on this poll.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        body = await self.body()
        option_id = str(body.get("option_id") or "")
        if not option_id:
            return error_response(
                422,
                "UNPROCESSABLE",
                "option_id is required.",
            )
        svc = self.svc(space_poll_service_key)
        try:
            await svc.cast_vote(
                post_id=post_id,
                option_id=option_id,
                voter_user_id=ctx.user_id,
                space_id=space_id,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(
            await svc.summary(
                post_id,
                voter_user_id=ctx.user_id,
                space_id=space_id,
            ),
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        svc = self.svc(space_poll_service_key)
        await svc.retract_vote(
            post_id=post_id,
            voter_user_id=ctx.user_id,
            space_id=space_id,
        )
        return web.json_response(
            await svc.summary(
                post_id,
                voter_user_id=ctx.user_id,
                space_id=space_id,
            ),
        )


class SpacePollCloseView(_SpacePollBase):
    """``POST /api/spaces/{id}/posts/{pid}/poll/close`` — close the poll."""

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        svc = self.svc(space_poll_service_key)
        try:
            await svc.close_poll(
                post_id=post_id,
                actor_user_id=ctx.user_id,
                space_id=space_id,
            )
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        return web.json_response(
            await svc.summary(
                post_id,
                voter_user_id=ctx.user_id,
                space_id=space_id,
            ),
        )


# ─── Schedule polls ─────────────────────────────────────────────────────


class SpaceSchedulePollCollectionView(_SpacePollBase):
    """``POST /api/spaces/{id}/posts/{pid}/schedule-poll``."""

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        body = await self.body()
        svc = self.svc(space_poll_service_key)
        try:
            summary = await svc.create_schedule_poll(
                post_id=post_id,
                title=str(body.get("title") or ""),
                deadline=body.get("deadline"),
                slots=list(body.get("slots") or []),
                space_id=space_id,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(summary, status=201)


class SpaceSchedulePollRespondView(_SpacePollBase):
    """``POST /api/spaces/{id}/schedule-polls/{pid}/respond``."""

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        body = await self.body()
        slot_id = str(body.get("slot_id") or "")
        response = str(body.get("response") or "")
        if not slot_id or not response:
            return error_response(
                422,
                "UNPROCESSABLE",
                "slot_id and response are required.",
            )
        svc = self.svc(space_poll_service_key)
        try:
            await svc.respond_schedule(
                poll_id=post_id,
                slot_id=slot_id,
                user_id=ctx.user_id,
                response=response,
                space_id=space_id,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(
            await svc.schedule_summary(post_id, space_id=space_id),
        )


class SpaceSchedulePollSlotResponseView(_SpacePollBase):
    """``DELETE /api/spaces/{id}/schedule-polls/{pid}/slots/{slot_id}/response``."""

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        slot_id = self.match("slot_id")
        svc = self.svc(space_poll_service_key)
        await svc.retract_schedule(
            poll_id=post_id,
            slot_id=slot_id,
            user_id=ctx.user_id,
            space_id=space_id,
        )
        return web.json_response(
            await svc.schedule_summary(post_id, space_id=space_id),
        )


class SpaceSchedulePollFinalizeView(_SpacePollBase):
    """``POST /api/spaces/{id}/schedule-polls/{pid}/finalize``."""

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        body = await self.body()
        slot_id = str(body.get("slot_id") or "")
        if not slot_id:
            return error_response(
                422,
                "UNPROCESSABLE",
                "slot_id is required.",
            )
        svc = self.svc(space_poll_service_key)
        try:
            summary = await svc.finalize_schedule_poll(
                post_id=post_id,
                slot_id=slot_id,
                actor_user_id=ctx.user_id,
                space_id=space_id,
            )
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(summary)


class SpaceSchedulePollSummaryView(_SpacePollBase):
    """``GET /api/spaces/{id}/schedule-polls/{pid}/summary``."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        svc = self.svc(space_poll_service_key)
        return web.json_response(
            await svc.schedule_summary(post_id, space_id=space_id),
        )
