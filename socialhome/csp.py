"""Content-Security-Policy for the SPA shell.

The bearer token lives in ``localStorage``, so any script that runs in
the SPA's origin owns the account. The CSP is defence in depth behind
DOMPurify / Preact escaping: even if HTML lands in the DOM unsanitised,
the browser refuses to run inline or foreign script.

Every external host the SPA talks to goes through
:data:`SPA_CSP_DIRECTIVES` and :func:`build_spa_csp` — never hand-edit
the header string in a route, and never add an inline ``<script>`` to
``client/index.html`` (move it to a file under ``client/public/assets/``
instead; ``tests/routes/test_spa.py`` fails on an inline script).

Directive notes (what each allowance is for):

* ``script-src 'self'`` — no ``'unsafe-inline'`` / ``'unsafe-eval'``.
  The pre-paint theme bootstrap is the file ``assets/theme-boot.js``
  and the STT AudioWorklet is a Vite-emitted asset, so nothing needs a
  hash or nonce. The bundle has no ``eval`` / ``new Function``.
* ``style-src 'self' https://fonts.googleapis.com`` — bundled CSS plus
  the Google Fonts stylesheet ``client/src/styles/app.css`` @imports.
  ``style-src-attr 'unsafe-inline'`` allows ``style="…"`` attributes
  only: the Leaflet pin / popup HTML strings carry them
  (``LocationMap`` / ``FederationMap``). ``<style>`` elements stay
  blocked. (Preact's ``style={{…}}`` goes through the CSSOM, which CSP
  does not govern.)
* ``font-src`` — bundled Nunito + ``fonts.gstatic.com`` (Google Fonts).
* ``img-src`` — ``data:`` (QR codes, Leaflet CSS sprites), ``blob:``
  (local upload previews) and ``https:`` because Pages / event markdown
  may embed external ``https`` images (``client/src/utils/markdown.ts``).
  Map tiles need no host: the backend proxies them
  (``routes/map_tiles.py``) whatever ``map_tile_url`` is configured, and
  hands Leaflet a relative template. Link-preview thumbnails are local
  media too (``services/link_preview_service.py``).
* ``media-src 'self' blob:`` — signed media + local voice-note /
  video previews. Call streams use ``srcObject``, which CSP ignores.
* ``connect-src 'self'`` — every fetch and WebSocket is same-origin
  (CSP3 ``'self'`` matches ``ws:``/``wss:`` on the same host).
  WebRTC peer connections (calls, federation DataChannels run on the
  backend) are not governed by CSP.
* ``worker-src 'self'`` — the push service worker ``sw.js``. (The STT
  AudioWorklet module is governed by ``script-src``.)
* ``frame-src 'self'`` — sandboxed app bundles
  (``routes/app_bundle.py``, which sets its own stricter CSP), served
  same-origin.
* ``frame-ancestors 'self'`` — in every platform mode. HA's add-on
  panel frames ``/api/hassio_ingress/<token>/`` on HA's own origin (a
  same-origin frame), and an ``ha`` install may sit behind a
  same-origin path-prefix proxy that an HA ``panel_iframe`` frames.
  Same-origin framing gives an attacker nothing; every other origin is
  refused. The legacy ``X-Frame-Options: SAMEORIGIN`` that agrees with
  it is the global default in ``hardening.py`` — no SPA override.
* ``base-uri 'self'`` — ``SpaIndexView`` rewrites ``<base href>`` to
  the ingress prefix, which is a same-origin path. A header smuggling
  ``//evil.example`` into the base is refused by the browser.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping

#: Directive → sources of the SPA shell's policy. Order is kept in the
#: header.
SPA_CSP_DIRECTIVES: Mapping[str, tuple[str, ...]] = {
    "default-src": ("'self'",),
    "script-src": ("'self'",),
    "style-src": ("'self'", "https://fonts.googleapis.com"),
    "style-src-attr": ("'unsafe-inline'",),
    "font-src": ("'self'", "https://fonts.gstatic.com"),
    "img-src": ("'self'", "data:", "blob:", "https:"),
    "media-src": ("'self'", "blob:"),
    "connect-src": ("'self'",),
    "worker-src": ("'self'",),
    "frame-src": ("'self'",),
    "manifest-src": ("'self'",),
    "object-src": ("'none'",),
    "base-uri": ("'self'",),
    "form-action": ("'self'",),
    "frame-ancestors": ("'self'",),
}


def render_csp(directives: Mapping[str, tuple[str, ...]]) -> str:
    """Serialise ``directives`` into a ``Content-Security-Policy`` value.

    Raises :class:`ValueError` on an empty source list or a token that
    would break the header grammar (``;`` / ``,`` / whitespace inside a
    source), so a bad edit fails loudly, not silently in the browser.
    """
    parts: list[str] = []
    for name, sources in directives.items():
        if not sources:
            raise ValueError(f"CSP directive {name!r} has no sources")
        for src in (name, *sources):
            if not src or any(c in src for c in ";, \t\r\n"):
                raise ValueError(f"invalid CSP token {src!r} in {name!r}")
        parts.append(" ".join((name, *sources)))
    return "; ".join(parts)


@functools.cache
def build_spa_csp() -> str:
    """The SPA shell's ``Content-Security-Policy`` (built once, cached).

    The same in every platform mode.
    """
    return render_csp(SPA_CSP_DIRECTIVES)
