"""Base view class for GFS aiohttp routes.

Mirrors the pattern of :class:`socialhome.routes.base.BaseView` but
without the core-auth plumbing — GFS routes authenticate via the
admin-cookie middleware (``/admin/api/*``) or via Ed25519 signatures
on the wire body (``/gfs/*`` and ``/cluster/*``). Either way, the view
itself stays thin: just service access, match-info, body parsing, and
a JSON response helper.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from .. import app_keys as K
from ...domain.errors import PayloadTooLargeError
from ...hardening import read_body_capped

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

    async def bounded_raw(self, max_bytes: int) -> bytes:
        """Read the raw body, refusing anything over *max_bytes* with 413.

        Bounded BEFORE it is buffered: a declared ``Content-Length`` over the
        cap is refused outright, and the read itself is capped so a chunked
        body (which declares no length at all) cannot exceed it either.
        Delegates to :func:`socialhome.hardening.read_body_capped`, which
        reads the stream directly and is therefore independent of the
        app-wide ``client_max_size`` — a route can take a larger (or
        smaller) body than the default without widening every other route.
        GFS views map no ``CodedError``, so the refusal is re-raised as
        aiohttp's own 413.
        """
        try:
            return await read_body_capped(self.request, max_bytes)
        except PayloadTooLargeError as exc:
            declared = self.request.content_length
            raise web.HTTPRequestEntityTooLarge(
                max_size=max_bytes,
                actual_size=declared if declared is not None else max_bytes + 1,
            ) from exc

    async def bounded_json(self, max_bytes: int) -> dict:
        """:meth:`bounded_raw` + parse as a JSON object (400 otherwise)."""
        raw = await self.bounded_raw(max_bytes)
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=f"Invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise web.HTTPBadRequest(reason="Invalid JSON body: expected an object")
        return parsed

    def client_ip(self) -> str:
        """Resolve the caller's address under the trusted-proxy policy.

        Delegates to the server's single
        :class:`~socialhome.global_server.public.ClientIpResolver`, which
        believes ``X-Forwarded-For`` only when the TCP peer is itself a
        trusted proxy. Admin views write this straight into the audit log's
        ``admin_ip``, so a local re-parse of the header would let any caller
        forge the trail.
        """
        return self.svc(K.gfs_client_ip_key)(self.request)
