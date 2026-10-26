"""Moments routes — ``/api/moments/*`` (§Momentum).

REST surface for the household-broadcast Moments pillar. Every
endpoint here is auth-gated and household-feature-gated by
``feat_momentum``. The service layer (``services/moment_service.py``)
does the orchestration; this file translates HTTP to method calls and
JSON.

Routes:

* ``POST   /api/moments``                     — create a moment (text + media + parent_moment_id?)
* ``GET    /api/moments``                     — list visible moments (24h default; followers until expires_at);
  ``?top_level=1`` — roots only, each with its newest reply (``latest_reply``);
  ``?tag=`` — one hashtag; ``?before=`` / ``?limit=`` — paging
* ``GET    /api/moments/archive``             — full retention-window list (same data, full window);
  ``?scope=own`` — the caller's own archive (every moment they wrote here);
  ``?scope=saved`` — the moments the caller saved (decision 6b)
* ``GET    /api/moments/archive/usage``       — the caller's archived media vs. the household cap
* ``PUT    /api/admin/moments/archive-cap``   — admin sets the per-person archive cap
* ``GET    /api/moments/{id}``                — detail incl. replies + reactions
* ``DELETE /api/moments/{id}``                — author / admin delete
* ``PUT    /api/moments/{id}/reaction``       — set / change reaction
* ``DELETE /api/moments/{id}/reaction``       — clear own reaction
* ``PUT    /api/moments/{id}/save``           — save someone's moment (decision 6b)
* ``DELETE /api/moments/{id}/save``           — remove it from your saved moments
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
    moment_archive_service_key,
    moment_public_share_service_key,
    moment_service_key,
    person_summary_service_key,
    report_service_key,
    user_service_key,
)
from ..domain.moment import (
    REACTION_REACH_AUTHOR,
    Moment,
    MomentReaction,
    MomentReactionTotals,
    SavedMoment,
    is_valid_moment_reaction,
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
    save: tuple[bool, bool] | None = None,
    reach: str = REACTION_REACH_AUTHOR,
    totals: MomentReactionTotals | None = None,
) -> dict:
    saved_by_me, can_save = save if save is not None else (False, False)
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
        # Decision 6b: whether the caller saved it, and whether Save is
        # offered (only where the author's delete can still reach the copy).
        "saved_by_me": saved_by_me,
        "can_save": can_save,
        # v_67: ``author`` when a reaction reaches the author's household,
        # ``household`` when it is seen in this household only.
        "reactions_reach": reach,
        # How far away it was written: 1 = this household or a paired one,
        # 2–3 = relayed (the card's provenance line, "2 hops away").
        "hop_count": m.hop_count,
    }
    if counts is not None:
        base["reaction_count"] = counts.get("reaction_count", 0)
        base["reply_count"] = counts.get("reply_count", 0)
    if totals is not None:
        # v_69: everyone sees the full count — the author household's totals
        # where they reach it (``reaction_totals_at`` = when it sent them,
        # Unix ms), else the reactions held here (``null``). Per-emoji counts
        # and the caller's own emoji; never who else reacted.
        base["reaction_count"] = totals.total
        base["reaction_counts"] = totals.counts
        base["reaction_totals_at"] = totals.totals_rev
        base["my_reaction"] = totals.my_reaction
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
    view: BaseView,
    moments: list[Moment],
    viewer_user_id: str,
    *,
    saved_at: dict[str, str] | None = None,
    latest: dict[str, Moment] | None = None,
) -> list[dict]:
    """The inbox / archive body: counts, video status, named authors and
    the caller's save state. ``saved_at`` (the Saved list) adds each
    item's ``saved_at`` — the paging cursor. ``latest`` (the top-level
    list) adds each root's ``latest_reply`` — the newest reply the caller
    may read, or ``null``."""
    svc = view.svc(moment_service_key)
    replies = list(latest.values()) if latest is not None else []
    counts = await svc.count_engagement(
        [m.id for m in moments], viewer_user_id=viewer_user_id
    )
    saves = await svc.save_flags(moments, viewer_user_id=viewer_user_id)
    reach = await svc.reaction_reach(moments)
    totals = await svc.reaction_totals(
        moments, viewer_user_id=viewer_user_id, reach=reach
    )
    statuses = await _video_statuses(view, [*moments, *replies])
    authors = await view.named_people(
        (m.author_user_id for m in (*moments, *replies)),
        gfs_via=_gfs_via((*moments, *replies)),
    )
    items = []
    for m in moments:
        item = _moment_dict(
            m,
            counts=counts.get(m.id),
            media_status=_moment_status(statuses, m),
            author=authors[m.author_user_id],
            save=saves.get(m.id),
            reach=reach.get(m.id, REACTION_REACH_AUTHOR),
            totals=totals.get(m.id),
        )
        if saved_at is not None:
            item["saved_at"] = saved_at[m.id]
        if latest is not None:
            reply = latest.get(m.id)
            item["latest_reply"] = (
                _moment_dict(
                    reply,
                    media_status=_moment_status(statuses, reply),
                    author=authors[reply.author_user_id],
                )
                if reply is not None
                else None
            )
        items.append(item)
    return view.sign_media(items)


async def _saved_list(
    view: BaseView, saved: list[SavedMoment], viewer_user_id: str
) -> list[dict]:
    """The Saved list body: the inbox item plus ``saved_at``."""
    return await _moment_list(
        view,
        [s.moment for s in saved],
        viewer_user_id,
        saved_at={s.moment.id: s.saved_at for s in saved},
    )


def _limit(view: BaseView, default: int = 50) -> int:
    try:
        return int(view.request.query.get("limit", default))
    except ValueError:
        return default


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
        top_level = self.request.query.get("top_level") in ("1", "true")
        moments = await svc.list_inbox(
            viewer_user_id=ctx.user_id,
            before=before,
            limit=limit,
            tag=self.request.query.get("tag") or None,
            top_level=top_level,
        )
        latest = (
            await svc.latest_replies(
                [m.id for m in moments], viewer_user_id=ctx.user_id
            )
            if top_level
            else None
        )
        return self._json(await _moment_list(self, moments, ctx.user_id, latest=latest))

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

    ``?scope=own`` (decision 6a) instead returns the caller's own archive:
    every moment they wrote on this household and haven't deleted —
    top-level moments and replies, live or past their expiry (kept here
    for them only) — newest first, paged with ``before`` (the
    ``created_at`` of the last row) and ``limit`` (default 50, ≤ 100).
    Same per-item shape (author, counts).

    ``?scope=saved`` (decision 6b) returns the moments the caller saved and
    may still read, newest save first, paged with ``before`` (the
    ``saved_at`` of the last row) and ``limit``. Each item is the inbox
    item plus ``saved_at``. Any other ``scope`` is 422.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(moment_service_key)
        scope = self.request.query.get("scope")
        if scope is not None and scope not in ("own", "saved"):
            return error_response(
                422, "INVALID_SCOPE", "scope must be 'own' or 'saved'."
            )
        if scope == "own":
            moments = await svc.list_own_archive(
                author_user_id=ctx.user_id,
                before=self.request.query.get("before"),
                limit=_limit(self),
            )
            return self._json(await _moment_list(self, moments, ctx.user_id))
        if scope == "saved":
            saved = await svc.list_saved(
                user_id=ctx.user_id,
                before=self.request.query.get("before"),
                limit=_limit(self),
            )
            return self._json(await _saved_list(self, saved, ctx.user_id))
        tag = self.request.query.get("tag")
        moments = await svc.list_inbox(
            viewer_user_id=ctx.user_id,
            limit=100,
            tag=tag,
        )
        return self._json(await _moment_list(self, moments, ctx.user_id))


def _usage_dict(used_bytes: int, cap_bytes: int) -> dict:
    return {"used_bytes": used_bytes, "cap_bytes": cap_bytes}


class MomentArchiveUsageView(BaseView):
    """``GET /api/moments/archive/usage`` — ``{used_bytes, cap_bytes}``:
    how much media the caller's own moments hold here, and the household's
    per-person cap (``0`` = no limit). Past the cap the oldest archived
    pictures go first."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        usage = await self.svc(moment_archive_service_key).usage_for(ctx.user_id)
        return self._json(_usage_dict(usage.used_bytes, usage.cap_bytes))


class MomentArchiveCapView(BaseView):
    """``PUT /api/admin/moments/archive-cap`` — body ``{cap_bytes: int}``
    (≥ 0; ``0`` = no limit). Admins only (403 otherwise); 422 for a value
    that isn't a whole number ≥ 0. Returns ``{cap_bytes}``."""

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        cap = await self.svc(moment_archive_service_key).set_cap_bytes(
            body.get("cap_bytes"), actor_is_admin=ctx.is_admin
        )
        return self._json({"cap_bytes": cap})


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
        saves = await svc.save_flags([moment, *replies], viewer_user_id=ctx.user_id)
        reach = await svc.reaction_reach([moment, *replies])
        totals = await svc.reaction_totals(
            [moment, *replies], viewer_user_id=ctx.user_id, reach=reach
        )
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
                        save=saves.get(moment.id),
                        reach=reach.get(moment.id, REACTION_REACH_AUTHOR),
                        totals=totals.get(moment.id),
                    ),
                    "replies": [
                        _moment_dict(
                            r,
                            counts=counts.get(r.id),
                            media_status=_moment_status(statuses, r),
                            author=authors[r.author_user_id],
                            save=saves.get(r.id),
                            reach=reach.get(r.id, REACTION_REACH_AUTHOR),
                            totals=totals.get(r.id),
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
        if not is_valid_moment_reaction(emoji):
            return error_response(422, "INVALID_REACTION", "A reaction is one emoji.")
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


class MomentSaveView(BaseView):
    """``PUT|DELETE /api/moments/{id}/save`` — keep someone's moment past
    its expiry until its author deletes it (decision 6b).

    ``PUT`` is idempotent: the unknown-id 404 for a moment the caller can't
    read, 422 ``OWN_MOMENT`` for their own (already archived), 422
    ``SAVE_UNAVAILABLE`` where the author's delete couldn't reach the copy
    (``can_save`` on the item says so up front). ``DELETE`` always answers
    200 — removing a save says nothing about the moment."""

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        moment_id = self.match("id")
        await self.svc(moment_service_key).save_moment(moment_id, user_id=ctx.user_id)
        return self._json({"moment_id": moment_id, "saved": True})

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        moment_id = self.match("id")
        await self.svc(moment_service_key).unsave_moment(moment_id, user_id=ctx.user_id)
        return self._json({"moment_id": moment_id, "saved": False})


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
    affect the Momentum retention window today (24h → the author's chosen
    1–30 days when the viewer follows). Future surfaces can also consult ``user_follows``
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
