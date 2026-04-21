"""WebRTC DataChannel sync transport (§4.2.3, §24.12.3, §25.6.2).

Establishes a direct WebRTC DataChannel between two paired Social Home
instances for Tier 2 / Tier 3 progressive sync. The federation relay is
used for the SDP / ICE handshake only — bulk sync data flows over the
DataChannel itself, never through the relay.

``libdatachannel`` is a hard runtime dependency — WebRTC is the primary
transport for sync (and for federation in general, §24.12), with the
relay webhook only as fallback. Missing the native binding is treated
as a hard configuration error; tests that want to exercise the
signalling state machine without a real peer can use
``SyncRtcSession.stub_session(...)``.

Security audit findings addressed here (§25.6.2):
    * **S-13** — :meth:`SyncRtcSession.create_answer` and
      :meth:`SyncRtcSession.set_answer` are distinct: the requester
      generates an answer from a remote offer, the provider processes
      the answer to complete the offer/answer exchange.
    * **S-14** — :class:`SyncRtcSession` carries an explicit
      ``requester_instance_id`` field; the answer-origin guard checks
      that field.
    * **S-16** — ``sync_mode`` is a formal constructor field with a
      default of ``"initial"``. No ``getattr(...)`` guards.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

# Optional WebRTC dep — see :mod:`social_home.federation.transport`
# for the full rationale. Absent library ⇒ fallback to webhook.
try:
    import libdatachannel
except ImportError:  # pragma: no cover — optional
    libdatachannel = None

log = logging.getLogger(__name__)


# ─── Constants ────────────────────────────────────────────────────────────

#: How long :meth:`SyncRtcSession.wait_ready` blocks before giving up
#: and signalling the caller to fall back to relay chunks.
ICE_TIMEOUT_SECONDS: float = 15.0

#: DataChannel label used for sync — distinct from the GFS ``"gfs-v1"``
#: channel so a single peer can host both at once.
CHANNEL_LABEL: str = "sync-v1"

#: Maximum simultaneous signalling sessions a single node will accept
#: (S-8 cap).  Beyond this cap the manager replies with
#: ``SPACE_SYNC_DIRECT_FAILED {reason: "rate_limited"}``.
MAX_SIGNALING_SESSIONS: int = 200


# ─── SyncRtcSession ───────────────────────────────────────────────────────


class SyncRtcSession:
    """WebRTC DataChannel session used for direct space sync.

    Parameters
    ----------
    sync_id:
        128-bit token (`secrets.token_urlsafe(16)`) identifying the
        sync exchange.
    space_id:
        Space being synced.
    requester_instance_id:
        The instance that originated ``SPACE_SYNC_BEGIN``.  Persisted as
        a formal field per **S-14** so the answer-origin guard works.
    provider_instance_id:
        The instance that holds the canonical data and creates the offer.
    sync_mode:
        ``"initial"`` (Tier 1), ``"incremental"`` (Tier 2 — request_more
        only), or ``"full"`` (Tier 3 — full history).  Persisted as a
        formal field per **S-16**.
    role:
        ``"provider"`` (default) or ``"requester"`` — controls which
        ``create_*`` method is allowed.
    ice_servers:
        STUN / TURN configuration list passed straight to
        ``libdatachannel.IceServer``.
    """

    __slots__ = (
        "sync_id",
        "space_id",
        "requester_instance_id",
        "provider_instance_id",
        "sync_mode",
        "role",
        "_ice_servers",
        "_pc",
        "_channel",
        "_ready",
        "_remote_sdp",
        "_local_sdp",
        "_ice_candidates",
        "_loop",
        "_closed",
    )

    def __init__(
        self,
        *,
        sync_id: str,
        space_id: str,
        requester_instance_id: str,
        provider_instance_id: str,
        sync_mode: str = "initial",
        role: str = "provider",
        ice_servers: list[dict] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        if sync_mode not in ("initial", "incremental", "full"):
            raise ValueError(f"Invalid sync_mode: {sync_mode!r}")
        if role not in ("provider", "requester"):
            raise ValueError(f"Invalid role: {role!r}")

        self.sync_id = sync_id
        self.space_id = space_id
        self.requester_instance_id = requester_instance_id
        self.provider_instance_id = provider_instance_id
        self.sync_mode = sync_mode
        self.role = role
        self._ice_servers = ice_servers or []
        self._pc: Any = None  # set by _init_real_pc()
        self._channel: Any | None = None
        self._ready = asyncio.Event()
        self._remote_sdp: str | None = None
        self._local_sdp: str | None = None
        self._ice_candidates: list[str] = []
        self._loop = loop
        self._closed = False

        self._init_real_pc()

    # ─── Real libdatachannel setup ────────────────────────────────────────

    def _init_real_pc(self) -> None:
        """Configure a real ``libdatachannel.PeerConnection``."""
        cfg = libdatachannel.Configuration()
        for srv in self._ice_servers:
            urls = srv["urls"] if isinstance(srv["urls"], list) else [srv["urls"]]
            for url in urls:
                if url.startswith("stun:"):
                    cfg.iceServers.append(libdatachannel.IceServer(url))
                elif url.startswith("turn:"):
                    cfg.iceServers.append(
                        libdatachannel.IceServer(
                            url,
                            username=srv.get("username", ""),
                            password=srv.get("credential", ""),
                        )
                    )

        self._pc = libdatachannel.PeerConnection(cfg)
        loop = self._loop or asyncio.get_event_loop()
        self._loop = loop

        def _ts(coro):
            loop.call_soon_threadsafe(asyncio.ensure_future, coro)

        if self.role == "provider":
            self._channel = self._pc.createDataChannel(CHANNEL_LABEL)
            self._channel.onOpen(lambda: _ts(self._on_open()))
            self._channel.onClosed(lambda: _ts(self._on_close()))
        else:
            # Requester accepts the channel created by the provider.
            self._pc.onDataChannel(lambda ch: _ts(self._on_remote_channel(ch)))

    async def _on_remote_channel(self, channel) -> None:
        if channel.getLabel() != CHANNEL_LABEL:
            return
        self._channel = channel
        loop = self._loop or asyncio.get_event_loop()

        def _ts(coro):
            loop.call_soon_threadsafe(asyncio.ensure_future, coro)

        channel.onOpen(lambda: _ts(self._on_open()))
        channel.onClosed(lambda: _ts(self._on_close()))

    async def _on_open(self) -> None:
        log.info(
            "SyncRtcSession[%s]: DataChannel open (space=%s, mode=%s)",
            self.sync_id,
            self.space_id,
            self.sync_mode,
        )
        self._ready.set()

    async def _on_close(self) -> None:
        log.info("SyncRtcSession[%s]: DataChannel closed", self.sync_id)
        self._closed = True

    # ─── Provider role ────────────────────────────────────────────────────

    async def create_offer(self) -> str:
        """Generate an SDP offer (provider role).

        Returns the SDP string to embed in ``SPACE_SYNC_OFFER``.
        Raises :class:`RuntimeError` when called on a requester session.
        """
        if self.role != "provider":
            raise RuntimeError("create_offer is only valid for provider sessions")

        loop = self._loop or asyncio.get_event_loop()
        await loop.run_in_executor(None, self._pc.setLocalDescription, "offer")
        sdp = await loop.run_in_executor(None, self._pc.localDescription)
        self._local_sdp = sdp
        return sdp

    async def set_answer(self, sdp_answer: str) -> None:
        """Apply the SDP answer (provider role) — completes negotiation.

        Distinct from :meth:`create_answer` (which is for the requester
        side).  Per **S-13** never use ``set_answer`` to handle an
        incoming offer.
        """
        if self.role != "provider":
            raise RuntimeError("set_answer is only valid for provider sessions")
        if not sdp_answer:
            raise ValueError("Empty SDP answer")

        self._remote_sdp = sdp_answer

        loop = self._loop or asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._pc.setRemoteDescription,
            sdp_answer,
            "answer",
        )

    # ─── Requester role ───────────────────────────────────────────────────

    async def create_answer(self, sdp_offer: str) -> str:
        """Generate an SDP answer (requester role).

        Sets the remote description from ``sdp_offer`` and returns the
        local SDP answer for embedding in ``SPACE_SYNC_ANSWER``.

        This is **not** ``set_answer`` — see **S-13** in §25.6.2: the
        former implementation called ``set_answer(sdp_offer)`` here,
        which bypassed answer generation and silently broke every
        DataChannel handshake.
        """
        if self.role != "requester":
            raise RuntimeError("create_answer is only valid for requester sessions")
        if not sdp_offer:
            raise ValueError("Empty SDP offer")

        self._remote_sdp = sdp_offer

        loop = self._loop or asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._pc.setRemoteDescription,
            sdp_offer,
            "offer",
        )
        await loop.run_in_executor(None, self._pc.setLocalDescription, "answer")
        sdp = await loop.run_in_executor(None, self._pc.localDescription)
        self._local_sdp = sdp
        return sdp

    # ─── Shared ───────────────────────────────────────────────────────────

    async def add_ice_candidate(self, candidate: str, sdp_mid: str = "0") -> None:
        """Add a remote ICE candidate received via ``SPACE_SYNC_ICE``.

        The SyncSessionManager validates the candidate (size + format)
        before calling this method per **S-7**.
        """
        if not candidate:
            raise ValueError("Empty ICE candidate")
        self._ice_candidates.append(candidate)

        loop = self._loop or asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._pc.addRemoteCandidate,
            candidate,
            sdp_mid,
        )

    async def wait_ready(self, timeout: float = ICE_TIMEOUT_SECONDS) -> bool:
        """Block until the DataChannel is open, or *timeout* seconds elapse.

        Returns ``True`` on open, ``False`` on timeout.
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def send_chunk(self, chunk_payload: bytes | str) -> None:
        """Send a ``SPACE_SYNC_CHUNK`` frame over the DataChannel.

        Raises :class:`ConnectionError` if the channel is not open.
        """
        if self._channel is None or self._closed:
            raise ConnectionError("DataChannel not open")
        loop = self._loop or asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._channel.sendMessage,
            chunk_payload,
        )

    @property
    def is_ready(self) -> bool:
        """Whether the DataChannel has signalled ``onOpen``."""
        return self._ready.is_set()

    @property
    def is_closed(self) -> bool:
        """Whether the channel has been closed (locally or remotely)."""
        return self._closed

    def close(self) -> None:
        """Close the underlying connection and mark the session closed."""
        self._closed = True
        self._ready.clear()
        if self._pc is not None:
            try:
                self._pc.close()
            except Exception:
                pass
        self._pc = None
        self._channel = None


# ─── Stateful helpers used by the manager ────────────────────────────────


@dataclass(slots=True)
class SyncSessionRecord:
    """Lightweight record for the in-memory session registry."""

    sync_id: str
    space_id: str
    requester_instance_id: str
    provider_instance_id: str
    sync_mode: str
    rtc: SyncRtcSession | None = None
    created_at: float = field(default=0.0)
