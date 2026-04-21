"""Search route — ``GET /api/search`` (spec §23.2)."""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..repositories.search_repo import ALLOWED_SCOPES, SCOPE_TYPE_GROUPS
from .base import BaseView


class SearchView(BaseView):
    """``GET /api/search`` — full-text search across scopes.

    Query params:

    * ``q``         — the query text. Under 2 chars → empty result.
    * ``type``      — high-level filter: ``posts``, ``people``,
      ``spaces``, ``pages``, ``messages`` (spec §23.2.3).
    * ``scope``     — low-level scope for API compatibility.
    * ``space_id``  — restrict to a single space.
    * ``limit``     — cap on returned hits (default 20, max 100).

    Response shape::

        {
            "hits":   [{scope, ref_id, space_id, title, snippet}, ...],
            "counts": {"post": 4, "space_post": 1, "user": 2, ...}
        }

    The ``counts`` map lets the client label its filter chips without
    running a second request.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        q = self.request.query.get("q", "").strip()
        type_ = self.request.query.get("type")
        if type_ is not None and type_ not in SCOPE_TYPE_GROUPS:
            return web.json_response({"error": "invalid type"}, status=422)
        scope = self.request.query.get("scope")
        if scope is not None and scope not in ALLOWED_SCOPES:
            return web.json_response({"error": "invalid scope"}, status=422)
        space_id = self.request.query.get("space_id")
        try:
            limit = int(self.request.query.get("limit", 20))
        except ValueError:
            limit = 20
        try:
            offset = int(self.request.query.get("offset", 0))
        except ValueError:
            offset = 0

        svc = self.svc(K.search_service_key)
        result = await svc.search_with_counts(
            q,
            scope=scope,
            type_=type_,
            space_id=space_id,
            caller_user_id=ctx.user_id if ctx else None,
            caller_username=ctx.username if ctx else None,
            limit=limit,
            offset=offset,
        )
        return self._json(
            {
                "hits": [
                    {
                        "scope": h.scope,
                        "ref_id": h.ref_id,
                        "space_id": h.space_id,
                        "title": h.title,
                        "snippet": h.snippet,
                    }
                    for h in result["hits"]
                ],
                "counts": result["counts"],
            }
        )
