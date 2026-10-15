"""Friends route — /api/friends.

A non-admin, social-shape view of "everyone we're connected to": the
local household members + every social remote household plus its
members, each with presence, status and bio. The payload is built by
:class:`~socialhome.services.friends_service.FriendsService` (privacy
rules live there); this view only signs the avatar URLs.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import friends_service_key, media_signer_key
from ..media_signer import sign_media_urls_in
from .base import BaseView


class FriendsView(BaseView):
    """``GET /api/friends`` — the connected-people dashboard payload.

    Open to any authenticated household member. Every field is
    informational and whitelisted — no routing or session keys.
    """

    async def get(self) -> web.Response:
        ctx = self.user  # auth check; raises 401 for anonymous
        payload = await self.svc(friends_service_key).overview(ctx.user_id)
        # Sign nested ``picture_url`` so the Friends grid avatars load
        # in plain ``<img src>`` without a Bearer token attached.
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return self._json(payload)
