"""Validation for URLs another household (or a connection server) gave us.

A federation inbox URL arrives from outside — a scanned pairing QR, a
``PAIRING_PEER_ACCEPT`` body, a trust-relay intro, a ``URL_UPDATED``
envelope, a connection-server pairing code — and this household later
POSTs to it. Every such URL goes through :func:`validate_peer_url` where
it enters, before it is stored or called.

Two policies share one set of structural rules:

* **Household inbox URLs** — ``http://`` or ``https://``, a host, no
  credentials, no whitespace / control characters. Plain ``http://`` stays
  allowed on any host: households legitimately pair across a LAN by
  hostname (``homeassistant.local``) or address, the federation demo
  harness pairs over ``http://127.0.0.1:<port>``, and the admin's own
  external-URL setting accepts ``http://``. Envelope content is protected
  by the pairing keys, not by the transport.
* **Connection-server URLs** (``require_tls_unless_private=True``) — the
  same rules plus ``https://`` unless the host is loopback / LAN-private.
  ``GET /gfs/info`` delivers the key the household pins on first contact,
  so a public plain-http connection server has nothing trustworthy to pin.

The connection server applies the household policy to the ``inbox_url`` a
household registers with, since its fan-out POSTs there too.

This module sits at the package top level (beside
:mod:`socialhome.capabilities_sig`, same discipline) and depends only on the
standard library, so BOTH the household and the connection-server process
can import it without dragging in each other's stack.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: Schemes a household ever talks to another household over.
ALLOWED_PEER_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Upper bound on an accepted URL. Real inbox URLs are well under 200
#: characters; anything longer is not a household address.
MAX_PEER_URL_LENGTH = 2048


class InvalidPeerUrlError(ValueError):
    """A URL from outside is not a usable household address.

    ``str(exc)`` is safe to show an admin: it names the field and the rule
    that failed, never the URL itself.
    """

    __slots__ = ("field",)

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


def is_private_host(host: str) -> bool:
    """Whether *host* is loopback / link-local / RFC1918-private.

    A literal IP is classified by :mod:`ipaddress`; the bare name
    ``localhost`` counts as loopback. Everything else — every DNS name — is
    treated as public. DNS is deliberately NOT resolved: a resolver answer is
    attacker-influenced and would turn a TLS check into a rebinding oracle.
    """
    name = host.strip("[]").lower()
    if name in {"localhost", "localhost."}:
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def _has_unsafe_chars(url: str) -> bool:
    return any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url)


def validate_peer_url(
    url: object,
    *,
    field: str,
    require_tls_unless_private: bool = False,
) -> str:
    """Return *url* unchanged if it is a usable household address.

    Raises :class:`InvalidPeerUrlError` (a :class:`ValueError`) otherwise.
    See the module docstring for the two policies.
    """
    if not isinstance(url, str) or not url:
        raise InvalidPeerUrlError(
            f"{field} is not a valid household address: it is empty or not text",
            field=field,
        )
    if len(url) > MAX_PEER_URL_LENGTH:
        raise InvalidPeerUrlError(
            f"{field} is not a valid household address: it is too long",
            field=field,
        )
    if _has_unsafe_chars(url):
        raise InvalidPeerUrlError(
            f"{field} is not a valid household address: it contains"
            " whitespace or control characters",
            field=field,
        )
    try:
        parts = urlsplit(url)
    except ValueError as exc:  # e.g. an unterminated ``[`` IPv6 literal
        raise InvalidPeerUrlError(
            f"{field} is not a valid household address: it cannot be parsed",
            field=field,
        ) from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_PEER_URL_SCHEMES:
        raise InvalidPeerUrlError(
            f"{field} is not a valid household address: it must be an"
            " http:// or https:// URL",
            field=field,
        )
    if "@" in parts.netloc:
        raise InvalidPeerUrlError(
            f"{field} is not a valid household address: it must not carry credentials",
            field=field,
        )
    host = parts.hostname or ""
    if not host:
        raise InvalidPeerUrlError(
            f"{field} is not a valid household address: it has no host",
            field=field,
        )
    try:
        parts.port
    except ValueError as exc:
        raise InvalidPeerUrlError(
            f"{field} is not a valid household address: it has an invalid port",
            field=field,
        ) from exc
    if require_tls_unless_private and scheme != "https" and not is_private_host(host):
        raise InvalidPeerUrlError(
            f"{field} must use https:// — plain http:// is only allowed on"
            " loopback or a private network (RFC1918, fc00::/7, fe80::/10,"
            " localhost)",
            field=field,
        )
    return url


__all__ = [
    "ALLOWED_PEER_URL_SCHEMES",
    "MAX_PEER_URL_LENGTH",
    "InvalidPeerUrlError",
    "is_private_host",
    "validate_peer_url",
]
