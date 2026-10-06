"""Minimal, XSS-safe Markdown→HTML for GFS public pages.

The GFS renders a space's owner-supplied ``about_markdown`` on an
anonymous public page. That text is untrusted, and the GFS process is
deliberately dependency-light — so rather than pull in a full Markdown
engine *plus* an HTML sanitizer, this renders a small, safe subset:

- ``#`` … ``######`` headings (single-line blocks)
- ``**bold**`` / ``__bold__`` and ``*italic*`` / ``_italic_``
- `` `inline code` ``
- ``-`` / ``*`` unordered lists
- ``>`` blockquotes
- ``[text](url)`` links — ``http`` / ``https`` / ``mailto`` only,
  ``rel``-guarded, opened in a new tab
- blank-line-separated paragraphs; a single newline becomes ``<br>``

**XSS-safe by construction:** the source is HTML-escaped *first*, then
only the whitelist of tags this module emits is introduced. No raw user
HTML is ever passed through, so there is nothing left to sanitize — a
``<script>`` in the source is already ``&lt;script&gt;`` before any
formatting runs, and a link's URL is both scheme-checked and
quote-escaped so it can't break out of the ``href`` attribute.
"""

from __future__ import annotations

import re

#: Hard cap on the source length we will render. ``about_markdown`` is a
#: short space blurb; without a bound, a paired instance could publish a
#: large blob of pathological metacharacters and the (worst-case
#: super-linear) inline passes would block the GFS event loop on every
#: anonymous page view. Anything longer is truncated before rendering.
MAX_SOURCE_CHARS: int = 4000

_ESCAPE = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
}

#: Link schemes we will turn into an anchor. Anything else (notably
#: ``javascript:`` / ``data:``) is left as inert literal text.
_ALLOWED_SCHEMES = ("http://", "https://", "mailto:")

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC_RE = re.compile(r"(?<![*_\w])([*_])(?=\S)(.+?)(?<=\S)\1(?![*_\w])")
_CODE_RE = re.compile(r"`([^`]+)`")
_HEADING_RE = re.compile(r"(#{1,6})\s+(.*)")
_LIST_ITEM_RE = re.compile(r"[-*]\s+")


def _escape(text: str) -> str:
    return "".join(_ESCAPE.get(ch, ch) for ch in text)


def _render_link(m: re.Match[str]) -> str:
    text, url = m.group(1), m.group(2)
    if not any(url.lower().startswith(s) for s in _ALLOWED_SCHEMES):
        # Not a safe scheme — leave the original markdown as literal text
        # rather than emit a dangerous anchor.
        return m.group(0)
    # ``url`` is already HTML-escaped (quotes → &quot;) so it cannot break
    # out of the attribute; the scheme allow-list blocks script URLs.
    return f'<a href="{url}" rel="noopener noreferrer nofollow ugc" target="_blank">{text}</a>'


def _inline(text: str) -> str:
    text = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    # Skip the (super-linear-on-bracket-runs) link pass entirely unless the
    # ``](`` that a link requires is actually present — cheap guard that
    # flattens the common case to linear.
    if "](" in text:
        text = _LINK_RE.sub(_render_link, text)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(2)}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group(2)}</em>", text)
    return text


def render_markdown(src: str | None) -> str:
    """Render a safe HTML subset of ``src``. Returns ``""`` for empty input."""
    if not src or not src.strip():
        return ""
    if len(src) > MAX_SOURCE_CHARS:
        src = src[:MAX_SOURCE_CHARS] + "…"
    escaped = _escape(src.replace("\r\n", "\n").replace("\r", "\n"))
    out: list[str] = []
    for block in re.split(r"\n[ \t]*\n", escaped.strip()):
        lines = [ln for ln in block.split("\n")]
        nonblank = [ln for ln in lines if ln.strip()]
        if not nonblank:
            continue
        heading = _HEADING_RE.fullmatch(lines[0]) if len(lines) == 1 else None
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif all(_LIST_ITEM_RE.match(ln) for ln in nonblank):
            items = "".join(
                f"<li>{_inline(_LIST_ITEM_RE.sub('', ln, count=1))}</li>"
                for ln in nonblank
            )
            out.append(f"<ul>{items}</ul>")
        elif all(ln.startswith("&gt;") for ln in nonblank):
            inner = " ".join(re.sub(r"^&gt;[ \t]?", "", ln) for ln in nonblank)
            out.append(f"<blockquote>{_inline(inner)}</blockquote>")
        else:
            out.append("<p>" + "<br>".join(_inline(ln) for ln in lines) + "</p>")
    return "".join(out)
