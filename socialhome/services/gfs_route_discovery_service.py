"""Shared-GFS route discovery for the paired relay fallback (v_53).

Two paired households that opted into the connection-server relay with
each other (``RemoteInstance.gfs_relay``) need to know which connection
servers (GFSes) they BOTH use before either can relay to the other — and
neither may tell the other which servers it is connected to. A GFS
answers every ``POST /gfs/envelope`` with the same ``202`` (any other
answer would be a presence oracle), so the intersection cannot be asked
of a server; it is discovered through the peer instead:

1. **Probe.** Household A sends :attr:`FederationEventType.GFS_RELAY_PROBE`
   ``{nonce}`` through EACH of its own active connections that proved the
   ``envelope_relay`` capability — never through a server A is not itself
   registered with. A remembers ``nonce → (peer, our connection id)`` for
   :data:`PENDING_PROBE_TTL_S`.
2. **Receive.** Household B receives the probe only through the servers it
   shares with A (a server pushes a relayed blob only down a recipient's
   own socket). The relay inbound leg tells B which of B's OWN
   connections carried it (:data:`~socialhome.services.gfs_relay_inbound
   .RELAY_DELIVERED_VIA`); B records that connection as a route to A and
   answers :attr:`FederationEventType.GFS_RELAY_PROBE_ACK` ``{nonce}``
   through THAT SAME server. A probe that did not arrive over the relay
   proves nothing and is ignored.
3. **Ack.** A accepts the ack only if the nonce is pending, was sent to
   this peer, and the ack arrived over the very connection the probe was
   sent through; then A records the route and forgets the nonce.

Both payloads are ``{nonce}`` (128 random bits) inside the ordinary
AES-256-GCM federation payload — no server URL, server id, connection id
or inbox id ever goes on the wire, and the routes each side stores are
ITS OWN ``gfs_connections`` ids. A GFS sees two identity-free relay
blobs, nothing else.

Routes are refreshed by the next probe round (a received probe refreshes
B's route, an accepted ack A's) and expire once not refreshed for
:data:`ROUTE_MAX_AGE` (three discovery intervals) — see
:class:`~socialhome.infrastructure.gfs_route_discovery_scheduler
.GfsRouteDiscoveryScheduler`. ``docs/protocol/gfs-relay.md`` has the
sequence diagram and the privacy analysis.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..domain.federation import (
    FederationEvent,
    FederationEventType,
    GfsConnection,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from ..domain.federation_capabilities import FederationCapability
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from .gfs_relay_inbound import RELAY_DELIVERED_VIA

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


#: Random bytes behind one probe nonce (128 bits, urlsafe base64 on the wire).
PROBE_NONCE_BYTES: int = 16

#: Shape a received nonce must have before anything is looked up with it.
#: ``token_urlsafe(16)`` yields 22 characters; the window is generous so a
#: future, longer nonce still parses, and tight enough that a hostile
#: payload cannot make a dict key out of a megabyte.
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

#: How long a sent probe waits for its ack. A relay queues a blob for a
#: recipient that is offline, so an ack can legitimately trail the probe by
#: minutes; past this the nonce is forgotten and a late ack is ignored.
PENDING_PROBE_TTL_S: float = 15 * 60.0

#: Upper bound on outstanding probes — every confirmed, opted-in peer times
#: every own connection. Past it the oldest is forgotten first.
MAX_PENDING_PROBES: int = 1024

#: Minimum spacing between two probe rounds to one peer. Every trigger
#: (timer, reconnect, pairing) funnels through :meth:`probe_peer`, so a
#: storm of triggers never turns into a storm of probes.
PROBE_PEER_MIN_INTERVAL_S: float = 60.0

#: Minimum spacing between two acks to one peer over one of our
#: connections. A paired peer cannot make us spend an unbounded number of
#: relay posts by flooding probes.
PROBE_ANSWER_MIN_INTERVAL_S: float = 30.0

#: Size cap for the per-(peer, connection) answer throttle.
MAX_ANSWER_ENTRIES: int = 4096

#: Re-probe cadence (the scheduler adds ±:data:`ROUTE_DISCOVERY_JITTER_S`).
ROUTE_DISCOVERY_INTERVAL_S: float = 24 * 3600.0
ROUTE_DISCOVERY_JITTER_S: float = 3600.0

#: A route not refreshed for three discovery intervals is dropped: one
#: missed round (an outage, a restart) never costs a working route, three
#: in a row mean the peer has left that server.
ROUTE_MAX_AGE: timedelta = timedelta(seconds=3 * ROUTE_DISCOVERY_INTERVAL_S)


def _utc_now_iso() -> str:
    """Tz-aware UTC ISO 8601 — the shape ``peer_gfs_routes`` stores."""
    return datetime.now(timezone.utc).isoformat()


def _nonce_of(payload: object) -> str | None:
    """The probe nonce from a decrypted payload, or ``None`` if malformed."""
    if not isinstance(payload, dict):
        return None
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not _NONCE_RE.match(nonce):
        return None
    return nonce


@dataclass(slots=True, frozen=True)
class _PendingProbe:
    """One probe awaiting its ack. Local-only, never serialised."""

    peer_id: str
    gfs_connection_id: str
    sent_at: float


class GfsRouteDiscoveryService:
    """Probe / ack logic for shared-GFS route discovery."""

    __slots__ = (
        "_federation",
        "_federation_repo",
        "_gfs_repo",
        "_relay_supported",
        "_clock",
        "_now_iso",
        "_pending",
        "_last_probe_at",
        "_last_answer_at",
    )

    def __init__(
        self,
        *,
        federation: "FederationService",
        federation_repo: AbstractFederationRepo,
        gfs_connection_repo: AbstractGfsConnectionRepo,
        envelope_relay_supported: Callable[[GfsConnection], Awaitable[bool]],
        clock: Callable[[], float] = time.monotonic,
        now_iso: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._federation = federation
        self._federation_repo = federation_repo
        self._gfs_repo = gfs_connection_repo
        self._relay_supported = envelope_relay_supported
        self._clock = clock
        self._now_iso = now_iso
        #: nonce → the probe it belongs to. Insertion-ordered, so the
        #: first key is always the oldest.
        self._pending: dict[str, _PendingProbe] = {}
        self._last_probe_at: dict[str, float] = {}
        self._last_answer_at: dict[tuple[str, str], float] = {}

    def attach_to(self, federation_service: "FederationService") -> None:
        """Register the probe and ack handlers on the inbound registry."""
        federation_service._event_registry.register(  # noqa: SLF001
            FederationEventType.GFS_RELAY_PROBE,
            self._on_probe,
        )
        federation_service._event_registry.register(  # noqa: SLF001
            FederationEventType.GFS_RELAY_PROBE_ACK,
            self._on_probe_ack,
        )

    @property
    def pending_count(self) -> int:
        """Outstanding probes (diagnostics and tests)."""
        return len(self._pending)

    # ── Outbound: probes ────────────────────────────────────────────────

    async def probe_all(self) -> int:
        """Probe every eligible peer. Returns the number of probes sent."""
        sent = 0
        for peer in await self._federation_repo.list_instances(
            status=PairingStatus.CONFIRMED.value,
        ):
            sent += await self._probe(peer)
        return sent

    async def probe_peer(self, instance_id: str) -> int:
        """Probe one peer through each of our relay-capable connections.

        Public trigger for a fresh pairing / a newly enabled opt-in. A peer
        that is not eligible (not confirmed, not opted in, no key-wrap key,
        below v_53) or was probed within :data:`PROBE_PEER_MIN_INTERVAL_S`
        gets nothing. Returns the number of probes sent.
        """
        peer = await self._federation_repo.get_instance(instance_id)
        if peer is None:
            return 0
        return await self._probe(peer)

    async def _probe(self, peer: RemoteInstance) -> int:
        if not await self._eligible(peer):
            return 0
        now = self._clock()
        last = self._last_probe_at.get(peer.id)
        if last is not None and now - last < PROBE_PEER_MIN_INTERVAL_S:
            log.debug("gfs routes: probe to %s throttled", peer.id)
            return 0
        conns = await self._relay_connections()
        if not conns:
            return 0
        self._last_probe_at[peer.id] = now
        sent = 0
        for conn in conns:
            nonce = secrets.token_urlsafe(PROBE_NONCE_BYTES)
            self._remember(nonce, _PendingProbe(peer.id, conn.id, now))
            result = await self._federation.send_event_via_gfs(
                to_instance_id=peer.id,
                event_type=FederationEventType.GFS_RELAY_PROBE,
                payload={"nonce": nonce},
                gfs_url=conn.inbox_url,
            )
            if result.ok:
                sent += 1
            else:
                # Never left this household: no ack can come back for it.
                self._pending.pop(nonce, None)
                log.debug(
                    "gfs routes: probe to %s via connection %s not accepted: %s",
                    peer.id,
                    conn.id,
                    result.error,
                )
        return sent

    async def _eligible(self, peer: RemoteInstance) -> bool:
        """Whether *peer* may be probed at all (no side effects)."""
        if peer.status is not PairingStatus.CONFIRMED:
            return False
        if peer.source is InstanceSource.SPACE_SESSION:
            # A link-joined household already rides one known server.
            return False
        if not peer.gfs_relay or not peer.remote_keywrap_pk:
            return False
        return await self._federation.peer_supports(
            peer.id,
            min_version=FederationCapability.MIN_FOR_GFS_RELAY_ROUTES,
        )

    async def _relay_connections(self) -> list[GfsConnection]:
        """Our own active connections whose server proved ``envelope_relay``.

        The privacy policy in one place: a probe only ever travels through
        a server this household is itself registered with.
        """
        out: list[GfsConnection] = []
        for conn in await self._gfs_repo.list_active():
            if conn.status != "active" or not conn.inbox_url:
                continue
            try:
                if await self._relay_supported(conn):
                    out.append(conn)
            except Exception:  # noqa: BLE001 — one bad server must not stop the round
                log.warning(
                    "gfs routes: capability check failed for connection %s",
                    conn.id,
                    exc_info=True,
                )
        return out

    def _remember(self, nonce: str, probe: _PendingProbe) -> None:
        self._prune_pending(probe.sent_at)
        while len(self._pending) >= MAX_PENDING_PROBES:
            self._pending.pop(next(iter(self._pending)))
        self._pending[nonce] = probe

    def _prune_pending(self, now: float) -> None:
        stale = [
            n for n, p in self._pending.items() if now - p.sent_at > PENDING_PROBE_TTL_S
        ]
        for n in stale:
            del self._pending[n]

    # ── Inbound: a probe reached us ─────────────────────────────────────

    async def _on_probe(self, event: FederationEvent) -> None:
        # Read SYNCHRONOUSLY, before the first await: the ContextVar is set
        # only for the duration of the relayed dispatch.
        via = RELAY_DELIVERED_VIA.get()
        if via is None:
            # Arrived over RTC / the HTTPS inbox (or through a server that
            # is not one of our active connections): it proves no route.
            log.debug(
                "gfs routes: ignoring a probe from %s that did not arrive "
                "over one of our connection servers",
                event.from_instance,
            )
            return
        nonce = _nonce_of(event.payload)
        if nonce is None:
            log.debug("gfs routes: malformed probe from %s", event.from_instance)
            return
        now = self._clock()
        key = (event.from_instance, via)
        last = self._last_answer_at.get(key)
        if last is not None and now - last < PROBE_ANSWER_MIN_INTERVAL_S:
            log.debug("gfs routes: probe from %s throttled", event.from_instance)
            return
        self._note_answer(key, now)
        # The probe crossed our connection ``via`` from this peer, so the
        # peer can reach us there — and, being registered with the same
        # server, we can reach it there too.
        await self._federation_repo.upsert_gfs_route(
            event.from_instance,
            via,
            now=self._now_iso(),
        )
        url = await self._connection_url(via)
        if url is None:  # the connection was removed mid-dispatch
            return
        result = await self._federation.send_event_via_gfs(
            to_instance_id=event.from_instance,
            event_type=FederationEventType.GFS_RELAY_PROBE_ACK,
            payload={"nonce": nonce},
            gfs_url=url,
        )
        if not result.ok:
            log.debug(
                "gfs routes: ack to %s not accepted: %s",
                event.from_instance,
                result.error,
            )

    def _note_answer(self, key: tuple[str, str], now: float) -> None:
        if len(self._last_answer_at) >= MAX_ANSWER_ENTRIES:
            for k in [
                k
                for k, t in self._last_answer_at.items()
                if now - t >= PROBE_ANSWER_MIN_INTERVAL_S
            ]:
                del self._last_answer_at[k]
            while len(self._last_answer_at) >= MAX_ANSWER_ENTRIES:
                self._last_answer_at.pop(next(iter(self._last_answer_at)))
        self._last_answer_at[key] = now

    async def _connection_url(self, gfs_connection_id: str) -> str | None:
        for conn in await self._gfs_repo.list_active():
            if conn.id == gfs_connection_id and conn.status == "active":
                return conn.inbox_url or None
        return None

    # ── Inbound: an ack came back ───────────────────────────────────────

    async def _on_probe_ack(self, event: FederationEvent) -> None:
        via = RELAY_DELIVERED_VIA.get()
        nonce = _nonce_of(event.payload)
        if nonce is None:
            log.debug("gfs routes: malformed ack from %s", event.from_instance)
            return
        probe = self._pending.get(nonce)
        if probe is None:
            log.debug(
                "gfs routes: ack from %s for an unknown nonce", event.from_instance
            )
            return
        if self._clock() - probe.sent_at > PENDING_PROBE_TTL_S:
            self._pending.pop(nonce, None)
            log.debug("gfs routes: ack from %s arrived too late", event.from_instance)
            return
        if probe.peer_id != event.from_instance:
            # Kept: the right peer's ack may still come.
            log.debug(
                "gfs routes: ack from %s for a probe sent to another peer",
                event.from_instance,
            )
            return
        if via is None or via != probe.gfs_connection_id:
            log.debug(
                "gfs routes: ack from %s did not come back over the probed server",
                event.from_instance,
            )
            return
        self._pending.pop(nonce, None)
        await self._federation_repo.upsert_gfs_route(
            event.from_instance,
            probe.gfs_connection_id,
            now=self._now_iso(),
        )
        log.info(
            "gfs routes: confirmed a shared connection server with %s",
            event.from_instance,
        )

    # ── Expiry ──────────────────────────────────────────────────────────

    async def expire_stale_routes(self) -> int:
        """Drop every route not refreshed within :data:`ROUTE_MAX_AGE`."""
        cutoff = (datetime.now(timezone.utc) - ROUTE_MAX_AGE).isoformat()
        removed = await self._federation_repo.delete_gfs_routes_older_than(cutoff)
        if removed:
            log.info("gfs routes: expired %d stale relay route(s)", removed)
        return removed


__all__ = [
    "MAX_PENDING_PROBES",
    "PENDING_PROBE_TTL_S",
    "PROBE_ANSWER_MIN_INTERVAL_S",
    "PROBE_PEER_MIN_INTERVAL_S",
    "ROUTE_DISCOVERY_INTERVAL_S",
    "ROUTE_DISCOVERY_JITTER_S",
    "ROUTE_MAX_AGE",
    "GfsRouteDiscoveryService",
]
