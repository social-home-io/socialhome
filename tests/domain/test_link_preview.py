"""Tests for :mod:`socialhome.domain.link_preview`."""

from __future__ import annotations

import pytest

from socialhome.domain.link_preview import (
    LINK_PREVIEW_DESCRIPTION_MAX,
    LINK_PREVIEW_TITLE_MAX,
    LinkPreview,
    clean_text,
    first_url,
    link_preview_from_dict,
    link_preview_to_dict,
    normalise_url,
)


def _keep(value: object) -> str | None:
    return value if isinstance(value, str) else None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see https://example.com/a?b=1 now", "https://example.com/a?b=1"),
        ("end of sentence https://example.com/x.", "https://example.com/x"),
        ("wow http://example.com!", "http://example.com"),
        ("[label](https://example.com/p) and more", "https://example.com/p"),
        ("<https://example.com/q>", "https://example.com/q"),
        ("two https://a.example then https://b.example", "https://a.example"),
        ("HTTPS://EXAMPLE.com/Up", "HTTPS://EXAMPLE.com/Up"),
        ("no link here", None),
        ("ftp://example.com/file", None),
        ("", None),
        (None, None),
    ],
)
def test_first_url(text: str | None, expected: str | None) -> None:
    assert first_url(text) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Example.COM/Path?q=1#frag", "https://example.com/Path?q=1"),
        ("http://example.com", "http://example.com/"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("https://[2001:db8::1]/", "https://[2001:db8::1]/"),
        ("javascript:alert(1)", None),
        ("data:text/html,x", None),
        ("https://user:pw@example.com/", None),
        ("https:///nohost", None),
        ("https://example.com:99999/", None),
        ("https://exa mple.com/", None),
        ("https://example.com/\x00", None),
        ("https://example.com/" + "a" * 3000, None),
        (42, None),
        (None, None),
    ],
)
def test_normalise_url(url: object, expected: str | None) -> None:
    assert normalise_url(url) == expected


def test_clean_text() -> None:
    assert clean_text("  a\n\tb\x07  ", limit=10) == "a b"
    assert clean_text("", limit=10) is None
    assert clean_text("   ", limit=10) is None
    assert clean_text(5, limit=10) is None
    clipped = clean_text("x" * 50, limit=10)
    assert clipped is not None and len(clipped) == 10 and clipped.endswith("…")


def test_round_trip() -> None:
    p = LinkPreview(
        url="https://example.com/",
        title="T",
        description="D",
        site_name="S",
        thumbnail_url="api/media/abc.webp",
    )
    d = link_preview_to_dict(p)
    assert d == {
        "url": "https://example.com/",
        "title": "T",
        "description": "D",
        "site_name": "S",
        "thumbnail_url": "api/media/abc.webp",
    }
    assert link_preview_from_dict(d, image_ref=_keep) == p
    assert link_preview_to_dict(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "string",
        [],
        {"title": "no url"},
        {"url": "javascript:alert(1)", "title": "x"},
        {"url": "https://example.com/"},  # nothing to show
        {"url": "https://example.com/", "title": 5, "description": ["x"]},
    ],
)
def test_from_dict_rejects(raw: object) -> None:
    assert link_preview_from_dict(raw, image_ref=_keep) is None


def test_from_dict_clips_and_filters_image() -> None:
    raw = {
        "url": "https://Example.com/a#x",
        "title": "t" * 1000,
        "description": "d" * 5000,
        "site_name": {"x": 1},
        "thumbnail_url": "https://evil.example/x.png",
    }
    p = link_preview_from_dict(raw, image_ref=lambda v: None)
    assert p is not None
    assert p.url == "https://example.com/a"
    assert p.title is not None and len(p.title) == LINK_PREVIEW_TITLE_MAX
    assert p.description is not None
    assert len(p.description) == LINK_PREVIEW_DESCRIPTION_MAX
    assert p.site_name is None
    assert p.thumbnail_url is None
