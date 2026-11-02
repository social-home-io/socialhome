"""Share household photos out as a copy (gallery D8).

* ``GET  /api/gallery/share/targets`` — where the caller may share: the
  household feed, their spaces (post / album, how many households) and
  the chats that take photos, plus the limits.
* ``POST /api/gallery/share`` — copy the picked photos and videos there.

The copy is the stored WebP / WebM, never an original, and goes through
the target's own service — see :mod:`socialhome.services.gallery_share_service`.
"""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..domain.gallery_share import LongVideo, ShareTarget
from .base import BaseView

_GALLERY_SECTION = "gallery"


def _opt_str(body: dict, key: str) -> str | None:
    """An optional string field; anything else counts as left out."""
    value = body.get(key)
    return value if isinstance(value, str) and value else None


class GalleryShareTargetsView(BaseView):
    """GET /api/gallery/share/targets — where a copy may go."""

    async def get(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        targets = await self.svc(K.gallery_share_service_key).targets(
            actor_user_id=ctx.user_id
        )
        return web.json_response(targets.to_dict())


class GalleryShareView(BaseView):
    """POST /api/gallery/share — ``{item_ids, target, space_id?, album_id?,
    new_album_name?, conversation_id?, text?, long_videos?}``.

    ``target`` is ``feed`` / ``space_post`` / ``space_album`` / ``chat``;
    ``long_videos`` (``trim`` / ``skip``) says what happens to a video
    longer than a minute. Answers ``{shared, skipped: [{item_id,
    reason}], trimming, queued}``."""

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        body = await self.body()
        ids = body.get("item_ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("item_ids must be a list of ids")
        try:
            target = ShareTarget(str(body.get("target", "")))
            long_videos = LongVideo(str(body.get("long_videos") or LongVideo.SKIP))
        except ValueError:
            raise ValueError("unknown target or long_videos") from None
        result = await self.svc(K.gallery_share_service_key).share(
            ids,
            actor_user_id=ctx.user_id,
            target=target,
            space_id=_opt_str(body, "space_id"),
            album_id=_opt_str(body, "album_id"),
            new_album_name=_opt_str(body, "new_album_name"),
            conversation_id=_opt_str(body, "conversation_id"),
            text=_opt_str(body, "text"),
            long_videos=long_videos,
        )
        return web.json_response(result.to_dict())
