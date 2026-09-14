"""Map routes — ``/api/map/config`` + ``/api/map/tiles``.

OpenStreetMap's tile policy requires an identifying ``User-Agent`` and
returns 403 to browsers, which cannot set one (``User-Agent`` and
``Referer`` are forbidden header names). Every map in the SPA therefore
renders grey when Leaflet points straight at the OSM tile servers. The
backend proxies the tiles instead — see
:mod:`socialhome.services.map_tile_service`.

Leaflet loads tiles through ``<img>`` tags, which carry no
``Authorization`` header, and under HA ingress the SPA holds no token at
all. So ``/api/map/config`` hands the SPA a *signed* URL template: one
HMAC (minted by the existing :class:`~socialhome.media_signer.MediaUrlSigner`)
covering the tile path, with the literal ``{z}``/``{x}``/``{y}``
placeholders left in place for Leaflet to substitute client-side.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..app_keys import map_tile_service_key, media_signer_key
from ..security import error_response
from ..services.map_tile_service import MAX_ZOOM
from .base import BaseView

log = logging.getLogger(__name__)

#: Relative (no leading slash) tile URL template handed to Leaflet. It is
#: relative so it resolves against the SPA's ``<base href>`` — under HA
#: ingress the document base carries the Supervisor's path prefix.
TILE_URL_TEMPLATE: str = "api/map/tiles?z={z}&x={x}&y={y}"

#: Lifetime of the signed tile template. A map session can stay open for
#: a long time and every pan mints more tile requests off the *same*
#: signature, so the TTL is a week rather than the media default hour.
TILE_URL_TTL_SECONDS: int = 7 * 24 * 3600

#: Attribution the SPA renders under every map. OSM's licence requires
#: it, and the string must reach the client byte-identical — Leaflet
#: injects it as HTML.
MAP_ATTRIBUTION: str = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    " contributors"
)

#: Browsers may keep a tile for a week — the OSMF tile policy asks
#: consumers to cache aggressively, and this pushes most of that caching
#: onto the browser rather than our in-memory LRU.
#:
#: ``public`` (where the sibling signed-media routes use ``private``) is
#: deliberate: a tile is public OSM data, identical for every user, and a
#: shared cache keys on the full URL — signature included — so nothing
#: user-specific can be served to the wrong person.
_TILE_CACHE_CONTROL: str = "public, max-age=604800"


class MapTileView(BaseView):
    """``GET /api/map/tiles?z=&x=&y=`` — proxy one raster tile.

    Auth is satisfied either by :class:`~socialhome.auth.SignedMediaStrategy`
    (the ``?exp=&sig=`` minted by :class:`MapConfigView`, which is what
    Leaflet's ``<img>`` loads carry) or by a normal bearer token.
    """

    async def get(self) -> web.StreamResponse:
        try:
            # ``get(..., "")`` folds "missing" into the same ValueError
            # branch as "not an integer" — both are a malformed request.
            z = int(self.request.query.get("z", ""))
            x = int(self.request.query.get("x", ""))
            y = int(self.request.query.get("y", ""))
        except ValueError:
            # Parsing, not a domain error: ``BaseView._iter`` maps a
            # bubbled ValueError to 422, and a malformed query is a 400.
            return error_response(
                400,
                "BAD_REQUEST",
                "z, x and y are required and must be integers.",
            )

        tile = await self.svc(map_tile_service_key).fetch(z, x, y)
        return web.Response(
            body=tile.body,
            content_type=tile.content_type,
            headers={"Cache-Control": _TILE_CACHE_CONTROL},
        )


class MapConfigView(BaseView):
    """``GET /api/map/config`` — signed tile template + attribution."""

    async def get(self) -> web.StreamResponse:
        signer = self.request.app.get(media_signer_key)
        if signer is None:
            # Fail closed: an unsigned template would 401 on every tile,
            # so say so instead of shipping a URL that cannot work.
            log.warning("map config requested before the media signer was wired")
            return error_response(
                503,
                "SIGNER_UNAVAILABLE",
                "Map tiles are not available yet.",
            )

        # ``sign`` hashes only the path before ``?``, so this single
        # signature authorises every tile — which is the only workable
        # shape, since Leaflet substitutes z/x/y locally and can never
        # ask the server for a per-tile signature.
        return self._json(
            {
                "tile_url": signer.sign(TILE_URL_TEMPLATE, ttl=TILE_URL_TTL_SECONDS),
                "attribution": MAP_ATTRIBUTION,
                "max_zoom": MAX_ZOOM,
            },
        )
