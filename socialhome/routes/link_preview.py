"""``POST /api/link-preview`` — the composer's live link card.

The composer asks for the preview of the link a member is typing so it can
show the card before the post is sent. The answer comes from the same
server-side builder the post create path uses
(:class:`~socialhome.services.link_preview_service.LinkPreviewService`), so
what the composer shows is what the post will carry — and the call warms
the cache, so submitting the post does not fetch the page again. The
client never supplies preview fields; it can only opt out of the card
(``no_link_preview`` on the post).

Any authenticated member may call it (members are the only authors). A
household with previews switched off answers 403 ``FEATURE_DISABLED``; a
link that yields no card answers ``{"preview": null}``.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import link_preview_service_key, media_signer_key
from ..domain.link_preview import link_preview_to_dict
from ..media_signer import sign_media_urls_in
from ..security import error_response
from .base import BaseView


class LinkPreviewView(BaseView):
    """``POST /api/link-preview`` — body ``{url}`` → ``{preview}``."""

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str) or not url.strip():
            return error_response(422, "VALIDATION_ERROR", "url is required.")
        svc = self.svc(link_preview_service_key)
        preview = await svc.preview_for_url(url.strip(), user_id=ctx.user_id)
        payload: dict = {"preview": link_preview_to_dict(preview)}
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return self._json(payload)
