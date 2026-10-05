"""Tests for the SPA Content-Security-Policy builder — ``socialhome.csp``."""

from __future__ import annotations

import mimetypes

import pytest

import socialhome.csp as csp_module
from socialhome.csp import (
    INLINE_MEDIA_TYPES,
    MEDIA_CSP,
    PLAYABLE_MEDIA_CSP,
    PLAYABLE_MEDIA_TYPES,
    SPA_CSP_DIRECTIVES,
    build_spa_csp,
    media_response_headers,
    media_type_for,
    render_csp,
)
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


# ── Stored-media responses ───────────────────────────────────────────────


def test_media_csp_blocks_script_and_sandboxes():
    d = _parse(MEDIA_CSP)
    assert d["default-src"] == ["'none'"]
    assert d["img-src"] == ["'self'", "data:"]
    assert d["media-src"] == ["'self'"]
    assert d["style-src"] == ["'unsafe-inline'"]
    assert d["sandbox"] == []
    # ``form-action`` does not fall back to ``default-src``.
    assert d["form-action"] == ["'none'"]
    assert "script-src" not in d


def test_playable_media_csp_is_the_media_csp_without_sandbox():
    """A sandboxed (opaque-origin) media document re-fetches its own
    ``<video>`` / ``<audio>`` src cross-origin and fails CORS, so
    video and audio drop ``sandbox`` — ``default-src 'none'`` still
    refuses every script."""
    d = _parse(PLAYABLE_MEDIA_CSP)
    assert "sandbox" not in d
    assert d == {k: v for k, v in _parse(MEDIA_CSP).items() if k != "sandbox"}


@pytest.mark.parametrize(
    "mime",
    sorted(INLINE_MEDIA_TYPES),
)
def test_safe_types_are_served_inline_and_sandboxed(mime):
    h = media_response_headers(mime, "abc.bin")
    assert h["Content-Type"] == mime
    assert h["Content-Disposition"] == 'inline; filename="abc.bin"'
    assert h["Content-Security-Policy"] == MEDIA_CSP


def test_playable_media_csp_refuses_form_submission():
    assert _parse(PLAYABLE_MEDIA_CSP)["form-action"] == ["'none'"]


def test_playable_types_are_exactly_what_the_server_writes():
    """``VideoProcessor`` → ``.webm``; ``AudioProcessor`` → ``.ogg`` /
    ``.webm`` / ``.m4a`` (served as ``audio/ogg`` / ``video/webm`` /
    ``audio/mp4``); a received DM voice note keeps ``audio/webm``."""
    assert PLAYABLE_MEDIA_TYPES == frozenset(
        {"video/webm", "audio/ogg", "audio/webm", "audio/mp4"},
    )


@pytest.mark.parametrize("mime", sorted(PLAYABLE_MEDIA_TYPES))
def test_video_and_audio_are_inline_and_playable(mime):
    h = media_response_headers(mime, "clip")
    assert h["Content-Type"] == mime
    assert h["Content-Disposition"].startswith("inline;")
    assert h["Content-Security-Policy"] == PLAYABLE_MEDIA_CSP


@pytest.mark.parametrize(
    "mime",
    [
        "image/svg+xml",
        "text/html",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
        "text/javascript",
        "application/javascript",
        "application/x-unknown",
        "image/heic",
        "multipart/x-mixed-replace",
        # audio / video the server never writes itself
        "video/mp4",
        "video/quicktime",
        "audio/mpeg",
        "audio/flac",
        "audio/x-mpegurl",
        "video/vnd.mpegurl",
        "audio/x-scpls",
        "video/x-ms-asf",
        "video/",
        "audio/",
        "",
        None,
    ],
)
def test_unsafe_or_unknown_types_become_sandboxed_attachments(mime):
    h = media_response_headers(mime, "x.svg")
    assert h["Content-Type"] == "application/octet-stream"
    assert h["Content-Disposition"] == 'attachment; filename="x.svg"'
    assert h["Content-Security-Policy"] == MEDIA_CSP


def test_media_type_parameters_and_case_are_normalised():
    h = media_response_headers("Image/JPEG; charset=binary", "a.jpg")
    assert h["Content-Type"] == "image/jpeg"
    assert h["Content-Disposition"].startswith("inline;")
    h = media_response_headers("TEXT/HTML; charset=utf-8", "a.html")
    assert h["Content-Type"] == "application/octet-stream"


def test_media_filename_cannot_break_the_disposition_header():
    h = media_response_headers("image/png", 'a"; x=1\r\n.png')
    assert h["Content-Disposition"] == 'inline; filename="a___x_1__.png"'


@pytest.mark.parametrize(
    "name,expected",
    [
        ("0a1d.m4a", "audio/mp4"),
        ("note.ogg", "audio/ogg"),
        ("clip.webm", "video/webm"),
        ("pic.webp", "image/webp"),
        ("PIC.JPG", "image/jpeg"),
        ("doc.pdf", "application/pdf"),
        ("x.svg", "image/svg+xml"),
        ("noext", None),
    ],
)
def test_media_type_for_ignores_the_hosts_mime_database(monkeypatch, name, expected):
    """A host /etc/mime.types that maps .m4a to audio/mpeg (or .webm to
    something odd) must not change what we serve: Safari voice notes would
    download instead of playing."""
    monkeypatch.setattr(
        mimetypes,
        "types_map",
        {**mimetypes.types_map, ".m4a": "audio/mpeg", ".webm": "application/x-odd"},
    )
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: ("audio/mpeg", None))
    assert media_type_for(name) == expected
