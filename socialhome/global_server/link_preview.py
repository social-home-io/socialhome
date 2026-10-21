"""Link previews for the GFS's public pages (owner decision 2026-10-21).

``/spaces/{id}``, ``/join/{token}`` and ``/moments/{user}`` open in the
pinned viewer (:mod:`.public_viewer`). A bare ``302`` there made every chat
app's unfurl read the viewer's own page — "Social Home", no title, no
picture — because the viewer knows nothing until its script reads the
fragment. So these routes answer a tiny HTML hand-off page instead:

* ``og:`` / ``twitter:`` tags naming what the link opens — a listed space's
  name, description and cover; a person's display name, bio and picture
  when their Momentum directory entry is active (it is public then, at
  ``GET /gfs/moments/users/{user}``); for an invite only GENERIC tags;
* ``<meta http-equiv="refresh">`` (and a plain link) to the same viewer URL
  the ``302`` carried, so a browser lands where it did before.

**An invite says nothing.** ``/join/{token}`` never looks the token up: its
page is the same for a live, expired or never-issued token, apart from the
token echoed back inside the viewer link's fragment — the uniform answer
``handle_invite_page`` has always given a prober.

**The page itself is inert:** a ``default-src 'none'`` policy (no script,
no style, no image, no frame, no form), ``no-store`` (the viewer link may
carry an invite token) and ``Referrer-Policy: no-referrer`` (the refresh
must not tell the viewer's host which GFS path was opened). Every value is
HTML-escaped and bounded. Images are absolute URLs on this GFS's own
``base_url`` that it already serves anonymously — the mirrored person
picture and a listed space's stored raster cover (or icon)
(``GET /gfs/pages/spaces/{id}/image``) — never a URL a household supplied.

No ``302`` for "non-HTML clients": unfurlers send ``Accept: */*`` like any
script would, so telling them apart would cost the previews this page
exists for.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from urllib.parse import quote

from aiohttp import web

from ..csp import render_csp
from .page_models import (
    image_data_uri,
    load_person_page,
    load_space_page,
    server_name,
)
from .public_viewer import PublicViewerLinks

#: The hand-off page's policy: nothing may load or run.
HANDOFF_CSP: str = render_csp(
    {
        "default-src": ("'none'",),
        "base-uri": ("'none'",),
        "form-action": ("'none'",),
        "frame-ancestors": ("'none'",),
    }
)

#: Longest title / description a preview carries (cards show far less).
MAX_TITLE_CHARS: int = 120
MAX_DESCRIPTION_CHARS: int = 300

#: The route serving a listed space's preview image (cover, else icon).
SPACE_IMAGE_ROUTE: str = "/gfs/pages/spaces/{space_id}/image"


@dataclass(slots=True, frozen=True)
class LinkPreview:
    """What a chat app's card shows for one public page."""

    title: str
    description: str
    site_name: str
    og_type: str = "website"
    image_url: str | None = None
    #: A wide cover (``summary_large_image``) rather than a square picture.
    large_image: bool = False
    #: The page's canonical GFS URL; ``None`` where it would name a secret.
    canonical_url: str | None = None


def _clip(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def render_handoff(preview: LinkPreview, target: str) -> web.Response:
    """The hand-off page: *preview*'s tags + a refresh to *target*."""
    title = _e(_clip(preview.title, MAX_TITLE_CHARS))
    description = _e(_clip(preview.description, MAX_DESCRIPTION_CHARS))
    site = _e(_clip(preview.site_name, MAX_TITLE_CHARS))
    link = _e(target)
    tags = [
        f'<meta property="og:type" content="{_e(preview.og_type)}">',
        f'<meta property="og:site_name" content="{site}">',
        f'<meta property="og:title" content="{title}">',
        f'<meta property="og:description" content="{description}">',
        f'<meta name="description" content="{description}">',
        f'<meta name="twitter:title" content="{title}">',
        f'<meta name="twitter:description" content="{description}">',
    ]
    if preview.canonical_url:
        tags.append(f'<meta property="og:url" content="{_e(preview.canonical_url)}">')
    card = "summary"
    if preview.image_url:
        image = _e(preview.image_url)
        tags.append(f'<meta property="og:image" content="{image}">')
        tags.append(f'<meta name="twitter:image" content="{image}">')
        if preview.large_image:
            card = "summary_large_image"
    tags.append(f'<meta name="twitter:card" content="{card}">')
    head = "\n".join(tags)
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
{head}
<meta http-equiv="refresh" content="0;url={link}">
</head>
<body>
<p><a href="{link}" rel="noreferrer">{title}</a></p>
</body>
</html>
"""
    return web.Response(
        text=body,
        content_type="text/html",
        headers={
            "Content-Security-Policy": HANDOFF_CSP,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


def space_image_url(gfs_base: str, space: dict) -> str | None:
    """Absolute URL of a listed space's preview image, or ``None``.

    ``?v=`` names the stored bytes, so a card re-reads a changed cover."""
    stored = image_data_uri(space.get("cover_url")) or image_data_uri(
        space.get("icon_url")
    )
    if stored is None:
        return None
    version = hashlib.sha256(stored.encode("ascii")).hexdigest()[:16]
    path = SPACE_IMAGE_ROUTE.format(space_id=quote(space["space_id"], safe=""))
    return f"{gfs_base}{path}?v={version}"


async def space_handoff(
    app: web.Application, links: PublicViewerLinks, space_id: str, target: str
) -> web.Response:
    """``/spaces/{id}``: the listed space's name, description and cover."""
    site = await server_name(app)
    space = await load_space_page(app, space_id)
    if space is None:
        return render_handoff(
            LinkPreview(title=f"A space on {site}", description="", site_name=site),
            target,
        )
    name = space["name"] or "A space"
    return render_handoff(
        LinkPreview(
            title=name,
            description=space["description"] or f"A public space on {site}",
            site_name=site,
            image_url=space_image_url(links.gfs_base, space),
            large_image=image_data_uri(space.get("cover_url")) is not None,
            canonical_url=f"{links.gfs_base}/spaces/{quote(space_id, safe='')}",
        ),
        target,
    )


async def invite_handoff(app: web.Application, target: str) -> web.Response:
    """``/join/{token}``: generic tags only — the token is never looked up."""
    site = await server_name(app)
    return render_handoff(
        LinkPreview(
            title="You're invited to a space",
            description=(
                f"An invitation shared through {site}. Open it to get the "
                "invite code for your Social Home."
            ),
            site_name=site,
        ),
        target,
    )


async def person_handoff(
    app: web.Application, links: PublicViewerLinks, user_id: str, target: str
) -> web.Response:
    """``/moments/{user}``: name, bio and picture of an ACTIVE directory
    entry — exactly what ``GET /gfs/moments/users/{user}`` already shows."""
    site = await server_name(app)
    page = await load_person_page(app, user_id)
    if page is None:
        return render_handoff(
            LinkPreview(
                title=f"Public Momentum on {site}", description="", site_name=site
            ),
            target,
        )
    reg = page.registration
    image_url = None
    if reg.picture_digest:
        image_url = (
            f"{links.gfs_base}/gfs/moments/users/{quote(user_id, safe='')}"
            f"/picture?v={quote(reg.picture_digest, safe='')}"
        )
    return render_handoff(
        LinkPreview(
            title=reg.display_name or reg.username,
            description=reg.bio or f"@{reg.username} on Public Momentum",
            site_name=site,
            og_type="profile",
            image_url=image_url,
            canonical_url=f"{links.gfs_base}/moments/{quote(user_id, safe='')}",
        ),
        target,
    )
