"""Viewer links for the GFS's public pages (:mod:`.public_viewer`)."""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl

import pytest

from socialhome.global_server.config import GfsConfig
from socialhome.global_server.public_viewer import (
    MAX_INVITE_TOKEN_CHARS,
    MAX_SPACE_ID_CHARS,
    MAX_USER_ID_CHARS,
    PublicViewerLinks,
    build_public_viewer_links,
)
from socialhome.pinned_viewer import PinnedViewer

_VIEWER = PinnedViewer(path="v/0123456789abcdef/", integrity="sha384-" + "A" * 64)


def _cfg(**kw) -> GfsConfig:
    base = {"base_url": "https://gfs.example.org", "public_viewer_base_url": ""}
    base.update(kw)
    return GfsConfig(**base)


def _links() -> PublicViewerLinks:
    links = build_public_viewer_links(
        _cfg(public_viewer_base_url="https://viewer.example.org/"), _VIEWER
    )
    assert links is not None
    return links


def _parse(link: str) -> tuple[str, dict[str, str]]:
    page, _, fragment = link.partition("#")
    return page, dict(parse_qsl(fragment, keep_blank_values=True))


def test_links_carry_everything_in_the_fragment():
    links = _links()
    for link, view, key, value in (
        (links.space("sp-1"), "space", "s", "sp-1"),
        (links.invite("tok_A-b.c~"), "join", "t", "tok_A-b.c~"),
        (links.person("u-1"), "person", "u", "u-1"),
    ):
        assert link is not None
        page, params = _parse(link)
        assert page == "https://viewer.example.org/v/0123456789abcdef/"
        assert params == {"v": view, "g": "https://gfs.example.org", key: value}


@pytest.mark.parametrize(
    ("method", "limit"),
    [
        ("space", MAX_SPACE_ID_CHARS),
        ("invite", MAX_INVITE_TOKEN_CHARS),
        ("person", MAX_USER_ID_CHARS),
    ],
)
def test_ids_are_bounded(method, limit):
    build = getattr(_links(), method)
    assert build("a" * limit) is not None
    assert build("a" * (limit + 1)) is None
    assert build("") is None


@pytest.mark.security
@pytest.mark.parametrize(
    "value",
    ["a b", "a#g=https://evil", "a&g=x", "../x", "a/b", "<s>", "é", "a\n", "..", "."],
)
def test_an_id_outside_the_viewers_alphabet_builds_no_link(value):
    links = _links()
    assert links.space(value) is None
    assert links.invite(value) is None
    assert links.person(value) is None


def test_no_viewer_configured_means_no_links(caplog):
    with caplog.at_level(logging.WARNING):
        assert build_public_viewer_links(_cfg(), _VIEWER) is None
    assert not caplog.records


@pytest.mark.parametrize(
    "kw",
    [
        {"public_viewer_base_url": "http://viewer.example.org"},
        {"public_viewer_base_url": "https://u:p@viewer.example.org"},
        {"public_viewer_base_url": "https://viewer.example.org/?x=1"},
        {"public_viewer_base_url": "ftp://viewer.example.org"},
        {
            "public_viewer_base_url": "https://v.example.org",
            "base_url": "http://gfs.lan",
        },
        {"public_viewer_base_url": "https://v.example.org", "base_url": ""},
    ],
)
def test_an_unusable_setting_warns_and_keeps_the_pages_here(kw, caplog):
    with caplog.at_level(logging.WARNING):
        assert build_public_viewer_links(_cfg(**kw), _VIEWER) is None
    assert caplog.records


def test_no_manifest_means_no_links():
    assert (
        build_public_viewer_links(
            _cfg(public_viewer_base_url="https://viewer.example.org"), None
        )
        is None
    )


def test_localhost_viewer_and_gfs_for_development():
    links = build_public_viewer_links(
        _cfg(
            public_viewer_base_url="http://127.0.0.1:8099",
            base_url="http://127.0.0.1:18765",
        ),
        _VIEWER,
    )
    assert links is not None
    assert links.space("s").startswith("http://127.0.0.1:8099/v/")
