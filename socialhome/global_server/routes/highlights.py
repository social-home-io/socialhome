"""GFS public-highlight routes (§highlights_public).

Two surfaces:

* **Signed wire endpoints** under ``/gfs/highlights/*`` and
  ``/gfs/highlight_tokens/*`` — used by author SH instances to publish,
  mint additional tokens, revoke single tokens, and unpublish. All
  bodies are Ed25519-signed and reuse the existing
  :func:`_rtc_authenticate` middleware in :mod:`global_server.routes.rtc`.
* **Public landing page** ``GET /highlight/{instance_id}/{highlight_id}/{token}`` —
  served as plain HTML to anyone with the URL. PR1 returns a placeholder
  with ``200 / 410 / 503`` shape; PR2 wires the WebRTC bootstrap script.

The author's instance is the only entity that holds highlight bytes; this
GFS only relays SDP/ICE later.
"""

from __future__ import annotations

import html as html_lib
import logging

from aiohttp import web

from .. import app_keys as K
from ..html_page import html_response
from ..safe_embed import script_json
from .base import GfsBaseView
from .rtc import _rtc_authenticate

log = logging.getLogger(__name__)


# ─── Signed wire endpoints (author SH → GFS) ─────────────────────────────


class HighlightPublishView(GfsBaseView):
    """``POST /gfs/highlights/{highlight_id}/publish``.

    Records a fresh publication and mints the first share token.
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        highlight_id = self.match("highlight_id")
        if str(body.get("highlight_id") or "") != highlight_id:
            return web.json_response(
                {"error": "highlight_id_mismatch"},
                status=422,
            )
        try:
            expires_at = int(body.get("expires_at") or 0)
        except TypeError, ValueError:
            return web.json_response({"error": "invalid_expires_at"}, status=422)
        if expires_at <= 0:
            return web.json_response({"error": "invalid_expires_at"}, status=422)
        label_raw = body.get("label")
        label = str(label_raw) if label_raw else None

        registry = self.svc(K.gfs_highlight_pub_service_key)
        # ``_rtc_authenticate`` already verified the Ed25519 signature
        # over the canonical body. PR2 will additionally cache the raw
        # signature for offline audit; for PR1 we record an empty
        # string and rely on the live-verification path to gate writes.
        token, url = await registry.record_publish(
            highlight_id=highlight_id,
            instance_id=instance_id,
            expires_at=expires_at,
            publish_signature="",
            label=label,
        )
        return web.json_response(
            {"token": token.token, "url": url, "label": token.label},
            status=201,
        )


class HighlightTokenMintView(GfsBaseView):
    """``POST /gfs/highlights/{highlight_id}/tokens`` — mint another share token
    under an existing publication."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        highlight_id = self.match("highlight_id")
        label_raw = body.get("label")
        label = str(label_raw) if label_raw else None

        registry = self.svc(K.gfs_highlight_pub_service_key)
        try:
            token, url = await registry.mint_token(
                highlight_id=highlight_id,
                instance_id=instance_id,
                label=label,
            )
        except LookupError:
            return web.json_response(
                {"error": "publication_not_found"},
                status=404,
            )
        return web.json_response(
            {"token": token.token, "url": url, "label": token.label},
            status=201,
        )


class HighlightTokenRevokeView(GfsBaseView):
    """``POST /gfs/highlight_tokens/{token}/revoke``."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        _body, instance_id = result
        token = self.match("token")
        registry = self.svc(K.gfs_highlight_pub_service_key)
        revoked = await registry.revoke_token(token, instance_id)
        if not revoked:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"status": "ok"})


class HighlightUnpublishView(GfsBaseView):
    """``POST /gfs/highlights/{highlight_id}/unpublish`` — drop the publication
    row + every token under it (CASCADE)."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        _body, instance_id = result
        highlight_id = self.match("highlight_id")
        registry = self.svc(K.gfs_highlight_pub_service_key)
        removed = await registry.remove_publish(highlight_id, instance_id)
        if not removed:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"status": "ok"})


# ─── Public landing page ─────────────────────────────────────────────────


class HighlightPublicLandingView(GfsBaseView):
    """``GET /highlight/{instance_id}/{highlight_id}/{token}`` — public viewer.

    PR1 returns a static placeholder body and the 410 / 503 logic so
    the publish/revoke contract can be tested end-to-end without the
    PR2 WebRTC layer. PR2 swaps the body for the actual bootstrap
    HTML + ``<script src="/static/highlight-public-viewer.js">``.
    """

    async def get(self) -> web.Response:
        instance_id = self.match("instance_id")
        highlight_id = self.match("highlight_id")
        token = self.match("token")

        registry = self.svc(K.gfs_highlight_pub_service_key)
        resolved = await registry.resolve_token(token)
        if resolved is None:
            return _gone_html(instance_id, highlight_id)

        # Token-vs-URL consistency check — guards against someone
        # crafting a URL that mixes the right token with a wrong
        # highlight_id (e.g. trying to enumerate other highlights on the
        # same instance).
        pub = resolved.publication
        if pub.highlight_id != highlight_id or pub.instance_id != instance_id:
            return _gone_html(instance_id, highlight_id)

        if not await registry.author_online(instance_id):
            return _unavailable_html(instance_id, highlight_id)

        # Boot payload — the bootstrap JS reads this <script id="boot">
        # tag for the (instance_id, highlight_id, token) triple it needs to
        # POST /gfs/highlight_rtc/offer. Keeping the values inline lets the
        # JS bundle stay zero-state — no URL parsing on the client side.
        # ``script_json`` (not bare ``json.dumps``) so an instance_id /
        # highlight_id containing ``</script>`` can't break out of the
        # inline <script> block — stored XSS on this anonymous page.
        boot = script_json(
            {"instanceId": instance_id, "highlightId": highlight_id, "token": token}
        )
        body = (
            "<!doctype html><html lang='en'><head>"
            "<meta charset='utf-8'>"
            # ``<base href>`` anchors the relative ``static/...`` script src
            # below to the GFS root rather than the deep
            # ``/highlight/<inst>/<hl>/<token>`` document URL. A future
            # path-prefixed deployment rewrites this to the prefix.
            "<base href='/'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Highlight</title>"
            "<meta property='og:title' content='A highlight shared with you'>"
            "<meta property='og:type' content='website'>"
            f"<style>{_VIEWER_CSS}</style>"
            "</head><body>"
            "<div id='root'></div>"
            f"<script id='boot' type='application/json'>{boot}</script>"
            "<script type='module' src='static/highlight_public_viewer.js'></script>"
            "</body></html>"
        )
        return html_response(body, inline_styles=[_VIEWER_CSS])


# ─── Internal helpers ────────────────────────────────────────────────────


#: The viewer page's inline ``<style>`` — admitted by its sha256 in the
#: public-page CSP (``html_page.html_response``).
_VIEWER_CSS = (
    "html,body{margin:0;padding:0;background:#111;color:#eee;"
    "font-family:system-ui,sans-serif;height:100%;}"
    "#root{height:100vh;display:flex;}"
    ".highlight-viewer{display:flex;flex-direction:column;width:100%;}"
    ".progress{display:flex;gap:4px;padding:12px;}"
    ".progress .seg{flex:1;height:3px;background:rgba(255,255,255,.2);border-radius:2px;}"
    ".progress .seg.done{background:rgba(255,255,255,.6);}"
    ".progress .seg.active{background:#fff;}"
    ".stage{flex:1;display:flex;align-items:center;justify-content:center;"
    "padding:0 12px;position:relative;}"
    ".stage img,.stage video{max-width:100%;max-height:100%;border-radius:8px;}"
    ".caption{position:absolute;bottom:24px;padding:8px 14px;"
    "background:rgba(0,0,0,.5);border-radius:8px;max-width:80%;}"
    ".highlight-error,.highlight-end{padding:24px;text-align:center;width:100%;}"
    ".status{color:#aaa;font-size:.9em;text-align:center;}"
)

#: Fallback style used by the gone/unavailable HTML responses (inline
#: ``<style>``, admitted by its sha256 like :data:`_VIEWER_CSS`).
#: Token values mirror ``--sh-*`` from ``client/src/styles/tokens.css`` —
#: paper / ink / hairline / hearth — so the failure pages read as
#: part of the SH product family rather than a stark federation
#: error screen. We don't try to load the Manrope/Fraunces webfont
#: here (failure path; no preconnect handshake to spend) — system
#: fallbacks are fine for two short paragraphs.
_FALLBACK_CSS = (
    "html,body{margin:0;padding:0;background:#F4ECE0;color:#1A1814;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "system-ui,sans-serif;min-height:100%;line-height:1.55;}"
    "main{max-width:560px;margin:0 auto;padding:48px 24px;"
    "text-align:center;}"
    "h1{font-family:'Iowan Old Style','Palatino Linotype',Georgia,serif;"
    "font-size:28px;letter-spacing:-0.01em;margin:0 0 12px;}"
    "p{color:#807766;margin:0 0 16px;}"
    "small{color:#A8A090;font-size:11px;}"
    ".accent-bar{height:6px;background:#D2542A;border-radius:3px;"
    "max-width:120px;margin:0 auto 24px;}"
)


def _gone_html(instance_id: str, highlight_id: str) -> web.Response:
    body = (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Highlight expired</title>"
        f"<style>{_FALLBACK_CSS}</style>"
        "</head><body><main>"
        "<div class='accent-bar'></div>"
        "<h1>This highlight has ended</h1>"
        "<p>The link is no longer valid — the author may have "
        "unpublished it, the highlight expired, or this token was revoked.</p>"
        # Both come from the URL path (router-decoded) — escape.
        f"<p><small>{html_lib.escape(instance_id)}/"
        f"{html_lib.escape(highlight_id)}</small></p>"
        "</main></body></html>"
    )
    return html_response(body, inline_styles=[_FALLBACK_CSS], status=410)


def _unavailable_html(instance_id: str, highlight_id: str) -> web.Response:
    body = (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Highlight unavailable</title>"
        "<meta http-equiv='refresh' content='10'>"
        f"<style>{_FALLBACK_CSS}</style>"
        "</head><body><main>"
        "<div class='accent-bar'></div>"
        "<h1>Currently unavailable</h1>"
        "<p>The author's instance is offline. This page will retry "
        "automatically in 10 seconds.</p>"
        # Both come from the URL path (router-decoded) — escape.
        f"<p><small>{html_lib.escape(instance_id)}/"
        f"{html_lib.escape(highlight_id)}</small></p>"
        "</main></body></html>"
    )
    return html_response(body, inline_styles=[_FALLBACK_CSS], status=503)
