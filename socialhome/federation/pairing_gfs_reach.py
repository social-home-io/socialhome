"""GFS reach for the §11 QR pairing — pair through a connection server.

A classic pairing code carries the code owner's inbox URL, and the scanner
answers at it. A household with no reachable URL (or an admin who wants a
fallback) can instead issue a code whose **reach** names ONE connection
server (GFS) the code owner is itself registered with:

* ``url`` — the classic code. No GFS information anywhere.
* ``url_gfs`` — the inbox URL plus the bootstrap GFS as a relay fallback.
* ``gfs`` — no inbox URL at all; the pairing travels through that server.

For ``url_gfs`` / ``gfs`` the code also carries the owner's static key-wrap
key (bound to its identity key by a self-signature, suite-tagged), so the
scanner can seal a peer-accept to it, and ``gfs: {url, instance_id}`` —
the bootstrap server's base URL and the ``gfs_instance_id`` the owner
pinned for it. That is the one server revealed, and only to whoever holds
the code (an in-person, out-of-band exchange).

Both sides then opt the pair into the relay fallback (``gfs_relay``), store
the other's key-wrap key, and SEED one relay route: their own connection to
the bootstrap server. Seeding is safe here, unlike from a received probe
(``docs/protocol/gfs-relay.md``): the server was named in the out-of-band
code and we are ourselves connected to it — a relaying GFS cannot plant it.
Route discovery refines the routes once the pair is confirmed; the seeded
route expires like any other one when no ack refreshes it.

This module owns the GFS-facing pieces the
:class:`~socialhome.federation.pairing_coordinator.PairingCoordinator`
needs: choosing the bootstrap connection, matching a scanned code's server
against our own connections, our own key-wrap fields, verifying the peer's,
and handing a sealed pairing body to a server.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..domain.federation import (
    PAIRING_REACH_URL,
    SUPPORTED_PAIRING_REACHES,
    GfsConnection,
    GfsNotConnectedError,
    PairingKeywrapInvalidError,
    PairingReachInvalidError,
)
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..services.gfs_envelope_sender import normalize_gfs_base
from .invite_bootstrap import RelayEnvelopeSender, verify_peer_keywrap
from .keywrap_seal import KEM_SUITE_X25519, SUPPORTED_KEM_SUITES

log = logging.getLogger(__name__)


def parse_reach(raw: object) -> str:
    """The reach a code (or an initiate request) asks for.

    Missing / empty means the classic ``url`` code — every code minted
    before reach existed. Anything else must be a known reach: an unknown
    value is refused, never defaulted (a newer reach an older build does
    not understand must not pair as something it is not).
    """
    if raw is None or raw == "":
        return PAIRING_REACH_URL
    if not isinstance(raw, str) or raw not in SUPPORTED_PAIRING_REACHES:
        raise PairingReachInvalidError()
    return raw


def gfs_block(conn: GfsConnection) -> dict[str, str]:
    """The ``gfs`` field of a pairing code: the bootstrap server's base URL
    and the ``gfs_instance_id`` we pinned for it — nothing else."""
    return {"url": conn.inbox_url, "instance_id": conn.gfs_instance_id}


def verified_peer_keywrap(
    fields: dict[str, Any],
    *,
    instance_id: str,
    identity_pk: str,
) -> str:
    """The peer's key-wrap public key (hex) from a code or a peer-accept.

    Fail closed: the suite must be one this build knows (no default on a
    missing tag — these fields are new, every sender ships the tag), and
    the key must be self-signed by the identity key that derives
    ``instance_id``. Raises :class:`PairingKeywrapInvalidError`.
    """
    suite = fields.get("keywrap_suite")
    if suite not in SUPPORTED_KEM_SUITES:
        raise PairingKeywrapInvalidError()
    keywrap_pk = fields.get("keywrap_pk")
    keywrap_sig = fields.get("keywrap_sig")
    if not isinstance(keywrap_pk, str) or not isinstance(keywrap_sig, str):
        raise PairingKeywrapInvalidError()
    if (
        verify_peer_keywrap(
            instance_id=instance_id,
            identity_pk=identity_pk,
            keywrap_pk=keywrap_pk,
            keywrap_sig=keywrap_sig,
        )
        is None
    ):
        raise PairingKeywrapInvalidError()
    return keywrap_pk


class PairingGfsReach:
    """GFS-facing helper for pairing codes with a ``url_gfs`` / ``gfs`` reach."""

    __slots__ = (
        "_gfs_repo",
        "_relay_supported",
        "_relay_sender",
        "_keywrap_public_key",
        "_keywrap_sig",
        "_probe_peer",
    )

    def __init__(
        self,
        *,
        gfs_connection_repo: AbstractGfsConnectionRepo,
        envelope_relay_supported: Callable[[GfsConnection], Awaitable[bool]],
        relay_sender: RelayEnvelopeSender,
        keywrap_public_key: bytes,
        keywrap_sig: str,
        probe_peer: Callable[[str], Awaitable[int]] | None = None,
    ) -> None:
        self._gfs_repo = gfs_connection_repo
        self._relay_supported = envelope_relay_supported
        self._relay_sender = relay_sender
        self._keywrap_public_key = keywrap_public_key
        self._keywrap_sig = keywrap_sig
        self._probe_peer = probe_peer

    def own_keywrap_fields(self) -> dict[str, str]:
        """Our static key-wrap key, its binding signature and suite tag —
        the same material ``/gfs/info`` serves; never a fresh key."""
        return {
            "keywrap_pk": self._keywrap_public_key.hex(),
            "keywrap_sig": self._keywrap_sig,
            "keywrap_suite": KEM_SUITE_X25519,
        }

    async def _relay_connections(self) -> list[GfsConnection]:
        """Our active connections whose server proved ``envelope_relay``."""
        out: list[GfsConnection] = []
        for conn in await self._gfs_repo.list_active():
            if conn.status != "active" or not conn.inbox_url:
                continue
            try:
                if await self._relay_supported(conn):
                    out.append(conn)
            except Exception:  # noqa: BLE001 — one bad server must not decide
                log.warning(
                    "pairing reach: capability check failed for connection %s",
                    conn.id,
                    exc_info=True,
                )
        return out

    async def bootstrap_connection(self, gfs_id: str | None) -> GfsConnection:
        """The connection a new code names: ``gfs_id`` when it is one of
        our relay-capable connections, else the first one.

        Raises :class:`GfsNotConnectedError` when there is none.
        """
        conns = await self._relay_connections()
        if not conns:
            raise GfsNotConnectedError()
        if gfs_id:
            for conn in conns:
                if conn.id == gfs_id:
                    return conn
        return conns[0]

    async def shared_connection(self, block: object) -> GfsConnection | None:
        """Our own relay-capable connection to a scanned code's bootstrap
        server, or ``None`` when we are not on it.

        Matched by the pinned ``gfs_instance_id`` first; the normalized
        base URL is the fallback (a code from a server we pinned under a
        different id is still the same address).
        """
        if not isinstance(block, dict):
            return None
        want_id = block.get("instance_id")
        want_url = block.get("url")
        conns = await self._relay_connections()
        if isinstance(want_id, str) and want_id:
            for conn in conns:
                if conn.gfs_instance_id and conn.gfs_instance_id == want_id:
                    return conn
        if isinstance(want_url, str) and want_url:
            want = normalize_gfs_base(want_url)
            for conn in conns:
                if normalize_gfs_base(conn.inbox_url) == want:
                    return conn
        return None

    async def send_sealed(
        self,
        *,
        to_instance_id: str,
        envelope: dict[str, Any],
        gfs_connection_id: str,
    ) -> bool:
        """Hand one sealed pairing body to OUR connection *gfs_connection_id*.

        Best-effort: ``False`` (logged at WARNING) when that connection is
        gone or the server did not accept the blob — the admin can retry
        the pairing, exactly as after a failed inbox POST.
        """
        url: str | None = None
        for conn in await self._gfs_repo.list_active():
            if conn.id == gfs_connection_id and conn.status == "active":
                url = conn.inbox_url or None
        if url is None:
            log.warning(
                "pairing reach: bootstrap connection %s is no longer active",
                gfs_connection_id,
            )
            return False
        try:
            ok = await self._relay_sender.send_sealed_envelope(
                to_instance_id=to_instance_id,
                envelope=envelope,
                gfs_url=url,
            )
        except Exception as exc:  # noqa: BLE001 — throttle / unavailable / bug
            log.warning("pairing reach: relay send to %s failed: %s", url, exc)
            return False
        if not ok:
            log.warning("pairing reach: %s did not accept the pairing body", url)
        return bool(ok)

    async def probe(self, instance_id: str) -> None:
        """Ask route discovery to probe a freshly confirmed peer.

        Best-effort and never raises: the seeded bootstrap route keeps the
        pair reachable until the next discovery round either way.
        """
        if self._probe_peer is None:
            return
        try:
            await self._probe_peer(instance_id)
        except Exception:  # noqa: BLE001
            log.warning(
                "pairing reach: route probe for %s failed",
                instance_id,
                exc_info=True,
            )


__all__ = [
    "PairingGfsReach",
    "gfs_block",
    "parse_reach",
    "verified_peer_keywrap",
]
