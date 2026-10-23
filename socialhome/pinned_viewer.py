"""The pinned public-highlight viewer (§highlights_public, "Pinned viewer").

A public highlight link opens a static, content-addressed page
(``{public_viewer_base_url}/v/<hash>/``) that no GFS serves, with its one
script pinned by SRI. The GFS can't rewrite the code that reads the link
secret, because it never serves that code. The page reads everything it
needs from the link's fragment, which browsers never send to any server::

    {base}/v/<hash>/#g=<GFS base URL>&i=<instance>&h=<highlight>&t=<token>&k=<secret>

``socialhome/pinned_viewer.json`` (written by
``client/scripts/build-pinned-viewer.mjs`` and committed; CI fails when it
drifts from the viewer code) names the version this release links to.
The ``social-home-io/viewer`` repo rebuilds that version from the release,
checks it against this manifest and publishes its files.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import orjson

from .crypto import b64url_encode

log = logging.getLogger(__name__)

#: The committed manifest, shipped inside the package.
MANIFEST_PATH = Path(__file__).with_name("pinned_viewer.json")

#: The only manifest shape this release understands.
MANIFEST_VERSION = 1

#: Longest GFS base URL the viewer accepts (``pinned_link.ts``).
MAX_GFS_URL_LEN = 512

_PATH_RE = re.compile(r"v/[0-9a-f]{16}/")
_INTEGRITY_RE = re.compile(r"sha384-[A-Za-z0-9+/]{64}")
_DEV_HOSTS = frozenset({"localhost", "127.0.0.1"})


@dataclass(slots=True, frozen=True)
class PinnedViewer:
    """One published viewer version: its path under the viewer base URL
    and the SRI integrity of its script."""

    path: str
    integrity: str


def parse_manifest(data: object) -> PinnedViewer:
    """The viewer a manifest names; ``ValueError`` on any other shape."""
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        raise ValueError("unsupported pinned-viewer manifest")
    path = data.get("path")
    integrity = data.get("integrity")
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
        raise ValueError("pinned-viewer manifest: bad path")
    if not isinstance(integrity, str) or not _INTEGRITY_RE.fullmatch(integrity):
        raise ValueError("pinned-viewer manifest: bad integrity")
    if len(base64.b64decode(integrity.removeprefix("sha384-"))) != 48:
        raise ValueError("pinned-viewer manifest: bad integrity")
    return PinnedViewer(path=path, integrity=integrity)


def load_pinned_viewer(path: Path = MANIFEST_PATH) -> PinnedViewer | None:
    """Read the manifest once at startup (sync: called from ``create_app``).

    ``None`` when it is missing or malformed: the household then mints the
    GFS-served link, as with ``public_viewer_base_url`` empty.
    """
    try:
        return parse_manifest(orjson.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        log.warning(
            "pinned highlight viewer manifest %s unusable (%s): public "
            "highlight links fall back to the GFS-served page",
            path,
            exc,
        )
        return None


#: A host as both the household and the browser read it: letters, digits,
#: dots and dashes, an optional port. Anything else (a backslash, a space,
#: a percent escape) could make the page parse another host than ours.
_PLAIN_NETLOC_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


def _https_base(url: str) -> str | None:
    """``url`` as a base the pinned link may name, or ``None``: ``https://``
    (``http://`` only for localhost / 127.0.0.1), a plain host, no
    credentials, query or fragment, at most :data:`MAX_GFS_URL_LEN`
    characters; trailing slashes dropped."""
    if not url or len(url) > MAX_GFS_URL_LEN or "?" in url or "#" in url:
        return None
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        _ = parts.port  # raises on a port the browser would refuse too
    except ValueError:
        return None
    if parts.scheme == "http":
        if hostname not in _DEV_HOSTS:
            return None
    elif parts.scheme != "https":
        return None
    if (
        not hostname
        or parts.username is not None
        or parts.password is not None
        or not _PLAIN_NETLOC_RE.match(parts.netloc)
    ):
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")


def viewer_base_url(url: str) -> str | None:
    """The configured ``public_viewer_base_url`` if a pinned link may name
    it, else ``None``. A plain-http page could be swapped on the network
    for one that reads ``#k``, so only ``https://`` (and localhost for
    development) is accepted."""
    return _https_base(url)


def viewer_gfs_base(url: str) -> str | None:
    """The GFS base URL as the pinned viewer will accept it, or ``None``.

    Mirrors ``gfsBaseUrl`` in ``client/gfs/pinned_link.ts``: ``https://``
    (``http://`` only for localhost / 127.0.0.1), no credentials, query or
    fragment, at most :data:`MAX_GFS_URL_LEN` characters; trailing slashes
    dropped; the host plain, so the household and the browser read the same
    one. A link the viewer would refuse is never minted.
    """
    return _https_base(url)


def viewer_page_link(
    base_url: str,
    viewer: PinnedViewer,
    *,
    gfs_base: str,
    view: str,
    params: list[tuple[str, str]],
) -> str:
    """The link that opens one of the viewer's public pages (``view``: a
    space, an invite, a person) for the GFS at ``gfs_base``. Like
    :func:`pinned_link`, everything rides in the fragment, so the viewer's
    host never logs which page was opened (nor an invite token)."""
    fragment = urlencode([("v", view), ("g", gfs_base), *params])
    return f"{base_url.rstrip('/')}/{viewer.path}#{fragment}"


def pinned_link(
    base_url: str,
    viewer: PinnedViewer,
    *,
    gfs_base: str,
    instance_id: str,
    highlight_id: str,
    token: str,
    secret: bytes,
) -> str:
    """The share link that opens ``viewer`` on ``base_url``. Everything the
    page needs is in the fragment, which never reaches a server."""
    fragment = urlencode(
        [
            ("g", gfs_base),
            ("i", instance_id),
            ("h", highlight_id),
            ("t", token),
            ("k", b64url_encode(secret)),
        ]
    )
    return f"{base_url.rstrip('/')}/{viewer.path}#{fragment}"
