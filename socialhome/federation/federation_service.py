"""Federation service — outbound delivery and inbound validation (§11–§13, §24.11).

This module owns two responsibilities:

A) **Outbound**: encrypt, sign, and POST federation events to paired peer
   instances using the per-pair directional session keys.

B) **Inbound**: run the §24.11 validation pipeline on received inbox
   bodies, then dispatch validated events to the in-process EventBus.

Pairing helpers (``initiate_pairing``, ``accept_pairing``,
``confirm_pairing``) drive the §11 QR-code handshake to establish the
shared session keys and ``RemoteInstance`` row.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp
import asyncio
import orjson as _orjson

if TYPE_CHECKING:
    from .route_discovery import RouteDiscoveryService
    from .routed_envelope import SpaceRoutedHandler

from ..crypto import (
    REPLAY_CACHE_WINDOW,
    ReplayCache,
    b64url_encode,
)
from ..db import AsyncDatabase
from ..domain.events import (
    ConnectionReachable,
    ConnectionUnreachable,
    LocalHomeLocationUpdated,
    PairingIntroRelayReceived,
    PeerHomeChanged,
    SpaceConfigChanged,
)
from ..domain.federation_capabilities import FederationCapability
from ..domain.media_validator import validate_inbound_media_meta
from ..webrtc_ice import warn_if_no_turn, warn_if_turn_unusable
from ..domain.federation import (
    DELIVERY_ERROR_QUEUED,
    DELIVERY_ERROR_ROUTE_COOLDOWN,
    BroadcastResult,
    DeliveryResult,
    FederationEvent,
    FederationEventType,
    PairingStatus,
    RemoteInstance,
)
from ..infrastructure.event_bus import EventBus
from ..infrastructure.key_manager import KeyManager
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.outbox_repo import AbstractOutboxRepo
from .encoder import FederationEncoder
from .app_framing import (
    APP_AEAD_SUITE_AESGCM_256,
    SUPPORTED_APP_AEAD_SUITES,
    UnsupportedAppAeadSuite,
)
from .media_framing import (
    MEDIA_AEAD_SUITE_AESGCM_256,
    SUPPORTED_MEDIA_AEAD_SUITES,
    UnsupportedMediaAeadSuite,
)
from .pq_signer import PqSigner
from .inbound_validator import (
    InboundContext,
    InboundPipeline,
    _InboxInstance,
    make_ban_check,
    make_check_deprovisioned_author,
    make_check_replay,
    make_check_timestamp,
    make_decrypt_and_parse,
    make_idempotency_check,
    make_lookup_instance,
    make_lookup_instance_by_id,
    make_parse_json,
    make_persist_replay,
    make_verify_signature,
)
from .pairing_coordinator import PairingCoordinator
from .peer_pairing_client import PeerPairingClient
from .event_dispatch_registry import EventDispatchRegistry


def _dumps(obj: dict) -> str:
    """Compact UTF-8 JSON — the wire format for every federation envelope."""
    return _orjson.dumps(obj).decode("utf-8")


def _loads(s: str | bytes) -> dict:
    return _orjson.loads(s)


log = logging.getLogger(__name__)

#: Maximum allowed clock skew for inbound envelopes (§24.11 §5).
_TIMESTAMP_SKEW_SECONDS = 300

#: Pairing QR token lifetime.
_PAIRING_TTL_SECONDS = 300

#: Length of the SAS verification code (digits).
_SAS_DIGITS = 6


class FederationService:
    """Core federation handler — outbound delivery and inbound dispatch.

    All constructor parameters are injected; the service has no I/O of its
    own beyond the HTTP client it uses to POST to peer inbox URLs.

    Parameters
    ----------
    db:
        The application database (used for replay cache persistence).
    federation_repo:
        Abstracts ``remote_instances``, replay cache, and pairing rows.
    outbox_repo:
        Abstracts ``federation_outbox`` for reliable at-least-once delivery.
    key_manager:
        KEK-based encrypt/decrypt for session keys at rest.
    bus:
        In-process domain event bus.
    own_instance_id:
        This instance's stable identifier (derived from ``own_identity_pk``).
    own_identity_seed:
        32-byte Ed25519 private key seed for signing outbound envelopes.
    own_identity_pk:
        32-byte Ed25519 public key corresponding to ``own_identity_seed``.
    http_client:
        Optional aiohttp ``ClientSession``-compatible object. When ``None``
        the service creates a session on first use. Injectable for testing.
    """

    __slots__ = (
        "_db",
        "_federation_repo",
        "_outbox_repo",
        "_key_manager",
        "_bus",
        "_own_instance_id",
        "_own_identity_seed",
        "_own_identity_pk",
        "_own_pq_seed",
        "_own_pq_pk",
        "_sig_suite",
        "_http_client",
        "_replay_cache",
        "_sync_manager",
        "_call_signaling",
        "_ice_servers",
        "_idempotency_cache",
        "_typing_service",
        "_dm_routing_service",
        "_transport",
        "_presence_service",
        "_online_status_service",
        "_space_sync_service",
        "_space_sync_receiver",
        "_encoder",
        "_pairing",
        "_inbound_pipeline",
        "_rtc_inbound_pipeline",
        "_event_registry",
        "_gfs_connection_service",
        "_user_repo",
        "_route_service",
        "_last_mesh_begin_at",
        "_routed_handler",
        "_app_fed",
    )

    def __init__(
        self,
        db: AsyncDatabase,
        federation_repo: AbstractFederationRepo,
        outbox_repo: AbstractOutboxRepo,
        key_manager: KeyManager,
        bus: EventBus,
        own_instance_id: str,
        own_identity_seed: bytes,
        own_identity_pk: bytes,
        http_client=None,
        sync_manager=None,
        call_signaling=None,
        ice_servers: list[dict] | None = None,
        own_pq_seed: bytes | None = None,
        own_pq_pk: bytes | None = None,
        sig_suite: str = "ed25519",
    ) -> None:
        self._db = db
        self._federation_repo = federation_repo
        self._outbox_repo = outbox_repo
        self._key_manager = key_manager
        self._bus = bus
        self._own_instance_id = own_instance_id
        self._own_identity_seed = own_identity_seed
        self._own_identity_pk = own_identity_pk
        self._own_pq_seed = own_pq_seed
        self._own_pq_pk = own_pq_pk
        self._sig_suite = sig_suite
        self._http_client = http_client
        self._replay_cache = ReplayCache(window=REPLAY_CACHE_WINDOW)
        self._sync_manager = sync_manager
        self._call_signaling = call_signaling
        self._ice_servers = ice_servers or []
        self._idempotency_cache = None
        self._typing_service = None
        self._dm_routing_service = None
        self._transport = None
        self._presence_service = None
        self._online_status_service = None
        self._space_sync_service = None
        self._space_sync_receiver = None
        self._gfs_connection_service = None
        # Set via :meth:`attach_user_repo` after construction. Enables
        # the receiver-side deprovisioned-author filter (§24.11 step 11).
        # When unset, the filter step is omitted from the pipeline — a
        # legacy boot path / unit-test fixture without a user repo still
        # functions, just without the visibility backstop.
        self._user_repo = None
        # §D2 PR2 mesh-routing primitives — set via :meth:`attach_mesh`.
        # When wired, :meth:`send_with_mesh_fallback` discovers a path
        # through the federation graph and ships SPACE_ROUTED when a
        # peer isn't directly CONFIRMED. Without them, the helper just
        # surfaces a failed DeliveryResult for non-CONFIRMED peers.
        self._route_service: RouteDiscoveryService | None = None
        #: requester instance_id → monotonic time of its last mesh
        #: SPACE_SYNC_BEGIN. Bounds how far back a cached route has to
        #: reach before we treat it as pre-restart junk (#648).
        self._last_mesh_begin_at: dict[str, float] = {}
        self._routed_handler: SpaceRoutedHandler | None = None
        # Social Home Apps federation bridge — set via :meth:`attach_apps`.
        # When unset, inbound APP_SESSION / APP_MESSAGE events are silently
        # dropped (no app bridge wired on this instance).
        self._app_fed = None
        # Envelope crypto delegate (encrypt/decrypt/sign/verify). Keeps the
        # AES-256-GCM + Ed25519 surface unit-testable in isolation. When
        # the hybrid suite is configured the PQ signer is attached so
        # outbound envelopes carry both signatures.
        pq_signer = PqSigner(own_pq_seed) if own_pq_seed else None
        self._encoder = FederationEncoder(
            own_identity_seed,
            pq_signer=pq_signer,
            sig_suite=sig_suite,
        )
        # §11 QR-code pairing handshake delegate. The peer-pairing
        # client (plaintext Ed25519-signed bootstrap transport) is
        # attached right after construction — circular-free.
        self._pairing = PairingCoordinator(
            federation_repo,
            key_manager,
            own_identity_pk,
            own_pq_pk=own_pq_pk,
            own_sig_suite=sig_suite,
            bus=bus,
        )
        self._pairing.attach_peer_pairing_client(
            PeerPairingClient(
                own_identity_seed=own_identity_seed,
                client_factory=self._get_http_client,
            ),
        )
        # §24.11 inbound validation pipeline (middleware chain).
        self._inbound_pipeline = None  # lazy-built on first use
        self._rtc_inbound_pipeline = None  # lazy-built on first RTC frame
        # Event dispatch registry for federation event handlers.
        # Handlers register themselves via attach_* methods.
        self._event_registry = EventDispatchRegistry()
        self._register_default_handlers()
        # Subscribe to domain events that trigger outbound federation.
        self._bus.subscribe(
            LocalHomeLocationUpdated,
            self._on_local_home_location_updated,
        )

    def _build_inbound_pipeline(self):
        """Lazily construct the §24.11 validation middleware chain.

        Must be called after ``attach_idempotency_cache`` since the
        pipeline references it. Built on first inbound envelope rather
        than in ``__init__`` so all optional wiring is in place.
        """
        return InboundPipeline(
            self._common_pipeline_steps(
                lookup_step=make_lookup_instance(
                    repo=self._federation_repo,
                    lookup_fn=_lookup_by_inbox_id,
                ),
            )
        )

    def _build_rtc_inbound_pipeline(self):
        """Build the §24.11 pipeline variant for WebRTC DataChannel frames.

        Identical to the HTTPS inbox variant except the instance lookup uses
        ``instance_id`` (already known from the peer connection) instead
        of ``inbox_id``.
        """
        return InboundPipeline(
            self._common_pipeline_steps(
                lookup_step=make_lookup_instance_by_id(
                    repo=self._federation_repo,
                ),
            )
        )

    def _common_pipeline_steps(self, *, lookup_step):
        """Return the shared step list for both inbox and RTC pipelines."""
        steps = [
            make_parse_json(loads=_loads),
            lookup_step,
            make_check_timestamp(),
            make_verify_signature(encoder=self._encoder),
            make_check_replay(replay_cache=self._replay_cache),
            make_decrypt_and_parse(
                key_manager=self._key_manager,
                encoder=self._encoder,
                loads=_loads,
            ),
            make_idempotency_check(
                cache_holder=lambda: self._idempotency_cache,
            ),
            make_ban_check(federation_repo=self._federation_repo),
            make_persist_replay(federation_repo=self._federation_repo),
        ]
        # Receiver-side deprovisioned-author backstop. Runs LAST so the
        # replay-id is recorded regardless — if we dropped this here on
        # a later retry the sender's outbox would still get a 200 OK
        # (early-response) and stop redelivering. ``user_repo`` is
        # threaded in via :meth:`attach_user_repo`; if it's unset we
        # skip the step so legacy fixtures still build a working
        # pipeline.
        if self._user_repo is not None:
            steps.append(
                make_check_deprovisioned_author(user_repo=self._user_repo),
            )
        return steps

    # ─── Wiring helpers ──────────────────────────────────────────────────

    def attach_sync_manager(self, sync_manager) -> None:
        """Attach a :class:`SyncSessionManager` after construction.

        Also wires the manager's outbound-ICE emitter so each
        ``SyncRtcSession``'s locally-gathered ICE candidates are
        trickled to the peer via :class:`FederationEventType.SPACE_SYNC_ICE`.
        Without this hook, sync RTC handshakes could only succeed when
        the host candidates inlined in the SDP were reachable
        (same-LAN); cross-NAT sessions hit the 15 s connectivity
        timeout.
        """
        self._sync_manager = sync_manager
        sync_manager._on_local_ice = self._send_outbound_sync_ice

    async def _send_outbound_sync_ice(
        self,
        sync_id: str,
        candidate: str,
        sdp_mid: str,
    ) -> None:
        """Forward a locally-gathered ICE candidate to the peer.

        Looks up the session by ``sync_id`` to figure out who the
        peer is (provider sends to requester, requester to provider).
        Skips when the session has gone away (close raced the
        callback). All inputs are already validated by libdatachannel
        before the candidate enters the iterator.
        """
        if self._sync_manager is None:
            return
        record = self._sync_manager.get_session(sync_id)
        if record is None:
            return
        # Provider sends to the requester; requester sends to the
        # provider. ``provider_instance_id`` is empty on a
        # requester-side record that was opened by ``apply_offer``
        # before a peer was known; in that case the offer's
        # ``from_instance`` was the provider — but the only way the
        # session got into _sessions on the requester side is via
        # ``apply_offer`` which doesn't record the provider id. The
        # easier path: drop the outbound when the target is unknown
        # — the inline candidates in the SDP still carry the host
        # set, and the peer will trickle their own to us regardless.
        if record.rtc is None or record.rtc.role is None:
            return
        if record.rtc.role == "provider":
            target = record.requester_instance_id
        else:
            target = record.provider_instance_id
        if not target:
            return
        await self.send_event(
            to_instance_id=target,
            event_type=FederationEventType.SPACE_SYNC_ICE,
            payload={
                "sync_id": sync_id,
                "candidate": candidate,
                "sdp_mid": sdp_mid,
            },
            space_id=record.space_id,
        )

    def attach_user_repo(self, user_repo) -> None:
        """Attach an :class:`AbstractUserRepo` after construction.

        Enables the §24.11 receiver-side deprovisioned-author filter —
        envelopes whose author user is marked ``deprovisioned_at`` in
        ``remote_users`` are dropped before dispatch. The check runs
        last in the pipeline so the replay-id is still persisted (the
        sender's outbox sees the 200 OK and stops redelivering).
        """
        self._user_repo = user_repo

    def attach_idempotency_cache(self, cache) -> None:
        """Attach an :class:`IdempotencyCache` for inbound dedup."""
        self._idempotency_cache = cache

    def attach_typing_service(self, typing_service) -> None:
        """Attach a :class:`TypingService` for DM_USER_TYPING dispatch."""
        self._typing_service = typing_service
        self._event_registry.register(
            FederationEventType.DM_USER_TYPING,
            self._handle_dm_user_typing,
        )

    def attach_dm_routing(self, dm_routing_service) -> None:
        """Attach a :class:`DmRoutingService` for DM_RELAY dispatch."""
        self._dm_routing_service = dm_routing_service
        self._event_registry.register(
            FederationEventType.DM_RELAY,
            self._handle_dm_relay,
        )

    def attach_transport(self, transport) -> None:
        """Attach a :class:`FederationTransport` facade.

        Once attached, :meth:`send_event` prefers its WebRTC
        DataChannel and falls back to HTTPS inbox only if the channel is
        unavailable.
        """
        self._transport = transport
        for event_type in (
            FederationEventType.FEDERATION_RTC_OFFER,
            FederationEventType.FEDERATION_RTC_ANSWER,
            FederationEventType.FEDERATION_RTC_ICE,
        ):
            self._event_registry.register(event_type, self._handle_transport_event)

    def attach_presence_service(self, presence_service) -> None:
        """Attach :class:`PresenceService` so ``PRESENCE_UPDATED`` lands."""
        self._presence_service = presence_service
        self._event_registry.register(
            FederationEventType.PRESENCE_UPDATED,
            self._handle_presence_updated,
        )

    def attach_online_status_service(self, online_status_service) -> None:
        """Attach :class:`OnlineStatusService` so cross-instance USER_ONLINE
        / USER_IDLE / USER_OFFLINE events land in the remote-state cache."""
        self._online_status_service = online_status_service
        for event_type in (
            FederationEventType.USER_ONLINE,
            FederationEventType.USER_IDLE,
            FederationEventType.USER_OFFLINE,
        ):
            self._event_registry.register(
                event_type,
                self._handle_user_online_changed,
            )

    def attach_space_sync(self, *, service, receiver) -> None:
        """Attach :class:`SpaceSyncService` + :class:`SpaceSyncReceiver`
        so direct-peer chunk streaming works when a DataChannel opens."""
        self._space_sync_service = service
        self._space_sync_receiver = receiver

    def attach_mesh(
        self,
        *,
        route_service: RouteDiscoveryService,
        routed_handler: SpaceRoutedHandler,
    ) -> None:
        """Wire the §D2-PR2 federation-mesh routing pair.

        Once attached, :meth:`send_with_mesh_fallback` falls back to
        SPACE_ROUTED multi-hop delivery when a peer is not directly
        CONFIRMED. Without it, the helper surfaces a failed
        :class:`DeliveryResult` for unconfirmed peers so the caller
        can decide whether to fail-hard or skip.
        """
        self._route_service = route_service
        self._routed_handler = routed_handler
        # The handler needs the discovery service to honour a verified
        # SPACE_ROUTE_STALE nack (invalidate + rediscover); this is the one
        # place that holds both halves.
        routed_handler.attach_route_service(route_service)

    def attach_gfs_connection_service(self, gfs_connection_service) -> None:
        """Attach :class:`GfsConnectionService` so spec §24.10.7 works.

        The provider asks the GFS for a least-loaded ``signaling_node``
        URL before generating ``SPACE_SYNC_OFFER``, and releases the
        slot when the direct path opens or fails. Without this attach,
        offers ship without ``signaling_node`` (single-node behaviour).
        """
        self._gfs_connection_service = gfs_connection_service

    def attach_call_signaling(self, call_signaling) -> None:
        """Attach a :class:`CallSignalingService` after construction."""
        self._call_signaling = call_signaling
        for event_type in (
            FederationEventType.CALL_OFFER,
            FederationEventType.CALL_ANSWER,
            FederationEventType.CALL_DECLINE,
            FederationEventType.CALL_BUSY,
            FederationEventType.CALL_HANGUP,
            FederationEventType.CALL_END,
            FederationEventType.CALL_ICE,
            FederationEventType.CALL_ICE_CANDIDATE,
            FederationEventType.CALL_QUALITY,
        ):
            self._event_registry.register(event_type, self._handle_call_signal)

    def attach_apps(self, app_fed) -> None:
        """Attach an :class:`AppFederationService` after construction.

        Once wired, inbound ``APP_SESSION`` and ``APP_MESSAGE`` events
        are forwarded to ``app_fed.on_inbound_event``. Also registers the
        binary-channel handler used by the ``fed-app-v1`` DataChannel when
        the transport is a v_17+ peer.
        """
        self._app_fed = app_fed
        for event_type in (
            FederationEventType.APP_SESSION,
            FederationEventType.APP_MESSAGE,
        ):
            self._event_registry.register(event_type, self._handle_app_inbound_event)

    async def _handle_app_inbound_event(self, event: FederationEvent) -> None:
        """Delegate an inbound APP_SESSION / APP_MESSAGE event to the app bridge."""
        if self._app_fed is None:
            return
        await self._app_fed.on_inbound_event(event)

    async def _app_inbound_handler(
        self,
        instance_id: str,
        header_bytes: bytes,
        payload_bytes: bytes,
    ) -> None:
        """Binary-channel inbound for ``fed-app-v1`` frames.

        Called by the transport when a complete ``FRAME_TYPE_APP_MSG``
        frame arrives on the ``fed-app-v1`` DataChannel.  Mirrors
        :meth:`handle_inbound_media_frame`:

        1. Run the full §24.11 pipeline on the header (origin auth via
           Ed25519, replay, timestamp, ban, decrypt metadata).
        2. Validate ``app_aead_suite`` against the supported set — no
           default fallback (CLAUDE.md crypto-suite rule).
        3. Decrypt the app payload under the directional receive key.
        4. Extract ``app_id`` and ``session_id`` from the validated
           envelope metadata.
        5. Hand ``(instance_id, app_id, session_id, decoded_payload)``
           to :meth:`AppFederationService.on_inbound_message`.

        Raises :class:`ValueError` (or
        :class:`~socialhome.federation.app_framing.UnsupportedAppAeadSuite`)
        on any validation failure so the transport logs + drops the frame.
        """
        if self._app_fed is None:
            return
        ctx = await self.validate_inbound_rtc(instance_id, header_bytes)
        if ctx.early_response is not None:
            return
        event = ctx.event
        if event is None:  # pragma: no cover
            return
        meta = event.payload if isinstance(event.payload, dict) else {}
        suite = meta.get("app_aead_suite")
        if suite not in SUPPORTED_APP_AEAD_SUITES:
            raise UnsupportedAppAeadSuite(
                f"unknown app_aead_suite: {suite!r}",
            )
        # Decrypt the app payload using the directional receive key.
        session_key = self._key_manager.decrypt(ctx.instance.key_remote_to_self)
        try:
            raw = self._encoder.decrypt_bytes(payload_bytes, session_key)
        except Exception as exc:
            raise ValueError(f"Failed to decrypt app payload: {exc}") from exc
        # Verify payload integrity: sha256(plaintext) must match the hash inside
        # the signed+encrypted metadata — same constant-time check as the media
        # handler's chunk_sha256, preventing payload-splice attacks by a relay.
        expected_sha = str(meta.get("payload_sha256") or "")
        actual_sha = b64url_encode(hashlib.sha256(raw).digest())
        if not expected_sha or not hmac.compare_digest(expected_sha, actual_sha):
            log.warning(
                "app frame payload_sha256 mismatch from %s — dropping",
                instance_id,
            )
            return
        try:
            decoded_payload = _loads(raw)
        except Exception as exc:
            raise ValueError(f"Failed to parse app payload JSON: {exc}") from exc
        app_id = meta.get("app_id")
        session_id = meta.get("session_id")
        if not app_id or not session_id:
            raise ValueError(
                f"app frame missing app_id or session_id: {meta!r}",
            )
        await self._app_fed.on_inbound_message(
            instance_id,
            app_id,
            session_id,
            decoded_payload,
        )

    def set_ice_servers(self, servers: list[dict]) -> None:
        """Update the WebRTC ICE-server config served to peers.

        Propagates the new list to the attached transport so future
        DataChannel handshakes pick it up. Already-connected peers are
        left alone — their DataChannel works, and renegotiating it buys
        nothing. Peers that never managed to connect are retired and
        rebuilt on their next send, so a late-arriving TURN list (the
        haos case, where the Cloudflare credentials land after the boot
        outbox drain has already built those peers STUN-only) does reach
        them.

        Re-runs the TURN diagnostics on the incoming list. The boot-time
        checks in ``create_app`` only ever saw the config-derived list, so a
        replacement arriving later (today: the HA Core pull) could drop TURN
        entirely, or bring one with no credentials, and nothing said a word —
        which is what made that class of misconfiguration invisible.
        """
        self._ice_servers = list(servers or [])
        warn_if_no_turn(self._ice_servers)
        warn_if_turn_unusable(self._ice_servers)
        if self._transport is not None and hasattr(self._transport, "set_ice_servers"):
            self._transport.set_ice_servers(self._ice_servers)

    def mark_ice_primed(self) -> None:
        """Release the transport's first-handshake ICE gate.

        The transport holds its first offerer handshake for a short window
        so a platform that supplies ICE servers asynchronously (the HA pull
        of ``web_rtc/ice_servers``) gets its TURN credentials in before the
        boot-time outbox drain builds STUN-only peers that can never relay.

        :meth:`set_ice_servers` already releases the gate on every push, so
        this exists for the platforms that will never push one — standalone,
        and an HA deployment whose first fetch came back empty or failed.
        Without the explicit call those deployments pay the full prime
        timeout on their first outbound send, waiting for a list that is
        never coming.
        """
        if self._transport is not None and hasattr(self._transport, "mark_ice_primed"):
            self._transport.mark_ice_primed()

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        """``True`` iff the named peer's advertised ``proto_version`` is
        at least ``min_version``.

        Used by outbound senders to gate optional fields on what the
        receiving peer actually understands — see
        :mod:`socialhome.domain.federation_capabilities` for the
        per-version thresholds. An unknown peer (not yet in
        ``remote_instances``) returns ``False``; a peer that hasn't
        sent its ``INSTANCE_CAPABILITIES_UPDATED`` envelope yet reads
        as ``proto_version=1`` (the most conservative wire), so any
        gate above v1 will return ``False`` until the announcement
        arrives. The conservative default is "feature not supported"
        so the sender omits the optional field.

        Fail-soft: if the repo lookup raises we return ``False`` —
        sending the legacy shape is always safer than crashing the
        outbound path mid-fan-out.
        """
        if not instance_id or min_version <= 0:
            return False
        try:
            peer = await self._federation_repo.get_instance(instance_id)
        except Exception:  # pragma: no cover — defensive
            return False
        if peer is None:
            return False
        return peer.proto_version >= min_version

    async def peer_identity_public_key(self, instance_id: str) -> bytes | None:
        """Return a known peer's pinned Ed25519 identity public key as bytes.

        Decodes the hex ``remote_identity_pk`` stored on the peer's
        ``remote_instances`` row — the same key the §24.11 inbound pipeline
        verified the envelope signature against. Inbound handlers use it to
        re-verify a *detached* credential (e.g. a relayed/cached per-user
        identity binding) against its issuing instance.

        Fail-soft: an unknown / unpaired peer, an empty id, or a malformed
        stored key all yield ``None`` (never a raise) so a caller can degrade
        rather than crash the inbound path.
        """
        if not instance_id:
            return None
        try:
            peer = await self._federation_repo.get_instance(instance_id)
        except Exception:  # pragma: no cover — defensive
            return None
        if peer is None:
            return None
        try:
            return bytes.fromhex(peer.remote_identity_pk)
        except ValueError:  # pragma: no cover — pinned key is always valid hex
            return None

    async def is_confirmed_peer(self, instance_id: str) -> bool:
        """``True`` iff ``instance_id`` is a CONFIRMED peer we've paired with.

        Reads the peer's ``remote_instances.status`` and checks it equals
        :data:`PairingStatus.CONFIRMED`. An unknown / unpaired / pending /
        unpairing peer reads as not-confirmed. Used by inbound handlers to
        refuse to serve sensitive replies to a peer we haven't actually
        confirmed (the §24.11 pipeline authenticates the *sender*, not that
        we trust them — a peer mid-pairing or merely known is still gated).

        Fail-soft: an empty id or a repo error yields ``False`` (treat as
        not-confirmed) so a caller can drop the request rather than crash.
        """
        if not instance_id:
            return False
        try:
            peer = await self._federation_repo.get_instance(instance_id)
        except Exception:  # pragma: no cover — defensive
            return False
        if peer is None:
            return False
        return peer.status is PairingStatus.CONFIRMED

    @property
    def own_identity_seed(self) -> bytes:
        return self._own_identity_seed

    @property
    def own_identity_pk(self) -> bytes:
        return self._own_identity_pk

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def warm_replay_cache(self) -> None:
        """Load recent replay-cache entries from the DB into memory.

        Call this once at startup so the in-memory cache is populated before
        any inbound requests are handled.
        """
        entries = await self._federation_repo.load_replay_cache(
            within_hours=int(REPLAY_CACHE_WINDOW.total_seconds() // 3600),
        )
        self._replay_cache.load(entries)

    # ─── HTTP client helper ───────────────────────────────────────────────

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        """Provide the shared aiohttp session after construction.

        Called from ``app._on_startup`` once the app-wide session has
        been created. Tests may inject a session via the ``http_client``
        constructor kwarg instead.
        """
        if self._http_client is None:
            self._http_client = session

    async def _get_http_client(self):
        """Return the wired aiohttp session.

        Retained as a callable so the ``FederationTransport`` inbox
        strategy can defer client resolution to delivery time.
        """
        if self._http_client is None:
            raise RuntimeError(
                "FederationService used before attach_session — "
                "no aiohttp client wired",
            )
        return self._http_client

    # ─── Outbound ─────────────────────────────────────────────────────────

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> DeliveryResult:
        """Encrypt, sign, and deliver a federation event to a peer.

        Steps (§24.11 §1-§8):

        1. Look up ``RemoteInstance`` by ``to_instance_id``.
        2. Decrypt ``key_self_to_remote`` via ``KeyManager``.
        3. Encrypt the payload JSON with AES-256-GCM.
        4. Build ``FederationEnvelope``.
        5. Sign the serialised envelope with ``own_identity_seed``.
        6. POST to ``remote_inbox_url``.
        7. On success: ``mark_reachable``, return ``DeliveryResult(ok=True)``.
        8. On failure: ``mark_unreachable``, enqueue to outbox, return
           ``DeliveryResult(ok=False)``.
        """
        instance = await self._federation_repo.get_instance(to_instance_id)
        if instance is None:
            log.warning("send_event: unknown instance %s", to_instance_id)
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="unknown_instance",
            )

        # Decrypt the directional session key (stored KEK-encrypted).
        try:
            session_key = self._key_manager.decrypt(instance.key_self_to_remote)
        except Exception as exc:
            # §Audit #12: don't surface the underlying crypto exception
            # text at error level — that leaks detail useful to a key-
            # tampering attacker and the operator can see the full
            # traceback at debug. Fixed-string warn is enough.
            log.warning(
                "send_event: failed to decrypt session key for %s",
                to_instance_id,
            )
            log.debug(
                "send_event: key decrypt error detail for %s: %s",
                to_instance_id,
                exc,
            )
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="key_decrypt_error",
            )

        # Encrypt the payload.
        payload_json = _dumps(payload)
        encrypted_payload = self._encrypt_payload(payload_json, session_key)

        # Build the envelope. The per-peer sig_suite (negotiated at
        # pairing time) decides which algorithms sign this envelope.
        msg_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        effective_suite = instance.sig_suite or self._encoder.sig_suite
        envelope_dict: dict = {
            "msg_id": msg_id,
            "event_type": event_type.value,
            "from_instance": self._own_instance_id,
            "to_instance": to_instance_id,
            "timestamp": timestamp,
            "encrypted_payload": encrypted_payload,
            "space_id": space_id,
            "proto_version": 1,
            "sig_suite": effective_suite,
        }
        # Signatures cover everything except the ``signatures`` field itself.
        envelope_bytes = _dumps(envelope_dict).encode("utf-8")
        envelope_dict["signatures"] = self._encoder.sign_envelope_all(
            envelope_bytes,
            suite=effective_suite,
        )

        # Dispatch the envelope. When a FederationTransport facade is
        # attached, it decides between WebRTC DataChannel (§24.12.5
        # primary) and HTTPS inbox (fallback). Without the facade we
        # run the legacy inline HTTPS-inbox path — still used by federation-
        # level tests that don't construct the facade.
        # Previous reachability state — used to fire ConnectionReachable
        # on the unreachable → reachable transition only (no noise on every
        # successful send).
        was_unreachable = instance.unreachable_since is not None

        status_code: int | None = None
        if self._transport is not None:
            result = await self._transport.send(
                instance=instance,
                envelope_dict=envelope_dict,
            )
            if result.ok:
                await self._federation_repo.mark_reachable(to_instance_id)
                if was_unreachable:
                    await self._bus.publish(
                        ConnectionReachable(instance_id=to_instance_id),
                    )
                return DeliveryResult(
                    instance_id=to_instance_id,
                    ok=True,
                    status_code=result.status_code,
                )
            status_code = result.status_code
        else:
            try:
                client = await self._get_http_client()
                async with client.post(
                    instance.remote_inbox_url,
                    json=envelope_dict,
                    timeout=_aiohttp_timeout(10),
                ) as resp:
                    status_code = resp.status
                    if 200 <= status_code < 300:
                        await self._federation_repo.mark_reachable(to_instance_id)
                        if was_unreachable:
                            await self._bus.publish(
                                ConnectionReachable(instance_id=to_instance_id),
                            )
                        return DeliveryResult(
                            instance_id=to_instance_id,
                            ok=True,
                            status_code=status_code,
                        )
                    log.warning(
                        "send_event: peer %s returned HTTP %d",
                        to_instance_id,
                        status_code,
                    )
            except Exception as exc:
                log.warning(
                    "send_event: transport error to %s: %s",
                    to_instance_id,
                    exc,
                )
                status_code = None

        # Delivery failed — mark and enqueue for retry.
        await self._federation_repo.mark_unreachable(to_instance_id)
        if not was_unreachable:
            # reachable → unreachable edge only (mirrors the reachable
            # publish above) so the SPA flips the dot red live.
            await self._bus.publish(
                ConnectionUnreachable(instance_id=to_instance_id),
            )
        await self._outbox_repo.enqueue(
            instance_id=to_instance_id,
            event_type=event_type,
            payload_json=_dumps(envelope_dict),
            msg_id=msg_id,
        )
        return DeliveryResult(
            instance_id=to_instance_id,
            ok=False,
            status_code=status_code if isinstance(status_code, int) else None,
            error=DELIVERY_ERROR_QUEUED,
        )

    def resign_for_redelivery(self, payload_json: str) -> str:
        """Refresh a queued envelope's timestamp + signature for redelivery.

        The outbox stores the full signed envelope from the original
        :meth:`send_event` call. Re-POSTing it verbatim fails the
        receiver's ±300s skew check once the entry is older than 5 min,
        which would silently drop NEVER_DROP events (bans / key
        revocations / SPACE_DISSOLVED) to any peer offline longer than
        that. We rebuild the canonical envelope (verifier field order)
        with a fresh timestamp and re-sign; ``msg_id`` / ``sig_suite`` /
        ``encrypted_payload`` are preserved so replay-dedup and
        decryption are unaffected.

        The timestamp check runs *before* the replay-cache step on the
        receiver, so a previously skew-rejected envelope was never
        recorded in the replay cache — a re-signed one carrying the same
        ``msg_id`` verifies cleanly and still dedupes a genuinely-
        already-delivered event.

        The field order MUST match
        :func:`~socialhome.federation.inbound_validator.make_verify_signature`'s
        ``envelope_for_verify`` exactly — we rebuild from the parsed
        fields rather than trusting the stored JSON's key order.
        """
        data = _loads(payload_json)
        suite = data.get("sig_suite") or self._encoder.sig_suite
        envelope_dict = {
            "msg_id": data["msg_id"],
            "event_type": data["event_type"],
            "from_instance": data["from_instance"],
            "to_instance": data["to_instance"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "encrypted_payload": data["encrypted_payload"],
            "space_id": data.get("space_id"),
            "proto_version": data.get("proto_version", 1),
            "sig_suite": suite,
        }
        envelope_bytes = _dumps(envelope_dict).encode("utf-8")
        envelope_dict["signatures"] = self._encoder.sign_envelope_all(
            envelope_bytes, suite=suite
        )
        return _dumps(envelope_dict)

    async def send_with_mesh_fallback(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> DeliveryResult:
        """Deliver to ``to_instance_id`` direct when CONFIRMED, else via mesh.

        Behaviour:

        * Peer is :data:`PairingStatus.CONFIRMED`: delegate to
          :meth:`send_event`. Same shape and side effects as a normal
          outbound.
        * Peer row is missing or non-CONFIRMED AND a mesh pair is attached
          via :meth:`attach_mesh`: discover a path via
          :class:`RouteDiscoveryService` and ship SPACE_ROUTED via
          :class:`SpaceRoutedHandler`. Returns ``DeliveryResult(ok=True)``
          on success; ``error="no_route"`` if discovery found nothing.
        * Peer non-CONFIRMED and mesh not attached: returns
          ``DeliveryResult(ok=False, error="not_confirmed")`` — the caller
          decides whether to surface this as a hard error.

        Does NOT raise on transport failure — callers branch on
        ``result.ok``.
        """
        instance = await self._federation_repo.get_instance(to_instance_id)
        if instance is not None and instance.status is PairingStatus.CONFIRMED:
            return await self.send_event(
                to_instance_id=to_instance_id,
                event_type=event_type,
                payload=payload,
                space_id=space_id,
            )
        if self._route_service is None or self._routed_handler is None:
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="not_confirmed",
            )
        # A live negative cooldown means ``discover_route`` will return
        # ``None`` immediately WITHOUT probing (anti-flood). Report that as a
        # distinct, waitable reason instead of the generic "no_route" — a
        # caller in a per-chunk loop otherwise spends its entire failure
        # budget in milliseconds on a window that is about to lift, and
        # abandons a stream the route would have carried seconds later.
        cooldown_s = self._route_service.cooldown_remaining(to_instance_id)
        if cooldown_s > 0.0:
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error=DELIVERY_ERROR_ROUTE_COOLDOWN,
                retry_after_s=cooldown_s,
            )
        discovery = await self._route_service.discover_route(to_instance_id)
        if discovery is None:
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="no_route",
            )
        path, target_eph_pk = discovery
        if len(path) < 2:
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="no_route",
            )
        # The pk discovery just verified for this target rides along so a
        # SPACE_ROUTE_STALE nack is checked against the key WE pinned.
        pinned_pk = self._route_service.cached_target_identity_pk(to_instance_id) or ""
        try:
            await self._routed_handler.send_routed(
                path=path,
                target_eph_pk_b64=target_eph_pk,
                inner_event_type=event_type,
                inner_payload=payload,
                target_identity_pk=pinned_pk,
            )
        except Exception as exc:
            # The cached path is the prime suspect — a relay that has since
            # gone away, or a route whose next hop no longer accepts us.
            # ``discover_route`` is cache-first, so a bare retry would
            # re-use the same dead chain: drop it first, then try once
            # against a freshly-probed path. This is ``invalidate()``'s
            # reason for existing (it had no production caller before #648).
            log.warning(
                "send_with_mesh_fallback: routed ship to %s failed (%s);"
                " invalidating the cached route and retrying once",
                to_instance_id,
                exc,
            )
            await self._route_service.invalidate(to_instance_id)
            retry = await self._route_service.discover_route(to_instance_id)
            if retry is None or len(retry[0]) < 2:
                return DeliveryResult(
                    instance_id=to_instance_id,
                    ok=False,
                    error="no_route",
                )
            retry_path, retry_eph = retry
            try:
                # Deliberately NOT a recursive call back into this helper:
                # one forced re-discovery, one retry, then give up. A
                # recursive retry would let a persistently-broken target
                # amplify a single send into an unbounded probe storm.
                await self._routed_handler.send_routed(
                    path=retry_path,
                    target_eph_pk_b64=retry_eph,
                    inner_event_type=event_type,
                    inner_payload=payload,
                    target_identity_pk=(
                        self._route_service.cached_target_identity_pk(to_instance_id)
                        or ""
                    ),
                )
            except Exception as retry_exc:
                log.warning(
                    "send_with_mesh_fallback: routed retry to %s failed: %s",
                    to_instance_id,
                    retry_exc,
                )
                return DeliveryResult(
                    instance_id=to_instance_id,
                    ok=False,
                    error="routed_send_failed",
                )
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    async def begin_mesh_catchup_sync(
        self,
        *,
        space_id: str,
        host_instance_id: str,
    ) -> bool:
        """Initiate a §25.6 catch-up sync FROM a mesh-only host.

        For a space whose host is NOT a confirmed direct peer (we joined over a
        relay), the normal SpaceSyncScheduler never triggers (it only syncs with
        CONFIRMED peers). This kicks the pull-sync explicitly: register a
        requester-side HTTPS receive session (so the routed SPACE_SYNC_CHUNK
        replies aren't dropped), then send SPACE_SYNC_BEGIN(prefer_direct=False)
        to the host via mesh fallback. No-op for a confirmed host (the scheduler
        already covers it) or when the sync machinery isn't wired. Fail-soft.

        Returns ``True`` only when the BEGIN actually left the building.
        ``False`` marks a *transient* failure — above all ``no_route``, which
        is what a freshly-rebooted household gets while the mesh is still
        warming up — so the caller can retry in seconds rather than writing
        the space off until the next 30-minute tick (#648).
        """
        try:
            if self._sync_manager is None:
                return False
            if await self.is_confirmed_peer(host_instance_id):
                # Not a failure: the confirmed-peer sweep owns this host, so
                # the caller should stop asking about it.
                return True
            sync_id = uuid.uuid4().hex
            self._sync_manager.register_requester_https_session(
                sync_id=sync_id,
                space_id=space_id,
                requester_instance_id=self._own_instance_id,
                provider_instance_id=host_instance_id,
            )
            result = await self.send_with_mesh_fallback(
                to_instance_id=host_instance_id,
                event_type=FederationEventType.SPACE_SYNC_BEGIN,
                payload={
                    "sync_id": sync_id,
                    "space_id": space_id,
                    "sync_mode": "initial",
                    "prefer_direct": False,
                },
                space_id=space_id,
            )
            if not result.ok:
                log.warning(
                    "mesh catch-up sync to host %s for space %s failed: %s",
                    host_instance_id,
                    space_id,
                    result.error,
                )
                # Don't leak a dangling receive-session for a sync that never
                # left the building.
                self._sync_manager.close_session(sync_id)
                return False
            return True
        except Exception as exc:  # fail-soft — must not break invite-accept
            log.warning(
                "begin_mesh_catchup_sync for space %s host %s errored: %s",
                space_id,
                host_instance_id,
                exc,
            )
            return False

    async def send_media_chunk(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        raw_chunk: bytes,
        space_id: str | None = None,
        mesh_fallback: bool = False,
    ) -> DeliveryResult:
        """Ship one media chunk, preferring the binary ``fed-media-v1`` channel.

        The binary channel is used iff **all** hold: the peer is a
        CONFIRMED direct peer, advertises
        :data:`FederationCapability.MIN_FOR_MEDIA_CHANNEL`, and its media
        channel is currently open. Then ``raw_chunk`` ships encrypted as
        binary (no base64) alongside a signed envelope whose encrypted
        metadata carries a ``chunk_sha256`` binding.

        Otherwise — sub-v_14 peer, non-CONFIRMED / mesh-only member, or
        the channel not yet open / a send error — falls back transparently
        to the JSON path: :meth:`send_event` for DM, or
        :meth:`send_with_mesh_fallback` when ``mesh_fallback`` is set (so
        a mesh-only space member still receives the bytes via
        ``SPACE_ROUTED``). The fallback adds the base64 ``bytes_b64``
        field, producing the exact wire shape sub-v_14 peers already
        understand.

        ``payload`` is the chunk metadata dict **without** the bytes; the
        binary path encrypts ``raw_chunk`` separately, the JSON fallback
        re-attaches it as base64.
        """
        instance = await self._federation_repo.get_instance(to_instance_id)
        if (
            self._transport is not None
            and instance is not None
            and instance.status is PairingStatus.CONFIRMED
            and self._transport.is_media_ready(to_instance_id)
            and await self.peer_supports(
                to_instance_id,
                min_version=FederationCapability.MIN_FOR_MEDIA_CHANNEL,
            )
        ):
            result = await self._send_media_binary(
                instance=instance,
                event_type=event_type,
                payload=payload,
                raw_chunk=raw_chunk,
                space_id=space_id,
            )
            if result is not None:
                return result
            # Binary send didn't land (channel raced closed / over HWM) —
            # fall through to the JSON path so the chunk still delivers.

        json_payload = {
            **payload,
            "bytes_b64": base64.b64encode(raw_chunk).decode("ascii"),
        }
        if mesh_fallback:
            return await self.send_with_mesh_fallback(
                to_instance_id=to_instance_id,
                event_type=event_type,
                payload=json_payload,
                space_id=space_id,
            )
        return await self.send_event(
            to_instance_id=to_instance_id,
            event_type=event_type,
            payload=json_payload,
            space_id=space_id,
        )

    async def _send_media_binary(
        self,
        *,
        instance: RemoteInstance,
        event_type: FederationEventType,
        payload: dict,
        raw_chunk: bytes,
        space_id: str | None,
    ) -> DeliveryResult | None:
        """Build + sign the media envelope and ship it as a binary frame.

        Returns :class:`DeliveryResult` on success, ``None`` when the
        binary send didn't land (caller falls back to JSON). Mirrors
        :meth:`send_event`'s envelope construction byte-for-byte — same
        field set, same ``proto_version=1``, same ``_dumps`` + suite-aware
        signing — so the receiver's signature reconstruction matches and
        the chunk inherits the full §24.11 guarantees. The only addition
        is the second AEAD over the raw chunk bytes.
        """
        if self._transport is None:  # pragma: no cover — caller gates on transport
            return None
        try:
            session_key = self._key_manager.decrypt(instance.key_self_to_remote)
        except Exception:
            log.warning(
                "send_media_chunk: failed to decrypt session key for %s",
                instance.id,
            )
            return None
        chunk_sha256 = b64url_encode(hashlib.sha256(raw_chunk).digest())
        metadata = {
            **payload,
            "chunk_sha256": chunk_sha256,
            "media_aead_suite": MEDIA_AEAD_SUITE_AESGCM_256,
        }
        encrypted_payload = self._encrypt_payload(_dumps(metadata), session_key)
        msg_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        effective_suite = instance.sig_suite or self._encoder.sig_suite
        envelope_dict: dict = {
            "msg_id": msg_id,
            "event_type": event_type.value,
            "from_instance": self._own_instance_id,
            "to_instance": instance.id,
            "timestamp": timestamp,
            "encrypted_payload": encrypted_payload,
            "space_id": space_id,
            "proto_version": 1,
            "sig_suite": effective_suite,
        }
        envelope_bytes = _dumps(envelope_dict).encode("utf-8")
        envelope_dict["signatures"] = self._encoder.sign_envelope_all(
            envelope_bytes,
            suite=effective_suite,
        )
        payload_bytes = self._encoder.encrypt_bytes(raw_chunk, session_key)
        sent = await self._transport.send_media(
            instance=instance,
            header_dict=envelope_dict,
            payload_bytes=payload_bytes,
        )
        if not sent:
            return None
        await self._federation_repo.mark_reachable(instance.id)
        return DeliveryResult(instance_id=instance.id, ok=True, status_code=None)

    async def send_app_message(
        self,
        *,
        to_instance_id: str,
        app_id: str,
        session_id: str,
        payload: dict,
        to_user: str | None = None,
        from_user: str | None = None,
    ) -> DeliveryResult:
        """Ship one app message, preferring the binary ``fed-app-v1`` channel.

        The binary channel is used iff **all** hold: the transport is
        attached, the peer is CONFIRMED, ``fed-app-v1`` is currently open
        (``is_app_ready``), and the peer advertises
        :data:`FederationCapability.MIN_FOR_APP_CHANNEL`. Otherwise falls
        back to the JSON event path: :meth:`send_event` with
        ``event_type=APP_MESSAGE`` and the payload nested inside the
        encrypted envelope — encryption-first invariant maintained on both
        paths.

        ``payload`` is the app-specific data dict (chess move, whiteboard
        delta, game-state patch, …). It MUST NOT appear in plaintext in any
        envelope field; both paths place it inside the AES-256-GCM-sealed
        layer.

        ``to_user`` / ``from_user`` are plaintext *routing* fields (a local
        username on each side, never a name/PII) that let the receiver route
        the message to the addressed person instead of fanning out to every
        local user. They ride **only** the JSON ``APP_MESSAGE`` event path:
        the binary ``fed-app-v1`` frame format (v1) carries no routing slot,
        so a binary send stays household-scoped and the receiver disambiguates
        by ``session_id`` (a non-party local user's app ignores an unknown
        session — harmless). Adding routing to the binary fast path is a
        deliberate v2 concern (a new frame field would break the signed-bytes
        contract), so when ``to_user`` is set this method MUST still allow the
        binary path to run — the JSON fallback only carries it when the binary
        send doesn't land. APP_SESSION (the challenge/invite that drives
        notifications) is always JSON and IS per-user routed by the caller.
        """
        instance = await self._federation_repo.get_instance(to_instance_id)
        if (
            self._transport is not None
            and instance is not None
            and instance.status is PairingStatus.CONFIRMED
            and self._transport.is_app_ready(to_instance_id)
            and await self.peer_supports(
                to_instance_id,
                min_version=FederationCapability.MIN_FOR_APP_CHANNEL,
            )
        ):
            result = await self._send_app_binary(
                instance=instance,
                app_id=app_id,
                session_id=session_id,
                payload=payload,
            )
            if result is not None:
                return result
            # Binary send didn't land — fall through to the JSON path.

        event_payload: dict = {
            "app_id": app_id,
            "session_id": session_id,
            "data": payload,
        }
        # Per-user routing fields are plaintext (usernames only) and ride the
        # JSON path exclusively — the binary frame format has no routing slot.
        if to_user is not None:
            event_payload["to_user"] = to_user
        if from_user is not None:
            event_payload["from_user"] = from_user
        return await self.send_event(
            to_instance_id=to_instance_id,
            event_type=FederationEventType.APP_MESSAGE,
            payload=event_payload,
        )

    async def _send_app_binary(
        self,
        *,
        instance: RemoteInstance,
        app_id: str,
        session_id: str,
        payload: dict,
    ) -> DeliveryResult | None:
        """Build + sign the app envelope and ship it as a binary frame.

        Returns :class:`DeliveryResult` on success, ``None`` when the
        binary send didn't land (caller falls back to JSON). Mirrors
        :meth:`_send_media_binary` — same envelope construction, same
        ``_dumps`` + suite-aware signing, same §24.11 guarantees. The
        ``payload`` dict is AES-256-GCM-sealed as the binary payload;
        ``app_id`` / ``session_id`` / ``app_aead_suite`` travel inside
        the signed+encrypted metadata so no plaintext application data
        ever appears in the envelope.
        """
        if self._transport is None:  # pragma: no cover — caller gates on transport
            return None
        try:
            session_key = self._key_manager.decrypt(instance.key_self_to_remote)
        except Exception:
            log.warning(
                "send_app_message: failed to decrypt session key for %s",
                instance.id,
            )
            return None
        # Serialize the plaintext payload before encrypting so we can hash it.
        payload_plaintext = _dumps(payload).encode("utf-8")
        payload_sha256 = b64url_encode(hashlib.sha256(payload_plaintext).digest())
        metadata = {
            "app_id": app_id,
            "session_id": session_id,
            "app_aead_suite": APP_AEAD_SUITE_AESGCM_256,
            # Integrity binding: ties the binary payload to the signed envelope.
            # Mirror of _send_media_binary's chunk_sha256 — prevents a relay from
            # splicing payload_2 under header_1 (signature covers the hash).
            "payload_sha256": payload_sha256,
        }
        encrypted_payload = self._encrypt_payload(_dumps(metadata), session_key)
        msg_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        effective_suite = instance.sig_suite or self._encoder.sig_suite
        envelope_dict: dict = {
            "msg_id": msg_id,
            "event_type": FederationEventType.APP_MESSAGE.value,
            "from_instance": self._own_instance_id,
            "to_instance": instance.id,
            "timestamp": timestamp,
            "encrypted_payload": encrypted_payload,
            "space_id": None,
            # Envelope wire-shape version (not the peer capability proto_version).
            "proto_version": 1,
            "sig_suite": effective_suite,
        }
        envelope_bytes = _dumps(envelope_dict).encode("utf-8")
        envelope_dict["signatures"] = self._encoder.sign_envelope_all(
            envelope_bytes,
            suite=effective_suite,
        )
        # Seal the app payload as binary (not inside the JSON envelope).
        payload_bytes = self._encoder.encrypt_bytes(payload_plaintext, session_key)
        sent = await self._transport.send_app(
            instance=instance,
            header_dict=envelope_dict,
            payload_bytes=payload_bytes,
        )
        if not sent:
            return None
        await self._federation_repo.mark_reachable(instance.id)
        return DeliveryResult(instance_id=instance.id, ok=True, status_code=None)

    async def broadcast_to_peers(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        instance_ids: list[str] | None = None,
        space_id: str | None = None,
    ) -> BroadcastResult:
        """Send to multiple peers (direct-only).

        If ``instance_ids`` is ``None``, sends to all confirmed peers.
        Mesh fallback is deliberately NOT used here — capability /
        directory broadcasts target direct peers by construction.
        Space-content fanout uses :meth:`broadcast_to_space_members`
        which honours mesh routing per-peer.
        """
        if instance_ids is None:
            instances = await self._federation_repo.list_instances(
                status=PairingStatus.CONFIRMED.value,
            )
            instance_ids = [inst.id for inst in instances]

        results: list[DeliveryResult] = []
        for iid in instance_ids:
            result = await self.send_event(
                to_instance_id=iid,
                event_type=event_type,
                payload=payload,
                space_id=space_id,
            )
            results.append(result)

        succeeded = sum(1 for r in results if r.ok)
        return BroadcastResult(
            attempted=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=tuple(results),
        )

    async def broadcast_to_space_members(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
        *,
        min_proto_version: int | None = None,
    ) -> BroadcastResult:
        """Fan out to every member household of ``space_id``.

        Each per-peer ship goes through :meth:`send_with_mesh_fallback`
        so a member whose household is *not* a direct CONFIRMED peer
        (admin-initiated private invite accepted via mesh, post-pairing
        churn, transient unpair) still receives the envelope via
        SPACE_ROUTED when the mesh is wired. Mesh-only members are
        looked up via :meth:`AbstractFederationRepo.list_member_instance_ids`
        — that path skips the ``remote_instances.status = CONFIRMED``
        filter so an unconfirmed-but-reachable member still appears
        in the broadcast set; the per-peer ``send_with_mesh_fallback``
        decides direct vs mesh based on the actual pairing state at
        send time.

        When ``min_proto_version`` is set, peers whose advertised
        ``proto_version`` is below the threshold are skipped silently —
        the outbox never queues, the receiver never sees an unknown
        event_type. Use this for best-effort additions where older
        peers should simply not get the new payload (vs forward-secrecy
        events that MUST always ship and degrade noisily on older
        peers).
        """
        instance_ids = await self._federation_repo.list_member_instance_ids(
            space_id,
        )
        results: list[DeliveryResult] = []
        for iid in instance_ids:
            if iid == self._own_instance_id:
                # ``create_space`` puts the host's OWN row in ``space_instances``,
                # so the query returns the sender itself; sending to self has no
                # ``remote_instances`` row → mesh branch → a wasted
                # ``discover_route(self)`` flood + a false ``no_route`` failure
                # (and a negative cooldown keyed on self) on EVERY broadcast.
                # Skip at the loop — the repo's contract stays "all member
                # households" — and keep self out of attempted/failed/results.
                continue
            if min_proto_version is not None and not await self.peer_supports(
                iid,
                min_version=min_proto_version,
            ):
                # Below threshold — silently skip. Best-effort path.
                continue
            result = await self.send_with_mesh_fallback(
                to_instance_id=iid,
                event_type=event_type,
                payload=payload,
                space_id=space_id,
            )
            results.append(result)
        succeeded = sum(1 for r in results if r.ok)
        broadcast = BroadcastResult(
            attempted=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=tuple(results),
        )
        terminal = broadcast.terminal_failures
        if terminal:
            # Two kinds of ``ok=False`` come back from the per-peer send, and
            # only one is a loss:
            #
            # * Direct path (CONFIRMED peer → :meth:`send_event`): the failed
            #   envelope is enqueued to the durable ``federation_outbox``
            #   BEFORE the result is returned, tagged
            #   :data:`DELIVERY_ERROR_QUEUED`. The outbox redelivers when the
            #   peer is back, so the event self-heals — a household that is
            #   briefly unreachable (e.g. the pair-settle window) is NOT
            #   "not reached". Those are deliberately excluded here; warning
            #   for them was false and noisy.
            # * Mesh path (``no_route`` / ``unknown_instance`` /
            #   ``not_confirmed`` / ``routed_send_failed`` /
            #   ``route_cooldown``): there is NO outbox, so a failed target is
            #   a permanent, invisible loss, and for a mesh-only member that
            #   is the ONLY delivery attempt the event ever gets. A durable
            #   outbox for space gossip is a larger design change; until then
            #   a WARNING naming the space, the event and each such peer is
            #   the minimum bar, so the loss is diagnosable rather than
            #   silent.
            log.warning(
                "broadcast_to_space_members: space=%s event=%s did not reach "
                "%d/%d member household(s): %s",
                space_id,
                event_type.value,
                len(terminal),
                len(results),
                ", ".join(f"{r.instance_id}={r.error}" for r in terminal),
            )
        return broadcast

    # ─── Inbound ──────────────────────────────────────────────────────────

    async def handle_inbound_envelope(
        self,
        inbox_id: str,
        raw_body: bytes,
    ) -> dict:
        """§24.11 validation pipeline for an inbound federation inbox.

        Delegates to :class:`InboundPipeline` — a composable middleware
        chain where each step (JSON parse → instance lookup → timestamp
        check → signature verify → replay check → decrypt → idempotency
        → ban check → persist replay) is an independently-testable
        async callable. See ``federation/inbound_validator.py``.

        Returns ``{"status": "ok"}`` on success.
        Raises ``ValueError`` on any validation failure (caller returns 400/403).
        """
        if self._inbound_pipeline is None:
            self._inbound_pipeline = self._build_inbound_pipeline()

        pipeline: InboundPipeline = self._inbound_pipeline  # type: ignore[assignment]
        ctx = InboundContext(raw_body=raw_body, inbox_id=inbox_id)
        result = await pipeline.run(ctx)

        # Early-response means a step (e.g. idempotency) short-circuited.
        if ctx.early_response is not None:
            return result

        # Dispatch the validated event.
        if ctx.event is not None:
            await self._dispatch_event(ctx.event)

        return result

    async def validate_inbound_rtc(
        self,
        instance_id: str,
        raw_body: bytes,
    ) -> InboundContext:
        """Run the §24.11 RTC validation pipeline WITHOUT dispatching.

        Resolves the sender by ``instance_id`` (already known from the
        peer connection) instead of ``inbox_id``, runs the full
        validation chain (parse → lookup → timestamp → sig verify →
        replay → decrypt → idempotency → ban → persist replay →
        deprovisioned-author), and returns the populated
        :class:`InboundContext`. ``ctx.event`` is the validated event
        (or ``None`` if a step set ``ctx.early_response``).

        Shared by :meth:`handle_inbound_rtc` (which then dispatches) and
        :meth:`handle_inbound_media_frame` (which attaches the decrypted
        chunk bytes before dispatch). Keeping the chain identical means
        the binary media path inherits replay, ban, and idempotency
        protection unchanged.
        """
        if self._rtc_inbound_pipeline is None:
            self._rtc_inbound_pipeline = self._build_rtc_inbound_pipeline()
        pipeline: InboundPipeline = self._rtc_inbound_pipeline  # type: ignore[assignment]
        ctx = InboundContext(raw_body=raw_body, instance_id=instance_id)
        await pipeline.run(ctx)
        return ctx

    async def handle_inbound_rtc(
        self,
        instance_id: str,
        raw_body: bytes,
    ) -> dict:
        """§24.11 validation pipeline for a WebRTC DataChannel frame.

        Same pipeline as :meth:`handle_inbound_envelope` but resolves
        the sender by ``instance_id`` (already known from the peer
        connection) instead of ``inbox_id``.
        """
        ctx = await self.validate_inbound_rtc(instance_id, raw_body)

        if ctx.early_response is not None:
            return ctx.early_response

        if ctx.event is not None:
            await self._dispatch_event(ctx.event)

        return {"status": "ok"}

    async def handle_inbound_media_frame(
        self,
        instance_id: str,
        header_bytes: bytes,
        payload_bytes: bytes,
    ) -> dict:
        """Validate + decrypt one binary media frame, then dispatch.

        Inbound entry point for the ``fed-media-v1`` channel. The frame
        header is a signed federation envelope carrying the chunk
        metadata as its (encrypted) payload; ``payload_bytes`` is the
        AES-256-GCM-encrypted chunk bound to that envelope by a
        ``chunk_sha256`` field inside the signed+encrypted metadata.

        Flow:

        1. Run the full §24.11 pipeline on the header (origin auth via
           Ed25519, replay, timestamp, ban, decrypt metadata). Honour
           ``ctx.early_response`` (idempotency / deprovisioned-author)
           exactly like dispatch would — skip assembly on a short-circuit.
        2. Validate ``media_aead_suite`` against the supported set — no
           default fallback (CLAUDE.md crypto-suite rule).
        3. Decrypt the chunk under the directional receive key.
        4. Verify ``sha256(plaintext) == chunk_sha256`` (constant-time).
           Because the hash lives inside the signature-covered encrypted
           metadata, this binds the binary payload to the signed
           envelope: tamper the bytes → hash mismatch; tamper the hash →
           GCM tag / signature failure.
        5. Attach the raw bytes to the event and dispatch through the
           SAME registry the JSON media events use — no new dispatch
           surface; the handlers prefer ``event.media_bytes`` over the
           base64 ``bytes_b64``.

        Raises :class:`ValueError` (or
        :class:`~socialhome.federation.media_framing.UnsupportedMediaAeadSuite`)
        on any validation failure so the transport logs + drops the frame.
        """
        ctx = await self.validate_inbound_rtc(instance_id, header_bytes)
        if ctx.early_response is not None:
            return ctx.early_response
        event = ctx.event
        if (
            event is None
        ):  # pragma: no cover — pipeline always sets event or early_response
            return {"status": "ok"}
        meta = event.payload if isinstance(event.payload, dict) else {}
        suite = meta.get("media_aead_suite")
        if suite not in SUPPORTED_MEDIA_AEAD_SUITES:
            raise UnsupportedMediaAeadSuite(
                f"unknown media_aead_suite: {suite!r}",
            )
        # The pipeline already decrypted the metadata payload with this
        # same directional key, so it is known-good here — no second
        # try/except needed.
        session_key = self._key_manager.decrypt(ctx.instance.key_remote_to_self)
        try:
            raw = self._encoder.decrypt_bytes(payload_bytes, session_key)
        except Exception as exc:
            raise ValueError(f"Failed to decrypt media chunk: {exc}") from exc
        expected = str(meta.get("chunk_sha256") or "")
        actual = b64url_encode(hashlib.sha256(raw).digest())
        if not expected or not hmac.compare_digest(expected, actual):
            raise ValueError("media chunk sha256 mismatch")
        await self._dispatch_event(replace(event, media_bytes=raw))
        return {"status": "ok"}

    async def _dispatch_event(self, event: FederationEvent) -> None:
        """Route a validated inbound event to registered handlers.

        Handlers register themselves via attach_* methods on the event dispatcher.
        This eliminates if/elif chains and None checks — each handler is only
        invoked if it was registered.
        """
        log.debug(
            "federation event from=%s type=%s space=%s",
            event.from_instance,
            event.event_type,
            event.space_id,
        )

        await self._event_registry.dispatch(event)

    def _register_default_handlers(self) -> None:
        """Register built-in event handlers available in all configurations.

        Optional services register additional handlers via attach_* methods.
        """
        # Space config — always active
        self._event_registry.register(
            FederationEventType.SPACE_CONFIG_CHANGED,
            self._handle_space_config_changed,
        )

        # Pairing intro relay — always active
        self._event_registry.register(
            FederationEventType.PAIRING_INTRO_RELAY,
            self._handle_pairing_intro_relay,
        )

        # Direct DataChannel sync handlers — always active
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_BEGIN,
            self._handle_space_sync_begin,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_OFFER,
            self._handle_space_sync_offer,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_ANSWER,
            self._handle_space_sync_answer,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_ICE,
            self._handle_space_sync_ice,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_DIRECT_READY,
            self._handle_space_sync_direct_ready,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_DIRECT_FAILED,
            self._handle_space_sync_direct_failed,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_REQUEST_MORE,
            self._handle_space_sync_request_more,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_CHUNK,
            self._handle_space_sync_chunk,
        )
        self._event_registry.register(
            FederationEventType.SPACE_SYNC_COMPLETE,
            self._handle_space_sync_complete,
        )
        self._event_registry.register(
            FederationEventType.INSTANCE_SYNC_STATUS,
            self._handle_instance_sync_status,
        )

        # Peer home-location update — always active
        self._event_registry.register(
            FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
            self._on_local_home_location_changed,
        )

        # Inbound media validation — strip non-conforming file_meta from
        # post payloads so the text is kept but invalid media is dropped.
        for _evt in (
            FederationEventType.SPACE_POST_CREATED,
            FederationEventType.SPACE_POST_UPDATED,
        ):
            self._event_registry.register(_evt, self._validate_inbound_media)

    # ─── Event handler dispatch (registry pattern) ───────────────────────────

    async def _handle_space_config_changed(self, event: FederationEvent) -> None:
        if event.space_id:
            await self._bus.publish(
                SpaceConfigChanged(
                    space_id=event.space_id,
                    event_type=event.event_type.value,
                    payload=event.payload,
                    sequence=int(event.payload.get("sequence", 0)),
                )
            )

    async def _validate_inbound_media(self, event: FederationEvent) -> None:
        """Validate ``file_meta`` in SPACE_POST_CREATED / SPACE_POST_UPDATED.

        On failure the ``file_meta`` key is stripped from the payload so
        downstream handlers still receive the post text. A warning is logged
        so operators can spot non-conforming peers.
        """
        file_meta = event.payload.get("file_meta")
        if file_meta is None:
            return
        try:
            validate_inbound_media_meta(file_meta)
        except ValueError as exc:
            log.warning(
                "Stripping invalid file_meta from %s (from=%s): %s",
                event.event_type,
                event.from_instance,
                exc,
            )
            event.payload.pop("file_meta", None)

    async def _on_local_home_location_changed(self, event: FederationEvent) -> None:
        """Inbound LOCAL_HOME_LOCATION_CHANGED: update peer row + publish PeerHomeChanged."""
        if "latitude" not in event.payload or "longitude" not in event.payload:
            log.warning(
                "LOCAL_HOME_LOCATION_CHANGED from %s missing latitude/longitude",
                event.from_instance,
            )
            return
        lat_raw = event.payload["latitude"]
        lon_raw = event.payload["longitude"]
        # Both-null = revoke signal: peer asks us to clear our copy.
        if lat_raw is None and lon_raw is None:
            latitude: float | None = None
            longitude: float | None = None
        elif lat_raw is None or lon_raw is None:
            log.warning(
                "LOCAL_HOME_LOCATION_CHANGED from %s has asymmetric nulls; ignored",
                event.from_instance,
            )
            return
        else:
            try:
                latitude = round(float(lat_raw), 4)
                longitude = round(float(lon_raw), 4)
            except (TypeError, ValueError):  # fmt: skip
                log.warning(
                    "LOCAL_HOME_LOCATION_CHANGED from %s has non-numeric coordinates",
                    event.from_instance,
                )
                return
        await self._federation_repo.update_instance_home(
            event.from_instance,
            latitude=latitude,
            longitude=longitude,
        )
        await self._bus.publish(
            PeerHomeChanged(
                instance_id=event.from_instance,
                latitude=latitude,
                longitude=longitude,
            ),
        )

    # ─── Domain-event subscribers (outbound fan-out) ─────────────────────────

    async def _on_local_home_location_updated(
        self,
        event: LocalHomeLocationUpdated,
    ) -> None:
        """Fan out LOCAL_HOME_LOCATION_CHANGED to every confirmed peer at proto_version >= 5 that has share_home enabled."""
        peers = await self._federation_repo.list_instances(status="confirmed")
        payload = {"latitude": event.latitude, "longitude": event.longitude}
        for peer in peers:
            if not peer.share_home:
                continue
            if not await self.peer_supports(
                peer.id,
                min_version=FederationCapability.MIN_FOR_HOME_LOCATION_BROADCAST,
            ):
                continue
            # Serial fanout: send_event does inline HTTP I/O; gather() would
            # parallelise at the cost of more complex error handling — acceptable
            # for the typical 1-3 peer count of a home instance.
            await self.send_event(
                to_instance_id=peer.id,
                event_type=FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
                payload=payload,
            )

    async def _handle_pairing_intro_relay(self, event: FederationEvent) -> None:
        """§11.9 friend-of-friend introduction request."""
        target = str(event.payload.get("target_instance_id") or "")
        message = str(event.payload.get("message") or "")[:500]
        log.info(
            "PAIRING_INTRO_RELAY: %s wants to introduce %s (via us)",
            event.from_instance,
            target,
        )
        await self._bus.publish(
            PairingIntroRelayReceived(
                from_instance=event.from_instance,
                target_instance_id=target,
                message=message,
            )
        )

    async def _handle_dm_relay(self, event: FederationEvent) -> None:
        if self._dm_routing_service is not None:
            outcome = await self._dm_routing_service.handle_inbound_relay(event)
            log.debug(
                "DM_RELAY %s from %s → %s",
                event.payload.get("message_id"),
                event.from_instance,
                outcome,
            )

    async def _handle_dm_user_typing(self, event: FederationEvent) -> None:
        if self._typing_service is not None:
            await self._typing_service.handle_remote_typing(event)

    async def _handle_presence_updated(self, event: FederationEvent) -> None:
        if self._presence_service is not None:
            await self._presence_service.apply_remote(
                from_instance=event.from_instance,
                payload=event.payload,
            )
        else:
            log.debug(
                "PRESENCE_UPDATED from %s dropped — no service attached",
                event.from_instance,
            )

    async def _handle_user_online_changed(self, event: FederationEvent) -> None:
        if self._online_status_service is None:
            log.debug(
                "%s from %s dropped — no online-status service attached",
                event.event_type.value,
                event.from_instance,
            )
            return
        await self._online_status_service.apply_remote(
            from_instance=event.from_instance,
            event_type=event.event_type,
            payload=event.payload,
        )

    async def _handle_transport_event(self, event: FederationEvent) -> None:
        """Dispatch P2P federation RTC events to the transport."""
        if self._transport is None:
            return
        match event.event_type:
            case FederationEventType.FEDERATION_RTC_OFFER:
                await self._transport.on_rtc_offer(
                    from_instance=event.from_instance,
                    payload=event.payload,
                )
            case FederationEventType.FEDERATION_RTC_ANSWER:
                await self._transport.on_rtc_answer(
                    from_instance=event.from_instance,
                    payload=event.payload,
                )
            case FederationEventType.FEDERATION_RTC_ICE:
                await self._transport.on_rtc_ice(
                    from_instance=event.from_instance,
                    payload=event.payload,
                )

    async def _handle_call_signal(self, event: FederationEvent) -> None:
        if self._call_signaling is not None:
            await self._call_signaling.handle_federated_signal(event)

    def close_sync_session(self, sync_id: str) -> None:
        """Tear down an in-memory sync session by id. No-op if unknown.

        Public so the provider can drop a stream it has abandoned. Leaving
        the session open costs the *requester* one of its
        ``MAX_ACTIVE_SESSIONS_PER_INSTANCE`` slots for the full stale-session
        TTL (30 min) — and since a mesh requester recovers by re-issuing
        BEGIN, a leaked session blocks the exact retry that would fix it
        (#648).
        """
        if self._sync_manager is not None:
            self._sync_manager.close_session(sync_id)

    async def _handle_space_sync_complete(self, event: FederationEvent) -> None:
        if self._sync_manager is not None:
            self._sync_manager.close_session(
                event.payload.get("sync_id", ""),
            )

    async def _handle_space_sync_begin(self, event) -> None:
        """Provider receives SPACE_SYNC_BEGIN — admit + create session.

        S-6 / S-8 admission is delegated to :class:`SyncSessionManager`.
        On rejection we send the canonical
        ``SPACE_SYNC_DIRECT_FAILED`` reply via the relay so the
        requester can fall back.
        """
        if self._sync_manager is None:
            return
        payload = event.payload
        sync_id = payload.get("sync_id") or ""
        space_id = event.space_id or payload.get("space_id") or ""
        if not sync_id or not space_id:
            return
        decision = await self._sync_manager.begin_session(
            sync_id=sync_id,
            space_id=space_id,
            requester_instance_id=event.from_instance,
            provider_instance_id=self._own_instance_id,
            sync_mode=str(payload.get("sync_mode", "initial")),
            ice_servers=self._ice_servers,
        )
        if not decision.accepted and decision.next_event is not None:
            if (
                decision.next_event is FederationEventType.SPACE_SYNC_REJECTED
                and not await self.peer_supports(
                    event.from_instance,
                    min_version=FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED,
                )
            ):
                # Sub-v_20 peer has no SPACE_SYNC_REJECTED handler — fall back
                # to the S-1 silent drop (the member still reconciles via the
                # normal SPACE_DISSOLVED broadcast / outbox).
                return
            # A mesh-only requester is not a CONFIRMED peer, so a bare
            # ``send_event`` here is silence — and silence is exactly what
            # makes a requester retry into the rate limit forever. Route
            # every rejection (``not_a_member``, ``rate_limited``,
            # ``too_many_sessions``, ``node_capacity``) through the mesh
            # fallback so it lands either way.
            await self.send_with_mesh_fallback(
                to_instance_id=event.from_instance,
                event_type=decision.next_event,
                payload=decision.next_payload or {},
                space_id=space_id,
            )
            return

        # A requester reachable only via the mesh (not a CONFIRMED direct
        # peer) cannot complete the WebRTC handshake — ICE can't traverse a
        # relay — so force HTTPS/event-chunk mode regardless of what
        # prefer_direct says. The RTC offer below would otherwise go out over
        # direct send_event and never reach a mesh-only peer.
        requester_is_mesh = not await self.is_confirmed_peer(event.from_instance)

        if decision.accepted and (
            requester_is_mesh or not bool(payload.get("prefer_direct"))
        ):
            # Relay-mode sync (Part C). ``prefer_direct=False`` arrives
            # either because the requester hit a 15 s ICE timeout and
            # called ``trigger_relay_sync``, or because they know
            # WebRTC won't work for them (carrier-grade NAT, no STUN
            # reachable, …). Stream chunks straight over signed
            # ``SPACE_SYNC_CHUNK`` federation events — the provider
            # doesn't bother with an SDP offer / answer dance.
            if requester_is_mesh and self._route_service is not None:
                # #648 — every chunk of this stream is sealed under the
                # ``target_eph_pk`` our route cache holds for the requester,
                # but the matching private half only ever lived in *their*
                # RAM. If they restarted (or the cached entry is near the end
                # of its window) that pub is already dead and every chunk is
                # silently dropped on arrival, losing the metadata for good —
                # the media path self-heals via its durable outbox, the
                # one-shot metadata stream does not.
                #
                # A BEGIN is proof the requester is alive *now*, so drop the
                # cached route: the first chunk's ``discover_route`` then
                # probes afresh and the whole stream is sealed under a
                # full-TTL key minted by the requester's current process.
                # Bounded by the host's own 5/h ``check_sync_begin_rate``.
                #
                # Only routes minted before this requester's PREVIOUS begin
                # are suspect. Invalidating unconditionally would discard a
                # route we re-discovered for that previous begin — including
                # one warmed by a ROUTE_FOUND that arrived just after the
                # discovery window closed — and re-probe straight back into
                # the same timeout, so a retrying requester could never make
                # progress.
                cutoff = self._last_mesh_begin_at.get(
                    event.from_instance,
                    time.monotonic(),
                )
                await self._route_service.invalidate_if_older_than(
                    event.from_instance,
                    cutoff=cutoff,
                )
                self._last_mesh_begin_at[event.from_instance] = time.monotonic()
            record = self._sync_manager.get_session(sync_id)
            if record is not None:
                record.transport_mode = "https"
                if record.rtc is not None:
                    # No RTC needed for HTTPS-mode delivery; tear it
                    # down so the PeerConnection doesn't sit around
                    # for the 15 s ICE timeout that would never fire.
                    record.rtc.close()
                    record.rtc = None
                if self._space_sync_service is not None:
                    asyncio.create_task(
                        self._space_sync_service.stream_initial(record),
                        name=f"space-sync-https-initial-{sync_id}",
                    )
            return

        if (
            decision.accepted
            and not requester_is_mesh
            and bool(payload.get("prefer_direct"))
        ):
            # Build SDP offer, send SPACE_SYNC_OFFER back over relay.
            record = self._sync_manager.get_session(sync_id)
            if record is not None and record.rtc is not None:
                sdp_offer = await record.rtc.create_offer()
                # Spec §24.10.7 — ask the paired GFS for a signaling
                # node so ICE candidates spread across cluster peers.
                # ``None`` = single-node GFS or no GFS paired; field is
                # then omitted from the offer per the spec.
                signaling_node: str | None = None
                if self._gfs_connection_service is not None:
                    signaling_node = (
                        await self._gfs_connection_service.request_signaling_node(
                            sync_id,
                            from_instance=self._own_instance_id,
                            signing_key=self._own_identity_seed,
                        )
                    )
                    if signaling_node:
                        record.signaling_node = signaling_node
                offer_payload: dict = {
                    "sync_id": sync_id,
                    "sdp_offer": sdp_offer,
                    "ice_servers": self._ice_servers,
                }
                if signaling_node:
                    offer_payload["signaling_node"] = signaling_node
                await self.send_event(
                    to_instance_id=event.from_instance,
                    event_type=FederationEventType.SPACE_SYNC_OFFER,
                    payload=offer_payload,
                    space_id=space_id,
                )

    async def _handle_space_sync_offer(self, event) -> None:
        """Requester receives SPACE_SYNC_OFFER — generate + send answer.

        After dispatching the answer this also spawns the DataChannel
        readiness watcher (Part A) that emits
        ``SPACE_SYNC_DIRECT_READY`` once the channel opens (so the
        provider can begin streaming) or ``SPACE_SYNC_DIRECT_FAILED``
        on a 15 s ICE timeout (so the existing relay-fallback hook
        re-issues the BEGIN with ``prefer_direct=False``). Before this
        fix the channel-open watcher set a local ``_ready`` event that
        nothing waited on — the happy path produced no useful state.
        """
        if self._sync_manager is None:
            return
        payload = event.payload
        sync_id = payload.get("sync_id") or ""
        sdp_offer = payload.get("sdp_offer") or ""
        space_id = event.space_id or ""
        if not sync_id or not sdp_offer:
            return
        # The OFFER/ANSWER/ICE dance is direct-path only: ICE can't traverse
        # a relay, and the host never offers a mesh-only requester (it forces
        # HTTPS mode in ``_handle_space_sync_begin``). An OFFER from a
        # provider we hold no CONFIRMED row for therefore has no answer we
        # could deliver — a plain ``send_event`` would be an un-queued
        # ``unknown_instance`` drop. Skip the whole handshake at debug.
        if not await self.is_confirmed_peer(event.from_instance):
            log.debug(
                "sync %s: SPACE_SYNC_OFFER from mesh-only provider %s — "
                "direct path unavailable, ignoring offer",
                sync_id,
                event.from_instance,
            )
            return
        sdp_answer = await self._sync_manager.apply_offer(
            sync_id=sync_id,
            sdp_offer=sdp_offer,
            requester_instance_id=self._own_instance_id,
            space_id=space_id,
            ice_servers=payload.get("ice_servers"),
        )
        await self.send_event(
            to_instance_id=event.from_instance,
            event_type=FederationEventType.SPACE_SYNC_ANSWER,
            payload={"sync_id": sync_id, "sdp_answer": sdp_answer},
            space_id=space_id,
        )
        record = self._sync_manager.get_session(sync_id)
        if record is not None and record.rtc is not None:
            record.rtc_watcher = asyncio.create_task(
                self._watch_requester_rtc(record, event.from_instance),
                name=f"space-sync-watch-{sync_id}",
            )

    async def _watch_requester_rtc(
        self,
        record,
        provider_instance_id: str,
    ) -> None:
        """Requester-side: wait for the DataChannel to open, emit
        DIRECT_READY/FAILED, and drain incoming chunks into the
        receiver.

        Owned by :meth:`_handle_space_sync_offer`. Cancelled when the
        session is closed.
        """
        rtc_session = record.rtc
        if rtc_session is None:
            return
        try:
            ready = await rtc_session.wait_ready()
        except Exception:
            log.exception(
                "sync %s: wait_ready raised",
                record.sync_id,
            )
            ready = False
        if not ready:
            log.info(
                "sync %s: DataChannel not ready in 15 s — sending DIRECT_FAILED",
                record.sync_id,
            )
            # The provider must hear this to release its half of the
            # session (RTC handle + GFS signaling node). A mesh-only
            # provider is not a CONFIRMED peer, so route through the mesh
            # fallback — it short-circuits to ``send_event`` for a paired
            # peer, so the direct path is unchanged.
            try:
                await self.send_with_mesh_fallback(
                    to_instance_id=provider_instance_id,
                    event_type=FederationEventType.SPACE_SYNC_DIRECT_FAILED,
                    payload={
                        "sync_id": record.sync_id,
                        "reason": "ice_timeout",
                    },
                    space_id=record.space_id,
                )
            except Exception:
                log.exception(
                    "sync %s: failed to send DIRECT_FAILED",
                    record.sync_id,
                )
            # Locally drive the relay retry — the requester owns the
            # BEGIN dispatch direction. Provider only gates the retry
            # on its v_13 capability (older providers can't accept
            # ``prefer_direct=False``); the manager-level guard
            # (Tier 3 abort) still applies inside
            # ``trigger_relay_sync``.
            if self._sync_manager is None:
                return
            await self._maybe_trigger_relay_retry(
                record.sync_id,
                provider_instance_id,
            )
            return
        # Channel open — tell the provider and start consuming chunks.
        # DIRECT_READY only exists on the direct path (an open DataChannel);
        # a mesh-only provider never sent the OFFER that could open one, so
        # rather than plain-send into ``unknown_instance``, skip at debug.
        if not await self.is_confirmed_peer(provider_instance_id):
            log.debug(
                "sync %s: DataChannel ready but provider %s is mesh-only — "
                "not sending DIRECT_READY",
                record.sync_id,
                provider_instance_id,
            )
            return
        try:
            await self.send_event(
                to_instance_id=provider_instance_id,
                event_type=FederationEventType.SPACE_SYNC_DIRECT_READY,
                payload={"sync_id": record.sync_id},
                space_id=record.space_id,
            )
        except Exception:
            log.exception(
                "sync %s: failed to send DIRECT_READY",
                record.sync_id,
            )
            return
        if self._space_sync_receiver is None:
            return
        while True:
            try:
                raw = await rtc_session.recv_chunk()
            except ConnectionError:
                # Channel closed — normal end of stream or peer hung up.
                return
            except Exception:
                log.exception(
                    "sync %s: recv_chunk raised",
                    record.sync_id,
                )
                return
            try:
                await self._space_sync_receiver.on_chunk(
                    raw,
                    from_instance=provider_instance_id,
                )
            except Exception:
                log.exception(
                    "sync %s: receiver.on_chunk raised",
                    record.sync_id,
                )

    async def _handle_space_sync_answer(self, event) -> None:
        """Provider receives SPACE_SYNC_ANSWER — applies S-14 origin guard."""
        if self._sync_manager is None:
            return
        payload = event.payload
        sync_id = payload.get("sync_id") or ""
        sdp_answer = payload.get("sdp_answer") or ""
        if not sync_id or not sdp_answer:
            return
        await self._sync_manager.apply_answer(
            sync_id=sync_id,
            sdp_answer=sdp_answer,
            from_instance=event.from_instance,
        )

    async def _handle_space_sync_ice(self, event) -> None:
        """Either side: trickle an ICE candidate through with S-7 validation."""
        if self._sync_manager is None:
            return
        payload = event.payload
        sync_id = payload.get("sync_id") or ""
        candidate = payload.get("candidate") or ""
        if not sync_id or not candidate:
            return
        # ``sdp_mid`` is optional — peers prior to the outbound-trickle
        # work omitted it and the manager defaulted to "0". Forward
        # whatever the sender chose; the manager / RTC session
        # consume the same default if missing.
        sdp_mid = str(payload.get("sdp_mid") or "0")
        await self._sync_manager.apply_ice(
            sync_id=sync_id,
            candidate=candidate,
            sdp_mid=sdp_mid,
        )

    async def _handle_space_sync_direct_ready(self, event) -> None:
        """DataChannel open → provider starts streaming content (§25.6)."""
        log.debug(
            "SPACE_SYNC_DIRECT_READY from %s sync_id=%s",
            event.from_instance,
            event.payload.get("sync_id"),
        )
        if self._sync_manager is None or self._space_sync_service is None:
            return
        sync_id = str(event.payload.get("sync_id") or "")
        if not sync_id:
            return
        session = self._sync_manager.get_session(sync_id)
        if session is None:
            return
        # Only the provider streams — the peer sending READY must be the
        # requester recorded at begin_session time.
        if session.requester_instance_id != event.from_instance:
            return
        await self._release_signaling_node(session)
        asyncio.create_task(
            self._space_sync_service.stream_initial(session),
            name=f"space-sync-initial-{sync_id}",
        )

    async def _handle_space_sync_direct_failed(self, event) -> None:
        """Direct path failed — fall back to relay sync (S-15).

        The originating direction of DIRECT_FAILED determines who
        owns the retry. If the local instance is the **requester** for
        this sync_id (the typical case: provider was rate-limited and
        sent DIRECT_FAILED back), this side calls
        :meth:`trigger_relay_sync` and re-issues the BEGIN with
        ``prefer_direct=False``. If the local instance is the
        **provider** (Part A path: requester's 15 s ICE watcher
        observed the timeout and emitted DIRECT_FAILED so we'd
        release our session + signaling node), we MUST NOT re-issue
        the BEGIN — the requester drives the retry from their side
        and a bounced BEGIN here would land back at the requester
        as a federation event they don't expect.
        """
        if self._sync_manager is None:
            return
        sync_id = event.payload.get("sync_id") or ""
        if not sync_id:
            return
        session = self._sync_manager.get_session(sync_id)
        if session is not None:
            await self._release_signaling_node(session)
            if session.provider_instance_id == self._own_instance_id:
                # Provider-side cleanup only — close the session, drop
                # the RTC handle. The requester will mint a fresh BEGIN
                # against us with ``prefer_direct=False``.
                self._sync_manager.close_session(sync_id)
                return
        decision = await self._sync_manager.trigger_relay_sync(sync_id)
        if decision.next_event is not None:
            # The relay BEGIN is the protocol step a mesh-relayed session
            # needs most: a mesh-only requester has no row for its provider,
            # so a plain ``send_event`` here is an un-queued drop. The
            # fallback short-circuits to ``send_event`` for a CONFIRMED peer.
            await self.send_with_mesh_fallback(
                to_instance_id=event.from_instance,
                event_type=decision.next_event,
                payload=decision.next_payload or {},
                space_id=event.space_id,
            )

    async def _maybe_trigger_relay_retry(
        self,
        sync_id: str,
        provider_instance_id: str,
    ) -> None:
        """Requester-side: turn an ICE timeout into a fresh BEGIN with
        ``prefer_direct=False``, gated on the provider supporting v_13+.

        Sub-v_13 providers don't know how to handle the relay branch
        of ``_handle_space_sync_begin``; sending the retry would leave
        the requester waiting on a session the provider will never
        start streaming. Skip the retry there and log loud — the
        operator-visible diagnosis is "WebRTC isn't reaching this peer
        AND they're too old for the HTTPS rescue; upgrade or fix
        TURN".
        """
        if self._sync_manager is None:
            return
        supports = await self.peer_supports(
            provider_instance_id,
            min_version=FederationCapability.MIN_FOR_SYNC_HTTPS_FALLBACK,
        )
        if not supports:
            log.warning(
                "sync %s: provider %s does not advertise v_%d — HTTPS "
                "fallback unavailable, sync will not complete",
                sync_id,
                provider_instance_id,
                FederationCapability.MIN_FOR_SYNC_HTTPS_FALLBACK,
            )
            self._sync_manager.close_session(sync_id)
            return
        decision = await self._sync_manager.trigger_relay_sync(sync_id)
        if decision.next_event is None:
            return
        # Same reasoning as ``_handle_space_sync_direct_failed``: the
        # re-BEGIN must reach a mesh-only provider too, and the fallback is
        # a no-op wrapper for a CONFIRMED one.
        try:
            await self.send_with_mesh_fallback(
                to_instance_id=provider_instance_id,
                event_type=decision.next_event,
                payload=decision.next_payload or {},
                space_id=(decision.next_payload or {}).get("space_id") or "",
            )
        except Exception:
            log.exception(
                "sync %s: failed to send relay-fallback BEGIN",
                sync_id,
            )

    async def _release_signaling_node(self, session) -> None:
        """Release the GFS-side counter for a sync session (spec §24.10.7).

        Idempotent: clears ``session.signaling_node`` after release so a
        subsequent ``DIRECT_FAILED`` after a ``DIRECT_READY`` does not
        decrement twice on this end (the GFS endpoint floors at 0
        anyway).
        """
        if self._gfs_connection_service is None:
            return
        node = session.signaling_node
        if not node:
            return
        session.signaling_node = None
        await self._gfs_connection_service.release_signaling_node(
            session.sync_id,
            node,
            from_instance=self._own_instance_id,
            signing_key=self._own_identity_seed,
        )

    async def _handle_space_sync_chunk(self, event) -> None:
        """Inbound ``SPACE_SYNC_CHUNK`` — HTTPS-fallback chunk delivery.

        WebRTC is the primary transport (``_send`` → ``rtc.send_chunk``
        when the session has an open DataChannel). When the channel
        never opens — carrier-grade NAT, no reachable STUN, hostile
        firewall — the provider streams chunks over signed federation
        events instead. This handler validates the from_instance
        matches the session's provider and forwards the inner chunk
        body to the receiver, which runs the same signature +
        decryption pipeline RTC frames go through.

        The chunk payload itself is already a fully signed envelope
        produced by the encoder — we don't strip / re-wrap anything
        here. The HTTPS hop just ferries opaque bytes the way the
        DataChannel does.
        """
        if self._sync_manager is None or self._space_sync_receiver is None:
            return
        payload = event.payload or {}
        sync_id = str(payload.get("sync_id") or "")
        raw = payload.get("chunk")
        if not sync_id or not raw:
            return
        record = self._sync_manager.get_session(sync_id)
        if record is None:
            log.debug(
                "SPACE_SYNC_CHUNK for unknown sync_id=%s from %s",
                sync_id,
                event.from_instance,
            )
            return
        # The provider on the originating side IS the from_instance for
        # an HTTPS-mode session. The receiver re-checks the per-chunk
        # signature against the peer's identity key, so this is a
        # belt-and-braces guard, not the authoritative check.
        if (
            record.provider_instance_id
            and record.provider_instance_id != event.from_instance
        ):
            log.warning(
                "SPACE_SYNC_CHUNK sync_id=%s from %s != expected provider %s",
                sync_id,
                event.from_instance,
                record.provider_instance_id,
            )
            return
        try:
            await self._space_sync_receiver.on_chunk(
                raw,
                from_instance=event.from_instance,
            )
        except Exception:
            log.exception(
                "sync %s: HTTPS receiver.on_chunk raised",
                sync_id,
            )

    async def _handle_space_sync_request_more(self, event) -> None:
        """Requester asks for an older slice (S-12 bounds check)."""
        if self._sync_manager is None:
            return
        cleaned = await self._sync_manager.clamp_request_more(event.payload)
        if cleaned is None:
            return
        log.debug(
            "SPACE_SYNC_REQUEST_MORE from %s: %s",
            event.from_instance,
            cleaned,
        )
        if self._space_sync_service is None:
            return
        sync_id = str(cleaned.get("sync_id") or event.payload.get("sync_id") or "")
        if not sync_id:
            return
        session = self._sync_manager.get_session(sync_id)
        if session is None:
            return
        asyncio.create_task(
            self._space_sync_service.stream_request_more(session, cleaned),
            name=f"space-sync-more-{sync_id}",
        )

    async def _handle_instance_sync_status(self, event) -> None:
        """Peer reports its known spaces — S-17 origin + cap guard."""
        if self._sync_manager is None:
            return
        spaces = await self._sync_manager.validate_instance_sync_status(
            from_instance=event.from_instance,
            payload=event.payload,
        )
        log.debug(
            "INSTANCE_SYNC_STATUS from %s: accepted %d spaces",
            event.from_instance,
            len(spaces),
        )

    # ─── Pairing ──────────────────────────────────────────────────────────
    # The §11 QR-code pairing flow is implemented in
    # :class:`PairingCoordinator`. The three methods below are thin
    # delegations so the public surface of FederationService is unchanged.

    async def initiate_pairing(self, inbox_base_url: str) -> dict:
        """Delegates to :class:`PairingCoordinator`.

        ``inbox_base_url`` is the external scheme+host+path prefix peers
        will POST to. The coordinator appends a freshly-minted
        ``own_local_inbox_id`` before baking the URL into the QR.
        """
        return await self._pairing.initiate(inbox_base_url)

    async def accept_pairing(
        self,
        qr_payload: dict,
        own_inbox_base_url: str | None = None,
    ) -> dict:
        """Delegates to :class:`PairingCoordinator`."""
        return await self._pairing.accept(qr_payload, own_inbox_base_url)

    async def confirm_pairing(
        self,
        token: str,
        verification_code: str,
    ) -> RemoteInstance:
        """Delegates to :class:`PairingCoordinator`."""
        return await self._pairing.confirm(token, verification_code)

    async def handle_peer_accept(
        self,
        body: dict,
        *,
        expected_local_inbox_id: str | None = None,
    ) -> dict:
        """Delegates to :class:`PairingCoordinator.handle_peer_accept`."""
        return await self._pairing.handle_peer_accept(
            body,
            expected_local_inbox_id=expected_local_inbox_id,
        )

    async def handle_peer_confirm(
        self,
        body: dict,
        *,
        expected_local_inbox_id: str | None = None,
    ) -> dict:
        """Delegates to :class:`PairingCoordinator.handle_peer_confirm`."""
        return await self._pairing.handle_peer_confirm(
            body,
            expected_local_inbox_id=expected_local_inbox_id,
        )

    # ─── Encryption helpers ───────────────────────────────────────────────

    def _encrypt_payload(self, payload_json: str, session_key: bytes) -> str:
        """Delegates to :class:`FederationEncoder`."""
        return self._encoder.encrypt_payload(payload_json, session_key)

    def _decrypt_payload(self, encrypted: str, session_key: bytes) -> str:
        """Delegates to :class:`FederationEncoder`."""
        return self._encoder.decrypt_payload(encrypted, session_key)

    def _sign_envelope(self, envelope_bytes: bytes) -> str:
        """Delegates to :class:`FederationEncoder`."""
        return self._encoder.sign_envelope(envelope_bytes)

    def _verify_signature(
        self,
        envelope_bytes: bytes,
        signature: str,
        public_key: bytes,
    ) -> bool:
        """Delegates to :class:`FederationEncoder`."""
        return self._encoder.verify_signature(
            envelope_bytes,
            signature,
            public_key,
        )


# ─── Internal helpers ─────────────────────────────────────────────────────


def _require_fields(data: dict, *fields: str) -> None:
    """Raise ``ValueError`` if any of ``fields`` are missing from ``data``."""
    missing = [f for f in fields if f not in data]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")


async def _lookup_by_inbox_id(
    repo: AbstractFederationRepo,
    inbox_id: str,
) -> "_InboxInstance | None":
    """Find a ``RemoteInstance`` by its ``local_inbox_id``."""
    inst = await repo.get_instance_by_local_inbox_id(inbox_id)
    return _InboxInstance(inst) if inst is not None else None


def _aiohttp_timeout(seconds: float):
    """Return an ``aiohttp.ClientTimeout``."""
    return aiohttp.ClientTimeout(total=seconds)
