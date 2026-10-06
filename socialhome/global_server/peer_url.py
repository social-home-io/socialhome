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
import socket
import unicodedata
from urllib.parse import urlsplit, urlunsplit

#: One DNS label after IDNA encoding. ``_`` is allowed: container and
#: service names (``gfs_node``) use it, and it can't inject anything.
_HOST_LABEL_RE = re.compile(r"[a-z0-9_-]{1,63}")

#: Addresses a peer URL may never point at: link-local (where cloud
#: instance-metadata services live — 169.254.169.254, and AWS's IPv6
#: ``fd00:ec2::254``), Alibaba Cloud's metadata address, and the RFC 8215
#: local-use NAT64 range ``64:ff9b:1::/48`` (where the embedded v4 address
#: sits depends on the operator's prefix length, so it cannot be checked —
#: a cluster peer has no reason to sit behind one). RFC 1918, ULA and
#: loopback stay allowed: cluster nodes legitimately sit on private networks.
_REFUSED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)

#: RFC 6052 well-known NAT64 prefix: ``64:ff9b::a.b.c.d`` reaches v4 a.b.c.d.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

#: SIIT IPv4-translated addresses (RFC 2765): ``::ffff:0:a.b.c.d``.
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")

#: Host names that resolve to a cloud instance-metadata service from inside
#: the instance: GCE (``metadata.google.internal`` and its short form
#: ``metadata``) and EC2 (``instance-data``, ``instance-data.ec2.internal``).
#: Azure, Oracle and DigitalOcean publish their metadata service on the
#: link-local IP only (refused above). Exact names, compared after IDNA
#: encoding and lower-casing; a name that merely RESOLVES to a metadata
#: address is still not caught (no lookup at validation time).
_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

#: A host made of digits and dots only — an IPv4 spelling.
_NUMERIC_HOST_RE = re.compile(r"[0-9.]+")


def _unsafe_char(ch: str) -> bool:
    """Whitespace, control, format (bidi, zero-width), separator,
    surrogate, private-use or unassigned — anything that could split a
    header line or render deceptively."""
    return ch.isspace() or unicodedata.category(ch)[0] in ("C", "Z")


def _refused_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Link-local / metadata, including an IPv6 spelling of a v4 one."""
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        packed = ip.packed
        if packed[:12] == bytes(12) and int(ip) > 1:  # IPv4-compatible ::a.b.c.d
            candidates.append(ipaddress.IPv4Address(packed[12:]))
        if ip in _NAT64 or ip in _SIIT:
            candidates.append(ipaddress.IPv4Address(packed[12:]))
    return any(c in net for c in candidates for net in _REFUSED_NETWORKS)


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """An IPv4 address in a spelling the system resolver accepts but
    ``ipaddress`` does not (``2852039166``, ``0xa9fea9fe``, octal,
    ``a.b.c`` short forms), or ``None``. Pure parsing — no DNS."""
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError, ValueError:
        return None


def _ascii_host(host: str, *, bracketed: bool) -> str:
    """The host in ASCII (IDNA for a name, canonical for an IP), or ``""``.

    A link-local or metadata address is ``""`` too, in any spelling.
    """
    if "%" in host:  # an IPv6 zone id, or a percent-escape in a name
        return ""
    if bracketed:
        try:
            ip6 = ipaddress.IPv6Address(host)
        except ValueError:
            return ""
        return "" if _refused_ip(ip6) else str(ip6)
    ip4 = _legacy_ipv4(host) if host.isascii() else None
    if ip4 is not None:
        return "" if _refused_ip(ip4) else str(ip4)
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
    ascii_host = ".".join(labels)
    # IDNA nameprep (NFKC) can turn a non-ASCII host into an IP address —
    # fullwidth digits and dots, ``。``, ``⑯`` — after the IP check above
    # saw only a name. An address must be written in ASCII: refuse any
    # non-ASCII host that encodes to one, in any spelling.
    if not host.isascii() and (
        _NUMERIC_HOST_RE.fullmatch(ascii_host) or _legacy_ipv4(ascii_host) is not None
    ):
        return ""
    if ascii_host in _METADATA_HOSTS:
        return ""
    return ascii_host


def normalized_peer_url(url: object) -> str:
    """A peer node's base URL, normalised, or ``""`` if unusable.

    ``http``/``https`` with a host; no userinfo, query or fragment (none
    has a meaning for a base URL, and userinfo would ship credentials in
    every sync POST). Refused outright: any whitespace, control, bidi or
    other format character anywhere (surrounding whitespace is stripped
    first), non-ASCII outside the host, a host IDNA cannot encode, a
    port that is out of range or 0, a link-local or cloud-metadata
    address (``169.254.0.0/16``, ``100.100.100.200``, ``fe80::/10``,
    ``fd00:ec2::254``, also as an IPv4-mapped, -compatible, -translated
    (SIIT) or NAT64 IPv6 address, any address in the local-use NAT64 range
    ``64:ff9b:1::/48``, or a legacy numeric IPv4 spelling), a cloud
    metadata host name (``metadata.google.internal``, ``metadata``,
    ``instance-data``, ``instance-data.ec2.internal``), and a non-ASCII
    host that IDNA maps onto an IP address (``１２７.０.０.１``). Private and loopback addresses are allowed. A DNS name
    that RESOLVES to a refused address is not caught here (no lookup). The host is IDNA-encoded and
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
