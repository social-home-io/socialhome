"""Tests for the SPA mount — ``socialhome.routes.spa``.

The Preact bundle lives in ``socialhome/static/`` at runtime; the
tests build their own throwaway tree in ``tmp_path`` and point
``mount_spa`` at it so we never depend on whether the repo has a
fresh ``pnpm --dir client run build``.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType

import pytest
from aiohttp import web

from socialhome.app import create_app
from socialhome.config import Config
from socialhome.app_keys import platform_adapter_key
from socialhome.csp import build_spa_csp
from socialhome.platform.adapter import Capability
from socialhome.routes import spa as spa_module

#: Repo root — the inline-script guard reads the real SPA template.
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_spa(tmp_path: Path) -> Path:
    """A minimal valid SPA tree the mount can pick up."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        '<!doctype html><head><base href="/" /><title>spa</title></head>'
    )
    (static / "manifest.json").write_text('{"name":"Social Home"}')
    (static / "sw.js").write_text("// service worker")
    (static / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (static / "assets" / "app-deadbeef.js").write_text("console.log('app');")
    return static


@pytest.fixture
async def spa_client(aiohttp_client, tmp_dir, fake_spa, monkeypatch):
    """An app whose SPA mount points at ``fake_spa``."""
    monkeypatch.setattr(spa_module, "DEFAULT_STATIC_DIR", fake_spa)
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )
    app = create_app(cfg)
    return await aiohttp_client(app)


# ── Happy path ────────────────────────────────────────────────────────────


async def test_root_serves_index_html(spa_client):
    resp = await spa_client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert "<title>spa</title>" in body
    assert resp.headers["Cache-Control"] == "no-cache"


async def test_root_is_unauthenticated(spa_client):
    """Browser must be able to fetch the bundle before logging in."""
    resp = await spa_client.get("/")  # no Authorization header
    assert resp.status == 200


async def test_assets_are_served(spa_client):
    resp = await spa_client.get("/assets/app-deadbeef.js")
    assert resp.status == 200
    assert "console.log" in await resp.text()


async def test_manifest_json_served(spa_client):
    resp = await spa_client.get("/manifest.json")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"name": "Social Home"}


async def test_service_worker_served_with_root_scope_header(spa_client):
    resp = await spa_client.get("/sw.js")
    assert resp.status == 200
    assert resp.headers["Service-Worker-Allowed"] == "/"
    assert resp.headers["Cache-Control"] == "no-cache"


async def test_favicon_svg_served_unauthenticated(spa_client):
    """``/favicon.svg`` is served without auth (browsers fetch it on every page)."""
    resp = await spa_client.get("/favicon.svg")
    assert resp.status == 200
    assert "image/svg" in resp.headers["Content-Type"]
    assert "<svg" in await resp.text()


# ── Backend routes stay backend routes ────────────────────────────────────


async def test_api_routes_not_shadowed_by_spa(spa_client):
    """``/api/me`` still 401s — the SPA mount must not touch /api/."""
    resp = await spa_client.get("/api/me")
    assert resp.status == 401
    assert resp.content_type == "application/json"


async def test_healthz_not_shadowed_by_spa(spa_client):
    resp = await spa_client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"


async def test_unknown_top_level_path_served_as_spa_shell(spa_client):
    """SPA catchall: any non-``/api/`` GET serves ``index.html`` so a
    hard refresh on a deep URL (``/feed``, ``/spaces/abc``, the
    ingress-prefixed ``/api/hassio_ingress/<token>/feed`` after HA
    Core strips the prefix) renders the SPA shell instead of 404.
    ``preact-iso`` then picks the right view client-side."""
    resp = await spa_client.get("/feed")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    body = await resp.text()
    assert "<title>spa</title>" in body


# ── Missing-build fallback ────────────────────────────────────────────────


async def test_missing_static_dir_skips_mount(
    aiohttp_client, tmp_dir, tmp_path, monkeypatch, caplog
):
    """No ``socialhome/static/`` — log a warning, leave routes untouched."""
    missing = tmp_path / "no-static"  # doesn't exist
    monkeypatch.setattr(spa_module, "DEFAULT_STATIC_DIR", missing)
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )
    with caplog.at_level("WARNING", logger=spa_module.__name__):
        app = create_app(cfg)
    tc = await aiohttp_client(app)

    # /healthz still works, /api/me still 401s, / has no handler so 404.
    healthz = await tc.get("/healthz")
    assert healthz.status == 200
    me = await tc.get("/api/me")
    assert me.status == 401
    root = await tc.get("/")
    assert root.status == 404

    assert any("SPA bundle missing" in r.message for r in caplog.records)


async def test_mount_spa_returns_false_when_missing(tmp_path):
    app = web.Application()
    assert spa_module.mount_spa(app, static_dir=tmp_path / "absent") is False


async def test_mount_spa_returns_true_when_present(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok")
    app = web.Application()
    assert spa_module.mount_spa(app, static_dir=static) is True


# ── Ingress: <base href> substitution from X-Ingress-Path ──────────────────


async def test_root_base_href_defaults_to_slash(spa_client):
    """No ``X-Ingress-Path`` (standalone / HA-Core-direct) → ``<base href="/">``."""
    resp = await spa_client.get("/")
    body = await resp.text()
    assert '<base href="/">' in body


async def test_root_base_href_rewritten_from_ingress_path(ingress_spa_client):
    """Supervisor stamps the prefix into ``X-Ingress-Path``; the SPA's
    ``<base href>`` reflects it (trailing slash forced) so the SPA's
    relative URLs (``./api/me``, ``./api/ws``, …) resolve under the
    ingress-prefixed document URL."""
    resp = await ingress_spa_client.get(
        "/",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
    )
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/abc123/">' in body
    # Sanity: the placeholder is gone.
    assert '<base href="/">' not in body


async def test_real_supervisor_token_shape_is_accepted(ingress_spa_client):
    """Supervisor mints the token with ``secrets.token_urlsafe()``
    (``supervisor/apps/validate.py``) — base64url, so ``-`` and ``_``
    occur; HA Core stamps ``/api/hassio_ingress/{token}`` (no slash)."""
    token = "Xy-9_kQ2" * 5 + "a-_"
    resp = await ingress_spa_client.get(
        "/feed",
        headers={"X-Ingress-Path": f"/api/hassio_ingress/{token}"},
    )
    assert f'<base href="/api/hassio_ingress/{token}/">' in await resp.text()


async def test_root_base_href_strips_trailing_slash_from_header(ingress_spa_client):
    """If the header arrives with a trailing slash, we don't double up."""
    resp = await ingress_spa_client.get(
        "/",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123/"},
    )
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/abc123/">' in body
    assert '<base href="/api/hassio_ingress/abc123//">' not in body


async def test_root_substitution_logs_warning_when_placeholder_missing(
    ingress_spa_client, monkeypatch, caplog
):
    """If a future build drops the ``<base href>`` placeholder we log a
    warning and serve the HTML untouched — the SPA will still load."""
    static_dir = ingress_spa_client.app[spa_module._static_dir_key]
    (static_dir / "index.html").write_text(
        "<!doctype html><title>no placeholder</title>"
    )
    with caplog.at_level("WARNING", logger=spa_module.__name__):
        resp = await ingress_spa_client.get(
            "/",
            headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
        )
    body = await resp.text()
    assert resp.status == 200
    assert "<title>no placeholder</title>" in body
    assert any("no <base href> placeholder" in r.message for r in caplog.records)


@pytest.mark.parametrize("path", ["/", "/feed"], ids=["root", "deep"])
async def test_standalone_ignores_ingress_path(spa_client, path):
    """Without ``Capability.INGRESS`` no Supervisor sits in front, so the
    header is any client's to forge — it never reaches ``<base href>``."""
    resp = await spa_client.get(
        path,
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
    )
    body = await resp.text()
    assert resp.status == 200
    assert '<base href="/">' in body
    assert "hassio_ingress" not in body


@pytest.mark.parametrize(
    "header",
    [
        "//evil.example",
        "//evil.example/api/hassio_ingress/abc",
        "javascript:alert(1)",
        '"><script>alert(1)</script>',
        '/api/hassio_ingress/x"><script>alert(1)</script>',
        "/other/path",
        "/api/hassio_ingress/",
        "/api/hassio_ingress/abc/def",
        "/api/hassio_ingress/abc//",
        "/api/hassio_ingress/a.b",
        "/api/hassio_ingress/a\\1b",
        "https://evil.example/api/hassio_ingress/abc",
    ],
)
async def test_ingress_ignores_invalid_ingress_path(ingress_spa_client, header):
    """Only ``/api/hassio_ingress/<token>`` (HA Core's exact shape) is
    honoured; anything else leaves the base at ``/``."""
    resp = await ingress_spa_client.get("/", headers={"X-Ingress-Path": header})
    body = await resp.text()
    assert resp.status == 200
    assert '<base href="/">' in body
    assert "<script>alert" not in body
    assert "evil.example" not in body


def test_ingress_path_regex_rejects_trailing_newline():
    """aiohttp refuses a header with a control character on the wire;
    ``fullmatch`` keeps the regex from trusting one regardless (``$``
    alone would match before a trailing ``\\n``)."""
    assert spa_module._INGRESS_PATH_RE.fullmatch("/api/hassio_ingress/abc\n") is None
    assert spa_module._INGRESS_PATH_RE.fullmatch("/api/hassio_ingress/abc/")


@pytest.mark.parametrize("path", ["/", "/feed"], ids=["root", "deep"])
async def test_shell_varies_on_ingress_path(spa_client, ingress_spa_client, path):
    """The shell body depends on ``X-Ingress-Path``, so a shared cache
    must key on it."""
    for client in (spa_client, ingress_spa_client):
        resp = await client.get(path)
        assert "X-Ingress-Path" in resp.headers.getall("Vary", [""])[0]


# ── Content-Security-Policy ───────────────────────────────────────────────

#: The HA Supervisor ingress handshake headers (see ``HaIngressStrategy``
#: in ``socialhome/auth.py``) plus the prefix Supervisor stamps.
_INGRESS_HEADERS = {
    "X-Ingress-Path": "/api/hassio_ingress/tok123",
    "X-Hass-Source": "core.ingress",
    "X-Remote-User-Name": "owner",
}


def _csp_directives(header: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in header.split(";"):
        name, *sources = part.split()
        out[name] = sources
    return out


@pytest.fixture
async def ingress_spa_client(aiohttp_client, tmp_dir, fake_spa, monkeypatch):
    """The SPA mount behind an adapter advertising ``Capability.INGRESS``
    (the ``haos`` shape), with a non-default ``map_tile_url``.

    The capability is patched in *after* ``create_app`` so the ingress
    auth strategy (which needs a real Supervisor) stays unwired; it
    proves the SPA headers do not depend on it."""
    monkeypatch.setattr(spa_module, "DEFAULT_STATIC_DIR", fake_spa)
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        map_tile_url="https://tiles.example.net/{z}/{x}/{y}.png",
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )
    app = create_app(cfg)
    adapter = app[platform_adapter_key]
    monkeypatch.setattr(
        type(adapter),
        "capabilities",
        property(lambda _self: frozenset({Capability.INGRESS})),
    )
    return await aiohttp_client(app)


@pytest.mark.parametrize("path", ["/", "/feed"], ids=["root", "deep"])
async def test_standalone_shell_carries_csp(spa_client, path):
    """The SPA shell — root and the deep-link catchall — carries the
    enforced (not report-only) CSP and admits same-origin framers only
    (an ``ha`` install behind a path-prefix proxy framed by HA)."""
    resp = await spa_client.get(path)
    assert resp.status == 200
    csp = resp.headers["Content-Security-Policy"]
    assert csp == build_spa_csp()
    assert "Content-Security-Policy-Report-Only" not in resp.headers
    d = _csp_directives(csp)
    assert d["default-src"] == ["'self'"]
    assert d["script-src"] == ["'self'"]
    assert d["object-src"] == ["'none'"]
    assert d["base-uri"] == ["'self'"]
    assert d["form-action"] == ["'self'"]
    assert d["frame-ancestors"] == ["'self'"]
    # The global hardening default, which agrees with ``frame-ancestors``.
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.parametrize("path", ["/", "/spaces/abc"], ids=["root", "deep"])
async def test_ingress_shell_carries_csp_frameable_by_ha(ingress_spa_client, path):
    """HA's add-on panel frames ``/api/hassio_ingress/<token>/`` on HA's
    own origin, so the frame is same-origin with its parent:
    ``frame-ancestors 'self'`` admits it and still refuses foreign
    embedders. ``X-Frame-Options`` agrees. The policy is the same one
    every mode gets — ``Capability.INGRESS`` changes nothing."""
    resp = await ingress_spa_client.get(path, headers=_INGRESS_HEADERS)
    assert resp.status == 200
    csp = resp.headers["Content-Security-Policy"]
    assert csp == build_spa_csp()
    d = _csp_directives(csp)
    assert d["script-src"] == ["'self'"]
    assert d["frame-ancestors"] == ["'self'"]
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    # The ingress <base href> rewrite is a same-origin path, which
    # ``base-uri 'self'`` permits.
    assert '<base href="/api/hassio_ingress/tok123/">' in await resp.text()


async def test_configured_tile_host_needs_no_csp_entry(ingress_spa_client):
    """Tiles go through the backend proxy whatever ``map_tile_url`` is
    configured, and the URL handed to Leaflet is relative — so
    ``img-src 'self'`` covers it and no tile host joins the policy."""
    resp = await ingress_spa_client.get("/", headers=_INGRESS_HEADERS)
    csp = resp.headers["Content-Security-Policy"]
    assert "tiles.example.net" not in csp
    assert "'self'" in _csp_directives(csp)["img-src"]


async def test_api_responses_do_not_carry_the_spa_csp(spa_client):
    resp = await spa_client.get("/healthz")
    assert "Content-Security-Policy" not in resp.headers


# ── Inline-script guard ──────────────────────────────────────────────────

_SCRIPT_TAG_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


def _inline_scripts(html: str) -> list[str]:
    """Return the bodies of ``<script>`` tags without a ``src``.

    The CSP is ``script-src 'self'`` with no hashes or nonces, so any
    inline script would be blocked in the browser — fail here instead.
    """
    no_comments = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    return [
        body
        for attrs, body in _SCRIPT_TAG_RE.findall(no_comments)
        if not re.search(r"\bsrc\s*=", attrs)
    ]


def test_inline_script_detector_catches_inline_and_ignores_src():
    html = (
        "<!-- <script>commented()</script> -->"
        '<script src="assets/a.js"></script>'
        "<script>boot()</script>"
        '<script type="module">x()</script>'
    )
    assert _inline_scripts(html) == ["boot()", "x()"]


def test_spa_template_has_no_inline_script():
    """``client/index.html`` must not grow an inline ``<script>`` — move
    it to a file under ``client/public/assets/`` (see
    ``assets/theme-boot.js``)."""
    html = (_REPO_ROOT / "client" / "index.html").read_text(encoding="utf-8")
    assert _inline_scripts(html) == []
    assert 'src="assets/theme-boot.js"' in html


def test_built_spa_index_has_no_inline_script():
    """Same guard over the built shell, when a build is present."""
    built = spa_module.DEFAULT_STATIC_DIR / "index.html"
    if not built.is_file():
        pytest.skip("SPA not built")
    assert _inline_scripts(built.read_text(encoding="utf-8")) == []


# ── SPA bundle hash extraction ───────────────────────────────────────────


def test_get_spa_bundle_hash_extracts_vite_hash(tmp_path):
    """The Vite content-hash on the entry script is parsed out."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        '<!doctype html><script type="module" crossorigin '
        'src="./assets/index-CChr7dg4.js"></script>'
    )
    assert spa_module.get_spa_bundle_hash(static) == "CChr7dg4"


def test_get_spa_bundle_hash_tolerates_leading_slash(tmp_path):
    """A Vite ``base: '/'`` build emits ``/assets/...`` instead of ``./assets/...``."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/index-XYZ_abc-1.js">'
    )
    assert spa_module.get_spa_bundle_hash(static) == "XYZ_abc-1"


def test_get_spa_bundle_hash_returns_none_without_template(tmp_path):
    """Missing ``index.html`` (dev mode with no build) is silent."""
    assert spa_module.get_spa_bundle_hash(tmp_path) is None


def test_get_spa_bundle_hash_returns_none_when_pattern_absent(tmp_path):
    """An ``index.html`` without the canonical script tag yields ``None``."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        "<!doctype html><title>spa</title><script>console.log('inline')</script>"
    )
    assert spa_module.get_spa_bundle_hash(static) is None


def test_get_spa_bundle_hash_is_cached_by_mtime(tmp_path, monkeypatch):
    """Re-reading after the file's mtime is unchanged hits the cache."""
    static = tmp_path / "static"
    static.mkdir()
    target = static / "index.html"
    target.write_text('<!doctype html><script src="./assets/index-AAAA.js"></script>')
    # First call — cache miss, reads the file.
    assert spa_module.get_spa_bundle_hash(static) == "AAAA"
    # Mutate the content WITHOUT bumping the mtime → cached value
    # should still come back.
    mtime = target.stat().st_mtime
    target.write_text('<!doctype html><script src="./assets/index-BBBB.js"></script>')
    import os

    os.utime(target, (mtime, mtime))
    assert spa_module.get_spa_bundle_hash(static) == "AAAA"
    # Bump the mtime → cache invalidates + new value returned.
    os.utime(target, (mtime + 1, mtime + 1))
    assert spa_module.get_spa_bundle_hash(static) == "BBBB"
