"""Base view class for GFS aiohttp routes.

Mirrors the pattern of :class:`socialhome.routes.base.BaseView` but
without the core-auth plumbing — GFS routes authenticate via the
admin-cookie middleware (``/admin/api/*``) or via Ed25519 signatures
on the wire body (``/gfs/*`` and ``/cluster/*``). Either way, the view
itself stays thin: just service access, match-info, body parsing, and
a JSON response helper.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)


class GfsBaseView(web.View):
    """Shared base for every GFS route view.

    Subclasses define ``async def get/post/patch/delete/put(self)``
    methods. aiohttp dispatches by HTTP method automatically.
    """

    def svc(self, key: web.AppKey) -> Any:
        """Fetch a service / repo from the app container by typed key."""
        return self.request.app[key]

    def match(self, name: str) -> str:
        """Shortcut for ``self.request.match_info[name]``."""
        return self.request.match_info[name]

    async def body(self) -> dict:
        """Parse JSON request body; returns ``{}`` on invalid body.

        Admin mutation routes tolerate a missing / malformed body so a
        subsequent service call can raise with a specific domain error.
        Public wire routes should check fields explicitly.
        """
        try:
            return await self.request.json()
        except Exception:
            return {}

    async def body_or_400(self) -> dict:
        """Parse JSON body or raise 400. Used by public wire endpoints."""
        try:
            return await self.request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                reason=f"Invalid JSON body: {exc}",
            ) from exc

    def client_ip(self) -> str:
        """Extract the client IP, honouring ``X-Forwarded-For``."""
        fwd = self.request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        peer = (
            self.request.transport.get_extra_info("peername")
            if self.request.transport
            else None
        )
        return str(peer[0]) if peer else "unknown"
