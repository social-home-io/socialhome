"""Feed routes — household post feed.

GET    /api/feed                                   — list feed posts
POST   /api/feed/posts                             — create a post
PATCH  /api/feed/posts/{id}                        — edit a post
DELETE /api/feed/posts/{id}                        — delete a post
POST   /api/feed/posts/{id}/reactions              — add a reaction
DELETE /api/feed/posts/{id}/reactions/{emoji}      — remove a reaction
POST   /api/feed/posts/{id}/comments               — add a comment
GET    /api/feed/posts/{id}/comments               — list comments
PATCH  /api/feed/posts/{id}/comments/{cid}         — edit a comment
DELETE /api/feed/posts/{id}/comments/{cid}         — delete a comment
POST   /api/feed/posts/{id}/save                   — bookmark a post
DELETE /api/feed/posts/{id}/save                   — remove bookmark
GET    /api/feed/saved                             — list bookmarked posts

All handlers are THIN — one service call + JSON response. No SQL here.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from urllib.parse import unquote

from aiohttp import web

from ..app_keys import feed_service_key, post_repo_key
from ..security import error_response, sanitise_for_api
from .base import BaseView

log = logging.getLogger(__name__)


def _serialise(obj) -> object:
    """Recursively serialise domain objects to JSON-safe types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        raw = dataclasses.asdict(obj)
        return sanitise_for_api(_coerce_datetimes(raw))
    if isinstance(obj, dict):
        return sanitise_for_api(_coerce_datetimes(obj))
    if isinstance(obj, list):
        return [_serialise(item) for item in obj]
    return obj


def _coerce_datetimes(d: dict) -> dict:
    """Convert datetime values to ISO strings and frozensets to sorted lists."""
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, (frozenset, set)):
            out[k] = sorted(v)
        elif isinstance(v, dict):
            out[k] = _coerce_datetimes(v)
        elif isinstance(v, list):
            out[k] = [
                _coerce_datetimes(i)
                if isinstance(i, dict)
                else sorted(i)
                if isinstance(i, (frozenset, set))
                else i.isoformat()
                if isinstance(i, datetime)
                else i
                for i in v
            ]
        elif isinstance(v, tuple):
            out[k] = list(v)
        else:
            out[k] = v
    return out


# ── Views ────────────────────────────────────────────────────────────────


class FeedCollectionView(BaseView):
    """GET /api/feed — list the household feed, newest-first."""

    async def get(self) -> web.Response:
        svc = self.svc(feed_service_key)
        before = self.request.query.get("before")
        try:
            limit = int(self.request.query.get("limit", 20))
        except ValueError:
            limit = 20
        posts = await svc.list_feed(before=before, limit=limit)
        return web.json_response([_serialise(p) for p in posts])


class PostCollectionView(BaseView):
    """POST /api/feed/posts — create a new post."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(feed_service_key)
        body = await self.body()
        post = await svc.create_post(
            author_user_id=ctx.user_id,
            type=body.get("type", "text"),
            content=body.get("content"),
            media_url=body.get("media_url"),
            pinned=bool(body.get("pinned", False)),
            no_link_preview=bool(body.get("no_link_preview", False)),
        )
        return web.json_response(_serialise(post), status=201)


class PostDetailView(BaseView):
    """PATCH/DELETE /api/feed/posts/{id} — edit or delete a post."""

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(feed_service_key)
        post_id = self.match("id")
        body = await self.body()
        new_content = body.get("content")
        if new_content is None:
            return error_response(422, "VALIDATION_ERROR", "content is required.")
        post = await svc.edit_post(
            post_id,
            editor_user_id=ctx.user_id,
            new_content=new_content,
        )
        return web.json_response(_serialise(post))

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(feed_service_key)
        post_id = self.match("id")
        await svc.delete_post(post_id, actor_user_id=ctx.user_id)
        return web.Response(status=204)


class PostReactionCollectionView(BaseView):
    """POST /api/feed/posts/{id}/reactions — add a reaction emoji."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(feed_service_key)
        post_id = self.match("id")
        body = await self.body()
        emoji = body.get("emoji", "")
        if not emoji:
            return error_response(422, "VALIDATION_ERROR", "emoji is required.")
        post = await svc.add_reaction(post_id, user_id=ctx.user_id, emoji=emoji)
        return web.json_response(_serialise(post))


class PostReactionDetailView(BaseView):
    """DELETE /api/feed/posts/{id}/reactions/{emoji} — remove a reaction."""

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(feed_service_key)
        post_id = self.match("id")
        emoji = unquote(self.match("emoji"))
        post = await svc.remove_reaction(post_id, user_id=ctx.user_id, emoji=emoji)
        return web.json_response(_serialise(post))


class PostCommentView(BaseView):
    """POST/GET /api/feed/posts/{id}/comments — add or list comments."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(feed_service_key)
        post_id = self.match("id")
        body = await self.body()
        comment = await svc.add_comment(
            post_id,
            author_user_id=ctx.user_id,
            content=body.get("content"),
            media_url=body.get("media_url"),
            comment_type=body.get("type", "text"),
            parent_id=body.get("parent_id"),
        )
        return web.json_response(_serialise(comment), status=201)

    async def get(self) -> web.Response:
        svc = self.svc(feed_service_key)
        post_id = self.match("id")
        comments = await svc.list_comments(post_id)
        return web.json_response([_serialise(c) for c in comments])


class PostCommentDetailView(BaseView):
    """PATCH/DELETE /api/feed/posts/{id}/comments/{cid}."""

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(feed_service_key)
        body = await self.body()
        new_content = body.get("content")
        if new_content is None:
            return error_response(422, "VALIDATION_ERROR", "content is required.")
        comment = await svc.edit_comment(
            self.match("cid"),
            editor_user_id=ctx.user_id,
            new_content=new_content,
        )
        return web.json_response(_serialise(comment))

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(feed_service_key)
        await svc.delete_comment(
            self.match("cid"),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)


class PostSaveView(BaseView):
    """POST/DELETE /api/feed/posts/{id}/save — bookmark / unbookmark a post."""

    async def post(self) -> web.Response:
        ctx = self.user
        post_id = self.match("id")
        repo = self.svc(post_repo_key)
        if await repo.get(post_id) is None:
            return error_response(404, "NOT_FOUND", "Post not found.")
        await repo.save_bookmark(ctx.user_id, post_id)
        return web.json_response({"ok": True, "saved": True})

    async def delete(self) -> web.Response:
        ctx = self.user
        post_id = self.match("id")
        repo = self.svc(post_repo_key)
        await repo.unsave_bookmark(ctx.user_id, post_id)
        return web.json_response({"ok": True, "saved": False})


class SavedPostsView(BaseView):
    """GET /api/feed/saved — list posts the current user has bookmarked."""

    async def get(self) -> web.Response:
        ctx = self.user
        repo = self.svc(post_repo_key)
        posts = await repo.list_bookmarks(ctx.user_id)
        return web.json_response([_serialise(p) for p in posts])
