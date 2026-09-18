"""Federation delivery transport (§24.12, §4.2.3) — aiolibdatachannel edition.

The :class:`FederationTransport` facade is the single delivery seam for
outbound federation events. It keeps one :class:`_RtcPeer` per paired
peer and switches between two transports at send time:

* **WebRTC DataChannel** — primary transport. Once the DTLS + SRTP
  negotiation completes the channel stays open for the lifetime of the
  peering; routine envelopes go over it with zero HTTP overhead.
* **HTTPS inbox** — fallback transport and bootstrap path. Used (a)
  before the DataChannel is established (the signed SDP offer/answer
  and ICE candidates ride on top of it), (b) whenever the channel is
  closed / failing, and (c) to reach peers behind a strictly-blocked
  UDP path.

The channel payload is identical to the HTTPS inbox payload: the caller
still builds the AES-256-GCM-encrypted + Ed25519-signed
:class:`FederationEnvelope` the same way. Only delivery differs.

Security invariants:

* **S-14 (answer-origin)** — an inbound
  ``FEDERATION_RTC_ANSWER`` must come from the peer we sent the offer
  to. :class:`_RtcPeer` tracks the expected responder and rejects
  mismatched answers with a warning log.
* **Sender signature** — RTC frames are plain UTF-8 JSON of the same
  envelope dict the HTTPS inbox transport would have POSTed. The Ed25519
  signature inside the envelope proves origin; DTLS protects the
  DataChannel against an on-path MITM but the envelope signature is
  what the receiving :class:`FederationService` actually checks.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiolibdatachannel as rtc
import orjson
from aiohttp import ClientTimeout

from . import app_framing
from . import media_framing
from ..domain.events import PeerTransportChanged
from ..exception_text import describe_exception
from ..domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    RemoteInstance,
)

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from .gfs_relay_transport import GfsRelayTransport

log = logging.getLogger(__name__)


# ─── Config ─────────────────────────────────────────────────────────────────

#: DataChannel label for federation-wide event traffic. Distinct from
#: ``sync-v1`` (§4.2.3) so sync + routine federation can coexist.
CHANNEL_LABEL: str = "fed-v1"

#: Maximum time we will wait for the DataChannel to finish negotiating
#: before giving up and falling back to HTTPS inbox.
RTC_READY_TIMEOUT_S: float = 10.0

#: Keep-alive interval once the channel is open. Matches the TS
#: client's 30 s cadence (spec §24.12.5).
PING_INTERVAL_S: float = 30.0

#: High-water mark for a DataChannel's send buffer. When
#: ``dc.buffered_amount`` exceeds this, we drop the frame and let the
#: caller fall back to HTTPS inbox instead of unbounded SCTP queuing.
#: 1 MiB is well above a single envelope (~10 KB) but far under the
#: default libdatachannel message size ceiling.
SEND_HWM_BYTES: int = 1 << 20


# ─── HTTPS inbox transport ─────────────────────────────────────────────────────


class HttpsInboxTransport:
    """HTTPS POST transport — always available, used as fallback.

    Thin wrapper around an aiohttp client session. Keeping it a class
    (rather than a bare function) lets tests swap it out without
    patching module-level state.
    """

    __slots__ = ("_client_factory", "_client", "_timeout_s")

    def __init__(
        self,
        client_factory: Callable[[], Awaitable[Any]],
        *,
        timeout_s: float = 10.0,
    ) -> None:
        self._client_factory = client_factory
        self._client: Any | None = None
        self._timeout_s = timeout_s

    async def _client_once(self) -> Any:
        if self._client is None:
            self._client = await self._client_factory()
        return self._client

    async def send(
        self,
        *,
        instance: RemoteInstance,
        envelope_dict: dict,
    ) -> tuple[bool, int | None]:
        """POST the envelope to the remote inbox URL.

        Returns ``(ok, status_code)``. ``ok`` is true iff the peer
        returned 2xx. Any network-level error returns
        ``(False, None)`` so the caller can record a failure and
        enqueue for retry.
        """
        try:
            client = await self._client_once()
            async with client.post(
                instance.remote_inbox_url,
                json=envelope_dict,
                timeout=ClientTimeout(total=self._timeout_s),
            ) as resp:
                status = resp.status
                return 200 <= status < 300, status
        except Exception as exc:
            # ``describe_exception``, not ``exc``: the common failures here
            # (ClientOSError, ClientConnectionError, ServerTimeoutError) all
            # render as an empty string, so this line used to name the peer
            # and then say nothing about what went wrong.
            log.warning(
                "HTTPS-inbox send to %s failed: %s",
                instance.id,
                describe_exception(exc),
            )
            return False, None


# ─── RTC peer ──────────────────────────────────────────────────────────────

# The frame format on the DataChannel is the same envelope dict the
# HTTPS inbox transport would have POSTed — serialised as UTF-8 JSON with
# orjson for consistency with the HTTPS-inbox path.
_InboundCallback = Callable[[dict], Awaitable[None]]

# Inbound binary media frames carry the signed envelope header bytes
# (verbatim, for §24.11 re-validation) and the encrypted chunk payload
# bytes, alongside the already-known sender ``instance_id``. The handler
# returns a status dict (like the other inbound entry points); the drain
# loop discards it, so the return type is intentionally ``Any``.
_MediaInboundCallback = Callable[[str, bytes, bytes], Awaitable[Any]]

# Inbound binary app frames on ``fed-app-v1`` follow the same signature
# as the media callback — ``(instance_id, header_bytes, payload_bytes)``.
# The payload is an AES-256-GCM-sealed app message; the header is the
# signed federation envelope verbatim so the §24.11 pipeline can
# re-validate the exact signed bytes.
_AppInboundCallback = Callable[[str, bytes, bytes], Awaitable[Any]]


def _build_rtc_config(ice_servers: list[dict]) -> rtc.RTCConfiguration:
    """Flatten a Chrome-style ``ice_servers`` list into an
    :class:`aiolibdatachannel.RTCConfiguration`.

    Each entry may carry a single ``urls`` string or a list of them
    plus optional TURN ``username`` / ``credential``. We map each URL
    to an :class:`~aiolibdatachannel.IceServer` so credentials ride as
    first-class fields rather than being spliced into URL userinfo.
    """
    servers: list[rtc.IceServer] = []
    for srv in ice_servers:
        url_field = srv["urls"]
        raw_urls = url_field if isinstance(url_field, list) else [url_field]
        username = srv.get("username") or None
        credential = srv.get("credential") or None
        for url in raw_urls:
            servers.append(
                rtc.IceServer(url=url, username=username, credential=credential),
            )
    return rtc.RTCConfiguration(ice_servers=servers)


def _ice_fingerprint(ice_servers: list[dict]) -> frozenset[tuple]:
    """Content fingerprint of an ICE-server list, ignoring ordering.

    Compares only the fields that affect connectivity — ``urls``,
    ``username``, ``credential``. Two lists with the same fingerprint
    produce the same :func:`_build_rtc_config`, so a re-push of an
    equal list must not churn peers (HA re-delivers a byte-identical
    list on every daily credential refresh).
    """
    entries: set[tuple] = set()
    for srv in ice_servers or []:
        url_field = srv.get("urls")
        raw_urls = url_field if isinstance(url_field, list) else [url_field]
        urls = tuple(sorted(str(u) for u in raw_urls if u))
        entries.add(
            (
                urls,
                srv.get("username") or None,
                srv.get("credential") or None,
            ),
        )
    return frozenset(entries)


#: Graduated backoff between RTC handshake rebuild attempts for a peer
#: whose previous PeerConnection entered FAILED. The n-th consecutive
#: failure suppresses the rebuild for
#: ``min(BASE * FACTOR ** (n - 1), MAX)`` seconds, scaled by a jitter
#: factor drawn from ``[1 - JITTER, 1 + JITTER]`` so a household with
#: many simultaneously-failed peers doesn't retry them in lockstep:
#: 60 s → 4 m → 16 m → 64 m → 4.3 h → 6 h (capped).
#:
#: **The 60 s base is a hard anti-hammer floor — do not flatten it.**
#: The outbox polls every 5 s, so without suppression EVERY queued
#: envelope to an unreachable peer rebuilds the handshake and re-fails
#: (one stuck 54-envelope backlog produced ~4,400 STUN bindings). The
#: floor bounds a failing peer to at most one handshake per minute even
#: at full outbox cadence — a 12x reduction on the 5 s poll — and a peer
#: that is genuinely unreachable reaches the 6 h ceiling after ~1.5 h of
#: compounding, settling at ≤ 4 attempts/day. That is a few more attempts
#: per day than the flat 24 h rule this replaced, at a cost of one
#: bounded handshake each; what it buys is that a TRANSIENT failure
#: recovers in a minute instead of costing a full day of HTTPS-only
#: federation — the outage this exists to fix.
#:
#: The count is reset by a successful channel open
#: (:meth:`FederationTransport._on_peer_open`) and by any push of an
#: ICE-server list via :meth:`FederationTransport.set_ice_servers` —
#: changed or not — which also clears all current suppressions: the
#: operator's "I changed something, try again" signal.
RTC_RETRY_BACKOFF_BASE_S: float = 60.0
RTC_RETRY_BACKOFF_FACTOR: float = 4.0
RTC_RETRY_BACKOFF_MAX_S: float = 6 * 60 * 60.0
RTC_RETRY_JITTER: float = 0.2


#: How long an ICE candidate may sit in the buffer before being dropped.
#: A trickled candidate can outrun the matching OFFER/ANSWER on independent
#: HTTPS inbox round-trips (separate sockets, separate retry timers), and a
#: stale outbox at the sender can stretch that gap to tens of seconds. The
#: timeout has to cover that — dropping the candidate is unrecoverable,
#: while parking it costs a few KiB of memory until the handshake either
#: completes or the peer is torn down. Tuned for the HA-add-on relay path
#: where SDP can cross continents and queue behind retries.
ICE_BUFFER_TIMEOUT_S: float = 30.0


#: How many times one :class:`_RtcPeer` may restart its handshake after
#: discovering that the remote description landed on a PeerConnection
#: whose trickled candidates had already been dropped. The restart is
#: the recovery for a peer whose ANSWER outran the candidate buffer; the
#: bound is what stops a *persistently* late peer (a permanently
#: congested outbox) from looping offer→drop→offer forever. Once it is
#: exhausted we leave the PeerConnection alone so it reaches FAILED and
#: ``_evict_peer``'s suppression backoff takes over.
MAX_HANDSHAKE_RESTARTS: int = 2


#: How long the FIRST outbound handshake waits for the ICE-server list
#: to be primed before giving up and building the PeerConnection with
#: whatever is on hand. Under haos the list arrives asynchronously from
#: HA Core (Cloudflare TURN credentials via ``HaIceServerSync``) some
#: time after boot, while the outbox drain wants to send immediately;
#: waiting briefly here means those peers are built WITH TURN instead of
#: STUN-only and then retired and rebuilt a generation later — one
#: offer/answer round-trip saved per backlogged peer, and one fewer
#: window in which a retire can race an in-flight OFFER. The bound must
#: be finite: a platform that never primes (standalone never pushes a
#: list at all) or an HA Core that never answers must not stall
#: federation indefinitely.
ICE_PRIME_TIMEOUT_S: float = 15.0


class _RtcPeer:
    """One DataChannel session for one paired peer."""

    __slots__ = (
        "instance_id",
        "_ice_provider",
        "ice_generation",
        "_signaling",
        "_inbound",
        "_pc",
        "_channel",
        "_open",
        "_closed",
        "_loop",
        "_expected_answer_from",
        "_send_hwm",
        "_remote_description_applied",
        "_bus",
        "_published_open",
        "_polite",
        "_making_offer",
        "_is_offerer",
        "_dropped_candidates",
        "_starved_awaiting_offer",
        "_restarts",
        "_on_failed",
        "_on_open",
        "_teardown_task",
        "_media_inbound",
        "_media_channel",
        "_media_open",
        "_media_buf",
        "_app_inbound",
        "_app_channel",
        "_app_open",
        "_app_buf",
    )

    def __init__(
        self,
        *,
        instance_id: str,
        ice_provider: Callable[[], list[dict]] | None = None,
        ice_generation: int = 0,
        signaling: Callable[[FederationEventType, dict], Awaitable[None]],
        inbound: _InboundCallback,
        send_hwm: int = SEND_HWM_BYTES,
        bus: "EventBus | None" = None,
        polite: bool = False,
        on_failed: Callable[[str], Awaitable[None]] | None = None,
        on_open: Callable[[str], Awaitable[None]] | None = None,
        media_inbound: _MediaInboundCallback | None = None,
        app_inbound: _AppInboundCallback | None = None,
    ) -> None:
        self.instance_id = instance_id
        self._ice_provider: Callable[[], list[dict]] = ice_provider or (lambda: [])
        #: The transport's ``_ice_generation`` at the moment this peer
        #: was built. Compared against the transport's current value in
        #: :meth:`needs_rehandshake` so a peer whose PeerConnection was
        #: constructed under a stale ICE-server list (no TURN yet) gets
        #: rebuilt instead of staying STUN-only forever.
        self.ice_generation = ice_generation
        self._signaling = signaling
        self._inbound = inbound
        self._pc: Any | None = None
        self._channel: Any | None = None
        self._open = asyncio.Event()
        self._closed = False
        # Second DataChannel — ``fed-media-v1`` — carries bulk media as
        # binary frames (no base64) so it doesn't head-of-line-block the
        # latency-sensitive control traffic on ``fed-v1``. Both channels
        # ride the same PeerConnection (independent SCTP streams), so no
        # extra ICE/DTLS/TURN handshake. ``_media_buf`` reassembles
        # frames that a peer split/coalesced across SCTP messages.
        self._media_inbound = media_inbound
        self._media_channel: Any | None = None
        self._media_open = asyncio.Event()
        self._media_buf = bytearray()
        # Third DataChannel — ``fed-app-v1`` — carries small app-to-app
        # messages (chess moves, whiteboard ops, custom mini-app payloads)
        # as binary frames. Mirrors the media channel: created in the same
        # offer so no extra ICE/DTLS/TURN handshake is needed. ``_app_buf``
        # reassembles frames split/coalesced across SCTP messages.
        self._app_inbound = app_inbound
        self._app_channel: Any | None = None
        self._app_open = asyncio.Event()
        self._app_buf = bytearray()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Notified when the PC transitions to ``RTCState.FAILED`` so the
        # parent transport can evict this peer from its registry and
        # let the next ``send()`` build a fresh handshake instead of
        # silently falling back to HTTPS forever.
        self._on_failed = on_failed
        # Mirror of ``on_failed`` for the success edge: invoked once per
        # channel-open edge so the parent transport can forget this
        # peer's accumulated failure backoff. See
        # :meth:`_publish_open_if_needed`'s call site in
        # :meth:`_drain_channel`.
        self._on_open = on_open
        # Strong ref so a teardown publish from ``close()`` doesn't get
        # GC'd before it runs (CPython's loop holds tasks via a weakref
        # set in some implementations).
        self._teardown_task: asyncio.Task | None = None
        # S-14: on the offerer side we lock the answer origin to the
        # peer we invited. Mismatches are rejected with a warning.
        self._expected_answer_from: str | None = None
        self._send_hwm = send_hwm
        # WebRTC requires every ``addRemoteCandidate`` call to land
        # *after* ``setRemoteDescription`` — applying earlier throws
        # ``rtcAddRemoteCandidate: runtime failure``. Trickle ICE
        # routinely violates this on the wire because the offerer
        # flushes candidates the moment the offer is sent, and the
        # receiver may see ``FEDERATION_RTC_ICE`` envelopes before (or
        # mid-construction of) the matching ``FEDERATION_RTC_OFFER``.
        # The event is set by :meth:`accept_offer` and
        # :meth:`apply_answer` once the remote description is in;
        # :meth:`add_ice_candidate` waits on it.
        self._remote_description_applied = asyncio.Event()
        self._bus = bus
        self._published_open = False
        # Perfect-negotiation role: polite peer defers to an incoming
        # OFFER by rolling back its own pending offer; impolite peer
        # ignores an incoming OFFER when one is already in flight.
        # Determined lexicographically: own_instance_id > peer_id.
        self._polite = polite
        # Set True between ``start_offer`` initiation and either the
        # successful ``apply_answer`` call or a polite rollback.
        self._making_offer: bool = False
        #: Which side of the handshake we are on. Only the offerer may
        #: re-offer in :meth:`_restart_handshake` — an answerer that
        #: re-offered would manufacture the very glare perfect
        #: negotiation exists to resolve.
        self._is_offerer: bool = False
        #: Trickled candidates thrown away in :meth:`add_ice_candidate`
        #: because the remote description never arrived inside
        #: :data:`ICE_BUFFER_TIMEOUT_S`. Scoped to the CURRENT
        #: negotiation: zeroed by :meth:`start_offer` and
        #: :meth:`accept_offer` as they build (or adopt) a
        #: PeerConnection, and by :meth:`apply_answer` once the remote
        #: description lands. Non-zero when a remote description finally
        #: arrives means that connection holds too few remote candidates
        #: to pass ICE, which is what triggers
        #: :meth:`_restart_handshake` — but it says nothing about any
        #: LATER connection, so it must never drive
        #: :meth:`needs_rehandshake` (that is
        #: :attr:`_starved_awaiting_offer`'s job).
        self._dropped_candidates: int = 0
        #: True while we are sitting without a usable PeerConnection
        #: waiting for the remote to (re-)offer: either
        #: :meth:`_restart_handshake` tore our starved answerer PC down,
        #: or candidates were dropped before any PeerConnection existed
        #: at all (the stub peer :meth:`FederationTransport.on_rtc_ice`
        #: creates for candidates that overtook the OFFER). Cleared the
        #: moment we build a PeerConnection in :meth:`start_offer` or
        #: :meth:`accept_offer`. This — not the drop count — is what
        #: makes :meth:`needs_rehandshake` true, so a peer that is
        #: negotiating normally is never retired out from under itself.
        self._starved_awaiting_offer: bool = False
        #: Handshake restarts already spent, bounded by
        #: :data:`MAX_HANDSHAKE_RESTARTS`.
        self._restarts: int = 0

    # ─── Transport change publication ─────────────────────────────────────

    async def _publish_open_if_needed(self) -> None:
        """Publish ``PeerTransportChanged(transport='rtc')`` exactly once
        per channel-open edge. Called from ``_drain_channel`` when the
        DataChannel transitions to OPEN.
        """
        if self._published_open or self._bus is None:
            return
        self._published_open = True
        await self._bus.publish(
            PeerTransportChanged(
                instance_id=self.instance_id,
                transport="rtc",
            ),
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start_offer(self) -> None:
        """Initiate the SDP offer/answer handshake (offerer role)."""
        self._making_offer = True
        self._is_offerer = True
        self._expected_answer_from = self.instance_id
        self._loop = asyncio.get_running_loop()
        self._pc = rtc.PeerConnection(_build_rtc_config(self._ice_provider()))
        # A fresh OFFER starts a clean negotiation: any candidates dropped
        # so far belonged to the PeerConnection we are replacing, so they
        # must not re-trigger recovery against this one — and we are no
        # longer waiting on anybody else's offer, we are making one.
        self._dropped_candidates = 0
        self._starved_awaiting_offer = False
        self._channel = await self._pc.create_data_channel(CHANNEL_LABEL)
        # Ask aiolibdatachannel to notify us once the buffered amount
        # drops below half the HWM — lets future refactors await
        # backpressure instead of polling. For now we just read
        # ``buffered_amount`` directly in ``send()``.
        self._channel.set_buffered_amount_low_threshold(self._send_hwm // 2)
        # Second channel for binary media, created up-front (not lazily)
        # so it negotiates as part of this single offer — adding a
        # channel after the PC is established would force a renegotiation
        # the perfect-negotiation glare logic isn't tuned for. One idle
        # channel on a peering that never sends media is cheap.
        self._media_channel = await self._pc.create_data_channel(
            media_framing.CHANNEL_LABEL,
        )
        self._media_channel.set_buffered_amount_low_threshold(self._send_hwm // 2)
        # Third channel for binary app messages, created up-front (not lazily)
        # so it negotiates as part of this single offer — adding a channel
        # after the PC is established would force a renegotiation the
        # perfect-negotiation glare logic isn't tuned for. One idle channel
        # on a peering that never sends app messages is cheap.
        self._app_channel = await self._pc.create_data_channel(
            app_framing.CHANNEL_LABEL,
        )
        self._app_channel.set_buffered_amount_low_threshold(self._send_hwm // 2)
        # Tasks bound to the pc: auto-cancelled on pc.close().
        self._pc.spawn_task(self._drain_channel(self._channel))
        self._pc.spawn_task(self._drain_media_channel(self._media_channel))
        self._pc.spawn_task(self._drain_app_channel(self._app_channel))
        self._pc.spawn_task(self._drain_events())

        local = await self._pc.set_local_description("offer")
        await self._signaling(
            FederationEventType.FEDERATION_RTC_OFFER,
            {"sdp": local.sdp, "sdp_type": local.type},
        )
        # Only now: start forwarding trickled ICE candidates. ``_drain_ice``
        # and the OFFER/ANSWER signaling are independent HTTPS posts to the
        # peer's inbox; if the drain task starts before the OFFER is queued,
        # a candidate POST can outrun the OFFER POST on the wire and the
        # receiver drops the candidate at the §24.12.5 buffer timeout.
        # Gathering keeps producing candidates inside libdatachannel's queue
        # until we start consuming, so deferral is lossless.
        self._pc.spawn_task(self._drain_ice())

    async def accept_offer(self, *, sdp: str, from_instance: str) -> None:
        """Receive an SDP offer (answerer role) and reply with an answer.

        Implements perfect-negotiation glare resolution: if we are
        already making an offer (or the PC is not in stable state),
        the impolite peer ignores this incoming OFFER and the polite
        peer rolls back its own offer before accepting.
        """
        # The signaling state comes back as ``aiolibdatachannel.SignalingState``
        # which is an ``IntEnum`` — comparing it against the string
        # ``"stable"`` always returns ``True`` (IntEnum vs str), so before
        # this fix every legitimate handshake was treated as a glare
        # collision. Compare against the enum value directly. See also
        # :meth:`apply_answer` below.
        collision = self._making_offer or (
            self._pc is not None
            and self._pc.signaling_state != rtc.SignalingState.STABLE
        )
        if collision and not self._polite:
            log.info(
                "RTC glare: impolite side ignoring incoming OFFER from %s "
                "(making_offer=%s, signaling_state=%s)",
                from_instance,
                self._making_offer,
                self._pc.signaling_state if self._pc else None,
            )
            return
        if collision and self._polite:
            log.info(
                "RTC glare: polite side rolling back our OFFER to accept %s's",
                from_instance,
            )
            await self._close_pc()
            self._making_offer = False

        self._expected_answer_from = None  # answerer, no outstanding offer
        # Set only once the incoming OFFER is actually accepted: the
        # impolite side returns above with its own offer still pending,
        # and it is still the offerer.
        self._is_offerer = False
        self._loop = asyncio.get_running_loop()
        self._pc = rtc.PeerConnection(_build_rtc_config(self._ice_provider()))

        self._pc.spawn_task(self._drain_incoming_channel())
        self._pc.spawn_task(self._drain_events())

        await self._pc.set_remote_description(sdp, "offer")
        # Release any ICE candidates that arrived before the offer (or
        # during the construction of the PC). They'll have been parked
        # in :meth:`add_ice_candidate` waiting on this event.
        self._remote_description_applied.set()
        # Candidates dropped while we waited for THIS offer starve THIS
        # PeerConnection, so the count is what decides the recovery
        # below — but it is charged to the connection we just built, and
        # zeroing it here is what stops one transient drop from tearing
        # down every SUBSEQUENT clean offer too. Same reset
        # :meth:`start_offer` does as it builds its PC; before it
        # existed, ``_dropped_candidates`` was a permanent property of
        # the peer object and a single late candidate poisoned the next
        # ``MAX_HANDSHAKE_RESTARTS`` handshakes.
        starved = self._dropped_candidates
        self._dropped_candidates = 0
        self._starved_awaiting_offer = False
        local = await self._pc.set_local_description("answer")
        await self._signaling(
            FederationEventType.FEDERATION_RTC_ANSWER,
            {"sdp": local.sdp, "sdp_type": local.type},
        )
        # Only now: start forwarding trickled ICE candidates — see the
        # matching comment in :meth:`start_offer` for the wire-ordering
        # rationale. The original code spawned this task before
        # ``set_local_description("answer")``, which let a candidate POST
        # race the ANSWER POST and arrive at the offerer first; the
        # offerer then dropped the candidate at the buffer timeout
        # because its remote description hadn't been applied yet.
        self._pc.spawn_task(self._drain_ice())
        # Same recovery trigger as :meth:`apply_answer`: the remote
        # description has landed on a PC that already lost candidates.
        # Deliberately at the END of the method rather than immediately
        # after ``_remote_description_applied.set()`` — ``_close_pc()``
        # nulls ``self._pc``, and the ANSWER still has to be built and
        # signalled from it first (the offerer needs the ANSWER to learn
        # the handshake reached us at all).
        if starved:
            await self._restart_handshake(starved)

    async def apply_answer(self, *, sdp: str, from_instance: str) -> bool:
        """Apply the peer's SDP answer to our pending offer.

        Returns ``True`` when accepted. Rejects (returns ``False``) if
        ``from_instance`` doesn't match the peer we sent the offer to —
        S-14 answer-origin guard. Also ignores late answers that arrive
        after a polite-side rollback (signaling_state will no longer be
        ``have-local-offer``).
        """
        if (
            self._expected_answer_from is not None
            and from_instance != self._expected_answer_from
        ):
            log.warning(
                "RTC answer for %s rejected — came from %s",
                self._expected_answer_from,
                from_instance,
            )
            return False
        # Perfect-negotiation guard: if we rolled back our offer (polite
        # side) the PC is either None or no longer in have-local-offer
        # state — silently drop this stale ANSWER.  ``signaling_state``
        # is ``IntEnum``; comparing against the string ``"have-local-offer"``
        # was always True (IntEnum vs str), so prior to this fix every
        # legitimate answer was rejected and the offerer's handshake
        # never completed → silent HTTPS fallback.
        if (
            self._pc is None
            or self._pc.signaling_state != rtc.SignalingState.HAVE_LOCAL_OFFER
        ):
            log.info(
                "RTC answer from %s ignored — pc is %s",
                from_instance,
                self._pc.signaling_state if self._pc else "absent",
            )
            return False
        self._expected_answer_from = None
        await self._pc.set_remote_description(sdp, "answer")
        # Offerer side: flush any ICE candidates the answerer
        # trickled before we applied their SDP answer. Same
        # rationale as :meth:`accept_offer`.
        self._remote_description_applied.set()
        self._making_offer = False
        # Same PeerConnection-scoped accounting as :meth:`accept_offer`:
        # the drops belong to the connection this ANSWER just completed.
        starved = self._dropped_candidates
        self._dropped_candidates = 0
        # The handshake completed — but if candidates were thrown away
        # while we waited for this ANSWER, this PeerConnection holds too
        # few remote candidates to ever pass ICE. Re-offer now instead of
        # waiting out the connectivity timer, a FAILED state and the
        # eviction backoff.
        if starved:
            await self._restart_handshake(starved)
        return True

    async def _restart_handshake(self, dropped: int) -> None:
        """Rebuild the handshake after candidates were dropped.

        *dropped* is the number of candidates charged to the connection
        the caller just completed — read before the caller zeroed the
        counter, since the count is scoped to that connection and no
        longer describes the one we are about to build.

        Called from :meth:`apply_answer` / :meth:`accept_offer` once the
        remote description lands on a PeerConnection that already threw
        candidates away. Those two run on inbound-signalling coroutines
        (``on_rtc_answer`` / ``on_rtc_offer``), never on a task spawned
        via ``pc.spawn_task`` — so the ``_close_pc()`` below, which
        cancels every spawned task of the PC it closes, cannot cancel the
        coroutine calling it. Do NOT move this trigger into
        :meth:`_drain_events` or :meth:`_drain_ice`: both ARE spawned
        tasks, and there the call would cancel itself mid-recovery (the
        reason :meth:`_drain_events` schedules its eviction callback on a
        separate task).

        Only the offerer re-offers. An answerer tears its starved
        PeerConnection down and lets the offerer drive the next
        handshake, so the restart can never manufacture glare —
        :meth:`accept_offer`'s ``_polite`` logic stays the sole glare
        arbiter. The answerer raises ``_starved_awaiting_offer`` as it
        tears down: that keeps :meth:`needs_rehandshake` true, so if the
        remote never re-offers (an older peer still suppressing rebuilds
        for a flat 24 h, or a current one deep into its graduated
        backoff) our own next ``send()`` rebuilds us as the offerer
        instead of stranding the peer on HTTPS. Both :meth:`start_offer`
        and :meth:`accept_offer` clear the flag as they build a
        PeerConnection, so whichever side re-offers first ends the wait
        and the two paths cannot ping-pong.

        The flag is deliberately NOT the drop count. The count belongs
        to one PeerConnection; reusing it as the rebuild signal made a
        single transient drop tear down the next
        ``MAX_HANDSHAKE_RESTARTS`` PeerConnections — each one answered
        and then immediately closed while the offerer believed the
        handshake had landed — and left ``needs_rehandshake`` true
        forever afterwards, so ``send()`` retired a healthy,
        still-negotiating peer on the next outbox poll.
        """
        if self._closed:
            return
        if not self._is_offerer:
            if self._restarts >= MAX_HANDSHAKE_RESTARTS:
                log.warning(
                    "fed RTC peer %s: starved answerer PC, but already tore "
                    "down %d time(s) — leaving it so the retry backoff applies",
                    self.instance_id,
                    self._restarts,
                )
                return
            self._restarts += 1
            log.info(
                "fed RTC peer %s: tearing down starved answerer PC "
                "(%d ICE candidate(s) dropped, teardown %d/%d) — awaiting a "
                "fresh OFFER",
                self.instance_id,
                dropped,
                self._restarts,
                MAX_HANDSHAKE_RESTARTS,
            )
            await self._close_pc()
            # We now have no PeerConnection and are waiting on the
            # offerer. If that OFFER never comes, this is what makes our
            # own next ``send()`` rebuild us as the offerer.
            self._starved_awaiting_offer = True
            return
        if self._restarts >= MAX_HANDSHAKE_RESTARTS:
            log.warning(
                "fed RTC peer %s: %d ICE candidate(s) dropped but the "
                "handshake already restarted %d time(s) — letting the "
                "PeerConnection fail so the retry backoff applies",
                self.instance_id,
                dropped,
                self._restarts,
            )
            return
        self._restarts += 1
        log.info(
            "fed RTC peer %s: restarting handshake (%d ICE candidate(s) "
            "dropped before the remote description landed, restart %d/%d)",
            self.instance_id,
            dropped,
            self._restarts,
            MAX_HANDSHAKE_RESTARTS,
        )
        # Closes the old PC (and its spawned drains) BEFORE ``start_offer``
        # installs the new one, so the ``self._pc is not pc`` guards in
        # :meth:`_drain_events` / :meth:`_drain_ice` see a mismatch: a
        # dying PC's FAILED event cannot flip ``_closed`` on the peer and
        # its last candidates cannot leak onto the new connection.
        # ``_close_pc`` also swaps in a fresh
        # ``_remote_description_applied`` event, so candidates trickled
        # against the new offer park correctly.
        await self._close_pc()
        self._making_offer = False
        # ``start_offer`` zeroes ``_dropped_candidates`` and clears
        # ``_starved_awaiting_offer`` as it builds the replacement
        # PeerConnection.
        await self.start_offer()

    async def add_ice_candidate(self, *, candidate: str, sdp_mid: str) -> None:
        """Apply a trickled ICE candidate, buffering until the remote
        description is in.

        WebRTC requires every ``addRemoteCandidate`` call to land after
        ``setRemoteDescription``; aiolibdatachannel surfaces a
        violation as ``rtcAddRemoteCandidate: runtime failure (-2)``
        and the candidate is silently lost — which then strands ICE
        with no remote candidates, the connectivity timer expires, and
        the PeerConnection transitions to ``failed``.

        We sidestep the race by gating every candidate on
        :attr:`_remote_description_applied`. The event is set by
        :meth:`accept_offer` (answerer) and :meth:`apply_answer`
        (offerer); until then the candidate parks here. If the
        handshake never lands within :data:`ICE_BUFFER_TIMEOUT_S` we
        drop the candidate rather than pinning memory forever.
        """
        if not candidate or self._closed:
            return
        if not self._remote_description_applied.is_set():
            try:
                await asyncio.wait_for(
                    self._remote_description_applied.wait(),
                    timeout=ICE_BUFFER_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "fed RTC: ICE candidate for %s dropped — remote "
                    "description not applied within %.0fs",
                    self.instance_id,
                    ICE_BUFFER_TIMEOUT_S,
                )
                # Account for the loss, but do NOT recover from here:
                # the OFFER/ANSWER may still be legitimately in flight
                # and restarting mid-flight would thrash. Recovery runs
                # when the description actually lands
                # (:meth:`_restart_handshake`).
                self._dropped_candidates += 1
                if self._pc is None:
                    # No PeerConnection at all — this is the stub peer
                    # ``on_rtc_ice`` creates for candidates that
                    # overtook the OFFER. The drop cannot be charged to
                    # a connection, and if the OFFER never lands nothing
                    # else would ever rebuild us: ``send()`` sees ``peer
                    # is not None`` and skips the rebuild forever, a
                    # permanent HTTPS-only black hole for that peer.
                    self._starved_awaiting_offer = True
                return
        if self._pc is None or self._closed:
            # Peer closed between event set and resume — race, drop.
            return
        try:
            await self._pc.add_remote_candidate(candidate, sdp_mid)
        except rtc.RTCError as exc:
            # Native layer can still reject a malformed or late
            # candidate — log at debug rather than letting the
            # exception bubble through the event dispatcher.
            log.debug(
                "fed RTC: add_remote_candidate failed for %s: %s",
                self.instance_id,
                exc,
            )

    # ─── Internal drain loops ─────────────────────────────────────────────

    async def _drain_events(self) -> None:
        """Log every PC state transition and trigger auto-recovery on
        ``RTCState.FAILED``.

        The library exposes a unified async iterator (`pc.events()`)
        that fires for every connection / ICE / signaling / gathering
        transition. Before this loop, the only visible signal at the
        federation layer was ``channel open`` / ``channel closed`` —
        an operator looking at a stuck handshake had no way to tell
        whether ICE never gathered, DTLS failed, the answer was
        rejected, or the peer just dropped. Now every transition is
        a log line tagged with the instance.

        When the connection enters ``FAILED`` we evict ourselves from
        the parent transport via ``_on_failed`` so the next ``send()``
        builds a fresh handshake instead of returning False forever.
        """
        pc = self._pc
        if pc is None:
            return
        try:
            async for ev in pc.events():
                if isinstance(ev, rtc.StateChangeEvent):
                    log.info(
                        "fed RTC peer %s: connection_state=%s",
                        self.instance_id,
                        ev.state.name,
                    )
                    if ev.state in (rtc.RTCState.FAILED, rtc.RTCState.CLOSED):
                        # Stale event for a PC we've already rolled
                        # back away from — leave the rebuilt
                        # connection alone. Without this guard the
                        # polite-rollback path can flip
                        # ``self._closed = True`` on the **reused**
                        # ``_RtcPeer``, so the freshly-handshaked
                        # DataChannel reports ``is_ready=False``
                        # forever and federation silently falls
                        # back to HTTPS for the life of the session.
                        if self._pc is not pc:
                            continue
                        self._closed = True
                        self._open.clear()
                        if (
                            ev.state == rtc.RTCState.FAILED
                            and self._on_failed is not None
                        ):
                            # Schedule the eviction as a separate
                            # task so we don't await it inline —
                            # the callback calls ``peer.close()``
                            # which closes ``self._pc`` which
                            # cancels the very ``_drain_events``
                            # task we're in the middle of.
                            # Decoupling avoids the self-cancel
                            # (and any cleanup logic future
                            # contributors might add below this
                            # branch).
                            assert self._loop is not None
                            evict_cb = self._on_failed
                            instance_id = self.instance_id

                            async def _evict_async() -> None:
                                try:
                                    await evict_cb(instance_id)
                                except Exception as exc:  # noqa: BLE001
                                    log.warning(
                                        "fed RTC on_failed handler raised for %s: %s",
                                        instance_id,
                                        exc,
                                    )

                            self._loop.create_task(
                                _evict_async(),
                                name=f"fed-rtc-evict[{instance_id}]",
                            )
                elif isinstance(ev, rtc.IceStateChangeEvent):
                    log.info(
                        "fed RTC peer %s: ice_state=%s",
                        self.instance_id,
                        ev.state.name,
                    )
                elif isinstance(ev, rtc.SignalingStateChangeEvent):
                    log.debug(
                        "fed RTC peer %s: signaling_state=%s",
                        self.instance_id,
                        ev.state.name,
                    )
                elif isinstance(ev, rtc.GatheringStateChangeEvent):
                    log.debug(
                        "fed RTC peer %s: gathering_state=%s",
                        self.instance_id,
                        ev.state.name,
                    )
                # LocalDescription / LocalCandidate / DataChannel events
                # are consumed by their specialised iterators
                # (``ice_candidates``, ``incoming_data_channels``,
                # ``set_local_description``); skip them here so we don't
                # double-log.
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug("fed RTC events drain to %s ended: %s", self.instance_id, exc)

    async def _drain_ice(self) -> None:
        """Pump local ICE candidates out to the peer over signalling.

        **Race guard**: ``pc`` is captured at task entry. The polite-
        rollback path in :meth:`accept_offer` calls
        :meth:`_close_pc` which both closes the old PC AND swaps
        ``self._pc`` to a freshly-built one. Without the
        ``self._pc is pc`` check below, the OLD-PC's ``_drain_ice``
        (still alive in the brief window between ``pc.close()`` and
        task cancellation) can read one more candidate from the
        dying PC and ship it via ``_signaling`` to the peer. That
        candidate refers to a socket about to close — worse, the
        peer might successfully pair-check against it, briefly
        flipping ICE state to ``CONNECTED`` only to lose it again on
        DTLS. From an operator log that's exactly the
        "ICE state to connected → DTLS handshake failed" pattern.
        """
        pc = self._pc
        assert pc is not None  # spawned from start_offer/accept_offer
        try:
            async for cand in pc.ice_candidates():
                if self._pc is not pc:
                    return
                await self._signaling(
                    FederationEventType.FEDERATION_RTC_ICE,
                    {"candidate": cand.candidate, "sdp_mid": cand.mid},
                )
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug("fed RTC ICE drain to %s ended: %s", self.instance_id, exc)

    async def _drain_incoming_channel(self) -> None:
        """Answerer path: latch the provider's DataChannels as they arrive.

        Three channels ride this one PeerConnection — ``fed-v1`` (control
        + routine JSON), ``fed-media-v1`` (binary media), and ``fed-app-v1``
        (binary app messages). We must keep iterating until all have arrived;
        returning after the first would strand the remaining channels and
        silently force their traffic onto the JSON fallback forever. Unknown
        labels are ignored. The iterator ends naturally when the PC closes.
        """
        pc = self._pc
        assert pc is not None  # spawned from accept_offer
        try:
            async for ch in pc.incoming_data_channels():
                if ch.label == CHANNEL_LABEL:
                    self._channel = ch
                    ch.set_buffered_amount_low_threshold(self._send_hwm // 2)
                    pc.spawn_task(self._drain_channel(ch))
                elif ch.label == media_framing.CHANNEL_LABEL:
                    self._media_channel = ch
                    ch.set_buffered_amount_low_threshold(self._send_hwm // 2)
                    pc.spawn_task(self._drain_media_channel(ch))
                elif ch.label == app_framing.CHANNEL_LABEL:
                    self._app_channel = ch
                    ch.set_buffered_amount_low_threshold(self._send_hwm // 2)
                    pc.spawn_task(self._drain_app_channel(ch))
                else:
                    log.debug(
                        "fed RTC peer %s: ignoring incoming channel %r",
                        self.instance_id,
                        ch.label,
                    )
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug("fed RTC incoming-channel wait ended: %s", exc)

    async def _drain_channel(self, channel) -> None:
        """Consume inbound frames on a DataChannel and mark open/closed."""
        try:
            await channel.wait_open()
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning(
                "fed RTC channel never opened to %s: %s",
                self.instance_id,
                exc,
            )
            return
        log.info("fed RTC channel open to %s", self.instance_id)
        self._open.set()
        await self._publish_open_if_needed()
        if self._on_open is not None:
            # Same edge as the transport-changed publish above — the
            # channel just reached OPEN — so the retry backoff is reset
            # exactly when (and as often as) the peer proves reachable.
            # Awaited inline because the handler only mutates the
            # transport's bookkeeping dicts: unlike ``_on_failed`` it
            # never closes the PeerConnection, so it cannot cancel this
            # pc-spawned task from under itself.
            try:
                await self._on_open(self.instance_id)
            except Exception as exc:  # noqa: BLE001 — never kill the channel
                log.warning(
                    "fed RTC on_open handler raised for %s: %s",
                    self.instance_id,
                    exc,
                )
        try:
            async for msg in channel:
                try:
                    data = orjson.loads(
                        msg if isinstance(msg, (bytes, str)) else bytes(msg)
                    )
                except Exception as exc:  # noqa: BLE001 — orjson raises orjson.JSONDecodeError + anything
                    log.warning(
                        "fed RTC malformed frame from %s: %s",
                        self.instance_id,
                        exc,
                    )
                    continue
                await self._inbound(data)
        except asyncio.CancelledError:
            raise
        except rtc.ConnectionClosedError:
            pass
        except rtc.RTCError as exc:
            log.debug("fed RTC recv loop to %s ended: %s", self.instance_id, exc)
        log.info("fed RTC channel closed to %s", self.instance_id)
        self._open.clear()
        self._closed = True

    async def _drain_media_channel(self, channel) -> None:
        """Consume inbound binary media frames on ``fed-media-v1``.

        Mirrors :meth:`_drain_channel` but the wire is length-prefixed
        binary (:mod:`media_framing`), not JSON. SCTP preserves message
        boundaries so one ``send`` is normally one whole frame; the
        running ``_media_buf`` nonetheless tolerates a peer that
        coalesced/split frames across messages. Each ``MEDIA_CHUNK``
        frame's header (the signed envelope) + payload (encrypted chunk)
        is handed to ``_media_inbound`` for §24.11 re-validation —
        nothing is trusted here on the strength of the channel alone.
        """
        try:
            await channel.wait_open()
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning(
                "fed RTC media channel never opened to %s: %s",
                self.instance_id,
                exc,
            )
            return
        log.info("fed RTC media channel open to %s", self.instance_id)
        self._media_open.set()
        try:
            async for msg in channel:
                if isinstance(msg, str):
                    # A string on the binary media channel is a protocol
                    # violation; coerce so framing rejects it cleanly.
                    raw = msg.encode("utf-8")
                elif isinstance(msg, (bytes, bytearray)):
                    raw = bytes(msg)
                else:
                    raw = bytes(msg)
                self._media_buf.extend(raw)
                try:
                    frames, leftover = media_framing.iter_complete_frames(
                        bytes(self._media_buf),
                    )
                except media_framing.MediaFramingError as exc:
                    log.warning(
                        "fed RTC media frame from %s malformed (%s) — resetting buffer",
                        self.instance_id,
                        exc,
                    )
                    self._media_buf.clear()
                    continue
                self._media_buf = bytearray(leftover)
                for frame in frames:
                    if frame.frame_type != media_framing.FRAME_TYPE_MEDIA_CHUNK:
                        # Forward-compat: a newer peer may ship frame
                        # types we don't know yet — skip, don't error.
                        log.debug(
                            "fed RTC media: skipping unknown frame_type=%d from %s",
                            frame.frame_type,
                            self.instance_id,
                        )
                        continue
                    if self._media_inbound is not None:
                        try:
                            await self._media_inbound(
                                self.instance_id,
                                frame.header,
                                frame.payload,
                            )
                        except Exception as exc:  # noqa: BLE001 — one bad frame must not kill the channel
                            log.warning(
                                "fed RTC media frame dropped from %s: %s",
                                self.instance_id,
                                exc,
                            )
                            continue
        except asyncio.CancelledError:
            raise
        except rtc.ConnectionClosedError:
            pass
        except rtc.RTCError as exc:
            log.debug("fed RTC media recv loop to %s ended: %s", self.instance_id, exc)
        log.info("fed RTC media channel closed to %s", self.instance_id)
        self._media_open.clear()

    async def _drain_app_channel(self, channel) -> None:
        """Consume inbound binary app frames on ``fed-app-v1``.

        Mirrors :meth:`_drain_media_channel` but the wire carries app-to-app
        messages (:mod:`app_framing`). SCTP preserves message boundaries so
        one ``send`` is normally one whole frame; the running ``_app_buf``
        nonetheless tolerates a peer that coalesced/split frames across
        messages. Each ``APP_MSG`` frame's header (the signed envelope) +
        payload (encrypted app message) is handed to ``_app_inbound`` for
        §24.11 re-validation — nothing is trusted here on the strength of
        the channel alone.
        """
        try:
            await channel.wait_open()
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning(
                "fed RTC app channel never opened to %s: %s",
                self.instance_id,
                exc,
            )
            return
        log.info("fed RTC app channel open to %s", self.instance_id)
        self._app_open.set()
        try:
            async for msg in channel:
                if isinstance(msg, str):
                    # A string on the binary app channel is a protocol
                    # violation; coerce so framing rejects it cleanly.
                    raw = msg.encode("utf-8")
                elif isinstance(msg, (bytes, bytearray)):
                    raw = bytes(msg)
                else:
                    raw = bytes(msg)
                self._app_buf.extend(raw)
                try:
                    frames, leftover = app_framing.iter_complete_frames(
                        bytes(self._app_buf),
                    )
                except app_framing.AppFramingError as exc:
                    log.warning(
                        "fed RTC app frame from %s malformed (%s) — resetting buffer",
                        self.instance_id,
                        exc,
                    )
                    self._app_buf.clear()
                    continue
                self._app_buf = bytearray(leftover)
                for frame in frames:
                    if frame.frame_type != app_framing.FRAME_TYPE_APP_MSG:
                        # Forward-compat: a newer peer may ship frame
                        # types we don't know yet — skip, don't error.
                        log.debug(
                            "fed RTC app: skipping unknown frame_type=%d from %s",
                            frame.frame_type,
                            self.instance_id,
                        )
                        continue
                    if self._app_inbound is not None:
                        try:
                            await self._app_inbound(
                                self.instance_id,
                                frame.header,
                                frame.payload,
                            )
                        except Exception as exc:  # noqa: BLE001 — one bad frame must not kill the channel
                            log.warning(
                                "fed RTC app frame dropped from %s: %s",
                                self.instance_id,
                                exc,
                            )
                            continue
        except asyncio.CancelledError:
            raise
        except rtc.ConnectionClosedError:
            pass
        except rtc.RTCError as exc:
            log.debug("fed RTC app recv loop to %s ended: %s", self.instance_id, exc)
        log.info("fed RTC app channel closed to %s", self.instance_id)
        self._app_open.clear()

    # ─── Sending ──────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """Whether the DataChannel is currently open."""
        return self._open.is_set() and not self._closed

    def needs_rehandshake(self, current_generation: int) -> bool:
        """Whether this peer should be torn down and rebuilt.

        True when the peer has not managed to open its DataChannel and
        at least one rebuild trigger applies. Today there are two:

        * its PeerConnection was built under a stale ICE-server list
          (``ice_generation`` behind the transport's), meaning it may be
          stuck STUN-only while a TURN relay is now available;
        * it is sitting without a usable PeerConnection waiting for the
          remote to (re-)offer (:attr:`_starved_awaiting_offer`) —
          either :meth:`_restart_handshake` tore our starved answerer PC
          down, or candidates were dropped before any PeerConnection
          existed. The latter rescues the stub peer ``on_rtc_ice``
          creates for candidates that overtook the OFFER: if the OFFER
          never lands, the stub has no PeerConnection at all, yet
          ``send()`` sees ``peer is not None`` and would skip the
          rebuild forever — a permanent HTTPS-only black hole for that
          peer.

        Deliberately NOT the raw drop count: that is scoped to one
        PeerConnection and stays set on a connection that is negotiating
        perfectly well, so reading it here retired healthy peers every
        outbox poll.

        A peer that is already open is never rebuilt: it works, and
        renegotiating a live DataChannel buys nothing.

        Known gap — a peer with ``_closed=True`` is never rebuilt from
        here, and only the FAILED path removes it from ``_peers``.
        ``_closed`` is also set when the PC reaches CLOSED and when
        :meth:`_drain_channel`'s recv loop simply ends on a normal
        channel close; neither evicts. Such a peer stays in ``_peers``
        with ``_closed=True``, so this predicate (which requires ``not
        self._closed``) stays False and the transport's ``peer is None``
        rebuild branch cannot fire either — permanent HTTPS fallback for
        the life of the process. Pre-dates the ICE-generation work; a
        fix belongs in its own change.
        """
        return (
            not self.is_ready
            and not self._closed
            and (
                self.ice_generation < current_generation or self._starved_awaiting_offer
            )
        )

    @property
    def is_media_ready(self) -> bool:
        """Whether the binary media DataChannel is currently open."""
        return (
            self._media_open.is_set()
            and self._media_channel is not None
            and not self._closed
        )

    @property
    def is_app_ready(self) -> bool:
        """Whether the binary app DataChannel is currently open."""
        return (
            self._app_open.is_set()
            and self._app_channel is not None
            and not self._closed
        )

    async def send(self, envelope_dict: dict) -> bool:
        """Push a JSON frame over the DataChannel.

        Returns ``True`` on success, ``False`` if the channel isn't
        currently open or the send buffer is over the HWM (caller
        should fall back to HTTPS inbox). Dropping under backpressure is
        preferable to unbounded SCTP queueing.
        """
        if not self.is_ready or self._channel is None:
            return False
        buffered = self._channel.buffered_amount
        if buffered >= self._send_hwm:
            log.warning(
                "fed RTC peer %s: buffered %d ≥ HWM %d — dropping frame",
                self.instance_id,
                buffered,
                self._send_hwm,
            )
            return False
        try:
            await self._channel.send(orjson.dumps(envelope_dict))
            return True
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning("fed RTC send to %s failed: %s", self.instance_id, exc)
            return False

    async def send_media(self, header: bytes, payload: bytes) -> bool:
        """Push one binary media frame over ``fed-media-v1``.

        ``header`` is the signed federation envelope JSON (bytes,
        verbatim so the receiver re-validates the exact signed bytes);
        ``payload`` is the AES-256-GCM-encrypted chunk. Returns ``True``
        on success, ``False`` when the channel isn't open, the send
        buffer is over the HWM, or the SCTP layer rejects the frame —
        the caller then falls back to the JSON media path.
        """
        if not self.is_media_ready or self._media_channel is None:
            return False
        buffered = self._media_channel.buffered_amount
        if buffered >= self._send_hwm:
            log.warning(
                "fed RTC media peer %s: buffered %d ≥ HWM %d — dropping frame",
                self.instance_id,
                buffered,
                self._send_hwm,
            )
            return False
        try:
            frame = media_framing.encode(header, payload)
        except media_framing.MediaFramingError as exc:
            log.warning(
                "fed RTC media frame encode failed for %s: %s",
                self.instance_id,
                exc,
            )
            return False
        try:
            await self._media_channel.send(frame)
            return True
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning("fed RTC media send to %s failed: %s", self.instance_id, exc)
            return False

    async def send_app(self, header: bytes, payload: bytes) -> bool:
        """Push one binary app frame over ``fed-app-v1``.

        ``header`` is the signed federation envelope JSON (bytes,
        verbatim so the receiver re-validates the exact signed bytes);
        ``payload`` is the AES-256-GCM-sealed app message. Returns ``True``
        on success, ``False`` when the channel isn't open, the send
        buffer is over the HWM, or the SCTP layer rejects the frame —
        the caller then falls back to the JSON app path.
        """
        if not self.is_app_ready or self._app_channel is None:
            return False
        buffered = self._app_channel.buffered_amount
        if buffered >= self._send_hwm:
            log.warning(
                "fed RTC app peer %s: buffered %d ≥ HWM %d — dropping frame",
                self.instance_id,
                buffered,
                self._send_hwm,
            )
            return False
        try:
            frame = app_framing.encode(header, payload)
        except app_framing.AppFramingError as exc:
            log.warning(
                "fed RTC app frame encode failed for %s: %s",
                self.instance_id,
                exc,
            )
            return False
        try:
            await self._app_channel.send(frame)
            return True
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning("fed RTC app send to %s failed: %s", self.instance_id, exc)
            return False

    async def _close_pc(self) -> None:
        """Close and discard the current PeerConnection, resetting negotiation state.

        Idempotent: safe to call when ``_pc`` is already ``None``.
        Used by the polite-side rollback path in perfect negotiation to
        tear down our pending offer so we can accept the peer's instead.
        """
        if self._pc is not None:
            try:
                self._pc.close()
            except rtc.RTCError:
                pass
            self._pc = None
        self._channel = None
        self._media_channel = None
        self._media_open.clear()
        self._media_buf.clear()
        self._app_channel = None
        self._app_open.clear()
        self._app_buf.clear()
        # Wake any coroutines parked in ``add_ice_candidate`` on the OLD
        # event before swapping in a fresh one. Without this they sit on
        # the old (never-set) event until the 30-second buffer timeout
        # fires — re-creating exactly the dropped-candidate symptom
        # PR #371 fixed. Once they wake, they either find ``_pc is None``
        # and return, or hit the new PC's ``add_remote_candidate`` and
        # get harmlessly rejected by the existing ``RTCError`` guard.
        self._remote_description_applied.set()
        self._remote_description_applied = asyncio.Event()

    def close(self) -> None:
        """Close the underlying connection and mark the peer closed.

        ``pc.close()`` tears down the PeerConnection; aiolibdatachannel
        auto-cancels any tasks registered via ``pc.spawn_task`` so we
        don't need to track them ourselves.

        If the DataChannel had previously been open, schedules a
        ``PeerTransportChanged(transport='https')`` publication via the
        running event loop so listeners know the peer has fallen back to
        HTTPS inbox. No event is emitted if the channel never opened —
        the transport never flipped away from HTTPS in the first place.
        """
        self._closed = True
        self._open.clear()
        # Wake any ICE candidates parked in
        # :meth:`add_ice_candidate` so they unblock immediately and
        # see ``_closed`` instead of waiting out the 10 s timeout.
        self._remote_description_applied.set()
        if self._pc is not None:
            try:
                self._pc.close()
            except rtc.RTCError:
                pass
        self._pc = None
        self._channel = None
        self._media_channel = None
        self._media_open.clear()
        self._media_buf.clear()
        self._app_channel = None
        self._app_open.clear()
        self._app_buf.clear()
        if self._published_open and self._bus is not None and self._loop is not None:
            self._published_open = False
            # Stash the task on the instance so asyncio doesn't garbage-
            # collect it before the publish coro runs. ``create_task``
            # only holds a weak ref via the loop's task set, so a
            # never-awaited handle can be reclaimed mid-flight and the
            # publish silently drops.
            self._teardown_task = self._loop.create_task(
                self._bus.publish(
                    PeerTransportChanged(
                        instance_id=self.instance_id,
                        transport="https",
                    ),
                ),
                name=f"fed-rtc-teardown[{self.instance_id}]",
            )


# ─── Facade ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class _TransportSendResult:
    """What :meth:`FederationTransport.send` returns to the caller."""

    ok: bool
    via: str  # "rtc" | "https" | "gfs_relay"
    status_code: int | None = None
    error: str | None = None


class FederationTransport:
    """Route outbound federation envelopes over RTC when possible.

    Wiring: construct with the instance's own id, a HTTPS inbox transport,
    and a callback used to dispatch the three ``FEDERATION_RTC_*``
    signalling events through :class:`FederationService.send_event`
    (which is the same signed HTTPS-inbox path used for everything else).
    """

    __slots__ = (
        "_own_instance_id",
        "_https_inbox",
        "_gfs_relay",
        "_signaling_send",
        "_ice_servers",
        "_peers",
        "_lock",
        "_inbound_handler",
        "_media_inbound_handler",
        "_app_inbound_handler",
        "_bus",
        "_rtc_suppressed_until",
        "_rtc_failure_count",
        "_rtc_retry_backoff_base_s",
        "_rtc_retry_backoff_max_s",
        "_rtc_jitter",
        "_ice_generation",
        "_ice_primed",
        "_ice_prime_timeout_s",
        "_last_retire_at",
        "_closing",
    )

    def __init__(
        self,
        *,
        own_instance_id: str,
        https_inbox: HttpsInboxTransport,
        gfs_relay: "GfsRelayTransport | None" = None,
        signaling_send: Callable[
            [str, FederationEventType, dict], Awaitable[DeliveryResult]
        ],
        ice_servers: list[dict] | None = None,
        inbound_handler: Callable[[str, bytes], Awaitable[dict]] | None = None,
        media_inbound_handler: _MediaInboundCallback | None = None,
        app_inbound_handler: _AppInboundCallback | None = None,
        bus: "EventBus | None" = None,
        rtc_retry_backoff_base_s: float = RTC_RETRY_BACKOFF_BASE_S,
        rtc_retry_backoff_max_s: float = RTC_RETRY_BACKOFF_MAX_S,
        ice_prime_timeout_s: float = ICE_PRIME_TIMEOUT_S,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._https_inbox = https_inbox
        #: Third transport tier — the connection-server envelope relay,
        #: the ONLY way to reach a household seated from an invite link
        #: (:data:`InstanceSource.SPACE_SESSION`): that pair deliberately
        #: never exchanged an address, so there is nothing for RTC to
        #: signal towards and nothing for the HTTPS inbox to POST to.
        #: ``None`` in tests and in builds with no connection server.
        self._gfs_relay = gfs_relay
        self._signaling_send = signaling_send
        self._ice_servers = ice_servers or []
        self._peers: dict[str, _RtcPeer] = {}
        self._lock = asyncio.Lock()
        # Callback for inbound DataChannel frames → §24.11 pipeline.
        # Signature: ``async (instance_id, raw_body) -> dict``.
        # Attached by FederationService after construction.
        self._inbound_handler = inbound_handler
        # Callback for inbound binary media frames on ``fed-media-v1`` →
        # §24.11 pipeline + per-chunk decrypt. Signature:
        # ``async (instance_id, header_bytes, payload_bytes) -> None``.
        self._media_inbound_handler = media_inbound_handler
        # Callback for inbound binary app frames on ``fed-app-v1`` →
        # §24.11 pipeline + app-message decrypt. Signature:
        # ``async (instance_id, header_bytes, payload_bytes) -> None``.
        self._app_inbound_handler = app_inbound_handler
        self._bus = bus
        #: ``instance_id`` → monotonic-time UNTIL we'll attempt
        #: another RTC handshake to that peer. Set when a peer's
        #: PeerConnection transitions to FAILED (e.g. ICE pair-checks
        #: never succeed — typical real-world failure when neither
        #: side has TURN and they're both behind symmetric NAT).
        #: Without this, every ``send()`` would rebuild the handshake
        #: immediately after the failed one was evicted, hammering
        #: the failing path on every outbound envelope. The window
        #: grows geometrically with the peer's consecutive-failure
        #: count — see :data:`RTC_RETRY_BACKOFF_BASE_S` — so a
        #: transient failure costs a minute while a peer that is
        #: genuinely unreachable settles at the 6 h ceiling.
        self._rtc_suppressed_until: dict[str, float] = {}
        #: ``instance_id`` → consecutive FAILED PeerConnections, the
        #: exponent driving the backoff above. Cleared on a successful
        #: channel open (:meth:`_on_peer_open`) and on any ICE-server
        #: push (:meth:`set_ice_servers`).
        self._rtc_failure_count: dict[str, int] = {}
        self._rtc_retry_backoff_base_s = rtc_retry_backoff_base_s
        self._rtc_retry_backoff_max_s = rtc_retry_backoff_max_s
        # ``rng`` is injectable for deterministic tests, mirroring
        # ``OutboxProcessor``. Default uses ``random.random`` — uniform
        # in [0, 1).
        self._rtc_jitter = rng or random.random
        #: Bumped by :meth:`set_ice_servers` whenever the effective
        #: ICE-server list actually changes. Every ``_RtcPeer`` records
        #: the value it was built under, so peers stuck on an older list
        #: are identifiable (and rebuildable) after the fact.
        self._ice_generation = 0
        #: Released by :meth:`mark_ice_primed` once the platform has
        #: pushed (or declined to push) an ICE-server list. The first
        #: outbound handshake waits on it — see
        #: :data:`ICE_PRIME_TIMEOUT_S`.
        self._ice_primed = asyncio.Event()
        self._ice_prime_timeout_s = ice_prime_timeout_s
        #: ``instance_id`` → monotonic time of its last retirement, the
        #: rate FLOOR for :meth:`send`'s retire-and-rebuild — see
        #: :meth:`_retire_rate_limited`. Pruned by :meth:`_on_peer_open`.
        self._last_retire_at: dict[str, float] = {}
        #: Set once by :meth:`close_all`. Checked by
        #: :meth:`_ensure_handshake` so a ``send()`` parked on the
        #: ICE-primed gate across ``on_cleanup`` cannot resume after
        #: shutdown and build a PeerConnection nobody will ever close.
        self._closing = False

    def _current_ice_servers(self) -> list[dict]:
        """Current ICE-server list, read at PeerConnection-construction
        time rather than snapshotted when the peer was created — so a
        peer built before ``set_ice_servers`` lands still gets TURN.
        """
        return self._ice_servers

    def set_ice_servers(self, servers: list[dict]) -> None:
        """Update the ICE-server list used for peer handshakes.

        Already-connected peers are left alone — their DataChannel is
        open, so it works and renegotiating it buys nothing. Peers that
        have NOT managed to connect are retired and rebuilt on their
        next :meth:`send`, which is what makes a late-arriving TURN list
        reach them: under haos the Cloudflare credentials land after
        ``HaIceServerSync`` has run, which can be well after the boot
        outbox drain already built those peers STUN-only. Before this,
        such a peer never connected for the lifetime of the process.

        EVERY call — changed list or not — clears all current
        ``_rtc_suppressed_until`` entries AND every peer's accumulated
        ``_rtc_failure_count``. Pushing an ICE-server list is the
        operator's "I changed something, try again" signal (e.g. they
        just deployed a TURN server), and it is the only lever in the
        codebase that clears a suppression early. Clearing the window
        without clearing the count would half-honour that signal: the
        next attempt would fire immediately but, if it failed, land
        straight back on a 6 h backoff instead of the 60 s base — the
        operator's fix would get one shot, then a six-hour silence.
        Gating either clear on a content change would mean a peer
        suppressed after a FAILED PC stays suppressed whenever HA
        returns byte-identical credentials — which is the common case,
        since ``HaIceServerSync`` re-applies the same list on its 24 h
        poll.

        Only the generation bump and the list store are gated on a real
        content change: HA re-pushes an equal-content list on every
        refresh, and bumping the generation for that would retire every
        unconnected peer on a timer for no benefit. Equality is on
        content (urls / username / credential), so a reordered or
        freshly-constructed but equal list still counts as unchanged.
        """
        # Any push — changed or not — means the list is now as good as
        # it is going to get, so release the first-handshake gate before
        # the unchanged-list early return below.
        self.mark_ice_primed()
        incoming = list(servers or [])
        if self._rtc_suppressed_until:
            log.info(
                "fed RTC: clearing %d suppressed peer(s) after ICE-server "
                "list push — next send will retry the handshake",
                len(self._rtc_suppressed_until),
            )
            self._rtc_suppressed_until.clear()
        self._rtc_failure_count.clear()
        if _ice_fingerprint(incoming) == _ice_fingerprint(self._ice_servers):
            log.debug(
                "fed RTC: ICE-server list unchanged (%d entr(ies)) — "
                "keeping generation %d",
                len(incoming),
                self._ice_generation,
            )
            return
        self._ice_servers = incoming
        self._ice_generation += 1
        log.info(
            "fed RTC: ICE-server list updated (%d entr(ies)); generation "
            "is now %d — unconnected peers rebuild on their next send",
            len(incoming),
            self._ice_generation,
        )

    def mark_ice_primed(self) -> None:
        """Release the first-handshake ICE gate.

        Idempotent and safe to call before or after any handshake:
        setting an already-set :class:`asyncio.Event` is a no-op and the
        event is never cleared again. Called by :meth:`set_ice_servers`
        on every push, and directly by a platform that knows no list is
        coming (so it need not pay :data:`ICE_PRIME_TIMEOUT_S`).
        """
        self._ice_primed.set()

    # ─── Outbound ─────────────────────────────────────────────────────────

    async def send(
        self,
        *,
        instance: RemoteInstance,
        envelope_dict: dict,
    ) -> _TransportSendResult:
        """Deliver ``envelope_dict`` to *instance*, RTC first, inbox on fallback.

        The envelope is unchanged across transports — the signature and
        AES-256-GCM payload are already baked in.
        """
        # A household seated from an invite link has no address AT ALL,
        # by design (§D2b): ``remote_inbox_url`` is empty and no RTC
        # signalling can reach it, because signalling itself travels over
        # the peer relationship this pair does not have. It rides the
        # connection-server relay or it goes nowhere — never a fall-
        # through to an HTTPS POST at the empty string.
        if instance.source is InstanceSource.SPACE_SESSION:
            if self._gfs_relay is None:
                log.warning(
                    "fed send to %s needs the connection-server relay, "
                    "which is not wired on this host",
                    instance.id,
                )
                return _TransportSendResult(
                    ok=False,
                    via="gfs_relay",
                    error="gfs_relay_unavailable",
                )
            ok, status = await self._gfs_relay.send(
                instance=instance,
                envelope_dict=envelope_dict,
            )
            return _TransportSendResult(
                ok=ok,
                via="gfs_relay",
                status_code=status,
                error=None if ok else "gfs_relay_failed",
            )

        peer = self._peers.get(instance.id)
        if peer is not None and peer.is_ready:
            try:
                sent = await peer.send(envelope_dict)
            except Exception as exc:
                log.warning(
                    "fed RTC send to %s raised (%s) — falling back to HTTPS inbox",
                    instance.id,
                    exc,
                )
                sent = False
            if sent:
                return _TransportSendResult(ok=True, via="rtc")
            log.debug(
                "fed RTC send to %s not ready — falling back to HTTPS inbox",
                instance.id,
            )

        # A peer whose PeerConnection was built under a stale ICE-server
        # list (typically before the TURN credentials landed) is retired
        # here so the ``peer is None`` branch below rebuilds it and the
        # fresh PeerConnection picks up TURN. Deliberately lazy rather
        # than eager from :meth:`set_ice_servers`: that method is sync
        # (called from ``FederationService.set_ice_servers`` and admin
        # routes), so spawning a rebuild task there would need a
        # strong-ref slot. Doing it here reuses the one existing rebuild
        # seam, and the outbox's 5 s poll fires it within seconds for
        # exactly the peers that have traffic. The rebuilt peer carries
        # the current generation, so the predicate is immediately false
        # — no rebuild loop.
        #
        # Accepted residual risk: retiring a peer with an OFFER already
        # in flight means the remote's ANSWER to that *previous* offer
        # can land after the replacement peer has sent its own offer.
        # The per-peer guards (``_expected_answer_from``, the
        # HAVE_LOCAL_OFFER signaling-state check) cannot tell two
        # successive offers from the same side apart, so the replacement
        # may apply the stale answer, go STABLE, reject the real answer
        # and end in FAILED. Distinguishing them needs a handshake nonce
        # on the wire — a protocol change requiring a ``proto_version``
        # bump. Accepted because the post-failure suppression is now
        # graduated from 60 s rather than flat at 24 h, so the FIRST
        # poisoned rebuild costs a minute, not a day.
        if peer is not None and peer.needs_rehandshake(self._ice_generation):
            if self._retire_rate_limited(instance.id):
                # Floor hit: leave the peer exactly where it is and fall
                # through to HTTPS. Deliberately NOT a suppression —
                # nothing failed, we simply decline to mint another
                # PeerConnection this soon.
                log.debug(
                    "fed RTC peer %s wants a rebuild but was retired less "
                    "than %.0f s ago — using the HTTPS inbox this time",
                    instance.id,
                    self._rtc_retry_backoff_base_s,
                )
            else:
                await self._retire_peer(instance.id, peer)
                peer = None

        # Kick off (or re-kick) the handshake lazily on first use.
        # The ``_rtc_suppressed_until`` gate skips the rebuild for the
        # peer's current graduated backoff window after a FAILED PC — see
        # :meth:`_evict_peer`. Without it, every outbound envelope
        # rebuilds the handshake and re-fails, hammering the failing
        # path indefinitely.
        if peer is None and not self._rtc_suppressed(instance.id):
            await self._ensure_handshake(instance)

        ok, status = await self._https_inbox.send(
            instance=instance,
            envelope_dict=envelope_dict,
        )
        return _TransportSendResult(
            ok=ok,
            via="https",
            status_code=status,
            error=None if ok else "https_inbox_failed",
        )

    async def send_media(
        self,
        *,
        instance: RemoteInstance,
        header_dict: dict,
        payload_bytes: bytes,
    ) -> bool:
        """Deliver one binary media frame over ``fed-media-v1``.

        ``header_dict`` is the signed federation envelope (serialised to
        JSON verbatim so the receiver re-validates the exact signed
        bytes); ``payload_bytes`` is the AES-256-GCM-encrypted chunk.

        Returns ``True`` only when the media channel was open and the
        frame was queued. Returns ``False`` for every other case (no
        peer, channel not yet negotiated, over HWM, send error) — the
        caller (:meth:`FederationService.send_media_chunk`) then falls
        back to the JSON media path, which also kicks the RTC handshake
        for next time. This method never initiates the handshake itself:
        media is opportunistic over an already-open channel.
        """
        peer = self._peers.get(instance.id)
        if peer is None or not peer.is_media_ready:
            return False
        try:
            return await peer.send_media(orjson.dumps(header_dict), payload_bytes)
        except Exception as exc:  # noqa: BLE001 — defensive: any RTC error → fallback
            log.warning(
                "fed RTC media send to %s raised (%s) — falling back to JSON",
                instance.id,
                exc,
            )
            return False

    def is_media_ready(self, instance_id: str) -> bool:
        peer = self._peers.get(instance_id)
        return peer is not None and peer.is_media_ready

    async def send_app(
        self,
        *,
        instance: RemoteInstance,
        header_dict: dict,
        payload_bytes: bytes,
    ) -> bool:
        """Deliver one binary app frame over ``fed-app-v1``.

        ``header_dict`` is the signed federation envelope (serialised to
        JSON verbatim so the receiver re-validates the exact signed
        bytes); ``payload_bytes`` is the AES-256-GCM-sealed app message.

        Returns ``True`` only when the app channel was open and the
        frame was queued. Returns ``False`` for every other case (no
        peer, channel not yet negotiated, over HWM, send error) — the
        caller then falls back to the JSON app path. This method never
        initiates the handshake itself: app delivery is opportunistic
        over an already-open channel.
        """
        peer = self._peers.get(instance.id)
        if peer is None or not peer.is_app_ready:
            return False
        try:
            return await peer.send_app(orjson.dumps(header_dict), payload_bytes)
        except Exception as exc:  # noqa: BLE001 — defensive: any RTC error → fallback
            log.warning(
                "fed RTC app send to %s raised (%s) — falling back to JSON",
                instance.id,
                exc,
            )
            return False

    def is_app_ready(self, instance_id: str) -> bool:
        peer = self._peers.get(instance_id)
        return peer is not None and peer.is_app_ready

    async def _ensure_handshake(self, instance: RemoteInstance) -> None:
        """Build a peer and start the offerer-side handshake.

        Gated on the ICE-server list being primed: see
        :data:`ICE_PRIME_TIMEOUT_S` for why the first offer waits, and
        why the wait is bounded.

        The wait is deliberately OUTSIDE ``self._lock`` — holding the
        registry lock for the whole timeout would block
        :meth:`on_rtc_offer`, :meth:`on_rtc_ice` and
        :meth:`_retire_peer` for every OTHER peer, turning a
        boot-ordering optimisation into a transport-wide stall.
        """
        if not self._ice_primed.is_set():
            try:
                await asyncio.wait_for(
                    self._ice_primed.wait(),
                    self._ice_prime_timeout_s,
                )
            except asyncio.TimeoutError:
                # Latch: set the event so only the FIRST peer ever pays
                # the wait. A platform that never primes costs the bound
                # once per process, not once per peer. Safe only because
                # the ICE-generation / retire mechanism still carries a
                # later list to every peer that has not connected yet.
                log.warning(
                    "fed RTC: no ICE-server list after %.0fs — handshaking "
                    "with the current list and latching the gate open; a "
                    "later push still rebuilds unconnected peers via the "
                    "ICE generation bump",
                    self._ice_prime_timeout_s,
                )
                self._ice_primed.set()
        async with self._lock:
            # The gate above is awaited OUTSIDE the lock, so a
            # ``close_all()`` during ``on_cleanup`` can land while we are
            # parked on it. Resuming afterwards would insert a peer into
            # the dict ``close_all`` already drained, build a
            # PeerConnection and POST an OFFER during shutdown — and
            # nothing would ever close it.
            if self._closing:
                log.debug(
                    "fed RTC: transport closing — dropping the handshake "
                    "for %s instead of resurrecting it",
                    instance.id,
                )
                return
            if instance.id in self._peers:
                return
            peer = _RtcPeer(
                instance_id=instance.id,
                ice_provider=self._current_ice_servers,
                ice_generation=self._ice_generation,
                signaling=self._signaling_factory(instance.id),
                inbound=self._inbound_factory(instance.id),
                bus=self._bus,
                polite=self._own_instance_id > instance.id,
                on_failed=self._evict_peer,
                on_open=self._on_peer_open,
                media_inbound=self._media_inbound_handler,
                app_inbound=self._app_inbound_handler,
            )
            self._peers[instance.id] = peer
        # Release lock before the network call — the signalling round
        # trip should not block peer-registry lookups on other tasks.
        try:
            await peer.start_offer()
        except Exception as exc:
            log.warning(
                "fed RTC handshake start failed for %s: %s",
                instance.id,
                exc,
            )
        # The lock is released across that await, so ``close_all()`` may
        # have run meanwhile. It closes whatever it finds in ``_peers``,
        # so this is belt-and-braces — but it is what makes leaking a
        # PeerConnection past shutdown impossible rather than merely
        # unlikely.
        if self._closing:
            async with self._lock:
                if self._peers.get(instance.id) is peer:
                    del self._peers[instance.id]
            peer.close()

    def _signaling_factory(
        self,
        instance_id: str,
    ) -> Callable[[FederationEventType, dict], Awaitable[None]]:
        async def _signal(et: FederationEventType, payload: dict) -> None:
            await self._signaling_send(instance_id, et, payload)

        return _signal

    def _inbound_factory(self, instance_id: str) -> _InboundCallback:
        async def _on_inbound(envelope: dict) -> None:
            log.debug(
                "fed RTC frame received from %s (msg_id=%s, type=%s)",
                instance_id,
                envelope.get("msg_id"),
                envelope.get("event_type"),
            )
            # Feed inbound DataChannel frames through the same §24.11
            # validation pipeline the HTTPS-inbox path uses — but with the
            # instance resolved by instance_id (already known from the
            # peer connection) instead of inbox_id.
            if self._inbound_handler is not None:
                raw = orjson.dumps(envelope)
                try:
                    await self._inbound_handler(instance_id, raw)
                except ValueError as exc:
                    log.warning(
                        "fed RTC inbound rejected from %s: %s",
                        instance_id,
                        exc,
                    )

        return _on_inbound

    # ─── Inbound signalling ──────────────────────────────────────────────

    async def on_rtc_offer(
        self,
        *,
        from_instance: str,
        payload: dict,
    ) -> None:
        """Handle a ``FEDERATION_RTC_OFFER`` from a paired peer."""
        async with self._lock:
            peer = self._peers.get(from_instance)
            if peer is None:
                peer = _RtcPeer(
                    instance_id=from_instance,
                    ice_provider=self._current_ice_servers,
                    ice_generation=self._ice_generation,
                    signaling=self._signaling_factory(from_instance),
                    inbound=self._inbound_factory(from_instance),
                    bus=self._bus,
                    polite=self._own_instance_id > from_instance,
                    on_failed=self._evict_peer,
                    on_open=self._on_peer_open,
                    media_inbound=self._media_inbound_handler,
                    app_inbound=self._app_inbound_handler,
                )
                self._peers[from_instance] = peer
        sdp = str(payload.get("sdp") or "")
        if not sdp:
            return
        await peer.accept_offer(sdp=sdp, from_instance=from_instance)

    async def on_rtc_answer(
        self,
        *,
        from_instance: str,
        payload: dict,
    ) -> None:
        """Handle a ``FEDERATION_RTC_ANSWER`` (S-14 origin-guarded)."""
        peer = self._peers.get(from_instance)
        if peer is None:
            log.warning(
                "RTC answer from %s ignored — no pending peer",
                from_instance,
            )
            return
        sdp = str(payload.get("sdp") or "")
        if not sdp:
            return
        await peer.apply_answer(sdp=sdp, from_instance=from_instance)

    async def on_rtc_ice(
        self,
        *,
        from_instance: str,
        payload: dict,
    ) -> None:
        """Handle a trickled ``FEDERATION_RTC_ICE`` candidate.

        Creates a buffering :class:`_RtcPeer` stub if no peer exists
        yet for ``from_instance`` so an ICE envelope that overtook the
        ``FEDERATION_RTC_OFFER`` doesn't end up silently dropped (which
        previously stranded ICE with no remote candidates, expiring
        the connectivity timer and failing the DataChannel). The stub
        peer just queues candidates inside
        :meth:`_RtcPeer.add_ice_candidate`; ``on_rtc_offer`` reuses the
        same dict slot, builds the PeerConnection, and once
        ``set_remote_description`` lands the buffered candidates flush
        in :meth:`_RtcPeer.add_ice_candidate`. The §24.11 pipeline has
        already authenticated the sender by signature, so the stub
        peer can't be a DoS vector against unpaired instance_ids.
        """
        async with self._lock:
            peer = self._peers.get(from_instance)
            if peer is None:
                peer = _RtcPeer(
                    instance_id=from_instance,
                    ice_provider=self._current_ice_servers,
                    ice_generation=self._ice_generation,
                    signaling=self._signaling_factory(from_instance),
                    inbound=self._inbound_factory(from_instance),
                    bus=self._bus,
                    polite=self._own_instance_id > from_instance,
                    on_failed=self._evict_peer,
                    on_open=self._on_peer_open,
                    media_inbound=self._media_inbound_handler,
                    app_inbound=self._app_inbound_handler,
                )
                self._peers[from_instance] = peer
        await peer.add_ice_candidate(
            candidate=str(payload.get("candidate") or ""),
            sdp_mid=str(payload.get("sdp_mid") or "0"),
        )

    async def _evict_peer(self, instance_id: str) -> None:
        """Drop a ``_RtcPeer`` after its underlying PC entered FAILED.

        Without this hook a failed PC stayed in ``_peers`` forever and
        every subsequent ``send()`` short-circuited to HTTPS fallback
        without trying to rebuild the channel. Removing the entry lets
        the next outbound envelope retry — but ALSO records a
        suppression timestamp so the rebuild is throttled. Otherwise
        every outbound envelope to a peer behind symmetric NAT would
        rebuild the handshake and re-fail, hammering the failing path
        on every send (the outbox polls every 5 s).

        The window grows geometrically with the peer's consecutive
        failure count — :data:`RTC_RETRY_BACKOFF_BASE_S` upwards, jittered
        and capped at :data:`RTC_RETRY_BACKOFF_MAX_S` — so a transient
        failure self-heals in about a minute while a genuinely
        unreachable peer costs at most a handful of attempts per day.
        The count is reset by :meth:`_on_peer_open` and by
        :meth:`set_ice_servers`.

        Bound to ``_RtcPeer(on_failed=...)`` at instantiation.
        """
        async with self._lock:
            peer = self._peers.pop(instance_id, None)
        if peer is None:
            return
        failures = self._rtc_failure_count.get(instance_id, 0) + 1
        self._rtc_failure_count[instance_id] = failures
        delay = self._retry_delay(failures)
        self._rtc_suppressed_until[instance_id] = time.monotonic() + delay
        log.info(
            "fed RTC peer %s evicted after FAILED state (failure #%d); "
            "suppressing rebuild for ~%.0f s (push fresh ICE servers to "
            "override)",
            instance_id,
            failures,
            delay,
        )
        try:
            peer.close()
        except Exception as exc:  # noqa: BLE001 — defensive on shutdown path
            log.debug("fed RTC peer %s close after evict: %s", instance_id, exc)

    def _retry_delay(self, failures: int) -> float:
        """Jittered suppression window for the *failures*-th consecutive
        FAILED PeerConnection (1-based).

        ``BASE * FACTOR ** (failures - 1)``, clamped at
        :data:`RTC_RETRY_BACKOFF_MAX_S`, then scaled by a factor drawn
        uniformly from ``[1 - RTC_RETRY_JITTER, 1 + RTC_RETRY_JITTER]``
        so a household whose peers all failed together does not retry
        them in lockstep. The jitter source is injectable (``rng``) so
        tests are deterministic.
        """
        exponent = max(failures, 1) - 1
        nominal = min(
            self._rtc_retry_backoff_base_s * (RTC_RETRY_BACKOFF_FACTOR**exponent),
            self._rtc_retry_backoff_max_s,
        )
        # Convert the ``[0, 1)`` sample into ``[-1, 1]`` then scale by
        # RTC_RETRY_JITTER — same shape as ``OutboxProcessor._backoff``.
        signed = (self._rtc_jitter() * 2.0) - 1.0
        return max(0.0, nominal * (1.0 + signed * RTC_RETRY_JITTER))

    async def _on_peer_open(self, instance_id: str) -> None:
        """Forget *instance_id*'s failure history — its channel opened.

        Bound to ``_RtcPeer(on_open=...)``, fired on the exactly-once
        channel-open edge. A peer that eventually connects must start
        its NEXT failure from the 60 s base rather than from whatever
        ceiling its past failures had climbed to; otherwise one bad
        afternoon permanently pins a now-healthy peer at a 6 h backoff.

        Deliberately dict-mutation only — no ``close()``, no ``cancel()``,
        no lock. It is awaited inline from :meth:`_RtcPeer._drain_channel`,
        a pc-spawned task, so anything that tore the PeerConnection down
        would cancel its own caller, and taking ``self._lock`` here could
        deadlock against a :meth:`send` / :meth:`_ensure_handshake` that
        holds it while the handshake completes.
        """
        self._rtc_failure_count.pop(instance_id, None)
        self._rtc_suppressed_until.pop(instance_id, None)
        # A peer that connected carries no retire history worth keeping:
        # leaving the stamp would rate-limit its next genuine retirement
        # against a timestamp from a different era.
        self._last_retire_at.pop(instance_id, None)

    def _retire_rate_limited(self, instance_id: str) -> bool:
        """Whether *instance_id* was retired too recently to retire again.

        A rate FLOOR, not a backoff: it stamps nothing, suppresses
        nothing, and never inflates the failure exponent — a skipped
        retire just leaves the peer in place and lets the envelope go
        out over the HTTPS inbox.

        It exists because :meth:`_retire_peer` deliberately records no
        suppression (retirement is our decision, not evidence the peer
        is unreachable), and ``peer.close()`` on that path pre-empts the
        FAILED → :meth:`_evict_peer` route that would otherwise stamp
        one. A paired peer that trickles ICE candidates while its ANSWER
        never lands — a congested outbox, or a hostile-but-authenticated
        peer — therefore flips ``needs_rehandshake`` roughly every
        :data:`ICE_BUFFER_TIMEOUT_S`, and every ``send()`` in between
        would mint a fresh PeerConnection (ICE gathering plus a TURN
        allocation) and POST a new OFFER, indefinitely. The outbox polls
        every 5 s, so nothing else throttles it.

        :data:`RTC_RETRY_BACKOFF_BASE_S` is the floor: the real case (an
        ICE-server list finally landing, which bumps the generation
        once) still rebuilds immediately, while the pathological loop is
        capped at one rebuild per minute.
        """
        last = self._last_retire_at.get(instance_id)
        if last is None:
            return False
        return (time.monotonic() - last) < self._rtc_retry_backoff_base_s

    async def _retire_peer(self, instance_id: str, peer: "_RtcPeer") -> None:
        """Drop *peer* — the exact object the caller decided to rebuild.

        Deliberately distinct from :meth:`_evict_peer`, which reacts to
        the peer's PeerConnection entering FAILED. Retirement is OUR
        decision (the ICE-server list moved on under a peer that never
        connected), not evidence that the peer is unreachable — so it
        must NOT stamp ``_rtc_suppressed_until`` and must NOT touch any
        failure counter. Doing either would block the very rebuild this
        retirement exists to trigger — and, worse, inflate the exponent
        so a later genuine failure starts hours deep — re-creating the
        outage where a haos peer stayed STUN-only forever.

        Identity-scoped on purpose: we pop only while the mapping still
        holds *this* object. Two concurrent :meth:`send` calls to the
        same instance (a live send overlapping an outbox redelivery)
        both capture peer P1 and both see ``needs_rehandshake`` — there
        is no await between the ``_peers.get()`` and the check, so the
        second caller cannot observe the first's decision. Caller A
        retires P1, ``_ensure_handshake`` inserts P2, then releases the
        lock to await ``start_offer()``. If caller B then popped by id
        it would close P2 mid-handshake (``close()`` nulls ``_pc``
        under A's running offer), producing handshake churn and a
        suppressed FAILED PC — the exact outage this code exists to
        fix. A mismatch means somebody already rebuilt it, so we return
        without closing anything.
        """
        async with self._lock:
            if self._peers.get(instance_id) is not peer:
                return
            del self._peers[instance_id]
        # Stamped only on a retirement that actually happened, so the
        # floor in :meth:`_retire_rate_limited` measures rebuilds rather
        # than attempts.
        self._last_retire_at[instance_id] = time.monotonic()
        log.info(
            "fed RTC peer %s retired for rebuild under ICE generation %d",
            instance_id,
            self._ice_generation,
        )
        try:
            peer.close()
        except Exception as exc:  # noqa: BLE001 — defensive on teardown path
            log.debug("fed RTC peer %s close after retire: %s", instance_id, exc)

    def _rtc_suppressed(self, instance_id: str) -> bool:
        """Whether the RTC rebuild for ``instance_id`` is currently
        suppressed after a FAILED PC. Side effect: prunes the entry
        from the dict once its TTL has elapsed so the next call
        runs the normal handshake path. Operator can clear
        suppressions in bulk via any :meth:`set_ice_servers` push,
        whether or not the list content actually changed.
        """
        until = self._rtc_suppressed_until.get(instance_id)
        if until is None:
            return False
        if time.monotonic() >= until:
            self._rtc_suppressed_until.pop(instance_id, None)
            return False
        return True

    # ─── Inspection + shutdown ───────────────────────────────────────────

    def is_ready(self, instance_id: str) -> bool:
        peer = self._peers.get(instance_id)
        return peer is not None and peer.is_ready

    def peer_count(self) -> int:
        return len(self._peers)

    async def close_peer(self, instance_id: str) -> None:
        peer = self._peers.pop(instance_id, None)
        if peer is not None:
            peer.close()

    async def close_all(self) -> None:
        """Close every peer and latch the transport shut.

        The latch is one-way: this runs on ``on_cleanup``, and a
        ``send()`` parked on the ICE-primed gate must not be able to
        rebuild a peer behind it — see :meth:`_ensure_handshake`.
        """
        self._closing = True
        for peer in list(self._peers.values()):
            peer.close()
        self._peers.clear()
