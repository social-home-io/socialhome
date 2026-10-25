"""Moments routes — ``/api/moments/*`` (§Momentum).

REST surface for the household-broadcast Moments pillar. Every
endpoint here is auth-gated and household-feature-gated by
``feat_momentum``. The service layer (``services/moment_service.py``)
does the orchestration; this file translates HTTP to method calls and
JSON.

Routes:

* ``POST   /api/moments``                     — create a moment (text + media + parent_moment_id?)
* ``GET    /api/moments``                     — list visible moments (24h default; 7d for followers)
* ``GET    /api/moments/archive``             — full retention-window list (same data, full window)
* ``GET    /api/moments/{id}``                — detail incl. replies + reactions
* ``DELETE /api/moments/{id}``                — author / admin delete
* ``PUT    /api/moments/{id}/reaction``       — set / change reaction
* ``DELETE /api/moments/{id}/reaction``       — clear own reaction
* ``POST   /api/moments/{id}/report``         — wrap content_reports
* ``GET    /api/moments/follows``             — list of who I follow (name,
  signed picture, household and my nickname for each)
* ``POST   /api/moments/follows``             — follow a user
* ``DELETE /api/moments/follows/{user_id}``   — unfollow
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    media_signer_key,
    media_transcode_repo_key,
    moment_public_share_service_key,
    moment_service_key,
    person_summary_service_key,
    report_service_key,
    user_service_key,
)
from ..domain.moment import Moment, MomentReaction
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


def _moment_dict(
    m: Moment,
    *,
    counts: dict[str, int] | None = None,
    media_status: str | None = None,
    author: PersonSummary | None = None,
) -> dict:
    base: dict = {
        "id": m.id,
        "author_user_id": m.author_user_id,
        "content": m.content,
        "media_url": m.media_url,
        "media_type": m.media_type,
        "duration_ms": m.duration_ms,
        "parent_moment_id": m.parent_moment_id,
        "origin_instance_id": m.origin_instance_id,
        "created_at": m.created_at,
        "expires_at": m.expires_at,
        "reaction_count": 0,
        "reply_count": 0,
        # §Momentum-public provenance — the inbox uses these to render
        # a "via {gfs}" chip and to suppress the relay path locally.
        "is_public": m.is_public,
        "received_via": m.received_via,
        "received_via_gfs_id": m.received_via_gfs_id,
    }
    if counts is not None:
        base["reaction_count"] = counts.get("reaction_count", 0)
        base["reply_count"] = counts.get("reply_count", 0)
    # Video moments transcode in the background — surface a live
    # ``media_status`` so the SPA shows a "Processing…" placeholder.
    if m.media_type == "video":
        base["media_status"] = media_status if media_status is not None else READY
        # Signed poster (the ``.webp`` sibling of the ``.webm``
        # ``media_url``) — set the unsigned path so ``_sign_payload``'s
        # ``sign_media_urls_in`` pass signs it alongside ``media_url``.
        poster = video_poster_path(m.media_url)
        if poster is not None:
            base["media_thumbnail_url"] = poster
    # Who wrote it, as the caller may see them (``named_people``): the
    # SPA's household cache only knows local people, so a remote
    # friend's moment would read "Someone" without this.
    if author is not None:
        base["author"] = author.to_dict()
    return base


def _gfs_via(moments) -> dict[str, str]:
    """Author → GFS id for moments that arrived through a GFS, so an
    author followed only there is named from the follow (``named_people``)."""
    return {
        m.author_user_id: m.received_via_gfs_id
        for m in moments
        if m.received_via == "gfs" and m.received_via_gfs_id
    }


async def _video_statuses(view: BaseView, moments) -> dict[str, str]:
    """Batch one ``status_for`` read for every video moment's filename."""
    video_fns = [
        fn
        for m in moments
        if m.media_type == "video" and (fn := media_filename(m.media_url)) is not None
    ]
    return await view.svc(media_transcode_repo_key).status_for(video_fns)


def _moment_status(statuses: dict[str, str], m: Moment) -> str | None:
    """Per-moment readiness, or ``None`` for non-video moments."""
    if m.media_type != "video":
        return None
    fn = media_filename(m.media_url)
    return statuses.get(fn, READY) if fn else READY


async def _moment_list(
    view: BaseView, moments: list[Moment], viewer_user_id: str
) -> list[dict]:
    """The inbox / archive body: counts, video status and named authors."""
    counts = await view.svc(moment_service_key).count_engagement(
        [m.id for m in moments], viewer_user_id=viewer_user_id
    )
    statuses = await _video_statuses(view, moments)
    authors = await view.named_people(
        (m.author_user_id for m in moments), gfs_via=_gfs_via(moments)
    )
    return view.sign_media(
        [
            _moment_dict(
                m,
                counts=counts.get(m.id),
                media_status=_moment_status(statuses, m),
                author=authors[m.author_user_id],
            )
            for m in moments
        ]
    )


def _reaction_dict(r: MomentReaction) -> dict:
    return {
        "moment_id": r.moment_id,
        "reactor_user_id": r.reactor_user_id,
        "emoji": r.emoji,
        "reacted_at": r.reacted_at,
    }


# ─── Moments collection / detail ─────────────────────────────────────────


class MomentCollectionView(BaseView):
    """``GET|POST /api/moments``."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(moment_service_key)
        before = self.request.query.get("before")
        try:
            limit = int(self.request.query.get("limit", 50))
        except ValueError:
            limit = 50
        moments = await svc.list_inbox(
            viewer_user_id=ctx.user_id,
            before=before,
            limit=limit,
        )
        return self._json(await _moment_list(self, moments, ctx.user_id))

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        svc = self.svc(moment_service_key)
        moment = await svc.create_moment(
            author_user_id=ctx.user_id,
            content=str(body.get("content") or ""),
            # The composer's preview consumes a signed URL minted at
            # upload time. If the SPA echoes that signed form back
            # here, drop the ``?exp=&sig=`` so we don't persist a
            # short-lived auth fragment on the moment row — the server
            # signs fresh on every read.
            media_url=strip_signature_query(body.get("media_url")),
            media_type=body.get("media_type"),
            duration_ms=body.get("duration_ms"),
            parent_moment_id=body.get("parent_moment_id"),
            is_public=bool(body.get("is_public", False)),
        )
        return self._json(_sign_payload(self.request, _moment_dict(moment)), status=201)


class MomentArchiveView(BaseView):
    """``GET /api/moments/archive`` — full retention-window list.

    Same data shape as the inbox; the page chrome on the SPA side
    differs (calendar grouping vs. flat list). The visibility filter
    (block-aware, follower-aware) still applies.

    Optional ``?tag=<name>`` restricts the result to moments tagged
    with that hashtag (lowercase, no leading ``#``). The Browse page
    uses this for the trending-tag chip row.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(moment_service_key)
        tag = self.request.query.get("tag")
        moments = await svc.list_inbox(
            viewer_user_id=ctx.user_id,
            limit=100,
            tag=tag,
        )
        return self._json(await _moment_list(self, moments, ctx.user_id))


class MomentHashtagsView(BaseView):
    """``GET /api/moments/hashtags`` — trending tags for the chip row.

    Returns ``[{"tag": str, "count": int}, …]`` sorted by descending
    count. The aggregation re-applies the inbox visibility filter so
    blocked authors and out-of-window moments don't pollute the row.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(moment_service_key)
        try:
            limit = int(self.request.query.get("limit", 20))
        except ValueError:
            limit = 20
        rows = await svc.list_top_hashtags(
            viewer_user_id=ctx.user_id,
            limit=limit,
        )
        return self._json({"hashtags": rows})


class MomentDetailView(BaseView):
    """``GET|DELETE /api/moments/{id}``."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(moment_service_key)
        moment_id = self.match("id")
        # Not readable by the caller → the unknown-id 404 (MomentNotFoundError,
        # mapped by BaseView), the same body as for an id that never existed.
        moment = await svc.get_for_viewer(moment_id, ctx.user_id)
        replies = await svc.list_replies(moment_id, viewer_user_id=ctx.user_id)
        reactions = await svc.list_reactions(moment_id)
        ids = [moment.id, *[r.id for r in replies]]
        counts = await svc.count_engagement(ids, viewer_user_id=ctx.user_id)
        statuses = await _video_statuses(self, [moment, *replies])
        authors = await self.named_people(
            (m.author_user_id for m in (moment, *replies)),
            gfs_via=_gfs_via((moment, *replies)),
        )
        return self._json(
            _sign_payload(
                self.request,
                {
                    "moment": _moment_dict(
                        moment,
                        counts=counts.get(moment.id),
                        media_status=_moment_status(statuses, moment),
                        author=authors[moment.author_user_id],
                    ),
                    "replies": [
                        _moment_dict(
                            r,
                            counts=counts.get(r.id),
                            media_status=_moment_status(statuses, r),
                            author=authors[r.author_user_id],
                        )
                        for r in replies
                    ],
                    "reactions": [_reaction_dict(r) for r in reactions],
                },
            )
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(moment_service_key)
        moment_id = self.match("id")
        await svc.delete_moment(
            moment_id,
            actor_user_id=ctx.user_id,
            actor_is_admin=ctx.is_admin,
        )
        return self._json({"id": moment_id, "deleted": True})


class MomentReactionView(BaseView):
    """``PUT|DELETE /api/moments/{id}/reaction``."""

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        emoji = str(body.get("emoji") or "").strip()
        if not emoji:
            return error_response(422, "UNPROCESSABLE", "emoji is required.")
        svc = self.svc(moment_service_key)
        moment_id = self.match("id")
        await svc.react(moment_id, reactor_user_id=ctx.user_id, emoji=emoji)
        return self._json({"moment_id": moment_id, "emoji": emoji})

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(moment_service_key)
        moment_id = self.match("id")
        await svc.clear_reaction(moment_id, reactor_user_id=ctx.user_id)
        return self._json({"moment_id": moment_id, "emoji": None})


class MomentReportView(BaseView):
    """``POST /api/moments/{id}/report`` — wraps the existing
    :class:`ReportService` with ``target_type='moment'`` pre-filled."""

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
        moment_id = self.match("id")
        try:
            report, federated = await svc.create_report(
                reporter_user_id=ctx.user_id,
                target_type=ReportTargetType.MOMENT.value,
                target_id=moment_id,
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


class MomentShareLinkView(BaseView):
    """``POST /api/moments/{id}/share-link`` — the author's link to
    one of their public moments. Optional body ``{gfs_id}``. The link opens
    the pinned viewer with the moment's item key in its fragment; a moment
    that isn't the caller's live public moment is the unknown-id 404."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body() if self.request.can_read_body else {}
        gfs_id = body.get("gfs_id") if isinstance(body, dict) else None
        if gfs_id is not None and not isinstance(gfs_id, str):
            return error_response(400, "BAD_REQUEST", "gfs_id must be a string.")
        url = await self.svc(moment_public_share_service_key).link(
            user_id=ctx.user_id, moment_id=self.match("id"), gfs_id=gfs_id or None
        )
        return self._json({"url": url})


# ─── Follows ──────────────────────────────────────────────────────────────


class MomentFollowsCollectionView(BaseView):
    """``GET|POST /api/moments/follows``.

    Follows live on the same surface as moments because they only
    affect the Momentum retention window today (24h → 7d when the
    viewer follows). Future surfaces can also consult ``user_follows``
    without changing the route shape.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(user_service_key)
        rows = await svc.list_following(ctx.username)
        people = self.svc(person_summary_service_key)
        payload = {"follows": await people.enrich(ctx.user_id, rows)}
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return self._json(payload)

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        target = str(body.get("user_id") or "").strip()
        if not target:
            return error_response(400, "BAD_REQUEST", "Missing user_id.")
        svc = self.svc(user_service_key)
        await svc.follow(ctx.username, target)
        return self._json({"user_id": target}, status=201)


class MomentFollowsDetailView(BaseView):
    """``DELETE /api/moments/follows/{user_id}``."""

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        target = self.match("user_id")
        svc = self.svc(user_service_key)
        await svc.unfollow(ctx.username, target)
        return self._json({"user_id": target})
