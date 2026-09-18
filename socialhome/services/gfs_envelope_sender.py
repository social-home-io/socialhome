"""Hand a sealed §D2b bootstrap blob to a connection server (§D2b relay leg).

:mod:`socialhome.federation.invite_bootstrap` seals an invite-redeem
request to a household nobody here has an address for, and hands the
opaque blob to a :class:`~socialhome.federation.invite_bootstrap
.RelayEnvelopeSender`. This module is the production implementation of
that seam: it POSTs the blob to a paired connection server (GFS), which
pushes it down the recipient household's existing ``/gfs/ws`` socket.

## What the connection server sees

Exactly the outer envelope ``invite_bootstrap`` minted::

    POST {gfs_base}/gfs/envelope
    {"to_instance": "<32 hex>",
     "sealed": {"kem_suite": …, "eph_pk": …, "ciphertext": …}}

Two fields, and the body is rebuilt here from ``to_instance_id`` +
``envelope["sealed"]`` rather than forwarded verbatim, so no caller can
grow a third one by accident. There is no ``from_instance`` and no
household transport signature — the same identity-free discipline
``POST /gfs/publish`` follows (#677). The relay learns a recipient, a
size and a timing; the token, the space, the users and the sender all
live inside the ciphertext.

The answer is a uniform ``202 {"status": "accepted"}`` for anything
well-formed: a household learns nothing about whether the recipient is
online, known, or exists. ``400`` / ``413`` / ``429`` mean malformed,
oversize or throttled. Every non-2xx is treated as a transport failure
— logged at INFO with the server's name, never the blob.

## Why the capability gate

An older connection server has no ``/gfs/envelope`` route, so the POST
would 404 and the redeem would surface as a generic failure. The signed
capability block on ``GET /gfs/info`` (verified against the key pinned
for that connection — :meth:`~socialhome.services.gfs_connection_service
.GfsConnectionService.envelope_relay_supported`) says whether the
server relays invite envelopes, so an un-upgraded server fails the
redeem immediately with a reason a human can act on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from ..domain.federation import GfsConnection
from ..domain.space import SpacePermissionError
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from .gfs_connection_service import GfsConnectionService

log = logging.getLogger(__name__)

#: Path the connection server exposes for opaque household-to-household
#: envelopes. Fixed by the GFS contract.
GFS_ENVELOPE_PATH: str = "/gfs/envelope"

#: Per-POST ceiling. Matches the other GFS calls in this codebase.
GFS_ENVELOPE_TIMEOUT_S: float = 10.0


class EnvelopeRelayUnavailable(SpacePermissionError):
    """No connection server here can carry this invite envelope.

    A *configuration* refusal rather than a transport blip: the
    household isn't connected to the server that issued the invite, or
    that server's build has no envelope relay. Subclasses
    :class:`~socialhome.domain.space.SpacePermissionError` so
    ``POST /api/spaces/join`` answers 422 with this text instead of
    burning the redeem's ten-second timeout on a blob nobody will carry.
    """


def _normalize_base(url: str) -> str:
    """Compare-able form of a GFS base URL — scheme + host(+port), no path.

    The invite blob's ``gfs_url`` is typed by whoever published it and
    pasted through a chat client; the local pairing row is typed by the
    admin. ``https://gfs.example.org/`` and ``https://GFS.example.org``
    are the same server, so casing, a trailing slash and a trailing path
    must not decide whether a redeem works.
    """
    parsed = urlparse(url.strip().rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        return url.strip().rstrip("/").lower()
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


class GfsEnvelopeSender:
    """:class:`RelayEnvelopeSender` over a paired connection server.

    Reuses :class:`GfsConnectionService` for both the shared
    :class:`aiohttp.ClientSession` and the verified ``/gfs/info``
    capability cache — a second HTTP client (or a second capability
    probe) would double this household's footprint on a server whose
    whole job is to learn as little as possible about it.
    """

    __slots__ = ("_gfs", "_repo")

    def __init__(
        self,
        *,
        gfs_service: GfsConnectionService,
        gfs_repo: AbstractGfsConnectionRepo,
    ) -> None:
        self._gfs = gfs_service
        self._repo = gfs_repo

    async def send_sealed_envelope(
        self,
        *,
        to_instance_id: str,
        envelope: dict[str, Any],
        gfs_url: str = "",
    ) -> bool:
        """POST one sealed blob to the relay. ``True`` when it accepted.

        ``gfs_url`` names the server the blob must travel through — the
        one that served the invite (redeemer leg) or the one the request
        arrived on (issuer leg). Empty falls back to any active
        connection that advertises the capability, which is only correct
        when the household has a single connection server.

        Raises :class:`EnvelopeRelayUnavailable` when no connection can
        carry it; a reachable relay that refuses or errors is a plain
        ``False``.
        """
        conn = await self._resolve_connection(gfs_url)
        sealed = envelope.get("sealed") if isinstance(envelope, dict) else None
        if not isinstance(sealed, dict):
            log.warning("gfs.envelope: refusing to relay an envelope with no seal")
            return False
        # IDENTITY-FREE BODY — rebuilt, not forwarded: the relay gets the
        # recipient and the ciphertext, and there is no third field for a
        # future caller to slip an identity into.
        body = {"to_instance": to_instance_id, "sealed": sealed}
        url = f"{conn.inbox_url.rstrip('/')}{GFS_ENVELOPE_PATH}"
        try:
            async with self._gfs.client().post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=GFS_ENVELOPE_TIMEOUT_S),
            ) as resp:
                if resp.status < 300:
                    return True
                # Never log the body or the blob — a relay's rejection is
                # about shape and budget, and the payload is somebody's
                # invite. The server's name is what an operator needs.
                log.info(
                    "gfs.envelope: %r (%s) rejected an invite envelope — HTTP %d",
                    conn.display_name,
                    conn.inbox_url,
                    resp.status,
                )
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            log.info(
                "gfs.envelope: could not reach %r (%s): %s",
                conn.display_name,
                conn.inbox_url,
                exc,
            )
            return False

    async def _resolve_connection(self, gfs_url: str) -> GfsConnection:
        """The active connection that will carry the blob.

        Fail-closed with a named reason: a redeem that cannot leave is
        worth a sentence the user can act on ("connect to that server",
        "ask its operator to upgrade"), not a timeout.
        """
        conns = [c for c in await self._repo.list_active() if c.status == "active"]
        if gfs_url:
            want = _normalize_base(gfs_url)
            conns = [c for c in conns if _normalize_base(c.inbox_url) == want]
            if not conns:
                raise EnvelopeRelayUnavailable(
                    "this invite travels through a connection server your "
                    "home isn't connected to — add it under Settings → "
                    "Connections, then try the link again",
                )
        if not conns:
            raise EnvelopeRelayUnavailable(
                "joining a space from a link needs a connection server — "
                "your home isn't connected to one",
            )
        for conn in conns:
            if await self._gfs.envelope_relay_supported(conn):
                return conn
        raise EnvelopeRelayUnavailable(
            "this connection server can't relay invites yet — ask whoever "
            "runs it to update it",
        )


__all__ = [
    "EnvelopeRelayUnavailable",
    "GFS_ENVELOPE_PATH",
    "GFS_ENVELOPE_TIMEOUT_S",
    "GfsEnvelopeSender",
]
