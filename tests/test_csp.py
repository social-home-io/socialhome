"""Tests for the SPA Content-Security-Policy builder — ``socialhome.csp``."""

from __future__ import annotations

import pytest

from socialhome.csp import (
    SPA_CSP_DIRECTIVES,
    build_spa_csp,
    render_csp,
    spa_frame_options,
)
from socialhome.platform.adapter import Capability
from socialhome.routes.map_tiles import TILE_URL_TEMPLATE

#: Standalone / ``ha`` shape vs the HA Supervisor ingress (``haos``) shape.
_DIRECT = frozenset({Capability.PASSWORD_AUTH})
_INGRESS = frozenset({Capability.INGRESS, Capability.HA_PERSON_DIRECTORY})


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


@pytest.mark.parametrize("caps", [_DIRECT, _INGRESS], ids=["direct", "ingress"])
def test_build_spa_csp_is_the_shared_table_plus_frame_ancestors(caps):
    d = _parse(build_spa_csp(caps))
    shared = {k: list(v) for k, v in SPA_CSP_DIRECTIVES.items()}
    assert {k: v for k, v in d.items() if k != "frame-ancestors"} == shared
    assert "frame-ancestors" in d


def test_build_spa_csp_is_built_once_per_shape():
    """Cached: the same string object comes back for the same shape."""
    assert build_spa_csp(_DIRECT) is build_spa_csp(frozenset())
    assert build_spa_csp(_INGRESS) is build_spa_csp([Capability.INGRESS])


@pytest.mark.parametrize("caps", [_DIRECT, _INGRESS], ids=["direct", "ingress"])
def test_spa_csp_script_src_is_strict(caps):
    d = _parse(build_spa_csp(caps))
    assert d["script-src"] == ["'self'"]
    for directive in ("script-src", "default-src", "worker-src"):
        assert "'unsafe-inline'" not in d[directive]
        assert "'unsafe-eval'" not in d[directive]
        assert "data:" not in d[directive]
        assert "blob:" not in d[directive]


@pytest.mark.parametrize("caps", [_DIRECT, _INGRESS], ids=["direct", "ingress"])
def test_spa_csp_locks_down_plugins_base_and_forms(caps):
    d = _parse(build_spa_csp(caps))
    assert d["default-src"] == ["'self'"]
    assert d["object-src"] == ["'none'"]
    assert d["base-uri"] == ["'self'"]
    assert d["form-action"] == ["'self'"]


def test_frame_ancestors_none_without_ingress():
    """Nothing legitimately frames a standalone / HA-Core-direct SPA."""
    assert _parse(build_spa_csp(_DIRECT))["frame-ancestors"] == ["'none'"]
    assert spa_frame_options(_DIRECT) == "DENY"


def test_frame_ancestors_self_with_ingress():
    """HA's add-on panel frames ``/api/hassio_ingress/<token>/`` on HA's
    own origin — a same-origin frame, so ``'self'`` admits exactly it."""
    assert _parse(build_spa_csp(_INGRESS))["frame-ancestors"] == ["'self'"]
    assert spa_frame_options(_INGRESS) == "SAMEORIGIN"


def test_spa_csp_inline_styles_limited_to_attributes():
    """Leaflet pin/popup HTML carries ``style=`` attributes; ``<style>``
    elements stay blocked."""
    d = _parse(build_spa_csp(_DIRECT))
    assert "'unsafe-inline'" not in d["style-src"]
    assert d["style-src-attr"] == ["'unsafe-inline'"]


def test_spa_csp_allows_google_fonts():
    d = _parse(build_spa_csp(_DIRECT))
    assert "https://fonts.googleapis.com" in d["style-src"]
    assert "https://fonts.gstatic.com" in d["font-src"]


def test_spa_csp_media_and_previews():
    d = _parse(build_spa_csp(_DIRECT))
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
