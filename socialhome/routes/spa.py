"""Serve the Preact SPA bundle from the backend.

The dev workflow runs Vite at ``:5173`` and proxies ``/api`` to the
backend on ``:8099`` — see ``client/vite.config.ts``. In production
the same backend serves the SPA itself: ``client/`` builds into
``socialhome/static/`` and this module wires those files into the
aiohttp router.

What we mount:

* ``GET /``               → ``static/index.html``
* ``GET /manifest.json``  → ``static/manifest.json``
* ``GET /sw.js``          → ``static/sw.js``
* ``GET /assets/{file}``  → ``static/assets/{file}`` (content-hashed)

The SPA's own router (preact-iso) handles every in-app route, so the
backend doesn't need a catchall for ``/feed`` / ``/spaces/abc`` /
``/setup``. Refreshing the browser on those URLs is the SPA author's
responsibility (use hash routing, or sit the app behind a reverse
proxy that rewrites to ``/index.html``).

Ingress support: when the add-on runs behind HA Supervisor's ingress
proxy the URL prefix is dynamic — ``/api/hassio_ingress/<token>/``
in front of every request. Supervisor stamps the prefix into
``X-Ingress-Path``. :class:`SpaIndexView` substitutes that into the
``<base href>`` tag inside ``index.html`` at request time so every
relative URL the SPA constructs (fetch, WebSocket, navigation)
resolves against the ingress-prefixed document URL. The header is
honoured only when the platform adapter advertises
``Capability.INGRESS`` (a Supervisor sits in front and sets it) and it
matches the ingress-path regex (both rules live in
:func:`socialhome.routes.ingress_path.trusted_ingress_path`); otherwise —
standalone / HA-Core-direct, where any client could forge it — the base
stays ``/``.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from pathlib import Path

import aiofiles
import aiofiles.os
from aiohttp import web

from ..csp import build_spa_csp
from .base import BaseView
from .ingress_path import trusted_ingress_path

log = logging.getLogger(__name__)

# Replaces the ``<base href="...">`` already present in
# ``client/index.html``. The trailing ``/`` is required — relative URLs
# in HTML resolve against ``<base>`` as a directory, not as a file.
_BASE_HREF_RE = re.compile(r'<base href="[^"]*"\s*/?>')

#: Vite content-hashes the entry bundle as ``assets/index-{hash}.js``.
#: We surface ``{hash}`` so the SPA's open tabs can poll for changes
#: and prompt the user to reload when the backend has shipped a new
#: bundle while their tab was stale. Tolerates the leading ``./`` or
#: ``/`` that ``vite`` may emit depending on ``base`` configuration.
_BUNDLE_SCRIPT_RE = re.compile(
    r'<script[^>]*src="(?:\.?/)?assets/index-([A-Za-z0-9_-]+)\.[A-Za-z0-9]+"',
)

#: Cache the parsed hash keyed by the ``index.html`` mtime so each call
#: to :func:`get_spa_bundle_hash` is a stat-only test path-wise — the
#: file content is only re-read when an operator drops a new bundle in
#: place. Cleared by ``mount_spa`` so test instances start fresh.
_bundle_hash_cache: dict[Path, tuple[float, str | None]] = {}


def get_spa_bundle_hash(static_dir: Path) -> str | None:
    """Return the SPA entry bundle's content hash, or ``None``.

    ``None`` when the SPA isn't built (no ``index.html`` on disk) or
    the template has no recognisable ``<script src="…/assets/index-
    {hash}.js">`` tag. The SPA's update-banner client treats ``None``
    as "no version info" and silently skips the check.
    """
    target = static_dir / "index.html"
    try:
        mtime = target.stat().st_mtime
    except FileNotFoundError:
        return None
    cached = _bundle_hash_cache.get(target)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        html = target.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _BUNDLE_SCRIPT_RE.search(html)
    bundle_hash = match.group(1) if match else None
    _bundle_hash_cache[target] = (mtime, bundle_hash)
    return bundle_hash


#: Default location of the built SPA. ``client/vite.config.ts`` writes
#: here via ``build.outDir``; the production wheel ships the same tree
#: under ``socialhome/static/``.
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_static_dir_key: web.AppKey[Path] = web.AppKey("spa_static_dir", Path)


class _SpaFileView(BaseView):
    """Common plumbing for the per-file SPA views.

    Subclasses set :attr:`_filename` and (optionally) override the
    cache header / extra headers. The static directory is read off
    the request app at handler time so tests can swap it in via
    :func:`mount_spa` without monkey-patching a module-level constant.
    """

    _filename: str = ""
    _cache_control: str = "no-cache"
    _extra_headers: dict[str, str] = {}  # noqa: RUF012  (override in subclass)

    async def get(self) -> web.StreamResponse:
        static_dir: Path = self.request.app[_static_dir_key]
        target = static_dir / self._filename
        if not target.is_file():
            raise web.HTTPNotFound()
        headers = {"Cache-Control": self._cache_control, **self._extra_headers}
        return web.FileResponse(target, headers=headers)


class SpaIndexView(_SpaFileView):
    """``GET /`` — serves ``static/index.html`` (no auth, no cache).

    Reads the ``X-Ingress-Path`` header (set by HA Supervisor when
    the request is proxied through the ingress integration) and
    rewrites the ``<base href>`` element inside ``index.html`` so
    the SPA's relative URLs (``./api/me``, ``./api/ws``, …) resolve
    against the ingress-prefixed document URL. The header is honoured
    only under ``Capability.INGRESS`` and only in the
    ``/api/hassio_ingress/<token>`` shape; otherwise the base stays
    ``/``. The response carries ``Vary: X-Ingress-Path``.

    Served with the SPA ``Content-Security-Policy``
    (:func:`socialhome.csp.build_spa_csp`) — the same in every mode.
    """

    _filename = "index.html"

    async def get(self) -> web.StreamResponse:
        static_dir = self.request.app[_static_dir_key]
        target = static_dir / self._filename
        if not await aiofiles.os.path.isfile(target):
            raise web.HTTPNotFound()
        ingress_path = trusted_ingress_path(self.request)
        # Attribute-escape anyway (defence in depth — the regex already
        # rules out ``"``): an unescaped ``"`` would break out of
        # ``<base href>``.
        base_href = html_lib.escape(f"{ingress_path}/" if ingress_path else "/")
        # ``index.html`` is small (a few KiB) — reading + substituting
        # in-memory per request is cheaper than maintaining two copies
        # on disk or a per-prefix cache that invalidates on every token
        # rotation. ``Cache-Control: no-cache`` was already required
        # (the bundle is content-hashed but the shell isn't).
        async with aiofiles.open(target, encoding="utf-8") as fh:
            html = await fh.read()
        # Callable replacement: a template string would treat a backslash in the
        # (request-controlled) header as a regex group reference.
        base_tag = f'<base href="{base_href}">'
        substituted, count = _BASE_HREF_RE.subn(
            lambda _m: base_tag,
            html,
            count=1,
        )
        if count == 0:
            # The template is required to ship a ``<base href="/">``
            # placeholder so the substitution is deterministic. If a
            # future build drops it, fall back to serving the file
            # as-is — the SPA will still load, just without the
            # ingress-prefix rewrite.
            log.warning(
                "index.html has no <base href> placeholder; "
                "ingress prefix injection skipped"
            )
            substituted = html
        return web.Response(
            text=substituted,
            content_type="text/html",
            headers={
                "Cache-Control": self._cache_control,
                # See ``socialhome/csp.py`` — every external host and
                # every relaxation is declared there, not here.
                # ``X-Frame-Options: SAMEORIGIN`` comes from the global
                # hardening middleware and agrees with ``frame-ancestors``.
                "Content-Security-Policy": build_spa_csp(),
                # The body depends on the header (under ingress), so a
                # shared cache must key on it.
                "Vary": "X-Ingress-Path",
            },
        )


class SpaManifestView(_SpaFileView):
    """``GET /manifest.json`` — PWA manifest."""

    _filename = "manifest.json"


class SpaServiceWorkerView(_SpaFileView):
    """``GET /sw.js`` — service worker.

    ``Service-Worker-Allowed: /`` widens the worker's scope to the
    whole origin even though the script lives at ``/sw.js`` (the
    default scope would be the script's own directory). ``no-cache``
    keeps stale workers from sticking around after a deploy.
    """

    _filename = "sw.js"
    _extra_headers = {"Service-Worker-Allowed": "/"}  # noqa: RUF012


class SpaFaviconView(_SpaFileView):
    """``GET /favicon.svg`` — branded SVG icon shipped in the bundle.

    Browsers ask for ``/favicon.svg`` (or ``/favicon.ico``) when no
    ``<link rel="icon">`` is in the HTML head, and also when one is.
    Serving the file directly avoids the catchall returning the SPA
    shell for a request the browser expects to be an image.
    """

    _filename = "favicon.svg"
    # Cache the SVG for an hour — it changes rarely and the bundle
    # ships a fresh copy on every build.
    _cache_control = "public, max-age=3600"


class SpaCatchallView(SpaIndexView):
    """Serves the SPA shell for any non-``/api/`` GET path.

    Without this, refreshing the browser on a deep URL (``/feed``,
    ``/spaces/abc``, etc.) — including the prefixed-form
    ``/api/hassio_ingress/<token>/feed`` that HA Ingress proxies as
    ``GET /feed`` on the add-on side — returns 404. The SPA's own
    ``preact-iso`` router can't claim a path the backend doesn't
    serve, so the standard "single-page-app fallback" pattern is to
    serve the shell for every unmatched path and let the client
    router pick the right view.

    ``/api/`` and friends are protected because the catchall is
    registered **last**, after every concrete route. Anything matched
    by an earlier handler (``SpaIndexView`` at ``/``, ``/manifest.json``,
    ``/sw.js``, ``/assets/{file}``, every ``/api/...``) is served by
    that handler; everything else falls through to here and gets the
    SPA shell. The auth middleware's public-path list mirrors this
    exclusion set (see ``_DEFAULT_PUBLIC_PATH_PATTERNS`` in
    ``socialhome/auth.py``) so the catchall stays unauthenticated.
    """


def mount_spa(app: web.Application, static_dir: Path | None = None) -> bool:
    """Wire SPA routes onto ``app``.

    Returns ``True`` when the mount happened, ``False`` when the
    static directory is missing or empty (e.g. a dev environment
    without a ``pnpm --dir client run build``). In the missing-build
    case we log a warning and leave the router untouched so the
    backend still serves ``/api/*`` and ``/healthz`` for the Vite
    dev-server flow.

    ``static_dir`` defaults to :data:`DEFAULT_STATIC_DIR` resolved at
    call time (not import time), so tests can monkeypatch the module
    constant before ``create_app`` runs.
    """
    if static_dir is None:
        static_dir = DEFAULT_STATIC_DIR
    index = static_dir / "index.html"
    if not index.is_file():
        log.warning(
            "SPA bundle missing at %s — backend will only serve /api/*; "
            "run `pnpm --dir client run build` to enable.",
            index,
        )
        return False

    app[_static_dir_key] = static_dir
    # Drop the bundle-hash cache so a fresh mount sees fresh files
    # (per-test apps swap ``static_dir`` per fixture; without this they
    # could read each other's cached entries when paths collide).
    _bundle_hash_cache.pop(static_dir / "index.html", None)

    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        # ``append_version=False`` — bundle filenames are content-hashed
        # by Vite, so aiohttp's auto-versioning query string is noise.
        app.router.add_static("/assets/", str(assets_dir), append_version=False)

    app.router.add_view("/manifest.json", SpaManifestView)
    app.router.add_view("/sw.js", SpaServiceWorkerView)
    app.router.add_view("/favicon.svg", SpaFaviconView)
    app.router.add_view("/", SpaIndexView)
    # Registered LAST so every more-specific route (``/api/...``,
    # ``/healthz``, ``/manifest.json``, ``/sw.js``, ``/assets/...``,
    # ``/``) wins over the catchall.
    app.router.add_view("/{tail:.+}", SpaCatchallView)

    log.info("SPA bundle mounted from %s", static_dir)
    return True
