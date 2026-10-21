"""JSON for the public pages the pinned viewer renders (:mod:`..public_viewer`).

* ``GET  /gfs/pages/spaces/{space_id}`` — a listed global space's page.
* ``POST /gfs/pages/invite`` ``{"token": …}`` — an invite link's landing.
  A POST so the token rides in the body: the access log keeps the path
  only, and the request line never names it (``GET /join/{token}`` logs
  ``/join/***`` for the same reason).
* ``GET  /gfs/pages/people/{user_id}`` — a person's Momentum profile.

Anonymous, cross-origin (:mod:`..viewer_cors`, no credentials), per-IP
rate limited with the public pages (:data:`..public.LISTING_MAX_PER_MINUTE`)
and pure reads: they show exactly what this server's own HTML pages show,
through the same loaders (:mod:`..page_models`).

Every answer carries ``server_name`` — the not-found pages name the server
too. A miss is ``404`` with that body alone: one answer for "never existed",
"withdrawn" and "expired", as on the HTML pages.

Images go out as ``data:`` URIs only: the space's cover and icon are stored
that way (anything else is dropped, never passed on for the viewer to
fetch), the person's picture is the mirrored bytes, and the invite's QR is
rendered here, as on the HTML page. The viewer's CSP admits no other image
source, so a page never makes a visitor's browser reach a third party.
"""

from __future__ import annotations

import base64
import re

from aiohttp import web

from .. import app_keys as K
from ..html_page import css_color
from ..markdown_lite import MAX_SOURCE_CHARS
from ..page_models import (
    load_invite_page,
    load_person_page,
    load_space_page,
    server_name,
)
from ..public import CATEGORY_LABELS, _render_qr_png_data_uri
from .base import GfsBaseView

#: The invite POST body: ``{"token": "<≤256 chars>"}`` and nothing big.
INVITE_BODY_MAX_BYTES: int = 1024

#: Longest invite token looked up (``public_viewer.MAX_INVITE_TOKEN_CHARS``).
_MAX_TOKEN_CHARS = 256

#: A stored image the viewer may show: a base64 ``data:`` URI of a raster
#: type. An ``https://`` URL (or an SVG, which can carry script) is never
#: handed to the viewer.
_IMAGE_DATA_URI_RE = re.compile(
    r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}"
)

#: Mirrored picture types (``moments_public._PICTURE_MIME_ALLOWED``).
_PICTURE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

_CATEGORIES = frozenset(value for value, _label in CATEGORY_LABELS)

_DEFAULT_ACCENT = "#D2542A"


def image_data_uri(value: str | None) -> str | None:
    """``value`` if it is a raster ``data:`` image the viewer may show."""
    if value and _IMAGE_DATA_URI_RE.fullmatch(value):
        return value
    return None


def _not_found(name: str) -> web.Response:
    return web.json_response({"server_name": name}, status=404)


class SpacePageDataView(GfsBaseView):
    """``GET /gfs/pages/spaces/{space_id}`` — the space page's content."""

    async def get(self) -> web.Response:
        app = self.request.app
        name = await server_name(app)
        space = await load_space_page(app, self.match("space_id"))
        if space is None:
            return _not_found(name)
        primary = css_color(
            space["primary_color"],
            css_color(space["accent_color"], _DEFAULT_ACCENT),
        )
        category = space["category"] if space["category"] in _CATEGORIES else None
        return web.json_response(
            {
                "server_name": name,
                "space": {
                    "space_id": space["space_id"],
                    "name": space["name"] or "",
                    "description": space["description"] or "",
                    "about_markdown": (space["about_markdown"] or "")[
                        :MAX_SOURCE_CHARS
                    ],
                    "cover": image_data_uri(space["cover_url"]),
                    "icon": image_data_uri(space["icon_url"]),
                    "primary_color": primary,
                    "accent_color": css_color(space["accent_color"], primary),
                    "member_count": int(space["subscriber_count"] or 0),
                    "category": category or "general",
                },
            }
        )


class InvitePageDataView(GfsBaseView):
    """``POST /gfs/pages/invite`` — an invite landing's content.

    A pure READ, like ``GET /join/{token}``: no use counter, no row, no log
    line of its own.
    """

    async def post(self) -> web.Response:
        body = await self.bounded_json(INVITE_BODY_MAX_BYTES)
        token = body.get("token")
        if not isinstance(token, str) or not 0 < len(token) <= _MAX_TOKEN_CHARS:
            raise web.HTTPBadRequest(reason="Invalid field: token")
        app = self.request.app
        name = await server_name(app)
        invite = await load_invite_page(app, token)
        if invite is None:
            return _not_found(name)
        code = invite["invite_code"]
        resp = web.json_response(
            {
                "server_name": name,
                "space": {
                    "name": invite["name"] or "",
                    "accent_color": css_color(invite["accent_color"], _DEFAULT_ACCENT),
                    "icon": image_data_uri(invite["icon_url"]),
                },
                "invite_code": code,
                "qr": await _render_qr_png_data_uri(code),
            }
        )
        # It carries the code a redeemer needs: no cache may keep it.
        resp.headers["Cache-Control"] = "no-store"
        return resp


class PersonPageDataView(GfsBaseView):
    """``GET /gfs/pages/people/{user_id}`` — a person's profile + Follow."""

    async def get(self) -> web.Response:
        app = self.request.app
        name = await server_name(app)
        user_id = self.match("user_id")
        page = await load_person_page(app, user_id)
        if page is None:
            return _not_found(name)
        reg = page.registration
        picture: str | None = None
        if reg.picture_digest:
            pic = await self.svc(K.gfs_user_picture_repo_key).get(user_id)
            if pic is not None and pic.mime in _PICTURE_TYPES:
                picture = f"data:{pic.mime};base64," + base64.b64encode(
                    pic.bytes_
                ).decode("ascii")
        return web.json_response(
            {
                "server_name": name,
                "gfs_id": self.svc(K.gfs_config_key).instance_id,
                "person": {
                    "user_id": reg.user_id,
                    "username": reg.username,
                    "display_name": reg.display_name,
                    "instance_id": reg.instance_id,
                    "bio": reg.bio or "",
                    "picture": picture,
                    "follower_count": page.follower_count,
                },
            }
        )
