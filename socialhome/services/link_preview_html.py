"""Read a page's preview metadata out of its HTML ``<head>``.

Pure and synchronous (CPU-bound on a 512 KiB page — callers run it via
``asyncio.to_thread``). Uses the standard library's tolerant
:class:`html.parser.HTMLParser`; nothing is executed or rendered, and the
parser stops at ``</head>`` / ``<body>`` so a page's body is never walked.

Precedence per field: OpenGraph (``og:*``) → Twitter card
(``twitter:*``) → plain HTML (``<title>``, ``<meta name=description>``).
The values come back raw (entity-decoded, not yet clipped); the caller
cleans and bounds them through :mod:`socialhome.domain.link_preview`.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

#: ``<meta charset=…>`` / ``content="…; charset=…"`` sniffed from raw bytes.
_META_CHARSET_RE: re.Pattern[bytes] = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]{1,40})", re.IGNORECASE
)
#: How far into the body the charset sniff looks.
_SNIFF_BYTES: int = 4096


@dataclass(slots=True, frozen=True)
class PageMeta:
    """Raw preview fields found in one page (DTO, never stored)."""

    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    #: Absolute URL of the preview image, if the page names one.
    image_url: str | None = None
    #: Absolute ``og:url`` / ``<link rel=canonical>``, if present.
    canonical_url: str | None = None


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.title_parts: list[str] = []
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if tag == "body":
            self.done = True
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = a.get("content")
            if key and content is not None and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rels = a.get("rel", "").lower().split()
            if "canonical" in rels and a.get("href") and self.canonical is None:
                self.canonical = a["href"]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.done:
            self.title_parts.append(data)


def decode_html(body: bytes, charset: str | None) -> str:
    """Decode *body* by the header charset, a ``<meta charset>``, or UTF-8."""
    for candidate in (charset, _sniff_charset(body)):
        if not candidate:
            continue
        try:
            info = codecs.lookup(candidate)
        except LookupError:
            continue
        # ``codecs.lookup`` also knows bytes-to-bytes codecs (``zlib_codec``,
        # ``rot13``…) that ``bytes.decode`` refuses — a page must not be able
        # to pick one.
        if not getattr(info, "_is_text_encoding", True):
            continue
        try:
            return body.decode(info.name, errors="replace")
        except LookupError:  # pragma: no cover — defensive
            continue
    return body.decode("utf-8", errors="replace")


def _sniff_charset(body: bytes) -> str | None:
    m = _META_CHARSET_RE.search(body[:_SNIFF_BYTES])
    return m.group(1).decode("ascii", errors="ignore") if m else None


def _absolute(base_url: str, ref: str | None) -> str | None:
    if not ref or not ref.strip():
        return None
    try:
        return urljoin(base_url, ref.strip())
    except ValueError:
        return None


def extract_page_meta(body: bytes, charset: str | None, base_url: str) -> PageMeta:
    """Preview fields from an HTML document (see the module docstring)."""
    parser = _HeadParser()
    try:
        parser.feed(decode_html(body, charset))
        parser.close()
    except AssertionError:  # pragma: no cover — malformed-markup guard
        pass
    m = parser.meta

    def pick(*keys: str) -> str | None:
        for k in keys:
            v = m.get(k)
            if v and v.strip():
                return v
        return None

    html_title = "".join(parser.title_parts).strip() or None
    return PageMeta(
        title=pick("og:title", "twitter:title") or html_title,
        description=pick("og:description", "twitter:description", "description"),
        site_name=pick("og:site_name", "application-name"),
        image_url=_absolute(
            base_url,
            pick("og:image:secure_url", "og:image", "og:image:url", "twitter:image"),
        ),
        canonical_url=_absolute(base_url, pick("og:url") or parser.canonical),
    )
