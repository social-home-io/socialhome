"""Tests for the SPA Content-Security-Policy builder — ``socialhome.csp``."""

from __future__ import annotations

import pytest

import socialhome.csp as csp_module
from socialhome.csp import SPA_CSP_DIRECTIVES, build_spa_csp, render_csp
from socialhome.routes.map_tiles import TILE_URL_TEMPLATE


def _parse(header: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in header.split(";"):
        name, *sources = part.split()
        out[name] = sources
    return out


def test_render_csp_joins_directives_in_order():
    assert render_csp({"default-src": ("'self'",), "img-src": ("'self'", "data:")}) == (
        "default-src 'self'; img-src 'self' data:"
    )


@pytest.mark.parametrize(
    "directives",
    [
        {"img-src": ()},
        {"img-src": ("a;b",)},
        {"img-src": ("a b",)},
        {"img-src": ("a,b",)},
        {"img-src": ("",)},
        {"bad name": ("'self'",)},
    ],
)
def test_render_csp_rejects_malformed_input(directives):
    with pytest.raises(ValueError):
        render_csp(directives)


def test_build_spa_csp_renders_the_directive_table():
    d = _parse(build_spa_csp())
    assert d == {k: list(v) for k, v in SPA_CSP_DIRECTIVES.items()}


def test_build_spa_csp_is_built_once():
    """Cached: the same string object comes back every call."""
    assert build_spa_csp() is build_spa_csp()


def test_spa_csp_script_src_is_strict():
    d = _parse(build_spa_csp())
    assert d["script-src"] == ["'self'"]
    for directive in ("script-src", "default-src", "worker-src"):
        assert "'unsafe-inline'" not in d[directive]
        assert "'unsafe-eval'" not in d[directive]
        assert "data:" not in d[directive]
        assert "blob:" not in d[directive]


def test_spa_csp_locks_down_plugins_base_and_forms():
    d = _parse(build_spa_csp())
    assert d["default-src"] == ["'self'"]
    assert d["object-src"] == ["'none'"]
    assert d["base-uri"] == ["'self'"]
    assert d["form-action"] == ["'self'"]


def test_frame_ancestors_is_same_origin_in_every_mode():
    """``'self'`` everywhere: HA's add-on panel frames
    ``/api/hassio_ingress/<token>/`` on HA's own origin, and an ``ha``
    install may sit behind a same-origin path-prefix proxy framed by an
    HA ``panel_iframe``. Foreign embedders stay refused."""
    assert _parse(build_spa_csp())["frame-ancestors"] == ["'self'"]


def test_no_spa_specific_frame_options():
    """The global ``X-Frame-Options: SAMEORIGIN`` (``hardening.py``)
    already agrees with ``frame-ancestors 'self'`` — no SPA override."""
    assert not hasattr(csp_module, "spa_frame_options")


def test_spa_csp_inline_styles_limited_to_attributes():
    """Leaflet pin/popup HTML carries ``style=`` attributes; ``<style>``
    elements stay blocked."""
    d = _parse(build_spa_csp())
    assert "'unsafe-inline'" not in d["style-src"]
    assert d["style-src-attr"] == ["'unsafe-inline'"]


def test_spa_csp_allows_google_fonts():
    d = _parse(build_spa_csp())
    assert "https://fonts.googleapis.com" in d["style-src"]
    assert "https://fonts.gstatic.com" in d["font-src"]


def test_spa_csp_media_and_previews():
    d = _parse(build_spa_csp())
    assert {"'self'", "data:", "blob:"} <= set(d["img-src"])
    assert d["media-src"] == ["'self'", "blob:"]
    assert d["connect-src"] == ["'self'"]
    assert d["worker-src"] == ["'self'"]
    assert d["frame-src"] == ["'self'"]


def test_tile_template_is_relative_so_self_covers_it():
    """Map tiles are proxied — the template is a relative ``api/`` path,
    so the CSP needs no tile host whatever ``map_tile_url`` says."""
    assert not TILE_URL_TEMPLATE.startswith(("http:", "https:", "//", "/"))
    assert TILE_URL_TEMPLATE.startswith("api/")
