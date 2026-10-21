"""The GFS's public pages open in the pinned viewer ("one viewer for the
outside world", owner decision 2026-10-21).

A space's page (``/spaces/{id}``), an invite link (``/join/{token}``) and a
person's Momentum profile (``/moments/{user}``) are what a non-member
opens. With ``[server] public_viewer_base_url`` set they answer a ``302``
to the same static, content-addressed page the public highlight and moment
links open (:mod:`socialhome.pinned_viewer`)::

    {viewer}/v/<hash>/#v=space&g=<this GFS>&s=<space id>
    {viewer}/v/<hash>/#v=join&g=<this GFS>&t=<invite token>
    {viewer}/v/<hash>/#v=person&g=<this GFS>&u=<user id>

Everything rides in the FRAGMENT, which no browser sends: the viewer's host
never learns which page was opened, and an invite token never reaches its
logs or a ``Referer``. The viewer then reads this server's JSON
(``/gfs/pages/…``, :mod:`.routes.viewer_pages`) cross-origin.

**Not an open redirect.** The target is built from configuration only —
the configured viewer base, the committed manifest's ``v/<hash>/`` and this
server's own ``base_url`` — and the one request-derived value goes,
URL-encoded, after the ``#``. It is forwarded only when it is an id the
viewer accepts (URL-safe characters, bounded); anything else keeps the page
this server renders, so a request can never steer where the browser goes.

Empty ``public_viewer_base_url`` (a self-hosted GFS without a viewer), a
base that isn't ``https://`` (``http://`` only for localhost), a
``base_url`` the viewer would refuse, or no usable manifest: the pages
render here as before (:mod:`.public`), with a WARNING for a setting that
can't be used.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..pinned_viewer import (
    PinnedViewer,
    viewer_base_url,
    viewer_gfs_base,
    viewer_page_link,
)

if TYPE_CHECKING:
    from .config import GfsConfig

log = logging.getLogger(__name__)

#: The viewer's page names (``v`` in the fragment, ``client/gfs/page_link.ts``).
VIEW_SPACE: str = "space"
VIEW_JOIN: str = "join"
VIEW_PERSON: str = "person"

#: What the viewer accepts as an id or token: the URL-safe characters the
#: households and this server mint (``pinned_link.ts`` ``SAFE_ID``).
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._~-]+")

#: Longest value per fragment field the viewer accepts (``page_link.ts``):
#: space ids and user ids are wire ids (128), invite tokens 32 chars today.
MAX_SPACE_ID_CHARS: int = 128
MAX_INVITE_TOKEN_CHARS: int = 256
MAX_USER_ID_CHARS: int = 128


def _safe(value: str, max_chars: int) -> bool:
    return 0 < len(value) <= max_chars and _SAFE_ID_RE.fullmatch(value) is not None


@dataclass(slots=True, frozen=True)
class PublicViewerLinks:
    """Builds the viewer links for this GFS's public pages."""

    base_url: str
    viewer: PinnedViewer
    gfs_base: str

    def _link(self, view: str, key: str, value: str, max_chars: int) -> str | None:
        if not _safe(value, max_chars):
            return None
        return viewer_page_link(
            self.base_url,
            self.viewer,
            gfs_base=self.gfs_base,
            view=view,
            params=[(key, value)],
        )

    def space(self, space_id: str) -> str | None:
        """The viewer link of ``/spaces/{space_id}``, or ``None``."""
        return self._link(VIEW_SPACE, "s", space_id, MAX_SPACE_ID_CHARS)

    def invite(self, token: str) -> str | None:
        """The viewer link of ``/join/{token}``, or ``None``."""
        return self._link(VIEW_JOIN, "t", token, MAX_INVITE_TOKEN_CHARS)

    def person(self, user_id: str) -> str | None:
        """The viewer link of ``/moments/{user_id}``, or ``None``."""
        return self._link(VIEW_PERSON, "u", user_id, MAX_USER_ID_CHARS)


def build_public_viewer_links(
    config: GfsConfig, viewer: PinnedViewer | None
) -> PublicViewerLinks | None:
    """The links for ``config``, or ``None`` when the pages render here."""
    raw = config.public_viewer_base_url
    if not raw:
        return None
    base = viewer_base_url(raw)
    if base is None:
        log.warning(
            "GFS: public_viewer_base_url %s is not an https base URL; the "
            "public pages render on this server",
            ascii(raw),
        )
        return None
    gfs_base = viewer_gfs_base(config.base_url)
    if gfs_base is None:
        log.warning(
            "GFS: base_url %s is not one the pinned viewer accepts (https, a "
            "plain host); the public pages render on this server",
            ascii(config.base_url),
        )
        return None
    if viewer is None:
        # ``load_pinned_viewer`` already logged why.
        return None
    return PublicViewerLinks(base_url=base, viewer=viewer, gfs_base=gfs_base)
