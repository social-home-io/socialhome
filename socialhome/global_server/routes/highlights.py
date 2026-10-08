"""GFS public-highlight routes (§highlights_public).

Two surfaces:

* **Signed wire endpoints** under ``/gfs/highlights/*`` and
  ``/gfs/highlight_tokens/*`` — used by author SH instances to publish,
  mint additional tokens, revoke single tokens, and unpublish. All
  bodies are Ed25519-signed and reuse the existing
  :func:`_rtc_authenticate` middleware in :mod:`global_server.routes.rtc`.
* **Public landing page** ``GET /highlight/{instance_id}/{highlight_id}/{token}`` —
  served as plain HTML to anyone with the URL. Always the same viewer
  shell (no state lookup), so the page is not an author-presence oracle;
  the viewer's offer call is where state is consulted.

The author's instance is the only entity that holds highlight bytes; this
GFS only relays SDP/ICE later.
"""

from __future__ import annotations

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

    Always serves the same viewer shell, whatever the state behind the
    URL: no token lookup, no author-presence check. The GFS must not be a
    presence (or token-validity) oracle, so a live, revoked, expired,
    never-issued or offline-author link all render identically; the
    viewer's ``POST /gfs/highlight_rtc/offer`` is the one place state is
    consulted, and it answers every non-success state with the uniform
    ``503`` (:mod:`..public_unavailable`) the viewer shows as "This isn't
    available right now."
    """

    async def get(self) -> web.Response:
        instance_id = self.match("instance_id")
        highlight_id = self.match("highlight_id")
        token = self.match("token")

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
        resp = html_response(body, inline_styles=[_VIEWER_CSS])
        # The URL and the boot JSON carry the share token — a bearer
        # credential — so neither a shared nor a browser cache may keep it.
        resp.headers["Cache-Control"] = "no-store"
        return resp


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
    ".highlight-error,.highlight-end{padding:24px;text-align:center;width:100%;"
    "align-self:center;}"
    ".highlight-error p{margin:0 0 16px;}"
    ".viewer-retry{min-height:40px;padding:8px 18px;border-radius:999px;"
    "border:1px solid rgba(255,255,255,.35);background:transparent;color:#eee;"
    "font:inherit;cursor:pointer;}"
    ".viewer-retry:hover{border-color:#fff;}"
    ".viewer-retry:focus-visible{outline:2px solid #fff;outline-offset:2px;}"
    ".status{color:#aaa;font-size:.9em;text-align:center;}"
)
