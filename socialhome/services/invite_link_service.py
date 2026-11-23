"""Turn a GFS invite link into its invite code — ``POST /api/spaces/invite-link``.

The QR on a GFS invite page carries the short web link to that page,
``{gfs}/join/{token}`` (``global_server.invites.invite_link_url``), not the
~900-character ``socialhome://invite#…`` code: that code made a QR no phone
could read off a screen. A phone's camera opens the link in the browser; the
app's scanner and paste field hand it here, and this household asks the GFS
for the code behind it (``POST {gfs}/gfs/pages/invite {token}`` — the same
call the pinned viewer makes) so the SPA can carry on with the normal join.

The household fetches, never the browser: the SPA talks only to its own
household ("no third-party fetches"). The link is something a person typed
or scanned, so the request is guarded like any user-supplied URL:

* **Shape.** ``http(s)://host[:port][/base]/join/<token>`` — no user-info,
  query or fragment; a URL-safe token of at most
  :data:`MAX_INVITE_TOKEN_CHARS`. Host and port come from the link.
* **Who it may reach.** A GFS this household has an ACTIVE connection to
  (same scheme, host, port and path as the stored base URL) is asked at
  that stored URL over the household's own anonymous GFS session — the
  address was chosen by an admin, so a LAN or plain-http GFS works.
  Anything else must be ``https://`` and is asked over
  :func:`~socialhome.outbound_fetch.open_pinned_session`: the host is
  resolved here and refused unless every address is public (no loopback,
  private, link-local, CGNAT …), and the connection is pinned to the vetted
  address, so DNS rebinding can't swap it. Any port on a public host is
  allowed (a GFS may listen on its own), as for the v_57 listing targets.
* **Budget.** No redirects followed, one :data:`RESOLVE_TIMEOUT_S` deadline
  for the whole exchange, an answer of at most :data:`MAX_ANSWER_BYTES`.
* **Answer.** Only an ``invite_code`` that is a ``socialhome://invite#…``
  string of at most :data:`MAX_INVITE_CODE_CHARS` is passed on; the SPA
  decodes it exactly like a pasted code.

**Privacy.** The link's host sees this household's IP and the token once,
in an anonymous request (no cookie, no household id, the neutral
``User-Agent: SocialHome``). For a genuine GFS that is no more than a
redeem through it shows; a crafted link can make the household reveal its
IP to any public host the person scans — the same a pasted code naming
that host as its GFS already does. Logs here name the host only, never
the token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp
import orjson

from ..domain.errors import CodedError, NotFoundError
from ..domain.federation import GfsConnection
from ..gfs_proof import normalize_gfs_base_url
from ..outbound_fetch import (
    PINNED_SESSION_USER_AGENT,
    OutboundFetchRefused,
    open_pinned_session,
    read_capped,
)
from ..pinned_viewer import viewer_safe_id

if TYPE_CHECKING:
    from .gfs_connection_service import GfsConnectionService

log = logging.getLogger(__name__)

#: Longest link accepted.
MAX_LINK_CHARS: int = 2048

#: Longest invite token (``global_server.public_viewer.MAX_INVITE_TOKEN_CHARS``).
MAX_INVITE_TOKEN_CHARS: int = 256

#: One deadline for the whole exchange: vetting, connect, answer.
RESOLVE_TIMEOUT_S: float = 8.0

#: The answer carries the code (blob ≤ 4 KiB), the space icon (≤ 16 KiB as a
#: ``data:`` URI) and a QR PNG: 64 KiB is plenty, and bounds a hostile host.
MAX_ANSWER_BYTES: int = 64 * 1024

#: Longest invite code passed on (the viewer's own bound, ``public_pages.tsx``).
MAX_INVITE_CODE_CHARS: int = 8192

INVITE_CODE_PREFIX: str = "socialhome://invite#"

#: :class:`OutboundFetchRefused` reasons that mean "not reachable" rather
#: than "this address is not allowed".
_UNREACHABLE_REASONS = frozenset({"dns", "timeout", "network", "too_large"})


class InviteLinkRefusedError(CodedError):
    """The link is not one this household will follow."""

    status = 422
    code = "INVITE_LINK_REFUSED"
    detail = "This link can't be used to join a space."


class InviteLinkUnreachableError(CodedError):
    """The link's server did not answer with an invite code."""

    status = 502
    code = "INVITE_LINK_UNREACHABLE"
    detail = "Couldn't reach the server of this invite link."


class InviteLinkExpiredError(NotFoundError):
    """The GFS has no live invite behind this link (expired, revoked,
    unlisted — it gives one answer for all)."""

    code = "INVITE_LINK_EXPIRED"
    detail = "This invite has expired or was revoked."


def parse_invite_link(link: object) -> tuple[str, str]:
    """``(gfs_base, token)`` of a ``…/join/<token>`` link, or
    :class:`InviteLinkRefusedError`."""
    if (
        not isinstance(link, str)
        or not link
        or len(link) > MAX_LINK_CHARS
        or any(c.isspace() or ord(c) < 0x20 or c == "\\" for c in link)
    ):
        raise InviteLinkRefusedError()
    try:
        parts = urlsplit(link)
        _ = parts.port  # raises on a malformed port
    except ValueError as exc:
        raise InviteLinkRefusedError() from exc
    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in link
        or "#" in link
    ):
        raise InviteLinkRefusedError()
    head, sep, token = parts.path.rpartition("/join/")
    if not sep or not viewer_safe_id(token, MAX_INVITE_TOKEN_CHARS):
        raise InviteLinkRefusedError()
    return f"{parts.scheme.lower()}://{parts.netloc}{head}".rstrip("/"), token


class InviteLinkService:
    """Resolves a GFS invite link to its ``socialhome://invite#…`` code."""

    __slots__ = ("_gfs", "_open_session")

    def __init__(
        self,
        gfs: GfsConnectionService,
        *,
        open_session: Callable[[str], Awaitable[aiohttp.ClientSession]] = (
            open_pinned_session
        ),
    ) -> None:
        self._gfs = gfs
        #: Opens the address-pinned session for a GFS we are not connected
        #: to (:func:`open_pinned_session`); a seam for tests, which can't
        #: reach a loopback server through it.
        self._open_session = open_session

    async def resolve(self, link: object) -> str:
        """The invite code behind ``link`` (see the module docstring)."""
        base, token = parse_invite_link(link)
        host = urlsplit(base).hostname or "?"
        try:
            async with asyncio.timeout(RESOLVE_TIMEOUT_S):
                return await self._resolve(base, token)
        except OutboundFetchRefused as exc:
            log.warning("invite link to %s refused (%s)", host, exc.reason)
            if exc.reason in _UNREACHABLE_REASONS:
                raise InviteLinkUnreachableError() from exc
            raise InviteLinkRefusedError() from exc
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
            log.warning("invite link to %s: no answer (%s)", host, type(exc).__name__)
            raise InviteLinkUnreachableError() from exc

    async def _resolve(self, base: str, token: str) -> str:
        conn = await self._connection_for(base)
        if conn is not None:
            async with self._gfs.anon_session(conn) as session:
                if session is None:
                    raise InviteLinkUnreachableError()
                # The stored address, never the user's spelling of it.
                return await self._ask(session, conn.inbox_url.rstrip("/"), token)
        if urlsplit(base).scheme != "https":
            raise OutboundFetchRefused("scheme", "http")
        session = await self._open_session(base)
        try:
            return await self._ask(session, base, token)
        finally:
            await session.close()

    async def _connection_for(self, base: str) -> GfsConnection | None:
        """This household's ACTIVE GFS connection at ``base`` (same scheme,
        host, port and path — :func:`normalize_gfs_base_url`), if any. A
        pending or suspended one (e.g. it failed its key proof) gets no
        exemption: it takes the public-only pinned path like any link."""
        want = normalize_gfs_base_url(base)
        if not want:
            return None
        for conn in await self._gfs.list_connections():
            if (
                conn.status == "active"
                and normalize_gfs_base_url(conn.inbox_url) == want
            ):
                return conn
        return None

    @staticmethod
    async def _ask(session: aiohttp.ClientSession, base: str, token: str) -> str:
        async with session.post(
            f"{base}/gfs/pages/invite",
            data=orjson.dumps({"token": token}),
            headers={
                "Content-Type": "application/json",
                # Neutral on every path (the shared GFS session's default
                # names the aiohttp version), and no compression, so the
                # byte cap counts what is actually received.
                "User-Agent": PINNED_SESSION_USER_AGENT,
                "Accept-Encoding": "identity",
            },
            allow_redirects=False,
        ) as resp:
            if resp.status == 404:
                raise InviteLinkExpiredError()
            if resp.status != 200:
                raise OutboundFetchRefused("network", f"status {resp.status}")
            declared = resp.content_length
            if declared is not None and declared > MAX_ANSWER_BYTES:
                raise OutboundFetchRefused("too_large")
            raw, _cut = await read_capped(
                resp.content, max_bytes=MAX_ANSWER_BYTES, truncate=False
            )
        body = orjson.loads(raw)
        code = body.get("invite_code") if isinstance(body, dict) else None
        if (
            not isinstance(code, str)
            or not code.startswith(INVITE_CODE_PREFIX)
            or len(code) > MAX_INVITE_CODE_CHARS
        ):
            raise OutboundFetchRefused("network", "no invite code")
        return code
