"""Poll + schedule-poll routes (§9).

Separate from the feed routes to keep `feed.py` focused. Every endpoint
requires auth; the ``close`` endpoint additionally requires that the
caller authored the post (enforced in :class:`PollService`).
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import poll_service_key
from ..security import error_response
from .base import BaseView


class PollVoteView(BaseView):
    """``POST /api/posts/{id}/poll/vote`` — cast a vote.

    ``DELETE /api/posts/{id}/poll/vote`` — retract a vote.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        post_id = self.match("id")
        body = await self.body()
        option_id = str(body.get("option_id") or "")
        if not option_id:
            return error_response(
                422,
                "UNPROCESSABLE",
                "option_id is required.",
            )
        svc = self.svc(poll_service_key)
        try:
            await svc.cast_vote(
                post_id=post_id,
                option_id=option_id,
                voter_user_id=ctx.user_id,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(
            await svc.summary(post_id, voter_user_id=ctx.user_id),
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        post_id = self.match("id")
        svc = self.svc(poll_service_key)
        await svc.retract_vote(post_id=post_id, voter_user_id=ctx.user_id)
        return web.json_response(
            await svc.summary(post_id, voter_user_id=ctx.user_id),
        )


class PollCloseView(BaseView):
    """``POST /api/posts/{id}/poll/close`` — close the poll."""

    async def post(self) -> web.Response:
        ctx = self.user
        post_id = self.match("id")
        svc = self.svc(poll_service_key)
        try:
            await svc.close_poll(post_id=post_id, actor_user_id=ctx.user_id)
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        return web.json_response(
            await svc.summary(post_id, voter_user_id=ctx.user_id),
        )


class PollSummaryView(BaseView):
    """``GET /api/posts/{id}/poll`` — poll results summary.

    ``POST /api/posts/{id}/poll`` — create + attach a new reply poll
    to the feed post. Body:
    ``{question, options: [text, text, ...], allow_multiple?, closes_at?}``.
    """

    async def get(self) -> web.Response:
        ctx = self.user  # auth check
        post_id = self.match("id")
        svc = self.svc(poll_service_key)
        return web.json_response(
            await svc.summary(post_id, voter_user_id=ctx.user_id),
        )

    async def post(self) -> web.Response:
        ctx = self.user
        post_id = self.match("id")
        body = await self.body()
        svc = self.svc(poll_service_key)
        try:
            summary = await svc.create_poll(
                post_id=post_id,
                question=str(body.get("question") or ""),
                options=list(body.get("options") or []),
                allow_multiple=bool(body.get("allow_multiple", False)),
                closes_at=body.get("closes_at"),
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        # Refresh with user_vote so the creator's client renders the
        # poll immediately without another round-trip.
        return web.json_response(
            await svc.summary(post_id, voter_user_id=ctx.user_id)
            if summary.get("question")
            else summary,
            status=201,
        )


class SchedulePollCollectionView(BaseView):
    """``POST /api/posts/{id}/schedule-poll`` — attach a new schedule
    poll to the feed post ``{id}``. Body: ``{title, slots[], deadline?}``.
    """

    async def post(self) -> web.Response:
        self.user
        post_id = self.match("id")
        body = await self.body()
        svc = self.svc(poll_service_key)
        try:
            summary = await svc.create_schedule_poll(
                post_id=post_id,
                title=str(body.get("title") or ""),
                deadline=body.get("deadline"),
                slots=list(body.get("slots") or []),
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(summary, status=201)


class SchedulePollRespondView(BaseView):
    """``POST /api/schedule-polls/{id}/respond`` — respond to a schedule poll."""

    async def post(self) -> web.Response:
        ctx = self.user
        poll_id = self.match("id")
        body = await self.body()
        slot_id = str(body.get("slot_id") or "")
        response = str(body.get("response") or "")
        if not slot_id or not response:
            return error_response(
                422,
                "UNPROCESSABLE",
                "slot_id and response are required.",
            )
        svc = self.svc(poll_service_key)
        try:
            await svc.respond_schedule(
                poll_id=poll_id,
                slot_id=slot_id,
                user_id=ctx.user_id,
                response=response,
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(await svc.schedule_summary(poll_id))


class SchedulePollSlotResponseView(BaseView):
    """``DELETE /api/schedule-polls/{id}/slots/{slot_id}/response``."""

    async def delete(self) -> web.Response:
        ctx = self.user
        poll_id = self.match("id")
        slot_id = self.match("slot_id")
        svc = self.svc(poll_service_key)
        await svc.retract_schedule(
            poll_id=poll_id,
            slot_id=slot_id,
            user_id=ctx.user_id,
        )
        return web.json_response(await svc.schedule_summary(poll_id))


class SchedulePollFinalizeView(BaseView):
    """``POST /api/schedule-polls/{id}/finalize`` — author picks winning slot."""

    async def post(self) -> web.Response:
        ctx = self.user
        poll_id = self.match("id")
        body = await self.body()
        slot_id = str(body.get("slot_id") or "")
        if not slot_id:
            return error_response(
                422,
                "UNPROCESSABLE",
                "slot_id is required.",
            )
        svc = self.svc(poll_service_key)
        try:
            summary = await svc.finalize_schedule_poll(
                post_id=poll_id,
                slot_id=slot_id,
                actor_user_id=ctx.user_id,
            )
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(summary)


class SchedulePollSummaryView(BaseView):
    """``GET /api/schedule-polls/{id}/summary``."""

    async def get(self) -> web.Response:
        self.user  # auth check
        poll_id = self.match("id")
        svc = self.svc(poll_service_key)
        return web.json_response(await svc.schedule_summary(poll_id))


# ─── Space-scoped schedule-poll routes ────────────────────────────────


class _SpaceScheduleBase(BaseView):
    async def _require_member(self, space_id: str, user_id: str) -> bool:
        from .. import app_keys as K

        space_repo = self.svc(K.space_repo_key)
        return await space_repo.get_member(space_id, user_id) is not None


class SpaceSchedulePollCollectionView(_SpaceScheduleBase):
    """``POST /api/spaces/{id}/posts/{pid}/schedule-poll``."""

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        body = await self.body()
        svc = self.svc(poll_service_key)
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


class SpaceSchedulePollRespondView(_SpaceScheduleBase):
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
        svc = self.svc(poll_service_key)
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


class SpaceSchedulePollFinalizeView(_SpaceScheduleBase):
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
        svc = self.svc(poll_service_key)
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


class SpaceSchedulePollSummaryView(_SpaceScheduleBase):
    """``GET /api/spaces/{id}/schedule-polls/{pid}/summary``."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        post_id = self.match("pid")
        svc = self.svc(poll_service_key)
        return web.json_response(
            await svc.schedule_summary(post_id, space_id=space_id),
        )
