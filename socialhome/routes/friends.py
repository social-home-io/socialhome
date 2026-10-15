"""Friends routes — /api/friends and /api/friends/{user_id}.

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


class FriendView(BaseView):
    """``GET /api/friends/{user_id}`` — one person, for the person sheet.

    The same row ``GET /api/friends`` would list for them, plus
    ``is_local`` and ``household``. 404 for anyone the Friends page
    would not show the viewer (unknown, blocked, guardian-separated,
    deprovisioned, or only met through a shared space).
    """

    async def get(self) -> web.Response:
        ctx = self.user
        person = await self.svc(friends_service_key).person(
            ctx.user_id, self.match("user_id")
        )
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(person, signer)
        return self._json(person)
