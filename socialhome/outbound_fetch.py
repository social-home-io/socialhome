"""Fetch a URL a *user* typed — without letting it reach the household's LAN.

Every other outbound request this household makes goes to an address it
chose (Home Assistant, a paired peer, a connection server, the map tile
server). A link preview is different: the URL comes from whatever a member
pasted into a post, and the request leaves from inside the household's
network. Without a guard, ``http://192.168.1.1/`` or
``http://homeassistant.local:8123/api/`` would turn the preview feature into
a way to read the router's admin page (SSRF).

:class:`OutboundFetcher` refuses anything that is not a plain public web
page, and checks it at the moment the socket opens, not only when the URL
was parsed:

* **Scheme / shape.** ``http`` and ``https`` only (never ``file:``,
  ``data:``, ``ftp:``, …); a host is required; user-info
  (``user:pass@host``) is refused; the port must be in
  :data:`ALLOWED_PORTS` (80 / 443).
* **Address.** The host is resolved *here*, and every address it resolves
  to must be globally routable (:func:`is_public_address`) — loopback,
  RFC 1918, link-local, CGNAT (100.64/10), multicast, unspecified,
  reserved, IPv6 ULA / link-local / site-local, and IPv4 embedded in IPv6
  (``::ffff:10.0.0.1``, 6to4, Teredo) are all refused. A host written as a
  bare number (``2130706433``, ``0x7f.1``, ``0177.0.0.1``) is parsed the way
  the C resolver would parse it and vetted as the address it names.
* **Pinning.** The connection is made to the vetted address through a
  resolver that knows only that answer (:class:`_PinnedResolver`), so a DNS
  server that answers "public" to the check and "private" to the connect
  (DNS rebinding) never gets the second question. TLS still verifies the
  certificate against the host name.
* **Redirects** are followed by hand, at most :data:`MAX_REDIRECTS`, and
  every hop is put through the same scheme / port / resolve / vet / pin
  steps as the first.
* **Budget.** One wall-clock deadline covers every hop, DNS included
  (:data:`TOTAL_TIMEOUT_S`); connect and each read have their own shorter
  timeouts, so a server that drips one byte a second (slow-loris) is cut
  off. The body is read in chunks and never past ``max_bytes``.
* **Anonymous.** No cookie jar, no ``Authorization``, no proxy from the
  environment, and a generic ``User-Agent`` — the request says nothing
  about the household.

Failures raise :class:`OutboundFetchRefused` with a short machine-readable
``reason``; callers treat every refusal the same way (no preview). Logs
carry the host name only — never the path or query, which may hold tokens.

Top level beside :mod:`socialhome.peer_http` (same discipline: standard
library + aiohttp only).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

log = logging.getLogger(__name__)

#: Only the default web ports. A preview of ``host:8123`` is how an SSRF
#: probe of a LAN service looks; a real public page lives on 80 / 443.
ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})

#: Schemes a user link may be fetched over.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Redirect hops followed after the first request.
MAX_REDIRECTS: int = 3

#: Wall-clock budget for one :meth:`OutboundFetcher.fetch`, every hop and
#: DNS lookup included.
TOTAL_TIMEOUT_S: float = 5.0

#: Budget for opening one TCP (+ TLS) connection.
CONNECT_TIMEOUT_S: float = 3.0

#: Longest gap allowed between two reads of the response.
READ_TIMEOUT_S: float = 3.0

#: Size of one body read.
CHUNK_BYTES: int = 16 * 1024

#: A User-Agent that names the software class, not the household.
USER_AGENT: str = "Mozilla/5.0 (compatible; SocialHomeLinkPreview/1.0)"

_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

#: Resolves ``(host, port)`` to the IP address strings it names.
type HostResolver = Callable[[str, int], Awaitable[list[str]]]


class OutboundFetchRefused(Exception):
    """The URL was not fetched (or its answer not used).

    ``reason`` is a short token (``"scheme"``, ``"private_address"``,
    ``"too_large"``, ``"timeout"``, …) for logs and tests.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(slots=True, frozen=True)
class FetchResult:
    """One successful fetch."""

    #: The URL that answered (after any redirects).
    url: str
    #: Lower-cased media type without parameters (``text/html``).
    content_type: str
    #: The ``charset`` parameter of ``Content-Type``, if any.
    charset: str | None
    body: bytes
    #: ``True`` when the body was cut at ``max_bytes`` (``truncate=True``).
    truncated: bool


def _host_for_log(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries, if it is one of the
    transition forms that route to it (mapped, 6to4, Teredo client)."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    return None


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """``True`` only for a globally routable unicast address.

    ``is_global`` already excludes private, loopback, link-local, CGNAT,
    reserved, documentation and ULA ranges; multicast and unspecified are
    excluded explicitly (some multicast scopes count as global). An IPv6
    address that embeds an IPv4 one is judged by the embedded address too.
    """
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local:
        return False
    if ip.is_private or ip.is_reserved or not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_site_local:
            return False
        inner = _embedded_ipv4(ip)
        if inner is not None and not is_public_address(inner):
            return False
    return True


def parse_ip_literal(
    host: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address *host* names when it is written as an address.

    Besides the canonical forms, accepts the legacy IPv4 spellings the C
    resolver (``inet_aton``) accepts — ``2130706433``, ``0x7f000001``,
    ``0177.0.0.1``, ``127.1`` — so they are vetted as the address they
    really are rather than handed to DNS. ``None`` for a host name.
    """
    bare = host.strip("[]")
    try:
        return ipaddress.ip_address(bare)
    except ValueError:
        pass
    # ``inet_aton`` only ever accepts digits, dots and hex letters/x.
    if bare and all(c in "0123456789abcdefxABCDEFX." for c in bare):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(bare))
        except OSError:
            return None
    return None


async def system_resolve(host: str, port: int) -> list[str]:
    """Resolve *host* through the operating system's resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return [str(info[4][0]) for info in infos]


class _PinnedResolver(AbstractResolver):
    """Answers only the one host it was built for, with the addresses that
    were already vetted — the connection never asks DNS again."""

    def __init__(self, host: str, addresses: list[str]) -> None:
        self._host = host.lower()
        self._addresses = addresses

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host.lower() != self._host:
            raise OSError(f"unpinned host {host!r}")
        out: list[ResolveResult] = []
        for addr in self._addresses:
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
            out.append(
                ResolveResult(
                    hostname=host,
                    host=addr,
                    port=port,
                    family=fam,
                    proto=0,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        return out

    async def close(self) -> None:
        return None


@dataclass(slots=True, frozen=True)
class _Target:
    url: str
    host: str
    port: int
    addresses: list[str]


def check_url_shape(url: str) -> tuple[str, str, int]:
    """Validate scheme / host / user-info / port; return ``(scheme, host, port)``.

    Raises :class:`OutboundFetchRefused` on any violation.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise OutboundFetchRefused("malformed")
    if any(c in url for c in "\r\n\t\x00 "):
        raise OutboundFetchRefused("malformed")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise OutboundFetchRefused("malformed") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise OutboundFetchRefused("scheme", scheme)
    host = parts.hostname or ""
    if not host:
        raise OutboundFetchRefused("malformed", "no host")
    if parts.username is not None or parts.password is not None:
        raise OutboundFetchRefused("userinfo")
    effective = port if port is not None else (443 if scheme == "https" else 80)
    if effective not in ALLOWED_PORTS:
        raise OutboundFetchRefused("port", str(effective))
    return scheme, host, effective


class OutboundFetcher:
    """Guarded GET for user-supplied URLs (see the module docstring)."""

    __slots__ = ("_resolve", "_total_timeout", "_ssl")

    def __init__(
        self,
        *,
        resolver: HostResolver = system_resolve,
        total_timeout_s: float = TOTAL_TIMEOUT_S,
        ssl: Any = True,
    ) -> None:
        self._resolve = resolver
        self._total_timeout = total_timeout_s
        self._ssl = ssl

    async def _vet(self, url: str) -> _Target:
        _scheme, host, port = check_url_shape(url)
        literal = parse_ip_literal(host)
        if literal is not None:
            if not is_public_address(literal):
                raise OutboundFetchRefused("private_address", host)
            return _Target(url=url, host=host, port=port, addresses=[str(literal)])
        try:
            raw = await self._resolve(host, port)
        except OSError as exc:
            raise OutboundFetchRefused("dns", host) from exc
        if not raw:
            raise OutboundFetchRefused("dns", host)
        addresses: list[str] = []
        for value in raw:
            try:
                ip = ipaddress.ip_address(value.split("%", 1)[0])
            except ValueError as exc:
                raise OutboundFetchRefused("dns", host) from exc
            # One private answer is enough to refuse: a name that resolves
            # to both a public and a LAN address is the rebinding setup.
            if not is_public_address(ip):
                raise OutboundFetchRefused("private_address", host)
            addresses.append(str(ip))
        return _Target(url=url, host=host, port=port, addresses=addresses)

    async def fetch(
        self,
        url: str,
        *,
        accept: frozenset[str],
        max_bytes: int,
        truncate: bool = False,
    ) -> FetchResult:
        """GET *url* under every rule in the module docstring.

        ``accept`` lists the media types the answer may carry (anything
        else is refused). A body longer than ``max_bytes`` is refused, or —
        with ``truncate=True`` (HTML, whose ``<head>`` comes first) — cut
        at ``max_bytes`` and returned.
        """
        try:
            async with asyncio.timeout(self._total_timeout):
                return await self._fetch_hops(
                    url, accept=accept, max_bytes=max_bytes, truncate=truncate
                )
        except TimeoutError as exc:
            log.info("link fetch timed out (host=%s)", _host_for_log(url))
            raise OutboundFetchRefused("timeout") from exc

    async def _fetch_hops(
        self,
        url: str,
        *,
        accept: frozenset[str],
        max_bytes: int,
        truncate: bool,
    ) -> FetchResult:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            target = await self._vet(current)
            outcome = await self._get_once(
                target, accept=accept, max_bytes=max_bytes, truncate=truncate
            )
            if isinstance(outcome, FetchResult):
                return outcome
            # A redirect: resolve against the current URL; re-vetted on the
            # next pass exactly like the first URL.
            current = urljoin(current, outcome)
        raise OutboundFetchRefused("too_many_redirects")

    async def _get_once(
        self,
        target: _Target,
        *,
        accept: frozenset[str],
        max_bytes: int,
        truncate: bool,
    ) -> FetchResult | str:
        """One request. Returns the result, or the ``Location`` to follow."""
        connector = aiohttp.TCPConnector(
            resolver=_PinnedResolver(target.host, target.addresses),
            use_dns_cache=False,
            force_close=True,
            ssl=self._ssl,
        )
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=CONNECT_TIMEOUT_S,
            sock_connect=CONNECT_TIMEOUT_S,
            sock_read=READ_TIMEOUT_S,
        )
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": ", ".join(sorted(accept)) + ", */*;q=0.1",
            "Accept-Language": "en",
        }
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                headers=headers,
            ) as session:
                async with session.get(
                    target.url,
                    allow_redirects=False,
                    max_redirects=0,
                ) as resp:
                    if resp.status in _REDIRECT_STATUSES:
                        location = resp.headers.get("Location")
                        if not location:
                            raise OutboundFetchRefused("bad_redirect")
                        return location
                    if resp.status != 200:
                        raise OutboundFetchRefused("status", str(resp.status))
                    ctype = (resp.content_type or "").lower()
                    if ctype not in accept:
                        raise OutboundFetchRefused("content_type", ctype)
                    declared = resp.content_length
                    if declared is not None and declared > max_bytes and not truncate:
                        raise OutboundFetchRefused("too_large")
                    body, cut = await _read_capped(
                        resp.content, max_bytes=max_bytes, truncate=truncate
                    )
                    return FetchResult(
                        url=str(resp.url),
                        content_type=ctype,
                        charset=resp.charset,
                        body=body,
                        truncated=cut,
                    )
        except OutboundFetchRefused:
            raise
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            log.info(
                "link fetch failed (host=%s): %s",
                target.host,
                type(exc).__name__,
            )
            raise OutboundFetchRefused("network", type(exc).__name__) from exc


async def _read_capped(
    stream: aiohttp.StreamReader, *, max_bytes: int, truncate: bool
) -> tuple[bytes, bool]:
    """Read *stream* up to ``max_bytes``; never buffer more than one chunk
    past the cap."""
    buf = bytearray()
    while True:
        chunk = await stream.read(CHUNK_BYTES)
        if not chunk:
            return bytes(buf), False
        buf.extend(chunk)
        if len(buf) > max_bytes:
            if truncate:
                return bytes(buf[:max_bytes]), True
            raise OutboundFetchRefused("too_large")
