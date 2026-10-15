"""No font CDN anywhere in what Social Home serves.

Google Fonts (used until 2026-10) leaked every viewer's IP address and
user agent to Google on every page load and broke LAN-only / offline
installs. Every font is now bundled: the SPA's in
``client/src/styles/fonts.css``, the GFS public pages' in
``socialhome/global_server/static/fonts/``. This guard scans the Python
package (server-rendered pages, the household dashboard on port 8100,
GFS templates and static files, and the SPA bundle when it has been
built) plus the SPA source, and fails on any third-party font host.

Twins: ``client/src/styles/fonts.test.ts`` (SPA source) and
``client/scripts/fonts-check.js`` (built bundles, CI frontend job).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "socialhome"
CLIENT = ROOT / "client"
GFS_FONTS = PACKAGE / "global_server" / "static" / "fonts"

_HOST_RE = re.compile(
    r"fonts\.(googleapis|gstatic)\.com|use\.typekit\.net|fonts\.bunny\.net",
    re.IGNORECASE,
)
_TEXT_SUFFIXES = {
    ".py",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".svg",
    ".ts",
    ".tsx",
}


def _text_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_file():
            out.append(root)
            continue
        for path in root.rglob("*"):
            if "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            if path.is_file() and path.suffix in _TEXT_SUFFIXES:
                out.append(path)
    return out


def test_no_third_party_font_host_in_served_or_source_files():
    roots = [
        PACKAGE,
        CLIENT / "src",
        CLIENT / "gfs",
        CLIENT / "public",
        CLIENT / "index.html",
    ]
    offenders = [
        str(p.relative_to(ROOT))
        for p in _text_files(*roots)
        if p.name != Path(__file__).name
        and _HOST_RE.search(p.read_text("utf-8", "replace"))
    ]
    assert offenders == []


def test_gfs_fonts_css_references_only_bundled_files():
    css = (GFS_FONTS / "fonts.css").read_text("utf-8")
    faces = re.findall(r"@font-face\s*\{[^}]*\}", css)
    assert faces
    for face in faces:
        assert "font-display: swap" in face
    urls = re.findall(r"url\('([^']+)'\)", css)
    assert len(urls) == len(faces)
    for url in urls:
        assert "/" not in url and ":" not in url, url
        assert (GFS_FONTS / url).is_file(), url
    # Every shipped woff2 is used (nothing stale left behind).
    assert {p.name for p in GFS_FONTS.glob("*.woff2")} == set(urls)


@pytest.mark.parametrize("family", ["Manrope", "Fraunces"])
def test_gfs_fonts_ship_their_licence(family):
    licence = (GFS_FONTS / f"{family}-OFL.txt").read_text("utf-8")
    assert "SIL Open Font License, Version 1.1" in licence
