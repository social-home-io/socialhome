"""The household dashboard's Content-Security-Policy.

Same-origin only; no third-party host of any kind (no web fonts, no
CDNs). Scripts are files under ``/static/``; there is no
``'unsafe-inline'``. A page's one inline ``<style>`` (validated colours
and the household theme, :mod:`.presenter`) is admitted by its
sha256 hash — :func:`build_dashboard_csp` with the exact text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..csp import render_csp, style_hash

__all__ = ["DASHBOARD_CSP_DIRECTIVES", "build_dashboard_csp"]

DASHBOARD_CSP_DIRECTIVES: Mapping[str, tuple[str, ...]] = {
    "default-src": ("'self'",),
    "script-src": ("'self'",),
    "style-src": ("'self'",),
    "img-src": ("'self'", "data:"),
    "connect-src": ("'self'",),
    "font-src": ("'self'",),
    "object-src": ("'none'",),
    "base-uri": ("'none'",),
    "form-action": ("'self'",),
    "frame-ancestors": ("'none'",),
}


def build_dashboard_csp(inline_styles: Iterable[str] = ()) -> str:
    """The dashboard policy, admitting exactly ``inline_styles`` by hash."""
    hashes = tuple(dict.fromkeys(style_hash(css) for css in inline_styles))
    directives = dict(DASHBOARD_CSP_DIRECTIVES)
    directives["style-src"] = (*directives["style-src"], *hashes)
    return render_csp(directives)
