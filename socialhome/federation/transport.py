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
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiolibdatachannel as rtc
import orjson
from aiohttp import ClientTimeout

from ..domain.federation import DeliveryResult, FederationEventType, RemoteInstance

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
            log.warning(
                "HTTPS-inbox send to %s failed: %s",
                instance.id,
                exc,
            )
            return False, None


# ─── RTC peer ──────────────────────────────────────────────────────────────

# The frame format on the DataChannel is the same envelope dict the
# HTTPS inbox transport would have POSTed — serialised as UTF-8 JSON with
# orjson for consistency with the HTTPS-inbox path.
_InboundCallback = Callable[[dict], Awaitable[None]]


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


class _RtcPeer:
    """One DataChannel session for one paired peer."""

    __slots__ = (
        "instance_id",
        "_ice_servers",
        "_signaling",
        "_inbound",
        "_pc",
        "_channel",
        "_open",
        "_closed",
        "_loop",
        "_expected_answer_from",
        "_send_hwm",
    )

    def __init__(
        self,
        *,
        instance_id: str,
        ice_servers: list[dict] | None,
        signaling: Callable[[FederationEventType, dict], Awaitable[None]],
        inbound: _InboundCallback,
        send_hwm: int = SEND_HWM_BYTES,
    ) -> None:
        self.instance_id = instance_id
        self._ice_servers = ice_servers or []
        self._signaling = signaling
        self._inbound = inbound
        self._pc: Any | None = None
        self._channel: Any | None = None
        self._open = asyncio.Event()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # S-14: on the offerer side we lock the answer origin to the
        # peer we invited. Mismatches are rejected with a warning.
        self._expected_answer_from: str | None = None
        self._send_hwm = send_hwm

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start_offer(self) -> None:
        """Initiate the SDP offer/answer handshake (offerer role)."""
        self._expected_answer_from = self.instance_id
        self._loop = asyncio.get_running_loop()
        self._pc = rtc.PeerConnection(_build_rtc_config(self._ice_servers))
        self._channel = await self._pc.create_data_channel(CHANNEL_LABEL)
        # Ask aiolibdatachannel to notify us once the buffered amount
        # drops below half the HWM — lets future refactors await
        # backpressure instead of polling. For now we just read
        # ``buffered_amount`` directly in ``send()``.
        self._channel.set_buffered_amount_low_threshold(self._send_hwm // 2)
        # Tasks bound to the pc: auto-cancelled on pc.close().
        self._pc.spawn_task(self._drain_channel(self._channel))
        self._pc.spawn_task(self._drain_ice())

        local = await self._pc.set_local_description("offer")
        await self._signaling(
            FederationEventType.FEDERATION_RTC_OFFER,
            {"sdp": local.sdp, "sdp_type": local.type},
        )

    async def accept_offer(self, *, sdp: str, from_instance: str) -> None:
        """Receive an SDP offer (answerer role) and reply with an answer."""
        self._expected_answer_from = None  # answerer, no outstanding offer
        self._loop = asyncio.get_running_loop()
        self._pc = rtc.PeerConnection(_build_rtc_config(self._ice_servers))

        self._pc.spawn_task(self._drain_incoming_channel())
        self._pc.spawn_task(self._drain_ice())

        await self._pc.set_remote_description(sdp, "offer")
        local = await self._pc.set_local_description("answer")
        await self._signaling(
            FederationEventType.FEDERATION_RTC_ANSWER,
            {"sdp": local.sdp, "sdp_type": local.type},
        )

    async def apply_answer(self, *, sdp: str, from_instance: str) -> bool:
        """Apply the peer's SDP answer to our pending offer.

        Returns ``True`` when accepted. Rejects (returns ``False``) if
        ``from_instance`` doesn't match the peer we sent the offer to —
        S-14 answer-origin guard.
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
        self._expected_answer_from = None
        if self._pc is not None:
            await self._pc.set_remote_description(sdp, "answer")
        return True

    async def add_ice_candidate(self, *, candidate: str, sdp_mid: str) -> None:
        if not candidate:
            return
        if self._pc is not None:
            await self._pc.add_remote_candidate(candidate, sdp_mid)

    # ─── Internal drain loops ─────────────────────────────────────────────

    async def _drain_ice(self) -> None:
        """Pump local ICE candidates out to the peer over signalling."""
        pc = self._pc
        assert pc is not None  # spawned from start_offer/accept_offer
        try:
            async for cand in pc.ice_candidates():
                await self._signaling(
                    FederationEventType.FEDERATION_RTC_ICE,
                    {"candidate": cand.candidate, "sdp_mid": cand.mid},
                )
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug("fed RTC ICE drain to %s ended: %s", self.instance_id, exc)

    async def _drain_incoming_channel(self) -> None:
        """Answerer path: wait for the provider's DataChannel to arrive."""
        pc = self._pc
        assert pc is not None  # spawned from accept_offer
        try:
            async for ch in pc.incoming_data_channels():
                if ch.label != CHANNEL_LABEL:
                    continue
                self._channel = ch
                ch.set_buffered_amount_low_threshold(self._send_hwm // 2)
                pc.spawn_task(self._drain_channel(ch))
                return
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

    # ─── Sending ──────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """Whether the DataChannel is currently open."""
        return self._open.is_set() and not self._closed

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

    def close(self) -> None:
        """Close the underlying connection and mark the peer closed.

        ``pc.close()`` tears down the PeerConnection; aiolibdatachannel
        auto-cancels any tasks registered via ``pc.spawn_task`` so we
        don't need to track them ourselves.
        """
        self._closed = True
        self._open.clear()
        if self._pc is not None:
            try:
                self._pc.close()
            except rtc.RTCError:
                pass
        self._pc = None
        self._channel = None


# ─── Facade ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class _TransportSendResult:
    """What :meth:`FederationTransport.send` returns to the caller."""

    ok: bool
    via: str  # "rtc" | "https"
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
        "_signaling_send",
        "_ice_servers",
        "_peers",
        "_lock",
        "_inbound_handler",
    )

    def __init__(
        self,
        *,
        own_instance_id: str,
        https_inbox: HttpsInboxTransport,
        signaling_send: Callable[
            [str, FederationEventType, dict], Awaitable[DeliveryResult]
        ],
        ice_servers: list[dict] | None = None,
        inbound_handler: Callable[[str, bytes], Awaitable[dict]] | None = None,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._https_inbox = https_inbox
        self._signaling_send = signaling_send
        self._ice_servers = ice_servers or []
        self._peers: dict[str, _RtcPeer] = {}
        self._lock = asyncio.Lock()
        # Callback for inbound DataChannel frames → §24.11 pipeline.
        # Signature: ``async (instance_id, raw_body) -> dict``.
        # Attached by FederationService after construction.
        self._inbound_handler = inbound_handler

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

        # Kick off (or re-kick) the handshake lazily on first use.
        if peer is None:
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

    async def _ensure_handshake(self, instance: RemoteInstance) -> None:
        async with self._lock:
            if instance.id in self._peers:
                return
            peer = _RtcPeer(
                instance_id=instance.id,
                ice_servers=self._ice_servers,
                signaling=self._signaling_factory(instance.id),
                inbound=self._inbound_factory(instance.id),
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
                    ice_servers=self._ice_servers,
                    signaling=self._signaling_factory(from_instance),
                    inbound=self._inbound_factory(from_instance),
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
        """Handle a trickled ``FEDERATION_RTC_ICE`` candidate."""
        peer = self._peers.get(from_instance)
        if peer is None:
            return
        await peer.add_ice_candidate(
            candidate=str(payload.get("candidate") or ""),
            sdp_mid=str(payload.get("sdp_mid") or "0"),
        )

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
        for peer in list(self._peers.values()):
            peer.close()
        self._peers.clear()
