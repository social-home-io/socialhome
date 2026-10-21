"""Hand-off pages: link-preview tags + a refresh to the pinned viewer."""

from __future__ import annotations

from socialhome.global_server.link_preview import (
    HANDOFF_CSP,
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    LinkPreview,
    render_handoff,
    space_image_url,
)

_TARGET = "https://viewer.example.org/v/abc/#v=join&g=https%3A%2F%2Fgfs&t=tok"
_PNG = "data:image/png;base64,iVBORw0KGgo="


def _page(**kw) -> str:
    fields = {"title": "T", "description": "D", "site_name": "S"}
    fields.update(kw)
    return render_handoff(LinkPreview(**fields), _TARGET).text or ""


def test_the_page_runs_nothing_and_leaks_no_referrer():
    resp = render_handoff(
        LinkPreview(title="T", description="", site_name="S"), _TARGET
    )
    assert resp.headers["Content-Security-Policy"] == HANDOFF_CSP
    assert HANDOFF_CSP.startswith("default-src 'none'")
    assert "frame-ancestors 'none'" in HANDOFF_CSP
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"
    text = resp.text or ""
    assert "<script" not in text
    assert "<style" not in text
    assert '<meta name="referrer" content="no-referrer">' in text


def test_the_refresh_and_link_carry_the_escaped_target():
    text = _page()
    escaped = _TARGET.replace("&", "&amp;")
    assert f'<meta http-equiv="refresh" content="0;url={escaped}">' in text
    assert f'<a href="{escaped}" rel="noreferrer">' in text


def test_values_are_escaped_and_bounded():
    text = _page(title='"><b>' + "x" * 500, description="<i>" + "y" * 900)
    assert "<b>" not in text and "<i>" not in text
    assert "&quot;&gt;&lt;b&gt;" in text
    assert "x" * MAX_TITLE_CHARS not in text
    assert "y" * MAX_DESCRIPTION_CHARS not in text


def test_card_kind_follows_the_image():
    assert 'content="summary"' in _page()
    assert "og:image" not in _page()
    square = _page(image_url="https://gfs/p.png")
    assert 'content="summary"' in square
    assert '<meta property="og:image" content="https://gfs/p.png">' in square
    wide = _page(image_url="https://gfs/c.png", large_image=True)
    assert 'content="summary_large_image"' in wide


def test_no_canonical_url_unless_given():
    assert "og:url" not in _page()
    assert 'og:url" content="https://gfs/spaces/a"' in _page(
        canonical_url="https://gfs/spaces/a"
    )


def test_space_image_url_only_for_a_stored_raster_image():
    base = "https://gfs.example.org"
    url = space_image_url(base, {"space_id": "sp", "cover_url": _PNG})
    assert url is not None
    assert url.startswith(f"{base}/gfs/pages/spaces/sp/image?v=")
    icon_only = space_image_url(
        base, {"space_id": "sp", "cover_url": None, "icon_url": _PNG}
    )
    assert icon_only == url
    refused = {
        "space_id": "sp",
        "cover_url": "https://t.example/c.png",
        "icon_url": "data:image/svg+xml;base64,PHN2Zz4=",
    }
    assert space_image_url(base, refused) is None
