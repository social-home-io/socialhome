"""Link preview — the card a post shows for the first web link in its text.

The **author's** household builds the preview once (it fetches the page
through :mod:`socialhome.outbound_fetch`) and the preview then travels
inside the post — in the encrypted ``SPACE_POST_CREATED`` payload for a
space post — so a household that *receives* the post never fetches the URL:
no reader's IP reaches the linked site and a popular post causes no fetch
storm.

Everything here is pure: the value type, its wire / storage shape, and the
bounds a received preview is held to. What a peer sends is untrusted, so
:func:`link_preview_from_dict` re-checks every field (types, lengths, URL
scheme) and drops the whole preview on a malformed URL rather than trusting
it. The image reference is normalised by the caller-supplied
``image_ref`` (``inbound_media_store.local_media_ref`` for peer data): only
a local ``api/media/<name>`` reference survives, never a remote URL.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

#: Longest URL a preview may carry.
LINK_PREVIEW_URL_MAX: int = 2048
#: Longest title kept (characters).
LINK_PREVIEW_TITLE_MAX: int = 300
#: Longest description kept (characters).
LINK_PREVIEW_DESCRIPTION_MAX: int = 500
#: Longest site name kept (characters).
LINK_PREVIEW_SITE_NAME_MAX: int = 100

#: A web link in post text. Stops at whitespace and at the characters that
#: close a markdown link / HTML attribute, so ``[x](https://a.b/c)`` yields
#: ``https://a.b/c``.
_URL_RE: re.Pattern[str] = re.compile(r"https?://[^\s<>\"'()\[\]{}`]+", re.IGNORECASE)
#: Punctuation that ends a sentence, not a URL.
_TRAILING_PUNCT: str = ".,;:!?*_~"

_CONTROL_RE: re.Pattern[str] = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS_RE: re.Pattern[str] = re.compile(r"\s+")


@dataclass(slots=True, frozen=True)
class LinkPreview:
    """What a post's link card renders. Every text field is plain text."""

    #: The link the card opens — ``http(s)`` only, no credentials.
    url: str
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    #: Local ``api/media/<name>`` reference to the re-encoded image, or
    #: ``None``. Named ``thumbnail_url`` so the media signer signs it.
    thumbnail_url: str | None = None


def first_url(text: str | None) -> str | None:
    """The first ``http(s)`` link in *text*, trailing punctuation trimmed."""
    if not text:
        return None
    m = _URL_RE.search(text)
    if m is None:
        return None
    url = m.group(0).rstrip(_TRAILING_PUNCT)
    return url or None


def normalise_url(url: object) -> str | None:
    """Canonical form of a link, or ``None`` when it is not a plain web URL.

    Lower-cases scheme and host, drops the fragment, refuses any scheme but
    ``http`` / ``https``, user-info, a missing host and over-long input.
    """
    if not isinstance(url, str) or not url or len(url) > LINK_PREVIEW_URL_MAX:
        return None
    if _CONTROL_RE.search(url) or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host or parts.username is not None or parts.password is not None:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def clean_text(value: object, *, limit: int) -> str | None:
    """Plain, single-line text of at most *limit* characters, or ``None``."""
    if not isinstance(value, str):
        return None
    text = _WS_RE.sub(" ", _CONTROL_RE.sub("", value)).strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def link_preview_to_dict(preview: LinkPreview | None) -> dict | None:
    """Wire / storage shape. ``None`` stays ``None``."""
    if preview is None:
        return None
    return {
        "url": preview.url,
        "title": preview.title,
        "description": preview.description,
        "site_name": preview.site_name,
        "thumbnail_url": preview.thumbnail_url,
    }


def link_preview_from_dict(
    raw: object,
    *,
    image_ref: Callable[[object], str | None],
) -> LinkPreview | None:
    """Rebuild a preview from untrusted data, or ``None`` when unusable.

    A bad URL drops the preview (a card that opens something else than it
    says is worse than no card); over-long text is clipped; a card with
    neither title nor description carries nothing worth showing and is
    dropped too. ``image_ref`` maps the image reference to the local shape
    (or ``None``).
    """
    if not isinstance(raw, dict):
        return None
    url = normalise_url(raw.get("url"))
    if url is None:
        return None
    title = clean_text(raw.get("title"), limit=LINK_PREVIEW_TITLE_MAX)
    description = clean_text(raw.get("description"), limit=LINK_PREVIEW_DESCRIPTION_MAX)
    if title is None and description is None:
        return None
    return LinkPreview(
        url=url,
        title=title,
        description=description,
        site_name=clean_text(raw.get("site_name"), limit=LINK_PREVIEW_SITE_NAME_MAX),
        thumbnail_url=image_ref(raw.get("thumbnail_url")),
    )


def card_survives_edit(old_content: str | None, new_content: str | None) -> bool:
    """Whether a post keeps its link card across an edit.

    The card belongs to the first link it was built for: an edit that keeps
    that link keeps the card; one that removes or changes it drops the card
    (no re-fetch on edit). Every household applies the same rule to the
    content it already holds, so a ``SPACE_POST_UPDATED`` needs no new field.
    """
    before = normalise_url(first_url(old_content))
    return before is not None and before == normalise_url(first_url(new_content))
