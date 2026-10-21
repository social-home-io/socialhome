"""The pinned public-highlight viewer: its committed manifest and the links
the household mints for it (§highlights_public, "Pinned viewer")."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from socialhome.config import PROJECT_PINNED_VIEWER_URL
from socialhome.global_server.routes.highlights import _VIEWER_CSS
from socialhome.pinned_viewer import (
    MANIFEST_PATH,
    PinnedViewer,
    load_pinned_viewer,
    parse_manifest,
    pinned_link,
    viewer_base_url,
    viewer_gfs_base,
)

_REPO = Path(__file__).resolve().parent.parent
_SECRET = bytes(range(32))
_VIEWER = PinnedViewer(path="v/0123456789abcdef/", integrity="sha384-" + "A" * 64)


# ─── The committed manifest ──────────────────────────────────────────────


def test_committed_manifest_exists_and_is_well_formed():
    raw = json.loads(MANIFEST_PATH.read_text())
    assert set(raw) == {"version", "path", "integrity"}
    assert raw["version"] == 1
    assert re.fullmatch(r"v/[0-9a-f]{16}/", raw["path"])
    assert raw["integrity"].startswith("sha384-")
    assert len(base64.b64decode(raw["integrity"].removeprefix("sha384-"))) == 48
    viewer = load_pinned_viewer()
    assert viewer == PinnedViewer(path=raw["path"], integrity=raw["integrity"])


def test_manifest_ships_inside_the_package():
    assert MANIFEST_PATH == _REPO / "socialhome" / "pinned_viewer.json"


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"version": 2, "path": "v/0123456789abcdef/", "integrity": "sha384-x"},
        {"version": 1, "path": "../evil/", "integrity": "sha384-" + "A" * 64},
        {"version": 1, "path": "v/0123456789abcdef/", "integrity": "sha256-abc"},
        {"version": 1, "path": "v/0123456789abcdef/"},
    ],
)
def test_parse_manifest_refuses_malformed(data):
    with pytest.raises(ValueError):
        parse_manifest(data)


def test_load_returns_none_for_a_missing_or_broken_manifest(tmp_path):
    assert load_pinned_viewer(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_pinned_viewer(bad) is None


def test_pinned_page_styles_match_the_gfs_page():
    """``client/gfs/public_highlight.css`` is what the pinned page inlines;
    the GFS-served page carries the same rules as ``_VIEWER_CSS``."""
    text = (_REPO / "client" / "gfs" / "public_highlight.css").read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    assert "".join(line.strip() for line in text.split("\n")) == _VIEWER_CSS


# ─── The GFS address the viewer may talk to ──────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://gfs.example.org", "https://gfs.example.org"),
        ("https://gfs.example.org/", "https://gfs.example.org"),
        ("https://gfs.example.org:8443/p/", "https://gfs.example.org:8443/p"),
        ("http://127.0.0.1:18765", "http://127.0.0.1:18765"),
        ("http://localhost:8765/", "http://localhost:8765"),
        ("http://gfs.example.org", None),
        ("http://gfs:8765", None),
        ("javascript:alert(1)", None),
        ("https://user:pw@gfs.example.org", None),
        ("https://gfs.example.org/?a=b", None),
        ("https://gfs.example.org/#x", None),
        ("https://gfs.example.org:99999", None),
        ("https://[::1", None),
        ("", None),
        ("https://gfs.example.org/" + "a" * 600, None),
        # Review: anything a browser could parse into another host than ours.
        ("https://gfs.example.org\\@evil.example", None),
        ("https://gfs example.org", None),
        ("https://gfs.example.org%2F@evil", None),
    ],
)
def test_viewer_gfs_base_mirrors_the_viewer(url, expected):
    assert viewer_gfs_base(url) == expected


# ─── The link ────────────────────────────────────────────────────────────


def test_pinned_link_shape():
    link = pinned_link(
        "https://viewer.example/socialhome/",
        _VIEWER,
        gfs_base="https://gfs.example.org",
        instance_id="inst-self",
        highlight_id="hl-1",
        token="tok-A",
        secret=_SECRET,
    )
    parts = urlsplit(link)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://viewer.example/socialhome/v/0123456789abcdef/"
    )
    assert parts.query == ""
    fragment = dict(parse_qsl(parts.fragment))
    assert list(dict(parse_qsl(parts.fragment))) == ["g", "i", "h", "t", "k"]
    assert fragment == {
        "g": "https://gfs.example.org",
        "i": "inst-self",
        "h": "hl-1",
        "t": "tok-A",
        "k": base64.urlsafe_b64encode(_SECRET).rstrip(b"=").decode(),
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://viewer.social-home.io", "https://viewer.social-home.io"),
        ("https://viewer.example/socialhome/", "https://viewer.example/socialhome"),
        ("http://127.0.0.1:18931", "http://127.0.0.1:18931"),
        # Review: a page served over plain http can be swapped on the
        # network for one that reads ``#k``.
        ("http://viewer.example", None),
        ("https://viewer.example/?a=b", None),
        ("https://viewer.example/#x", None),
        ("https://user@viewer.example", None),
        ("ftp://viewer.example", None),
        ("", None),
    ],
)
def test_the_viewer_base_must_be_https(url, expected):
    assert viewer_base_url(url) == expected


def test_the_default_viewer_has_its_own_origin():
    """Review: the github.io origin is shared by every Pages site of the
    org (and its unclaimed root site could register a service worker over
    it), so the default is a custom domain of its own."""
    assert urlsplit(PROJECT_PINNED_VIEWER_URL).hostname == "viewer.social-home.io"
