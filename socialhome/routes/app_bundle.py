"""Sandboxed app bundle serving + /runtime endpoint (§SHApps).

Two views:

* ``AppRuntimeView`` — bearer-authed ``GET /api/apps/{app_id}/runtime``.
  Mints a short-lived prefix-signed URL and returns the entry point.

* ``AppBundleView`` — ``GET /api/apps/{app_id}/bundle/{tail:.*}``.
  Self-authorizing: the request carries either the ``?exp=&sig=``
  prefix signature on the entry load, or a short-lived path-scoped
  cookie for sub-resources. Served inside a sandboxed iframe via
  strict CSP + ``X-Frame-Options: SAMEORIGIN``.

MANDATORY IFRAME EMBEDDING CONTRACT
====================================
Bundles are served same-origin with the SH backend.  The SPA MUST embed
them as::

    <iframe sandbox="allow-scripts" src="...">

WITHOUT ``allow-same-origin``.  If ``allow-same-origin`` is present the
iframe shares the document origin and can read ``window.parent.localStorage``
(where the bearer token lives), call ``window.parent.fetch`` to bypass
``connect-src 'none'``, and exfiltrate credentials.

``connect-src 'none'`` alone is NOT sufficient — a same-origin iframe
ignores the parent's CSP for its own requests.  The ``sandbox`` attribute
without ``allow-same-origin`` is the *only* reliable boundary.

Never add ``allow-same-origin`` to the iframe sandbox — it voids the
entire security model of the apps surface.
"""

from __future__ import annotations

import logging
import mimetypes
import pathlib
import urllib.parse

import aiofiles.os
from aiohttp import web

from ..app_keys import app_service_key, config_key, media_signer_key
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)

#: Signed-URL lifetime for bundle entry + sub-resource cookie.
BUNDLE_TTL_SECONDS: int = 300

#: Cookie name prefix — the full name is ``{prefix}{app_id}``.
BUNDLE_COOKIE_PREFIX: str = "sh_app_bundle_"

#: CSP applied to every bundle response.  ``connect-src 'none'`` is the
#: key security invariant — the sandboxed app may NOT make outbound network
#: requests that would bypass the SPA's API layer.
#: ``worker-src 'none'`` blocks service-worker registration; without it a
#: service worker would inherit ``script-src`` and escape ``connect-src
#: 'none'`` by intercepting fetch events.
#: ``frame-ancestors 'self'`` is belt-and-suspenders with X-Frame-Options to
#: prevent embedding by third-party origins.
APP_CSP: str = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; media-src 'self' data:; "
    "connect-src 'none'; worker-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self'"
)


class AppRuntimeView(BaseView):
    """``GET /api/apps/{app_id}/runtime`` — mint a signed entry URL.

    Bearer-authed.  Returns the signed entry URL for the app iframe
    together with lightweight metadata (name, capabilities,
    ``self_user_id``) so the SPA can wire the sandbox without a second
    round-trip.
    """

    async def get(self) -> web.Response:
        user = self.user  # bearer auth check

        app_id = self.match("app_id")
        svc = self.svc(app_service_key)
        app = await svc.get(app_id)
        if app is None:
            return error_response(404, "NOT_FOUND", "App not found.")
        if not app.enabled:
            return error_response(403, "FORBIDDEN", "App is disabled.")

        # Age gate — a protected minor must not receive the signed entry URL.
        # Raises AppAgeRestrictedError which maps to 403 via BaseView._iter.
        await svc.assert_age_allowed(app, user.user_id)

        prefix = f"/api/apps/{app_id}/bundle/"
        signer = self.request.app[media_signer_key]
        signed = signer.sign(prefix, ttl=BUNDLE_TTL_SECONDS)

        # Parse exp / sig out of the signed URL.
        parsed = urllib.parse.urlsplit(signed)
        qs = urllib.parse.parse_qs(parsed.query)
        exp = qs.get("exp", [""])[0]
        sig = qs.get("sig", [""])[0]

        entry_url = f"{prefix}{app.manifest.entry}?exp={exp}&sig={sig}"
        return self._json(
            {
                "app_id": app_id,
                "name": app.name,
                "entry_url": entry_url,
                "self_user_id": user.user_id,
                "capabilities": list(app.manifest.capabilities),
            }
        )


class AppBundleView(BaseView):
    """``GET /api/apps/{app_id}/bundle/{tail:.*}`` — serve a bundle file.

    Self-authorizing (the path is in ``_DEFAULT_PUBLIC_PATH_PATTERNS``).
    Authorization is via one of:

    1. Query ``?exp=&sig=`` — a prefix signature over ``/api/apps/{id}/bundle/``
       minted by ``AppRuntimeView``.  Valid on the entry load; the handler then
       drops a short-lived path-scoped cookie.
    2. Cookie ``sh_app_bundle_{app_id}`` holding ``"{exp}.{sig}"`` — set by
       the entry response and used for all subsequent sub-resource loads inside
       the iframe.

    The file is served with strict CSP + ``X-Frame-Options: SAMEORIGIN``
    so browser plugins can't hijack the iframe, and the sandboxed app
    can't escape its iframe origin.
    """

    async def get(self) -> web.StreamResponse:
        app_id = self.match("app_id")
        tail = self.match("tail")

        prefix = f"/api/apps/{app_id}/bundle/"
        signer = self.request.app[media_signer_key]

        # ── Authorization ────────────────────────────────────────────────
        from_query = False
        authorized = False

        # Branch 1: query-string sig on entry load.
        exp = self.request.query.get("exp", "")
        sig = self.request.query.get("sig", "")
        if exp and sig and signer.verify(prefix, exp, sig):
            authorized = True
            from_query = True

        # Branch 2: cookie for sub-resource loads.
        if not authorized:
            cookie_val = self.request.cookies.get(f"{BUNDLE_COOKIE_PREFIX}{app_id}", "")
            if cookie_val:
                # sig is base64url — may not contain dots, but split safely.
                parts = cookie_val.split(".", 1)
                if len(parts) == 2:
                    c_exp, c_sig = parts
                    if signer.verify(prefix, c_exp, c_sig):
                        authorized = True

        if not authorized:
            return error_response(403, "FORBIDDEN", "Unauthorized bundle access.")

        # ── App lookup ───────────────────────────────────────────────────
        app = await self.svc(app_service_key).get(app_id)
        if app is None:
            return error_response(404, "NOT_FOUND", "App not found.")
        # Recheck enabled here (not just at runtime-mint time) so a disabled
        # app's residual access window is zero — a valid sig from before the
        # disable is immediately invalidated rather than lasting the 300-second
        # TTL.
        if not app.enabled:
            return error_response(403, "FORBIDDEN", "App is disabled.")

        # NOTE: The age gate CANNOT be rechecked here.
        # This route is unauthenticated (signed-URL / cookie, no user identity),
        # so we have no user_id to look up a protection record against.
        # The age gate is enforced at two authenticated boundaries:
        #   1. AppRuntimeView.get (/runtime, bearer-authed) — raises
        #      AppAgeRestrictedError and refuses to mint the signed bundle URL.
        #   2. AppDetailView.get, store ops, and federation fan-out — all
        #      check assert_age_allowed before any privileged action.
        # A signed URL minted while the app was unrestricted remains valid for
        # at most BUNDLE_TTL_SECONDS (currently 300 s) if an admin raises
        # min_age mid-session.  This window leaks only static bundle *file
        # reads* — no store writes, no federation sends, no session opens can
        # occur (those are gated at their own authenticated endpoints).
        # Rebuilding the signed-URL model to embed user identity would allow
        # a per-request age recheck here, but the short TTL bound makes the
        # risk acceptable and the change is deferred.

        # ── File resolution + path-traversal guard ───────────────────────
        config = self.svc(config_key)
        apps_root = pathlib.Path(config.apps_path).resolve()
        base = (pathlib.Path(config.apps_path) / app.bundle_path).resolve()
        if not base.is_relative_to(apps_root):
            log.warning(
                "bundle serve: bundle_path %r escapes apps root",
                app.bundle_path,
            )
            return error_response(403, "FORBIDDEN", "Path traversal blocked.")

        rel = tail if tail else app.manifest.entry
        target = (base / rel).resolve()

        if not target.is_relative_to(base):
            return error_response(403, "FORBIDDEN", "Path traversal blocked.")

        if not await aiofiles.os.path.isfile(target):
            return error_response(404, "NOT_FOUND", "Bundle file not found.")

        # ── Content-Type ─────────────────────────────────────────────────
        content_type, _ = mimetypes.guess_type(str(target))
        if not content_type:
            content_type = "application/octet-stream"

        # ── Response with explicit security headers ───────────────────────
        # ``hardening.install_security_headers`` uses ``setdefault`` so
        # any header set EXPLICITLY here on the response wins.
        response = web.StreamResponse(status=200)
        response.content_type = content_type
        response.headers["Content-Security-Policy"] = APP_CSP
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Cache-Control"] = "private, max-age=60"

        # Drop entry cookie BEFORE prepare() so it is included in the
        # response headers (StreamResponse headers are frozen post-prepare).
        # ``secure`` is derived from ``config.secure_cookies`` (set via
        # ``SH_SECURE_COOKIES=true`` or ``[server] secure_cookies = true`` in
        # the TOML) so that local plain-http dev keeps the cookie working while
        # HTTPS deployments get the Secure attribute.
        if from_query:
            # Under HA Supervisor Ingress the browser sees all URLs
            # prefixed with the ingress path (e.g.
            # ``/api/hassio_ingress/TOKEN/api/apps/…``).  The Supervisor
            # injects ``X-Ingress-Path`` on every request so the app can
            # reconstruct the full prefix.  Without this adjustment the
            # cookie's ``Path`` would not match the sub-resource URLs the
            # browser actually sees, causing multi-file bundle loads to
            # fail in haos mode.
            ingress_prefix = self.request.headers.get("X-Ingress-Path", "").rstrip("/")
            cookie_path = f"{ingress_prefix}{prefix}" if ingress_prefix else prefix
            response.set_cookie(
                f"{BUNDLE_COOKIE_PREFIX}{app_id}",
                f"{exp}.{sig}",
                max_age=BUNDLE_TTL_SECONDS,
                path=cookie_path,
                httponly=True,
                samesite="Lax",
                secure=config.secure_cookies,
            )

        await response.prepare(self.request)

        async with aiofiles.open(target, "rb") as fh:
            while True:
                chunk = await fh.read(64 * 1024)
                if not chunk:
                    break
                await response.write(chunk)

        await response.write_eof()
        return response
