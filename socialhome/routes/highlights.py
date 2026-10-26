"""Highlights routes — ``/api/highlights/*`` (§Highlights).

REST surface for the personal highlights pillar. Every endpoint here is
auth-gated and household-feature-gated by ``feat_highlights`` (§22). The
service layer (``services/highlight_service.py``) does the orchestration —
this file only translates HTTP to method calls and the result back to
JSON.

Routes:

* ``POST   /api/highlights/frames``                       — append today's frame
* ``GET    /api/highlights``                              — list visible highlights
* ``GET    /api/highlights/{id}``                         — highlight detail
* ``DELETE /api/highlights/{id}``                         — delete whole highlight
* ``DELETE /api/highlights/frames/{id}``                  — delete one frame
* ``POST   /api/highlights/frames/{id}/view``             — mark viewed
* ``PUT    /api/highlights/frames/{id}/reaction``         — set / change reaction
* ``DELETE /api/highlights/frames/{id}/reaction``         — clear reaction
* ``POST   /api/highlights/{id}/share``                   — share into a feed
* ``POST   /api/highlights/frames/{id}/dm-reply``         — DM reply with snapshot

Every ``{id}`` read and frame action is audience-gated in the service
(``HighlightService._require_readable``): a caller who may not read the
highlight gets the same 404 as an unknown id. The detail read and the
frame view / reaction / DM-reply calls take an optional ``?via_post=``
— the ``highlight_share`` post the caller opened it from — so a space or
household share card reads for everyone who can read that post.

The DM-reply endpoint is intentionally on the highlights surface (rather
than ``/api/conversations/.../messages``) so the snapshot construction
stays next to the highlight domain. Internally it routes through
``DmService`` which carries the standard DM federation + WS path.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    dm_service_key,
    feed_service_key,
    highlight_service_key,
    media_signer_key,
    media_transcode_repo_key,
    report_service_key,
    space_service_key,
)
from ..domain.highlight import (
    Highlight,
    HighlightAudience,
    HighlightFrame,
    HighlightFrameReaction,
    HighlightFrameType,
    HighlightFrameView,
    audience_label,
)
from ..domain.report import (
    DuplicateReportError,
    ReportRateLimitedError,
    ReportTargetType,
)
from ..domain.user import PersonSummary
from ..media_signer import sign_media_urls_in, strip_signature_query
from ..security import error_response
from .base import BaseView
from .media_status import READY, media_filename, video_poster_path

# ─── Serialisation helpers ────────────────────────────────────────────────


def _sign_payload(request: web.Request, payload):
    """Sign every ``media_url`` field nested in ``payload`` so the SPA
    can drop them straight into ``<img src>`` / ``<video src>`` without
    a Bearer token attached. The browser doesn't propagate the
    Authorization header to plain media-element requests, so the
    canonical ``/api/media/{file}`` URL would 401 — the signed form
    carries its own ``?exp=&sig=`` query that the media route
    accepts in lieu of auth.
    """
    signer = request.app.get(media_signer_key)
    if signer is not None:
        sign_media_urls_in(payload, signer)
    return payload


def _highlight_dict(
    highlight: Highlight, viewer_user_id: str, *, author: PersonSummary | None = None
) -> dict:
    out: dict = {
        "id": highlight.id,
        "author_user_id": highlight.author_user_id,
        "highlight_date": highlight.highlight_date,
        "audience_kind": highlight.audience_kind.value,
        "audience_label": audience_label(highlight, viewer_user_id).value,
        "created_at": highlight.created_at,
        "expires_at": highlight.expires_at,
    }
    # The picked user / household ids are the author's alone: a viewer
    # of a ``users`` highlight must not learn who else was picked.
    if viewer_user_id == highlight.author_user_id:
        out["audience"] = list(highlight.audience)
    # Who wrote it, as the caller may see them (``named_people``): the
    # SPA's household cache only knows local people, so a remote
    # friend's highlight would read "Someone" without this.
    if author is not None:
        out["author"] = author.to_dict()
    return out


def _frame_dict(frame: HighlightFrame, *, media_status: str | None = None) -> dict:
    out = {
        "id": frame.id,
        "highlight_id": frame.highlight_id,
        "sequence": frame.sequence,
        "frame_type": frame.frame_type.value,
        "media_url": frame.media_url,
        "caption_text": frame.caption_text,
        "caption_emoji": frame.caption_emoji,
        "duration_ms": frame.duration_ms,
        "created_at": frame.created_at,
    }
    # Video frames transcode in the background — surface a live
    # ``media_status`` so the SPA shows a "Processing…" placeholder.
    if frame.frame_type is HighlightFrameType.VIDEO:
        out["media_status"] = media_status if media_status is not None else READY
        # The still a row shows instead of a 🎬: the ``.webp`` poster the
        # upload minted next to the ``.webm`` (signed by ``_sign_payload``).
        poster = video_poster_path(frame.media_url)
        if poster is not None:
            out["thumbnail_url"] = poster
    return out


def _via_post(view: BaseView) -> str | None:
    """``?via_post=`` — the ``highlight_share`` post the caller opened the
    highlight from. Only a hint: the service checks the caller can read
    that post and that it shares this highlight."""
    return view.request.query.get("via_post") or None


async def _frame_statuses(view: BaseView, frames) -> dict[str, str]:
    """Batch one ``status_for`` read for every video frame's filename."""
    video_fns = [
        fn
        for f in frames
        if f.frame_type is HighlightFrameType.VIDEO
        and (fn := media_filename(f.media_url)) is not None
    ]
    return await view.svc(media_transcode_repo_key).status_for(video_fns)


def _frame_status(statuses: dict[str, str], frame: HighlightFrame) -> str | None:
    """Per-frame readiness, or ``None`` for non-video frames."""
    if frame.frame_type is not HighlightFrameType.VIDEO:
        return None
    fn = media_filename(frame.media_url)
    return statuses.get(fn, READY) if fn else READY


# ─── Views ────────────────────────────────────────────────────────────────


class HighlightFramesCollectionView(BaseView):
    """``POST /api/highlights/frames`` — create or append today's frame."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        body = await self.body()
        media_url = body.get("media_url")
        if not isinstance(media_url, str) or not media_url:
            return error_response(400, "BAD_REQUEST", "media_url is required")
        frame_type_raw = body.get("frame_type", "image")
        try:
            frame_type = HighlightFrameType(frame_type_raw)
        except ValueError:
            return error_response(
                400, "BAD_REQUEST", f"invalid frame_type {frame_type_raw!r}"
            )
        audience_kind_raw = body.get("audience_kind")
        audience_kind: HighlightAudience | None = None
        if audience_kind_raw is not None:
            try:
                audience_kind = HighlightAudience(audience_kind_raw)
            except ValueError:
                return error_response(
                    400,
                    "BAD_REQUEST",
                    f"invalid audience_kind {audience_kind_raw!r}",
                )
        audience_ids_raw = body.get("audience") or []
        if not isinstance(audience_ids_raw, list):
            return error_response(400, "BAD_REQUEST", "audience must be a list")
        audience_ids: tuple[str, ...] = tuple(
            str(x) for x in audience_ids_raw if isinstance(x, str)
        )
        if audience_ids_raw and audience_kind is None:
            # Ids alone would be ignored and the frame join today's
            # (possibly wider) highlight — refuse instead of guessing.
            return error_response(
                422, "VALIDATION_ERROR", "audience needs an audience_kind"
            )

        svc = self.svc(highlight_service_key)
        highlight, frame = await svc.create_or_append_frame(
            author_user_id=ctx.user_id,
            frame_type=frame_type,
            # The composer's preview consumes a signed URL minted at
            # upload time. If the SPA echoes that signed form back
            # here, drop the ``?exp=&sig=`` so we don't persist a
            # short-lived auth fragment on the frame row — the server
            # signs fresh on every read.
            media_url=strip_signature_query(media_url),
            caption_text=body.get("caption_text"),
            caption_emoji=body.get("caption_emoji"),
            duration_ms=body.get("duration_ms"),
            audience_kind=audience_kind,
            audience=audience_ids,
        )
        # A freshly-uploaded video frame transcodes in the background —
        # the ``.webm`` doesn't exist yet. Tell the SPA to render a
        # "processing" placeholder; the LIST/detail endpoints derive the
        # live status from the transcode repo on refetch.
        new_frame_status = (
            "processing" if frame.frame_type is HighlightFrameType.VIDEO else None
        )
        return self._json(
            _sign_payload(
                self.request,
                {
                    "highlight": _highlight_dict(highlight, ctx.user_id),
                    "frame": _frame_dict(frame, media_status=new_frame_status),
                },
            ),
            status=201,
        )


class HighlightsCollectionView(BaseView):
    """``GET /api/highlights`` — list highlights visible to the caller."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        svc = self.svc(highlight_service_key)
        rows = await svc.list_visible(ctx.user_id)
        authors = await self.named_people(r["highlight"].author_user_id for r in rows)
        all_frames = [f for row in rows for f in row["frames"]]
        statuses = await _frame_statuses(self, all_frames)
        out: list[dict] = []
        for row in rows:
            item: dict = {
                "highlight": _highlight_dict(
                    row["highlight"],
                    ctx.user_id,
                    author=authors[row["highlight"].author_user_id],
                ),
                "frames": [
                    _frame_dict(f, media_status=_frame_status(statuses, f))
                    for f in row["frames"]
                ],
                "unseen_count": row["unseen_count"],
            }
            # The author's "seen by N" — never on a viewer's row.
            if row["seen_by"] is not None:
                item["seen_by"] = row["seen_by"]
            out.append(item)
        return self._json(_sign_payload(self.request, out))


class HighlightDetailView(BaseView):
    """``GET /api/highlights/{id}`` + ``DELETE /api/highlights/{id}``."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        highlight_id = self.match("id")
        svc = self.svc(highlight_service_key)
        # Not the author or in the audience and no readable share → the
        # unknown-id 404 (``HighlightNotFoundError``, mapped by BaseView).
        highlight, frames = await svc.get_for_viewer(
            highlight_id, ctx.user_id, via_post_id=_via_post(self)
        )
        statuses = await _frame_statuses(self, frames)
        # Everyone's own reaction per frame, so the tray shows it and a
        # second tap changes or removes it.
        mine = await svc.my_reactions(ctx.user_id, frames)
        # Authors get the per-frame views + reactions inline so the
        # viewer can render "Viewed by N" without a second request.
        is_author = highlight.author_user_id == ctx.user_id
        views: dict[str, list[HighlightFrameView]] = {}
        reactions: dict[str, list[HighlightFrameReaction]] = {}
        if is_author:
            repo = self.svc(highlight_service_key)._highlights
            for f in frames:
                views[f.id] = await repo.list_views_for_frame(f.id)
                reactions[f.id] = await repo.list_reactions_for_frame(f.id)
        people = await self.named_people(
            [
                highlight.author_user_id,
                *(v.viewer_user_id for vs in views.values() for v in vs),
                *(r.reactor_user_id for rs in reactions.values() for r in rs),
            ]
        )
        body = {
            "highlight": _highlight_dict(
                highlight, ctx.user_id, author=people[highlight.author_user_id]
            ),
            "frames": [
                {
                    **_frame_dict(f, media_status=_frame_status(statuses, f)),
                    "my_reaction": mine.get(f.id),
                }
                for f in frames
            ],
        }
        if is_author:
            # Each viewer / reactor carries their name and picture too —
            # a friend on another household is not in the SPA's cache.
            body["views"] = {
                fid: [
                    {
                        "viewer_user_id": v.viewer_user_id,
                        "viewed_at": v.viewed_at,
                        **people[v.viewer_user_id].to_dict(),
                    }
                    for v in vs
                ]
                for fid, vs in views.items()
            }
            body["reactions"] = {
                fid: [
                    {
                        "reactor_user_id": r.reactor_user_id,
                        "emoji": r.emoji,
                        "reacted_at": r.reacted_at,
                        **people[r.reactor_user_id].to_dict(),
                    }
                    for r in rs
                ]
                for fid, rs in reactions.items()
            }
        return self._json(_sign_payload(self.request, body))

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        highlight_id = self.match("id")
        svc = self.svc(highlight_service_key)
        await svc.delete_highlight(highlight_id=highlight_id, actor_user_id=ctx.user_id)
        return web.json_response({}, status=204)


class HighlightFrameDetailView(BaseView):
    """``DELETE /api/highlights/frames/{id}`` — author removes a frame."""

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        svc = self.svc(highlight_service_key)
        await svc.delete_frame(frame_id=frame_id, actor_user_id=ctx.user_id)
        return web.json_response({}, status=204)


class HighlightFrameViewView(BaseView):
    """``POST /api/highlights/frames/{id}/view`` — mark a frame seen."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        svc = self.svc(highlight_service_key)
        await svc.mark_frame_viewed(
            frame_id=frame_id,
            viewer_user_id=ctx.user_id,
            via_post_id=_via_post(self),
        )
        return web.json_response({"ok": True})


class HighlightFrameReactionView(BaseView):
    """``PUT/DELETE /api/highlights/frames/{id}/reaction``."""

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        body = await self.body()
        emoji = body.get("emoji")
        if not isinstance(emoji, str) or not emoji.strip():
            return error_response(400, "BAD_REQUEST", "emoji is required")
        svc = self.svc(highlight_service_key)
        await svc.react_to_frame(
            frame_id=frame_id,
            reactor_user_id=ctx.user_id,
            emoji=emoji.strip(),
            via_post_id=_via_post(self),
        )
        return web.json_response({"ok": True})

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        svc = self.svc(highlight_service_key)
        await svc.clear_reaction(
            frame_id=frame_id,
            reactor_user_id=ctx.user_id,
            via_post_id=_via_post(self),
        )
        return web.json_response({"ok": True})


class HighlightShareView(BaseView):
    """``POST /api/highlights/{id}/share`` — share into a feed."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        highlight_id = self.match("id")
        body = await self.body()
        scope = body.get("scope")
        if scope not in ("household", "space"):
            return error_response(
                400, "BAD_REQUEST", "scope must be 'household' or 'space'"
            )
        space_id = body.get("space_id")
        note = body.get("note")
        svc = self.svc(highlight_service_key)
        post = await svc.share_to_feed(
            highlight_id=highlight_id,
            actor_user_id=ctx.user_id,
            scope=scope,
            space_id=space_id,
            note=note,
            feed_service=self.svc(feed_service_key),
            space_service=self.svc(space_service_key),
        )
        # A moderated space scope raises ContentQueuedForReview, which
        # BaseView answers 202 with the queue item id.
        return self._json(
            {"post_id": getattr(post, "id", None), "highlight_id": highlight_id},
            status=201,
        )


class HighlightDmReplyView(BaseView):
    """``POST /api/highlights/frames/{id}/dm-reply`` — DM with frame snapshot."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        body = await self.body()
        conversation_id = body.get("conversation_id")
        content = body.get("content", "")
        if not isinstance(conversation_id, str) or not conversation_id:
            return error_response(400, "BAD_REQUEST", "conversation_id is required")
        if not isinstance(content, str):
            return error_response(400, "BAD_REQUEST", "content must be a string")
        svc = self.svc(highlight_service_key)
        message = await svc.dm_reply_to_frame(
            frame_id=frame_id,
            sender_user_id=ctx.user_id,
            conversation_id=conversation_id,
            content=content,
            dm_service=self.svc(dm_service_key),
            via_post_id=_via_post(self),
        )
        return self._json(
            {"message_id": getattr(message, "id", None)},
            status=201,
        )


class HighlightReportView(BaseView):
    """``POST /api/highlights/{id}/report`` — wraps the existing
    :class:`ReportService` with ``target_type='highlight'`` pre-filled."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        category = str(body.get("category") or "").strip()
        if not category:
            return error_response(422, "UNPROCESSABLE", "category is required.")
        notes = body.get("notes")
        svc = self.svc(report_service_key)
        highlight_id = self.match("id")
        try:
            report, federated = await svc.create_report(
                reporter_user_id=ctx.user_id,
                target_type=ReportTargetType.HIGHLIGHT.value,
                target_id=highlight_id,
                category=category,
                notes=str(notes) if notes else None,
                forward_gfs=False,
            )
        except DuplicateReportError as exc:
            return error_response(409, "DUPLICATE_REPORT", str(exc))
        except ReportRateLimitedError as exc:
            return error_response(429, "REPORT_RATE_LIMIT", str(exc))
        return self._json(
            {
                "id": report.id,
                "status": report.status.value,
                "federated": federated,
            },
            status=201,
        )


__all__ = [
    "HighlightDetailView",
    "HighlightDmReplyView",
    "HighlightFrameDetailView",
    "HighlightFrameReactionView",
    "HighlightFrameViewView",
    "HighlightFramesCollectionView",
    "HighlightReportView",
    "HighlightShareView",
    "HighlightsCollectionView",
]
