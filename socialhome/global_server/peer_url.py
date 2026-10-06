"""Normalisation of a GFS cluster peer's base URL (spec §24.10).

One function, used wherever a peer URL enters the process: the admin
add-peer API, a shared-seed sibling's first ``NODE_HELLO`` and the
``[cluster] peers`` config list. Kept apart from :mod:`.cluster` so
:mod:`.config` can use it without an import cycle.

The URL is stored on the peer's ``cluster_nodes`` row, POSTed to on every
fan-out and echoed on the public ``GET /cluster/health``, so what comes
out is always printable ASCII re-serialised from validated parts — never
the input string itself.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

#: One DNS label after IDNA encoding. ``_`` is allowed: container and
#: service names (``gfs_node``) use it, and it can't inject anything.
_HOST_LABEL_RE = re.compile(r"[a-z0-9_-]{1,63}")


def _unsafe_char(ch: str) -> bool:
    """Whitespace, control, format (bidi, zero-width), separator,
    surrogate, private-use or unassigned — anything that could split a
    header line or render deceptively."""
    return ch.isspace() or unicodedata.category(ch)[0] in ("C", "Z")


def _ascii_host(host: str, *, bracketed: bool) -> str:
    """The host in ASCII (IDNA for a name, canonical for an IP), or ``""``."""
    if "%" in host:  # an IPv6 zone id, or a percent-escape in a name
        return ""
    if bracketed:
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError:
            return ""
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    try:
        encoded = host.encode("idna").decode("ascii")
        # Round-trip: catches ASCII labels the encoder passes through
        # unchecked, like a malformed ``xn--`` punycode label.
        encoded.encode("ascii").decode("idna")
    except UnicodeError:
        return ""
    labels = encoded.lower().split(".")
    if not all(_HOST_LABEL_RE.fullmatch(label) for label in labels):
        return ""
    return ".".join(labels)


def normalized_peer_url(url: object) -> str:
    """A peer node's base URL, normalised, or ``""`` if unusable.

    ``http``/``https`` with a host; no userinfo, query or fragment (none
    has a meaning for a base URL, and userinfo would ship credentials in
    every sync POST). Refused outright: any whitespace, control, bidi or
    other format character anywhere (surrounding whitespace is stripped
    first), non-ASCII outside the host, a host IDNA cannot encode, and a
    port that is out of range or 0. The host is IDNA-encoded and
    lower-cased, a trailing ``/`` is dropped, and the result is rebuilt
    with ``urlunsplit`` from the validated parts.
    """
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url or any(_unsafe_char(ch) for ch in url):
        return ""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ""
    if (
        parts.scheme not in ("http", "https")
        or not host
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or "?" in url
        or "#" in url
        or port == 0
    ):
        return ""
    path = parts.path.rstrip("/")
    if not path.isascii():
        return ""
    bracketed = parts.netloc.startswith("[")
    ascii_host = _ascii_host(host, bracketed=bracketed)
    if not ascii_host:
        return ""
    netloc = f"[{ascii_host}]" if bracketed else ascii_host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme, netloc, path, "", ""))
