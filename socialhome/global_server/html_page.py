"""HTML responses for the GFS's anonymous, server-rendered public pages.

Every public page — the landing (``/``), ``/spaces/{id}``,
``/join/{token}``, the highlight viewer and its gone / unavailable pages,
and the Momentum directory (``/moments``, ``/moments/{user}``) — goes out
through :func:`html_response`, which attaches the strict public-page
``Content-Security-Policy`` (:func:`socialhome.csp.build_public_page_csp`).

Rules for a page served this way (``tests/global_server/conftest.py``
``assert_strict_public_page`` enforces them):

* No inline executable ``<script>`` — ship a file under
  ``global_server/static/`` (e.g. ``copy_button.js``). An inert
  ``<script type="application/json">`` boot block is fine.
* No ``style="…"`` attributes or ``on*=`` handlers.
* An inline ``<style>`` is allowed only when its exact text is passed in
  ``inline_styles`` (the policy then carries its sha256).
* Any owner-supplied colour that lands in that ``<style>`` goes through
  :func:`css_color` first — the hash admits whatever text it covers, so
  the text must not carry attacker CSS.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from aiohttp import web

from ..csp import build_public_page_csp

#: ``#rgb`` / ``#rgba`` / ``#rrggbb`` / ``#rrggbbaa`` — the only colour shape
#: an owner may put into a public page's stylesheet.
_HEX_COLOR_RE = re.compile(
    r"#(?:[0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})",
)


def css_color(value: str | None, default: str) -> str:
    """``value`` if it is a plain hex colour, else ``default``."""
    if value and _HEX_COLOR_RE.fullmatch(value):
        return value
    return default


def html_response(
    html: str,
    *,
    inline_styles: Iterable[str] = (),
    status: int = 200,
) -> web.Response:
    """A ``text/html`` response carrying the strict public-page CSP.

    ``inline_styles`` is the exact text of every ``<style>`` element in
    ``html``.
    """
    return web.Response(
        text=html,
        content_type="text/html",
        status=status,
        headers={"Content-Security-Policy": build_public_page_csp(inline_styles)},
    )
