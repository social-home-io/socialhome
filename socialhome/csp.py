"""Content-Security-Policy for the SPA shell and for stored media.

The bearer token lives in ``localStorage``, so any script that runs in
the SPA's origin owns the account. The CSP is defence in depth behind
DOMPurify / Preact escaping: even if HTML lands in the DOM unsanitised,
the browser refuses to run inline or foreign script.

Stored user files (``/api/media/*``, profile / space pictures, GFS
picture proxies) are the other way script could reach the origin: a
stored ``.svg`` / ``.html`` opened directly would run as a document on
our origin. :func:`media_response_headers` closes that — see the
"Stored media" section at the bottom.

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
  the ingress prefix, which is a same-origin path. ``SpaIndexView``
  only accepts the ``/api/hassio_ingress/<token>`` shape under
  ``Capability.INGRESS``; a base smuggling ``//evil.example`` past it
  would still be refused by the browser.
"""

from __future__ import annotations

import functools
import re
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


# ── Stored media ─────────────────────────────────────────────────────────

#: Content types a stored file may be served *inline* (sandboxed) as,
#: besides :data:`PLAYABLE_MEDIA_TYPES`. Everything else — SVG, HTML,
#: XML, JS, foreign audio / video, unknown — is served as an
#: ``application/octet-stream`` attachment, so it downloads instead of
#: rendering on our origin.
INLINE_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/avif",
        "application/pdf",
        "text/plain",
        "text/csv",
    },
)

#: Audio / video served inline *without* ``sandbox`` (see
#: :data:`PLAYABLE_MEDIA_CSP`) — exactly the types the server writes
#: itself, as ``mimetypes`` names their stored extension:
#:
#: * ``video/webm`` — ``VideoProcessor`` / ``MediaTranscodeService``
#:   output (video posts, gallery, moments, DM video), and the ``.webm``
#:   voice note ``AudioProcessor`` keeps from Chromium.
#: * ``audio/ogg`` — ``AudioProcessor`` ``.ogg`` (Firefox voice notes).
#: * ``audio/mp4`` — ``AudioProcessor`` ``.m4a`` (Safari voice notes).
#: * ``audio/webm`` — the declared type of a Chromium voice note on the
#:   federation DM path (``federation_inbound_service._MEDIA_MIME_EXT``).
#:
#: Any other ``audio/*`` / ``video/*`` (``.mp3`` / ``.mov`` / playlists
#: stored through the file passthrough) gets the sandboxed attachment —
#: the server never produced it, so nothing needs it to play in place.
PLAYABLE_MEDIA_TYPES: frozenset[str] = frozenset(
    {"video/webm", "audio/ogg", "audio/webm", "audio/mp4"},
)

#: Directives for a stored file opened directly as a document: no script
#: at all; the browser's own image / media document may load the file
#: itself (``'self'``) and style it (``'unsafe-inline'``). ``form-action``
#: does not fall back to ``default-src``, so it is spelled out.
MEDIA_CSP_DIRECTIVES: Mapping[str, tuple[str, ...]] = {
    "default-src": ("'none'",),
    "img-src": ("'self'", "data:"),
    "media-src": ("'self'",),
    "style-src": ("'unsafe-inline'",),
    "form-action": ("'none'",),
}

#: ``PLAYABLE_MEDIA_CSP`` + ``sandbox`` (opaque origin, no script, no
#: forms, no storage access). Images, text and PDFs still display in
#: Chromium under it (checked against Chromium 152, PDF viewer included).
PLAYABLE_MEDIA_CSP: str = render_csp(MEDIA_CSP_DIRECTIVES)
MEDIA_CSP: str = f"{PLAYABLE_MEDIA_CSP}; sandbox"
# ``PLAYABLE_MEDIA_CSP`` exists because Chromium's video / audio document
# under ``sandbox`` gets an opaque origin, re-fetches its own ``src`` as
# a cross-origin request and fails CORS — the file would not play. A
# media document runs no page script, and ``default-src 'none'`` still
# refuses any, so dropping ``sandbox`` for :data:`PLAYABLE_MEDIA_TYPES`
# costs nothing.

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def media_response_headers(content_type: str | None, filename: str) -> dict[str, str]:
    """``Content-Type`` / ``Content-Disposition`` / CSP for a stored file.

    ``content_type`` is whatever the caller guessed or was told (file
    extension, a remote ``Content-Type`` header) — untrusted. Types in
    :data:`INLINE_MEDIA_TYPES` or :data:`PLAYABLE_MEDIA_TYPES` are served
    inline; anything else becomes an ``application/octet-stream``
    attachment. Every response carries a script-free CSP. Pair with
    ``X-Content-Type-Options: nosniff`` (the global hardening default).
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    playable = mime in PLAYABLE_MEDIA_TYPES
    if playable or mime in INLINE_MEDIA_TYPES:
        disposition = "inline"
    else:
        mime, disposition = "application/octet-stream", "attachment"
    safe_name = _UNSAFE_FILENAME_CHARS.sub("_", filename)
    return {
        "Content-Type": mime,
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
        "Content-Security-Policy": PLAYABLE_MEDIA_CSP if playable else MEDIA_CSP,
    }
