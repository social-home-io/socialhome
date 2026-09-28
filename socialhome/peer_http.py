"""POST to a URL another household gave us — without following it elsewhere.

Every federation inbox URL we POST to came from outside (a pairing QR, a
``PAIRING_PEER_ACCEPT`` body, a ``URL_UPDATED`` envelope, a household
registering with a connection server) and passed
:func:`~socialhome.peer_url.validate_peer_url` when it arrived. aiohttp's
default ``allow_redirects=True`` would undo that: a ``3xx`` from the peer
(or anything answering at its address) re-sends the request to any
``Location`` at all — another host, a downgraded scheme, a loopback
service — with no validation, up to ten times.

:func:`post_to_peer` turns automatic following off and allows at most
**one** hop, and only one that stays on the address the URL already named:

* same host (case-insensitive) — a trailing-slash or path redirect;
* same scheme and port, **or** an ``http`` → ``https`` upgrade onto the
  same port or the default ``443``; a downgrade is refused;
* the target itself passes :func:`validate_peer_url`;
* status 301 / 302 / 307 / 308 — the request is re-sent unchanged. A 303
  ("see other", i.e. GET it) is not a place to re-send an envelope.

Anything else is handed back to the caller as the ``3xx`` response it is,
which every caller already treats as a failed delivery; the refusal is
logged at WARNING with **host names only** — inbox paths and query strings
carry inbox ids / tokens and never reach the log.

Top level beside :mod:`socialhome.peer_url` (same discipline): standard
library + aiohttp only, so the connection-server process can use it for its
own fan-out to household inboxes without importing the household's
federation stack.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlsplit

from .peer_url import InvalidPeerUrlError, validate_peer_url

log = logging.getLogger(__name__)

#: Redirects that keep the method and body — the only ones worth one hop.
FOLLOWABLE_REDIRECTS: frozenset[int] = frozenset({301, 302, 307, 308})


def _host(url: str) -> str:
    """Host name of *url* for logging — never the path or query."""
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return {"http": 80, "https": 443}.get(scheme)


def safe_redirect_target(origin_url: str, location: str | None) -> str | None:
    """The absolute URL a redirect from *origin_url* to *location* may be
    followed to, or ``None`` when it must not be (see the module docstring).
    """
    if not location:
        return None
    try:
        target = urljoin(origin_url, location)
        validate_peer_url(target, field="redirect target")
        origin, dest = urlsplit(origin_url), urlsplit(target)
        origin_port, dest_port = origin.port, dest.port
    except InvalidPeerUrlError, ValueError:
        return None
    if target == origin_url:
        return None
    if (origin.hostname or "").lower() != (dest.hostname or "").lower():
        return None
    o_scheme, d_scheme = origin.scheme.lower(), dest.scheme.lower()
    o_port = _effective_port(o_scheme, origin_port)
    d_port = _effective_port(d_scheme, dest_port)
    if o_scheme == d_scheme:
        return target if o_port == d_port else None
    if o_scheme == "http" and d_scheme == "https":
        # Upgrade: the same port (a TLS listener on it) or the default 443.
        return target if d_port in (o_port, 443) else None
    return None


@asynccontextmanager
async def post_to_peer(client: Any, url: str, **kwargs: Any) -> AsyncIterator[Any]:
    """``client.post(url, **kwargs)`` with the redirect policy above.

    Yields the final response — the original one unless a single safe hop
    was followed. ``allow_redirects`` is always forced off, on both hops.
    """
    kwargs["allow_redirects"] = False
    target: str | None = None
    async with client.post(url, **kwargs) as resp:
        if 300 <= resp.status < 400:
            if resp.status in FOLLOWABLE_REDIRECTS:
                target = safe_redirect_target(url, resp.headers.get("Location"))
            if target is None:
                log.warning(
                    "peer POST to %s: refusing HTTP %d redirect to %s — "
                    "only one same-host hop (or an http→https upgrade) is "
                    "followed",
                    _host(url),
                    resp.status,
                    _host(urljoin(url, resp.headers.get("Location") or "")),
                )
                yield resp
                return
        else:
            yield resp
            return
    async with client.post(target, **kwargs) as resp:
        if 300 <= resp.status < 400:
            log.warning(
                "peer POST to %s: refusing a second redirect (HTTP %d) — "
                "at most one hop is followed",
                _host(target),
                resp.status,
            )
        yield resp


__all__ = [
    "FOLLOWABLE_REDIRECTS",
    "post_to_peer",
    "safe_redirect_target",
]
