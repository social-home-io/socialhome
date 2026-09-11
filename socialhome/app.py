"""aiohttp application factory (§5.2).

``create_app()`` wires the full dependency graph:

1. Load ``Config`` from environment / options.json
2. Create ``AsyncDatabase``
3. Instantiate repositories
4. Create ``EventBus``
5. Instantiate services (inject repos + bus)
6. Wire ``NotificationService``
7. Build auth middleware (``ChainedStrategy``: HA ingress + bearer token)
8. Build rate-limit middleware
9. Create ``aiohttp.web.Application`` with middlewares
10. Mount routes
11. Register ``on_startup`` (db.startup, ha_bootstrap) and ``on_cleanup`` (db.shutdown)

Entry point: ``python -m socialhome.app`` (or via ``socialhome/__main__.py``).
"""

from __future__ import annotations

import logging
import pathlib
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import aiolibdatachannel as rtc
from aiohttp import web

from . import app_keys as K
from .auth import (
    BearerTokenStrategy,
    ChainedStrategy,
    HaIngressStrategy,
    SignedMediaStrategy,
    require_auth,
)
from .exception_text import describe_exception
from .config import Config
from .crypto import REPLAY_CACHE_WINDOW
from .db import AsyncDatabase
from .domain.federation import FederationEventType
from .federation.auto_pair_coordinator import AutoPairCoordinator
from .federation.federation_service import FederationService
from .federation.sync_manager import SyncSessionManager
from .federation.transport import FederationTransport, HttpsInboxTransport
from .hardening import (
    build_body_size_middleware,
    build_cors_deny_middleware,
    build_security_headers_middleware,
)
from .i18n import Catalog
from .identity_bootstrap import ensure_instance_identity
from .infrastructure.user_identity import ensure_user_identities
from .media_signer import MediaUrlSigner, derive_signing_key
from .infrastructure import (
    PAIR_WINDOW_404_ATTEMPTS,
    DeliveryOutcome,
    EventBus,
    IdempotencyCache,
    KeyManager,
    OutboxProcessor,
    ReconnectSyncQueue,
    WebSocketManager,
)
from .infrastructure.page_lock_scheduler import PageLockExpiryScheduler
from .infrastructure.calendar_reminder_scheduler import (
    CalendarReminderScheduler,
)
from .infrastructure.task_deadline_scheduler import TaskDeadlineScheduler
from .infrastructure.task_recurrence_scheduler import TaskRecurrenceScheduler
from .infrastructure.post_draft_scheduler import PostDraftCleanupScheduler
from .infrastructure.gfs_ws_supervisor import GfsWebSocketSupervisor
from .infrastructure.dm_gc_scheduler import DmGcScheduler
from .infrastructure.media_orphan_sweep_scheduler import MediaOrphanSweepScheduler
from .infrastructure.dm_relay_seen_scheduler import DmRelaySeenPruneScheduler
from .infrastructure.pairing_relay_scheduler import PairingRelayRetentionScheduler
from .infrastructure.pairing_session_prune_scheduler import (
    PairingSessionPruneScheduler,
)
from .infrastructure.auth_audit_cleanup_scheduler import AuthAuditCleanupScheduler
from .infrastructure.notification_cleanup_scheduler import (
    NotificationCleanupScheduler,
)
from .infrastructure.password_reset_cleanup_scheduler import (
    PasswordResetCleanupScheduler,
)
from .infrastructure.audio_transcript_scheduler import AudioTranscriptScheduler
from .infrastructure.app_pending_session_scheduler import (
    AppPendingSessionPruneScheduler,
)
from .infrastructure.replay_cache_scheduler import ReplayCachePruneScheduler
from .infrastructure.space_retention_scheduler import SpaceRetentionScheduler
from .infrastructure.moment_retention_scheduler import MomentRetentionScheduler
from .infrastructure.highlight_retention_scheduler import HighlightRetentionScheduler
from .platform import build_platform_adapter
from .platform.adapter import Capability
from .rate_limiter import RateLimiter, build_rate_limit_middleware
from .webrtc_ice import build_ice_servers, warn_if_no_turn, warn_if_turn_unusable
from .repositories import (
    SqliteBazaarRepo,
    SqliteCalendarRepo,
    SqliteConversationRepo,
    SqliteFederationRepo,
    SqliteNotificationRepo,
    SqliteOutboxRepo,
    SqlitePageRepo,
    SqlitePeerUserVisibilityRepo,
    SqlitePostRepo,
    SqlitePushSubscriptionRepo,
    SqliteShoppingRepo,
    SqliteSpaceCalendarRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteSpaceTaskRepo,
    SqliteStickyRepo,
    SqliteTaskRepo,
    SqliteUserRepo,
)
from .repositories.call_repo import SqliteCallRepo
from .repositories.cp_repo import SqliteCpRepo
from .repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from .repositories.dm_contact_repo import SqliteDmContactRepo
from .repositories.dm_media_outbox_repo import SqliteDmMediaOutboxRepo
from .repositories.space_media_outbox_repo import SqliteSpaceMediaOutboxRepo
from .repositories.dm_routing_repo import SqliteDmRoutingRepo
from .repositories.gallery_repo import SqliteGalleryRepo
from .repositories.media_transcode_repo import SqliteMediaTranscodeRepo
from .repositories.alias_repo import SqliteAliasRepo
from .repositories.app_repo import SqliteAppRepo
from .repositories.preferences_repo import SqlitePreferencesRepo
from .repositories.pairing_relay_repo import SqlitePairingRelayRepo
from .repositories.password_reset_repo import SqlitePasswordResetRepo
from .repositories.auth_audit_log_repo import SqliteAuthAuditLogRepo
from .repositories.poll_repo import SqlitePollRepo
from .repositories.space_poll_repo import SqliteSpacePollRepo
from .repositories.profile_picture_repo import SqliteProfilePictureRepo
from .repositories.space_bot_repo import SqliteSpaceBotRepo
from .repositories.space_cover_repo import SqliteSpaceCoverRepo
from .repositories.space_icon_repo import SqliteSpaceIconRepo
from .repositories.space_zone_repo import SqliteSpaceZoneRepo
from .repositories.moment_repo import SqliteMomentRepo
from .repositories.highlight_repo import SqliteHighlightRepo
from .repositories.presence_repo import SqlitePresenceRepo
from .repositories.peer_space_directory_repo import SqlitePeerSpaceDirectoryRepo
from .repositories.public_space_repo import SqlitePublicSpaceRepo
from .repositories.space_remote_location_repo import (
    SqliteSpaceRemoteLocationRepo,
)
from .repositories.space_remote_member_repo import SqliteSpaceRemoteMemberRepo
from .repositories.space_proposal_repo import SqliteSpaceProposalRepo
from .services.space_approval_service import SpaceApprovalService
from .repositories.report_repo import SqliteReportRepo
from .repositories.search_repo import SqliteSearchRepo
from .repositories.space_key_repo import SqliteSpaceKeyRepo
from .repositories.theme_repo import SqliteThemeRepo
from .routes import setup_routes
from .services.auto_pair_inbox import AutoPairInbox
from .services import (
    DmService,
    FeedService,
    NotificationService,
    SpaceService,
    UserService,
)
from .services.backup_service import BackupService
from .services.bazaar_service import BazaarExpiryScheduler, BazaarService
from .services.moment_service import MomentService
from .services.highlight_publication_service import HighlightPublicationService
from .services.moment_public_service import MomentPublicService
from .services.profile_sync_service import ProfileSyncService
from .services.moment_public_outbound import MomentPublicOutbound
from .services.space_config_outbound import SpaceConfigOutbound
from .services.space_post_outbound import SpacePostOutbound
from .services.space_public_inbound import SpacePublicInbound
from .services.space_public_outbound import SpacePublicOutbound
from .services.space_subscriber_key_inbound import SpaceSubscriberKeyInbound
from .services.space_subscriber_key_outbound import SpaceSubscriberKeyOutbound
from .services.moment_public_inbound import MomentPublicInbound
from .repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicRegistrationRepo,
)
from .services.highlight_service import HighlightService
from .services.highlight_signaling_handler import HighlightSignalingHandler
from .services.moment_public_signaling_handler import MomentPublicSignalingHandler
from .services.bot_bridge_service import BotBridgeService
from .services.space_bot_service import SpaceBotService
from .services.calendar_import_service import CalendarImportService
from .services.calendar_service import CalendarService, SpaceCalendarService
from .services.child_protection_service import ChildProtectionService
from .services.audio_transcription_service import AudioTranscriptionService
from .services.data_export_service import DataExportService
from .repositories.media_reference_repo import SqliteMediaReferenceRepo
from .services.dm_media_sync_service import DmMediaSyncService
from .services.media_orphan_sweep_service import MediaOrphanSweepService
from .services.space_media_sync_service import SpaceMediaSyncService
from .services.dm_routing_service import DmRoutingService
from .services.federation_inbound_service import FederationInboundService
from .services.user_move_service import UserMoveService
from .services.relay_policy import RelayPolicy
from .repositories.instance_ban_repo import SqliteHouseholdInstanceBanRepo
from .services.poll_federation_outbound import PollFederationOutbound
from .services.calendar_feed_bridge import CalendarFeedBridge
from .services.space_rsvp_mirror_bridge import SpaceRsvpMirrorBridge
from .services.schedule_calendar_bridge import ScheduleCalendarBridge
from .services.space_calendar_reminder_scheduler import (
    SpaceCalendarReminderScheduler,
)
from .services.app_update_scheduler import AppUpdateScheduler
from .services.schedule_federation_outbound import ScheduleFederationOutbound
from .services.corner_service import CornerService
from .federation.peer_directory_handler import PeerDirectoryHandler
from .federation.invite_token_redeem import SpaceInviteTokenRedeemCoordinator
from .federation.route_discovery import RouteDiscoveryService
from .federation.routed_envelope import SpaceRoutedHandler
from .federation.private_invite_handler import PrivateSpaceInviteHandler
from .services.peer_directory_service import PeerDirectoryService
from .services.profile_federation_outbound import ProfileFederationOutbound
from .services.users_sync_outbound import UsersSyncOutbound
from .services.capabilities_outbound import CapabilitiesOutbound
from .services.url_update_outbound import UrlUpdateOutbound
from .services.space_member_profile_federation_outbound import (
    SpaceMemberProfileFederationOutbound,
)
from .services.bazaar_outbound import BazaarOutbound
from .services.gallery_federation_outbound import GalleryFederationOutbound
from .services.sticky_federation_outbound import StickyFederationOutbound
from .services.moment_federation_outbound import MomentFederationOutbound
from .services.highlight_federation_outbound import HighlightFederationOutbound
from .services.space_location_outbound import SpaceLocationOutbound
from .services.space_zone_outbound import SpaceZoneOutbound
from .services.space_zone_service import SpaceZoneService
from .services.page_federation_outbound import PageFederationOutbound
from .services.task_federation_outbound import TaskFederationOutbound
from .services.federation_inbound import (
    PairingInboundHandlers,
    PersonalCalendarInboundHandlers,
    ResyncInboundHandlers,
    SpaceContentInboundHandlers,
    SpaceInviteInboundHandlers,
    SpaceMembershipInboundHandlers,
)
from .federation.sync import (
    BansExporter,
    BazaarExporter,
    CalendarExporter,
    ChunkBuilder,
    CommentsExporter,
    GalleryExporter,
    MemberPicturesExporter,
    MembersExporter,
    PagesExporter,
    PollsExporter,
    PostsExporter,
    SchedulesExporter,
    SpaceSyncReceiver,
    SpaceSyncScheduler,
    SpaceSyncService,
    StickiesExporter,
    TasksArchivedExporter,
    TasksExporter,
    ZonesExporter,
)
from .federation.sync.dm_history import (
    DmHistoryProvider,
    DmHistoryReceiver,
    DmHistoryScheduler,
)
from .federation.sync.space.resume import SpaceSyncResumeProvider
from .services.gallery_service import GalleryService
from .services.media_transcode_service import MediaTranscodeService
from .media.video_processor import VideoProcessor
from .services.system_album_bridge import SystemAlbumBridge
from .services.pairing_relay_queue import PairingRelayQueue
from .services.alias_service import AliasResolver, AliasService
from .services.app_catalog_service import AppCatalogService
from .services.app_federation_service import AppFederationService
from .services.app_service import AppService
from .services.resync_on_upgrade import request_capability_resync_if_upgraded
from .services.preferences_service import PreferencesService
from .services.page_conflict_service import PageConflictService
from .services.poll_service import PollService
from .services.online_status_service import OnlineStatusService
from .services.presence_service import PresenceService
from .services.gfs_connection_service import GfsConnectionService
from .services.public_space_discovery_service import PublicSpaceDiscoveryService
from .services.push_service import PushService, load_or_create_vapid
from .services.recovery_kit_service import RecoveryKitService
from .services.recovery_reconnect_service import RecoveryReconnectService
from .services.report_service import ReportService
from .services.realtime_service import RealtimeService
from .services.search_service import SearchService
from .services.shopping_service import ShoppingService
from .services.peer_home_sharing_service import PeerHomeSharingService
from .services.pending_decrypts_cache import PendingDecryptsCache
from .services.space_crypto_service import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    SpaceContentEncryption,
)
from .services.storage_quota_service import StorageQuotaService
from .services.setup_service import SetupService
from .services.stt_service import SttService
from .services.task_service import SpaceTaskService, TaskService
from .services.theme_service import ThemeService
from .services.typing_service import TypingService
from .services.call_service import CallSignalingService, StaleCallCleanupScheduler

log = logging.getLogger(__name__)


async def _download_bytes(url: str) -> bytes:
    """GET *url* and return the raw response body.

    Opens a short-lived :class:`aiohttp.ClientSession` per call so it can be
    used before the shared app session is available (e.g. during catalog
    fetches in ``AppCatalogService``).  Calls ``raise_for_status()`` so any
    non-2xx response surfaces as an :class:`aiohttp.ClientResponseError`.
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def _redeliver_envelope(
    federation_service: FederationService,
    federation_repo,
    entry,
) -> DeliveryOutcome:
    """Re-POST a previously-built envelope from an :class:`OutboxEntry`.

    The envelope JSON stored in ``payload_json`` is already signed and
    encrypted from the original :meth:`FederationService.send_event`
    call. Before POSTing we re-stamp + re-sign it with a fresh
    ``timestamp`` via :meth:`FederationService.resign_for_redelivery`
    (same ``msg_id`` / ``encrypted_payload``) so an entry queued more
    than ±300s ago is no longer dropped for clock skew — without this a
    NEVER_DROP event (ban / key revocation / SPACE_DISSOLVED) to any
    peer offline longer than 5 min was silently lost.

    Status code mapping:

    * 2xx → :attr:`DeliveryOutcome.SUCCESS`.
    * 4xx → :attr:`DeliveryOutcome.PERMANENT` (drop). The federation
      inbox returns 4xx for several distinct reasons — most are
      genuinely "the receiver has this state already" (success-
      equivalent) and the rest are "the receiver will never accept
      this envelope" (irrecoverable). Either way retrying won't help.
      Timestamp skew is **no longer** a drop reason: the envelope is
      re-signed with a fresh timestamp on every attempt. The remaining
      terminal reasons are:

      * **410 ``Replay detected``** — receiver already saw this
        ``msg_id``; their state is consistent with ours. Most
        commonly the residue of a previously-mangled response (e.g.
        the HA integration's pre-2026.5.18 charset bug) that left
        the row queued even though the peer processed it.
      * **403 banned / signature invalid** — receiver refuses this
        sender. Dropping the entry is the right call: retrying gives
        the sender no path to recover, and the ban / key revocation
        is authoritative on the receiver side.
      * **400 / 404** — malformed envelope or unknown inbox. Same
        reasoning — the next attempt POSTs identical bytes.

    * 5xx, timeout, network error → :attr:`DeliveryOutcome.TRANSIENT`
      (reschedule with backoff).
    """
    instance = await federation_repo.get_instance(entry.instance_id)
    if instance is None:
        # Peer was unpaired (the row in ``remote_instances`` is gone)
        # between the original ``send_event`` enqueue and this retry —
        # behaviour change vs. pre-:class:`DeliveryOutcome`, where this
        # path returned False and burned through MAX_ATTEMPTS before
        # giving up. Retrying serves no purpose: there is no row to
        # mark reachable, no URL to POST to, and the operator already
        # decided to drop the peer.
        log.warning("outbox: unknown instance %s — dropping", entry.instance_id)
        return DeliveryOutcome.PERMANENT

    # Re-stamp + re-sign BEFORE the delivery try: a malformed / legacy /
    # suite-mismatched stored envelope can't be fixed by retrying, so a
    # parse/sign failure is PERMANENT (drop). Doing this inside the try
    # would turn a corrupt row into an immortal TRANSIENT retry — and for
    # the NEVER_DROP events this fix protects, that's a ceiling-backoff loop
    # that never ends. (A row whose stored sig_suite needs a signer this
    # node no longer has — e.g. a PQ suite after the PQ signer was removed —
    # also drops PERMANENT here; pre-fix it would have looped on skew. That's
    # correct: the node that originally signed it had the signer, so a missing
    # one is misconfiguration, not a transient condition.)
    try:
        body = federation_service.resign_for_redelivery(entry.payload_json)
    except Exception as exc:
        log.warning("outbox: undecodable entry %s — dropping: %s", entry.id, exc)
        return DeliveryOutcome.PERMANENT

    try:
        client = await federation_service._get_http_client()
        async with client.post(
            instance.remote_inbox_url,
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=_aiohttp_timeout(10),
        ) as resp:
            if 200 <= resp.status < 300:
                await federation_repo.mark_reachable(entry.instance_id)
                return DeliveryOutcome.SUCCESS
            if 400 <= resp.status < 500:
                # 4xx still proves reachability: the peer received the
                # HTTP request, ran our envelope through their §24.11
                # pipeline, and returned a deliberate refusal. The most
                # common case in the post-charset-bug fallout is 410
                # ``Replay detected`` on backlog entries the peer
                # already processed — without ``mark_reachable`` here
                # the ``RemoteInstance`` row stays stuck on its
                # last-seen ``unreachable_since`` value and the SPA's
                # online indicator never goes green even though every
                # outbox tick is succeeding from the receiver's view.
                await federation_repo.mark_reachable(entry.instance_id)
                # 404 ``No instance found`` is *transient*: the peer just
                # hasn't installed its RemoteInstance row for us yet,
                # which happens in the relay-pair handshake window where
                # our ``INSTANCE_CAPABILITIES_UPDATED`` (fired from the
                # synchronous ``PairingConfirmed`` bus subscriber) races
                # ahead of the ack reaching the peer. Retry through the
                # outbox so the envelope lands once the peer's mirror
                # catches up. Other 4xx (410 ``Replay detected`` / ``Skew
                # too old``, 422 ``Malformed`` etc.) stay PERMANENT —
                # those are genuinely "the peer will never accept this".
                if resp.status == 404:
                    # WHO produced this 404 matters, and the status alone
                    # can't say. Our own inbox answers a rejected inbox id
                    # with ``{"error": "unknown_inbox"}``
                    # (``routes/federation`` ``_GENERIC_ERROR_BY_STATUS``),
                    # so a body carrying that marker means the peer's
                    # Social Home saw the request and refused it. A 404
                    # WITHOUT it came from something in front of the peer —
                    # most often, under ha/haos, the companion integration's
                    # ``/api/socialhome/inbox/{id}`` view not being
                    # registered because the integration isn't loaded. That
                    # needs fixing on the peer's Home Assistant, not by
                    # re-pairing, and it can appear with no Social Home
                    # change at all (an HA restart is enough).
                    peer_rejected = False
                    try:
                        body_text = (await resp.text())[:200]
                        peer_rejected = "unknown_inbox" in body_text
                    except Exception:  # pragma: no cover — diagnostics only
                        body_text = ""
                    # Bounded, not indefinite. The race this covers clears
                    # in seconds; beyond that the peer genuinely cannot
                    # resolve the inbox id we hold, and retrying the full
                    # ladder just burns ~8 hours and a PeerConnection per
                    # attempt to reach the same conclusion.
                    if entry.attempts < PAIR_WINDOW_404_ATTEMPTS:
                        log.info(
                            "outbox: %s returned 404 for %s (%s) — attempt"
                            " %d, retrying (pair-window race)",
                            entry.instance_id,
                            entry.id,
                            "peer's Social Home rejected the inbox id"
                            if peer_rejected
                            else "not from the peer's Social Home",
                            entry.attempts + 1,
                        )
                        return DeliveryOutcome.TRANSIENT
                    if peer_rejected:
                        log.warning(
                            "outbox: %s still returns 404 for %s after %d"
                            " attempts — dropping. The peer's Social Home"
                            " cannot resolve the inbox id we hold for it:"
                            " either it has no pairing row for us (re-pair to"
                            " fix), or its row is still provisional because"
                            " an auto-pair ack never landed.",
                            entry.instance_id,
                            entry.id,
                            entry.attempts + 1,
                        )
                    else:
                        log.warning(
                            "outbox: %s still returns 404 for %s after %d"
                            " attempts — dropping. The response did not come"
                            " from the peer's Social Home (no 'unknown_inbox'"
                            " marker), so something in front of it answered:"
                            " under Home Assistant this is typically the"
                            " companion integration not being loaded, so its"
                            " /api/socialhome/inbox view isn't registered."
                            " Body was: %r",
                            entry.instance_id,
                            entry.id,
                            entry.attempts + 1,
                            body_text,
                        )
                    return DeliveryOutcome.PERMANENT
                log.warning(
                    "outbox: %s returned terminal HTTP %d for %s — dropping",
                    entry.instance_id,
                    resp.status,
                    entry.id,
                )
                return DeliveryOutcome.PERMANENT
            log.warning(
                "outbox: %s returned HTTP %d for %s",
                entry.instance_id,
                resp.status,
                entry.id,
            )
            return DeliveryOutcome.TRANSIENT
    except Exception as exc:
        # Same empty-message trap as the transport's send path — and worse
        # here, because this is the line that explains why an envelope is
        # being retried at all.
        log.debug(
            "outbox: redelivery error %s to %s: %s",
            entry.id,
            entry.instance_id,
            describe_exception(exc),
        )
        return DeliveryOutcome.TRANSIENT


def _default_ice_servers(
    config: Config,
    *,
    hmac_user_id: str | None = None,
) -> list[dict]:
    """Build the WebRTC ICE-server list from :class:`Config`.

    When ``hmac_user_id`` is provided AND ``webrtc_turn_secret`` is
    set, the TURN credentials are derived via coturn's TURN-REST
    HMAC scheme (time-limited, no static secret on the wire) — this
    is the production-recommended setup. Falls back to the static
    ``webrtc_turn_user`` / ``webrtc_turn_cred`` pair when those are
    configured instead. Returned in the form expected by both
    ``RTCPeerConnection`` and ``aiolibdatachannel``.

    For the federation transport (server-to-server WebRTC) pass the
    local ``instance_id`` as ``hmac_user_id`` — that's a
    reasonable per-instance identity that's already stable + known
    to the operator. The SPA's ``/api/calls/ice-servers`` builds the
    same list per-user via the same helper.
    """
    return build_ice_servers(
        stun_url=config.webrtc_stun_url,
        turn_url=config.webrtc_turn_url,
        turn_user=config.webrtc_turn_user,
        turn_cred=config.webrtc_turn_cred,
        turn_secret=config.webrtc_turn_secret,
        turn_ttl_seconds=config.webrtc_turn_ttl_seconds,
        hmac_user_id=hmac_user_id,
    )


def _aiohttp_timeout(seconds: float):
    """Return an :class:`aiohttp.ClientTimeout`."""
    return aiohttp.ClientTimeout(total=seconds)


def _build_repos(db: AsyncDatabase):
    """Instantiate every repository for the given database.

    Returned as a :class:`types.SimpleNamespace` so service builders can
    pick attributes by name (``repos.user``, ``repos.post`` …). This is
    the only place that knows about :mod:`socialhome.repositories` —
    keep new repos here so :func:`create_app` stays narrow.
    """
    return SimpleNamespace(
        user=SqliteUserRepo(db),
        post=SqlitePostRepo(db),
        space=SqliteSpaceRepo(db),
        space_post=SqliteSpacePostRepo(db),
        notification=SqliteNotificationRepo(db),
        conversation=SqliteConversationRepo(db),
        task=SqliteTaskRepo(db),
        space_task=SqliteSpaceTaskRepo(db),
        calendar=SqliteCalendarRepo(db),
        space_cal=SqliteSpaceCalendarRepo(db),
        shopping=SqliteShoppingRepo(db),
        outbox=SqliteOutboxRepo(db),
        federation=SqliteFederationRepo(db),
        page=SqlitePageRepo(db),
        sticky=SqliteStickyRepo(db),
        highlight=SqliteHighlightRepo(db),
        moment=SqliteMomentRepo(db),
        bazaar=SqliteBazaarRepo(db),
        push_sub=SqlitePushSubscriptionRepo(db),
        gallery=SqliteGalleryRepo(db),
        media_transcode=SqliteMediaTranscodeRepo(db),
        space_key=SqliteSpaceKeyRepo(db),
        search=SqliteSearchRepo(db),
        theme=SqliteThemeRepo(db),
        cp=SqliteCpRepo(db),
        dm_routing=SqliteDmRoutingRepo(db),
        dm_contact=SqliteDmContactRepo(db),
        dm_media_outbox=SqliteDmMediaOutboxRepo(db),
        space_media_outbox=SqliteSpaceMediaOutboxRepo(db),
        app=SqliteAppRepo(db),
        preferences=SqlitePreferencesRepo(db),
        presence=SqlitePresenceRepo(db),
        public_space=SqlitePublicSpaceRepo(db),
        peer_space_directory=SqlitePeerSpaceDirectoryRepo(db),
        space_remote_member=SqliteSpaceRemoteMemberRepo(db),
        space_proposal=SqliteSpaceProposalRepo(db),
        space_remote_location=SqliteSpaceRemoteLocationRepo(db),
        poll=SqlitePollRepo(db),
        space_poll=SqliteSpacePollRepo(db),
        gfs_connection=SqliteGfsConnectionRepo(db),
        call=SqliteCallRepo(db),
        profile_picture=SqliteProfilePictureRepo(db),
        space_cover=SqliteSpaceCoverRepo(db),
        space_icon=SqliteSpaceIconRepo(db),
        space_bot=SqliteSpaceBotRepo(db),
        alias=SqliteAliasRepo(db),
        pairing_relay=SqlitePairingRelayRepo(db),
        space_zone=SqliteSpaceZoneRepo(db),
        password_reset=SqlitePasswordResetRepo(db),
        auth_audit_log=SqliteAuthAuditLogRepo(db),
        peer_user_visibility=SqlitePeerUserVisibilityRepo(db),
    )


def _wire_federation_stack(
    *,
    app: web.Application,
    config: Config,
    db: AsyncDatabase,
    bus: EventBus,
    http_session: aiohttp.ClientSession,
    key_manager: KeyManager,
    identity,
    federation_repo,
    outbox_repo,
    conversation_repo,
    space_post_repo,
    space_repo,
    peer_space_directory_repo,
    space_remote_member_repo,
    space_remote_location_repo,
    space_cover_repo,
    space_icon_repo,
    user_repo,
    profile_picture_repo,
    page_repo,
    sticky_repo,
    highlight_repo,
    moment_repo,
    space_task_repo,
    space_calendar_repo,
    calendar_repo,
    dm_contact_repo,
    space_poll_repo,
    gallery_repo,
    bazaar_repo,
    space_crypto,
    reconnect_queue,
    idempotency_cache,
    typing_service,
    dm_service,
    dm_media_sync_service,
    space_media_sync_service,
    dm_routing_service,
    dm_routing_repo,
    presence_service,
    online_status_service,
    report_service,
    report_repo,
    pairing_relay_repo,
    space_zone_repo,
    presence_repo,
    ws_manager,
    peer_user_visibility_repo,
):
    """Build :class:`FederationService` + attach the whole federation stack.

    Extracted from ``_on_startup`` so the wiring order is a readable flat
    sequence rather than 200 lines nested under the startup hook. Returns
    a :class:`SimpleNamespace` with the handles callers need:

    * ``federation_service`` — the built service (already has session,
      replay cache warmed, sync manager / typing / presence / dm-routing
      attached, plus the FederationInboundService bridge).
    * ``sync_manager`` — returned so the outer scope can stash it in
      ``app[K.sync_session_manager_key]``.
    * ``inbound_service`` — registered for the ``K.federation_inbound_service_key``.
    * ``pairing_relay_queue`` — §11.9 queue, already wired to the bus.
    """
    federation_service = FederationService(
        db=db,
        federation_repo=federation_repo,
        outbox_repo=outbox_repo,
        key_manager=key_manager,
        bus=bus,
        own_instance_id=identity.instance_id,
        own_identity_seed=identity.identity_seed,
        own_identity_pk=identity.identity_public_key,
        # ``hmac_user_id`` matters: without it a ``webrtc_turn_secret``
        # deployment gets TURN entries carrying no username/credential.
        # This list is not idle — space sync reads it and ships it to the
        # peer inside SPACE_SYNC_OFFER, so a credential-less entry is
        # advertised to the far side, coturn rejects it, and the session
        # quietly falls back to HTTPS. The transport builds its own list
        # with the id further down; these two must agree.
        ice_servers=_default_ice_servers(
            config,
            hmac_user_id=identity.instance_id,
        ),
        own_pq_seed=identity.pq_seed,
        own_pq_pk=identity.pq_public_key,
        sig_suite=config.federation_sig_suite,
    )
    federation_service.attach_session(http_session)
    # Enables the §24.11 receiver-side deprovisioned-author filter so
    # envelopes from a remote user we've marked ``deprovisioned_at``
    # (e.g. via an inbound ``USER_REMOVED``) get dropped before
    # dispatch — backstops the sender-side per-pair gate for events
    # that arrived via mesh relay (Momentum 3-hop).
    federation_service.attach_user_repo(user_repo)

    async def _get_max_seq(space_id: str) -> int:
        row = await db.fetchone(
            "SELECT MAX(seq) AS m FROM space_posts WHERE space_id=?",
            (space_id,),
        )
        return int(row["m"] or 0) if row else 0

    async def _check_member(space_id: str, instance_id: str) -> bool:
        row = await db.fetchone(
            "SELECT 1 FROM space_instances WHERE space_id=? AND instance_id=?",
            (space_id, instance_id),
        )
        return row is not None

    async def _sync_reject_reason(space_id: str) -> str:
        # SPACE_SYNC_BEGIN from a non-member: "removed" if we still host the
        # space (requester was dropped from it) or "dissolved" if the space
        # row is gone (a dissolve purged it). Drives the member's archive copy.
        row = await db.fetchone("SELECT 1 FROM spaces WHERE id=?", (space_id,))
        return "removed" if row is not None else "dissolved"

    sync_manager = SyncSessionManager(
        federation_repo,
        get_max_seq=_get_max_seq,
        check_member=_check_member,
        reject_reason=_sync_reject_reason,
    )
    federation_service.attach_sync_manager(sync_manager)
    federation_service.attach_idempotency_cache(idempotency_cache)
    federation_service.attach_typing_service(typing_service)
    typing_service.attach_federation(federation_service, identity.instance_id)
    dm_service.attach_federation(
        federation_service,
        federation_repo,
        identity.instance_id,
    )
    report_service.attach_federation(
        federation_service,
        identity.instance_id,
    )
    dm_routing_service.attach_federation(
        federation_service,
        own_instance_id=identity.instance_id,
    )
    # DM media sync needs federation to dispatch DM_MEDIA_BLOB
    # events; wire it now that the federation service exists.
    dm_media_sync_service.attach_federation(federation_service)
    space_media_sync_service.attach_federation(federation_service)
    federation_service.attach_dm_routing(dm_routing_service)
    federation_service.attach_presence_service(presence_service)
    # Online status (session presence) — federation hooks both ways:
    # outbound transitions fan to confirmed peers; inbound USER_ONLINE /
    # USER_IDLE / USER_OFFLINE events update the remote-state cache so
    # household members on a paired instance get their dot.
    federation_service.attach_online_status_service(online_status_service)
    online_status_service.attach_federation(
        federation_service=federation_service,
        federation_repo=federation_repo,
        own_instance_id=identity.instance_id,
        visibility_repo=peer_user_visibility_repo,
    )

    # §Momentum outbound is constructed first so the inbound handler
    # can hand it off as the relay bridge — the inbound calls
    # ``relay_inbound`` to forward an envelope another hop.
    # §Momentum-relay-policy gate — instance ban + open-report
    # short-circuit. Wired into both the inbound persist path and
    # every outbound fan-out / relay path.
    household_instance_ban_repo = SqliteHouseholdInstanceBanRepo(db)
    relay_policy = RelayPolicy(
        ban_repo=household_instance_ban_repo,
        report_repo=report_repo,
    )

    moment_federation_outbound = MomentFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        federation_repo=federation_repo,
        user_repo=user_repo,
        relay_policy=relay_policy,
        visibility_repo=peer_user_visibility_repo,
    )
    moment_federation_outbound.wire()

    inbound_service = FederationInboundService(
        bus=bus,
        conversation_repo=conversation_repo,
        space_post_repo=space_post_repo,
        space_repo=space_repo,
        user_repo=user_repo,
        highlight_repo=highlight_repo,
        moment_repo=moment_repo,
        moment_outbound=moment_federation_outbound,
        profile_picture_repo=profile_picture_repo,
        report_service=report_service,
        dm_routing_repo=dm_routing_repo,
        relay_policy=relay_policy,
        # v_3 DM media — receiver writes the embedded preview here
        # on DM_MESSAGE, then the full bytes from DM_MEDIA_BLOB.
        # ``realtime`` is wired later via :meth:`attach_realtime`
        # because :class:`RealtimeService` is constructed downstream
        # of this point.
        media_dir=pathlib.Path(config.media_path),
        realtime=None,
    )
    inbound_service.attach_to(federation_service)

    # Move-out redirect (move-out): land USER_MOVED redirects + serve the
    # USER_IDENTITY_RESOLVE pull backstop.
    user_move_service = UserMoveService(user_repo=user_repo)
    user_move_service.attach_to(federation_service)
    app[K.user_move_service_key] = user_move_service

    # Family-of-handler modules for pairing, space membership, invites,
    # and content mirroring (§13). Each registers its own slice of the
    # event-dispatch registry so federation_inbound_service stays thin.
    PairingInboundHandlers(
        bus=bus,
        federation_repo=federation_repo,
        dm_contact_repo=dm_contact_repo,
    ).attach_to(federation_service)

    # Transitive auto-pair coordinator (§11 "simple pairing") —
    # intermediaries auto-forward without admin approval; the target's
    # admin still reviews each incoming request (one click, no QR).
    auto_pair_inbox = AutoPairInbox(bus=bus)
    auto_pair_coordinator = AutoPairCoordinator(
        federation_repo=federation_repo,
        key_manager=key_manager,
        bus=bus,
        federation_service=federation_service,
        own_identity_seed=identity.identity_seed,
        own_identity_pk=identity.identity_public_key,
        inbox=auto_pair_inbox,
    )
    federation_service._event_registry.register(
        FederationEventType.PAIRING_INTRO_AUTO,
        auto_pair_coordinator.on_intro_at_target,
    )
    federation_service._event_registry.register(
        FederationEventType.PAIRING_INTRO_AUTO_ACK,
        auto_pair_coordinator.on_ack_at_originator,
    )
    federation_service._event_registry.register(
        FederationEventType.PAIRING_INTRO_AUTO_ACK_VIA,
        auto_pair_coordinator.on_ack_via_at_relay,
    )
    app[K.auto_pair_coordinator_key] = auto_pair_coordinator
    app[K.auto_pair_inbox_key] = auto_pair_inbox
    SpaceMembershipInboundHandlers(
        bus=bus,
        space_repo=space_repo,
    ).attach_to(federation_service)
    SpaceInviteInboundHandlers(
        bus=bus,
        space_repo=space_repo,
    ).attach_to(federation_service)
    PeerDirectoryHandler(peer_space_directory_repo).attach_to(federation_service)
    private_invite_handler = PrivateSpaceInviteHandler(
        bus=bus,
        space_repo=space_repo,
        remote_member_repo=space_remote_member_repo,
        cover_repo=space_cover_repo,
        icon_repo=space_icon_repo,
        space_crypto_service=space_crypto,
        remote_location_repo=space_remote_location_repo,
    )
    private_invite_handler.attach_to(federation_service)
    app[K.private_invite_handler_key] = private_invite_handler
    SpaceContentInboundHandlers(
        bus=bus,
        page_repo=page_repo,
        sticky_repo=sticky_repo,
        task_repo=space_task_repo,
        calendar_repo=space_calendar_repo,
        poll_repo=space_poll_repo,
        gallery_repo=gallery_repo,
        zone_repo=space_zone_repo,
        bazaar_repo=bazaar_repo,
    ).attach_to(federation_service)
    PersonalCalendarInboundHandlers(
        bus=bus,
        calendar_repo=calendar_repo,
        user_repo=user_repo,
    ).attach_to(federation_service)

    # §25.6 Direct Space Sync — content transfer over DataChannel.
    exporters: dict = {
        "bans": BansExporter(space_repo),
        "members": MembersExporter(space_repo),
        "member_pictures": MemberPicturesExporter(
            space_repo,
            profile_picture_repo,
        ),
        "posts": PostsExporter(space_post_repo),
        "comments": CommentsExporter(space_post_repo),
        "tasks": TasksExporter(space_task_repo),
        "tasks_archived": TasksArchivedExporter(space_task_repo),
        "pages": PagesExporter(page_repo),
        "stickies": StickiesExporter(sticky_repo),
        "calendar": CalendarExporter(space_calendar_repo),
        "gallery": GalleryExporter(gallery_repo),
        "polls": PollsExporter(space_poll_repo, space_post_repo),
        "schedules": SchedulesExporter(space_poll_repo, space_post_repo),
        "space_zones": ZonesExporter(space_zone_repo),
        "bazaar": BazaarExporter(bazaar_repo),
    }
    chunk_builder = ChunkBuilder(
        encoder=federation_service._encoder,
        crypto=space_crypto,
    )
    space_sync_service = SpaceSyncService(
        builder=chunk_builder,
        exporters=exporters,
        sig_suite=config.federation_sig_suite,
        # Catch-up media: after the metadata chunks stream the
        # requester also gets bytes for every post + gallery item +
        # bazaar listing in the space via the shared
        # SpaceMediaSyncService outbox.
        media_sync=space_media_sync_service,
        space_post_repo=space_post_repo,
        gallery_repo=gallery_repo,
        bazaar_repo=bazaar_repo,
    )
    space_sync_receiver = SpaceSyncReceiver(
        bus=bus,
        encoder=federation_service._encoder,
        crypto=space_crypto,
        federation_repo=federation_repo,
        space_repo=space_repo,
        space_post_repo=space_post_repo,
        space_task_repo=space_task_repo,
        page_repo=page_repo,
        sticky_repo=sticky_repo,
        space_calendar_repo=space_calendar_repo,
        gallery_repo=gallery_repo,
        zone_repo=space_zone_repo,
        bazaar_repo=bazaar_repo,
        profile_picture_repo=profile_picture_repo,
        poll_repo=space_poll_repo,
        pending_decrypts=app[K.pending_decrypts_cache_key],
    )
    federation_service.attach_space_sync(
        service=space_sync_service,
        receiver=space_sync_receiver,
    )
    # HTTPS-mode (Part C) needs to send SPACE_SYNC_CHUNK federation
    # events when a session can't open a DataChannel. Wire after
    # attach_space_sync so the chicken/egg between the two services
    # is resolved without a constructor cycle.
    space_sync_service.attach_federation(federation_service)
    app[K.space_sync_service_key] = space_sync_service
    app[K.space_sync_receiver_key] = space_sync_receiver

    # Scheduler: periodic tick + subscribe to PairingConfirmed.
    space_sync_scheduler = SpaceSyncScheduler(
        bus=bus,
        federation=federation_service,
        federation_repo=federation_repo,
        space_repo=space_repo,
        queue=reconnect_queue,
        sync_manager=sync_manager,
        own_instance_id=identity.instance_id,
    )
    space_sync_scheduler.wire()
    app[K.space_sync_scheduler_key] = space_sync_scheduler

    # Per-event outbound for space stickies (§19) — complements the
    # snapshot sync above with immediate fan-out of individual mutations
    # so co-members see changes within the same second, not the next tick.
    sticky_federation_outbound = StickyFederationOutbound(
        bus=bus,
        federation_service=federation_service,
    )
    sticky_federation_outbound.wire()

    task_federation_outbound = TaskFederationOutbound(
        bus=bus,
        federation_service=federation_service,
    )
    task_federation_outbound.wire()

    # F3 — broadcasts SPACE_PAGE_* mutations to member households so
    # wiki edits federate in realtime (matching the inbound side already
    # wired in federation_inbound.space_content).
    page_federation_outbound = PageFederationOutbound(
        bus=bus,
        federation_service=federation_service,
    )
    page_federation_outbound.wire()

    # §23.8.6 — fan a household PresenceUpdated out to opted-in spaces
    # as a GPS-only WS frame + sealed federation event. ``zone_name`` is
    # never on a space-bound payload (HA zones are household-only data).
    space_location_outbound = SpaceLocationOutbound(
        bus=bus,
        ws=ws_manager,
        federation_service=federation_service,
        space_repo=space_repo,
        space_zone_repo=space_zone_repo,
        user_repo=user_repo,
        presence_repo=presence_repo,
    )
    space_location_outbound.wire()

    # §23.8.7 — federate per-space zone CRUD to remote member instances.
    space_zone_outbound = SpaceZoneOutbound(
        bus=bus,
        federation_service=federation_service,
    )
    space_zone_outbound.wire()

    schedule_federation_outbound = ScheduleFederationOutbound(
        bus=bus,
        federation_service=federation_service,
    )
    schedule_federation_outbound.wire()

    poll_federation_outbound = PollFederationOutbound(
        bus=bus,
        federation_service=federation_service,
    )
    poll_federation_outbound.wire()

    profile_federation_outbound = ProfileFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        federation_repo=federation_repo,
        visibility_repo=peer_user_visibility_repo,
        user_repo=user_repo,
    )
    profile_federation_outbound.wire()

    # Roster catch-up — on PairingConfirmed, send the new peer a single
    # USERS_SYNC envelope carrying every (visible) local user so their
    # remote_users mirror is populated immediately. Without this the
    # peer only sees household members who happen to edit their profile
    # after pairing — which usually means only the admin shows up.
    users_sync_outbound = UsersSyncOutbound(
        bus=bus,
        federation_service=federation_service,
        user_repo=user_repo,
        profile_picture_repo=profile_picture_repo,
        visibility_repo=peer_user_visibility_repo,
    )
    users_sync_outbound.wire()

    # §Highlights — fan HighlightFrameAdded / HighlightRemoved / HighlightFrameRemoved
    # to peer instances based on the highlight's audience. The subscriber
    # gates on "is the author local?" so it doesn't re-fan inbound
    # republished events.
    highlight_federation_outbound = HighlightFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        federation_repo=federation_repo,
        user_repo=user_repo,
        visibility_repo=peer_user_visibility_repo,
    )
    highlight_federation_outbound.wire()

    # §11 URL rotation fan-out. Triggered by
    # PATCH /api/ha/integration/federation-base when the HA integration
    # reports a new externally-reachable base URL.
    url_update_outbound = UrlUpdateOutbound(
        federation_service=federation_service,
        federation_repo=federation_repo,
    )
    app[K.url_update_outbound_key] = url_update_outbound

    # Capabilities advertisement — fan out our ``proto_version`` to
    # every confirmed peer at startup AND on each newly-confirmed pair
    # (via the bus subscription below), so peers paired mid-run also
    # learn our version without waiting for the next restart.
    capabilities_outbound = CapabilitiesOutbound(
        federation_service=federation_service,
        federation_repo=federation_repo,
        bus=bus,
    )
    capabilities_outbound.wire()
    app[K.capabilities_outbound_key] = capabilities_outbound

    peer_directory_service = PeerDirectoryService(
        bus=bus,
        federation_service=federation_service,
        federation_repo=federation_repo,
        space_repo=space_repo,
    )
    peer_directory_service.wire()
    app[K.peer_directory_service_key] = peer_directory_service

    space_member_profile_federation_outbound = SpaceMemberProfileFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        space_repo=space_repo,
    )
    space_member_profile_federation_outbound.wire()

    # §23.119 — gallery items federate per-event so SPACE_SYNC_RESUME
    # has something to replay after long offlines, and so peers see
    # uploads in near real-time between chunked sync ticks. Albums
    # still ride the chunked sync only.
    gallery_federation_outbound = GalleryFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        gallery_repo=gallery_repo,
        space_repo=space_repo,
        media_sync=space_media_sync_service,
    )
    gallery_federation_outbound.wire()

    # Bazaar listing federation — the wrapper PostType.BAZAAR post
    # federates via SpacePostOutbound with just the caption; this
    # service ships the full BazaarListing payload + image bytes so
    # remote members see price / mode / photos / status, not just the
    # caption. See ``socialhome/services/bazaar_outbound.py``.
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=space_media_sync_service,
        federation_repo=federation_repo,
    )

    # DM history sync: reconcile missed messages when a peer reconnects.
    dm_history_provider = DmHistoryProvider(
        conversation_repo=conversation_repo,
        federation_service=federation_service,
        user_repo=user_repo,
        visibility_repo=peer_user_visibility_repo,
    )
    dm_history_receiver = DmHistoryReceiver(
        conversation_repo=conversation_repo,
        bus=bus,
        federation_service=federation_service,
    )

    async def _dm_history_request(event) -> None:
        await dm_history_provider.handle_request(event)

    async def _dm_history_chunk(event) -> None:
        await dm_history_receiver.handle_chunk(event)

    async def _dm_history_chunk_ack(event) -> None:
        await dm_history_provider.handle_ack(event)

    federation_service._event_registry.register(
        FederationEventType.DM_HISTORY_REQUEST,
        _dm_history_request,
    )
    federation_service._event_registry.register(
        FederationEventType.DM_HISTORY_CHUNK,
        _dm_history_chunk,
    )
    federation_service._event_registry.register(
        FederationEventType.DM_HISTORY_COMPLETE,
        dm_history_receiver.handle_complete,
    )
    federation_service._event_registry.register(
        FederationEventType.DM_HISTORY_CHUNK_ACK,
        _dm_history_chunk_ack,
    )

    # Spec §4.4 / §11452 — long-offline catch-up. Reconnecting peer asks
    # for events newer than ``since``; we replay individual ``SPACE_*_CREATED``
    # events for posts, comments, tasks, pages, stickies, and calendar
    # events. Gallery items will join when ``SPACE_GALLERY_*`` lands.
    space_sync_resume_provider = SpaceSyncResumeProvider(
        federation_service=federation_service,
        space_repo=space_repo,
        space_post_repo=space_post_repo,
        space_task_repo=space_task_repo,
        page_repo=page_repo,
        sticky_repo=sticky_repo,
        space_calendar_repo=space_calendar_repo,
        gallery_repo=gallery_repo,
    )

    async def _space_sync_resume(event) -> None:
        await space_sync_resume_provider.handle_request(event)

    federation_service._event_registry.register(
        FederationEventType.SPACE_SYNC_RESUME,
        _space_sync_resume,
    )

    # §319.6 — let a peer ask us to re-broadcast capabilities / a space's
    # content / a space's calendar. Reuses the resume provider's
    # membership-gated replay so a non-member can never pull space content.
    ResyncInboundHandlers(
        capabilities_outbound=capabilities_outbound,
        space_resume=space_sync_resume_provider,
    ).attach_to(federation_service)

    dm_history_scheduler = DmHistoryScheduler(
        bus=bus,
        federation=federation_service,
        conversation_repo=conversation_repo,
        queue=reconnect_queue,
        own_instance_id=identity.instance_id,
    )
    dm_history_scheduler.wire()
    app[K.dm_history_provider_key] = dm_history_provider
    app[K.dm_history_receiver_key] = dm_history_receiver
    app[K.dm_history_scheduler_key] = dm_history_scheduler

    pairing_relay_queue = PairingRelayQueue(
        bus=bus,
        federation=federation_service,
        repo=pairing_relay_repo,
        own_instance_id=identity.instance_id,
    )
    pairing_relay_queue.wire()

    # Register handles — each one has a matching AppKey so later startup
    # / cleanup hooks (and tests) can look them up by name.
    app[K.federation_service_key] = federation_service
    app[K.sync_session_manager_key] = sync_manager
    app[K.dm_routing_service_key] = dm_routing_service
    app[K.federation_inbound_service_key] = inbound_service
    app[K.pairing_relay_queue_key] = pairing_relay_queue
    app[K.household_instance_ban_repo_key] = household_instance_ban_repo
    app[K.relay_policy_key] = relay_policy

    return SimpleNamespace(
        federation_service=federation_service,
        sync_manager=sync_manager,
        inbound_service=inbound_service,
        pairing_relay_queue=pairing_relay_queue,
        household_instance_ban_repo=household_instance_ban_repo,
        relay_policy=relay_policy,
    )


def _build_middleware(config: Config, limiter: RateLimiter):
    """Compose the HTTP middleware stack.

    Order matters: hardening runs first (cheap rejects), then auth,
    then per-route rate limiting. This mirrors the §25.7 hardening
    section in the spec.
    """
    body_size_middleware = build_body_size_middleware()
    cors_middleware = build_cors_deny_middleware(
        allowed_origins=config.cors_allowed_origins,
    )
    rate_middleware = build_rate_limit_middleware(
        limiter,
        default_limit=60,
        default_window_s=60,
        # Order matters: the most-specific patterns must come first so
        # they short-circuit the broader prefix matches that follow.
        limits={
            # Action endpoints (use ``*`` glob so the {id} segment matches).
            "/api/spaces/*/ban": (5, 60),  # moderation
            "/api/calls/*/decline": (10, 60),
            "/api/calls/*/hangup": (30, 60),
            # Sensitive surfaces — tighter than the 60/min default.
            "/api/me/tokens": (10, 60),  # API token create
            "/api/feed/posts": (30, 60),  # household posting
            "/api/presence/location": (10, 60),  # GPS pings
            "/api/calls": (10, 60),  # initiate / signal
            "/api/pairing": (5, 60),  # pairing handshakes
        },
    )
    security_headers_middleware = build_security_headers_middleware()
    return (
        security_headers_middleware,
        body_size_middleware,
        cors_middleware,
        rate_middleware,
    )


def create_app(config: Config | None = None) -> web.Application:
    """Build and return the configured :class:`aiohttp.web.Application`.

    The application is **not** started here — call ``web.run_app()`` or
    let aiohttp's runner do it. Startup/shutdown hooks are registered so
    the app is self-contained.

    Parameters
    ----------
    config:
        Optional pre-built config. When ``None`` (the default) the factory
        calls ``Config.from_env()`` — suitable for production. Pass an
        explicit config in tests.
    """
    if config is None:
        config = Config.from_env()

    # Configure logging
    logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO))

    # Capture ``warnings.warn(...)`` calls through the standard logger
    # so deprecation / resource warnings flow through the same
    # handlers and filters as everything else (mirrors HA Core's
    # ``async_enable_logging``).
    logging.captureWarnings(True)

    # Quiet aiohttp's per-request access log — every request was
    # being emitted at INFO and drowning real signal. Mirrors
    # ``logging.getLogger("aiohttp.access").setLevel(WARNING)`` in
    # both home-assistant/core (``homeassistant/bootstrap.py``) and
    # the Supervisor (``supervisor/bootstrap.py``).
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    # Route libdatachannel's native ICE/DTLS/SCTP logs through Python's
    # logging module so operators see them in the same stream they
    # already watch. The adapter derives the native filter level from
    # the Python logger's effective level, so INFO-level deployments
    # pay no formatting cost for DEBUG traffic.
    rtc.install_python_logger(logging.getLogger("aiolibdatachannel"))

    # ── Database ─────────────────────────────────────────────────────────
    db = AsyncDatabase(
        config.db_path,
        batch_max=config.db_write_batch_max,
        batch_timeout_ms=config.db_write_batch_timeout_ms,
    )

    # ── Repositories ─────────────────────────────────────────────────────
    repos = _build_repos(db)
    # Local aliases so the rest of the wiring stays readable.
    user_repo = repos.user
    post_repo = repos.post
    space_repo = repos.space
    space_post_repo = repos.space_post
    notification_repo = repos.notification
    conversation_repo = repos.conversation
    task_repo = repos.task
    space_task_repo = repos.space_task
    calendar_repo = repos.calendar
    space_cal_repo = repos.space_cal
    shopping_repo = repos.shopping
    outbox_repo = repos.outbox
    federation_repo = repos.federation
    page_repo = repos.page
    sticky_repo = repos.sticky
    highlight_repo = repos.highlight
    moment_repo = repos.moment
    dm_contact_repo = repos.dm_contact
    bazaar_repo = repos.bazaar
    push_sub_repo = repos.push_sub
    gallery_repo = repos.gallery
    space_key_repo = repos.space_key
    search_repo = repos.search
    theme_repo = repos.theme
    profile_picture_repo = repos.profile_picture
    space_cover_repo = repos.space_cover
    space_icon_repo = repos.space_icon
    space_bot_repo = repos.space_bot

    # ── Event bus ────────────────────────────────────────────────────────
    bus = EventBus()

    # ── Services ─────────────────────────────────────────────────────────
    # own_instance_public_key is fetched in on_startup (db not open yet);
    # we pass a sentinel and patch in the startup hook.
    _sentinel_pk: bytes = bytes(32)

    user_service = UserService(
        user_repo,
        bus,
        own_instance_public_key=_sentinel_pk,
        profile_picture_repo=profile_picture_repo,
    )
    feed_service = FeedService(
        post_repo,
        user_repo,
        bus,
        media_dir=pathlib.Path(config.media_path),
    )
    space_service = SpaceService(
        space_repo,
        space_post_repo,
        user_repo,
        bus,
        own_instance_id="unknown",  # patched on startup
        media_dir=pathlib.Path(config.media_path),
    )
    space_service.attach_profile_picture_repo(profile_picture_repo)
    space_service.attach_cover_repo(space_cover_repo)
    space_service.attach_icon_repo(space_icon_repo)
    space_service.attach_gallery_repo(gallery_repo)
    space_service.attach_bazaar_repo(bazaar_repo)
    # Multi-admin approval (quorum) for critical space actions (dissolve /
    # publication-tier). ``own_instance_id`` is patched on startup like
    # SpaceService; ``attach`` wires federation + space_service below.
    space_approval_service = SpaceApprovalService(
        repos.space_proposal,
        space_repo,
        repos.space_remote_member,
        user_repo,
        bus,
        own_instance_id="unknown",  # patched on startup
    )
    space_approval_service.attach(space_service=space_service)
    # i18n catalog — loaded once at process start, used by NotificationService.
    i18n_dir = Path(__file__).parent / "i18n" / "messages"
    i18n = Catalog.from_directory(i18n_dir)

    notification_service = NotificationService(
        notification_repo,
        user_repo,
        space_repo,
        bus,
        i18n=i18n,
    )
    # v_3 DM media sync — preview builder + DM_MEDIA_BLOB outbox
    # scheduler. Wired before DmService so it can be passed in;
    # ``attach_federation`` later sets the FederationService once it
    # exists (DM ↔ federation cycle).
    dm_media_sync_service = DmMediaSyncService(
        convos=conversation_repo,
        outbox=repos.dm_media_outbox,
        federation=None,  # set by attach_federation below
        media_dir=pathlib.Path(config.media_path),
        visibility_repo=repos.peer_user_visibility,
    )
    # Space media sync — same shape as DmMediaSyncService but tied
    # to space_media_outbox so the two streams backoff
    # independently. Federation attached post-stack-build like DM.
    space_media_sync_service = SpaceMediaSyncService(
        outbox=repos.space_media_outbox,
        federation=None,
        media_dir=pathlib.Path(config.media_path),
    )
    # DmService starts without ``audio_transcription`` — the platform
    # adapter is built much later in ``create_app``, so the service is
    # attached via :meth:`DmService.attach_audio_transcription` (same
    # pattern as ``attach_federation``) once the adapter exists.
    # ``media_dir`` is safe to pass now; it doesn't depend on the
    # adapter.
    dm_service = DmService(
        conversation_repo,
        user_repo,
        bus,
        dm_routing_repo=repos.dm_routing,
        media_sync=dm_media_sync_service,
        media_dir=pathlib.Path(config.media_path),
        visibility_repo=repos.peer_user_visibility,
    )
    report_repo = SqliteReportRepo(db)
    report_service = ReportService(
        report_repo=report_repo,
        user_repo=user_repo,
        bus=bus,
        space_repo=space_repo,
        space_post_repo=space_post_repo,
    )
    task_service = TaskService(task_repo, bus, user_repo=user_repo)
    space_task_service = SpaceTaskService(space_task_repo, bus)
    calendar_service = CalendarService(calendar_repo, bus)
    # Subscribe to UserProvisioned so every freshly-created household
    # member gets a default calendar row — without this, the household
    # calendar's member-filter strip stays hidden until the new member
    # manually clicks "+ New event" the first time.
    calendar_service.wire()
    space_cal_service = SpaceCalendarService(space_cal_repo, bus)
    # Phase E: subscribe to SpaceMemberLeft so leaving a space drops
    # your RSVPs on its events.
    space_cal_service.wire()
    shopping_service = ShoppingService(shopping_repo, bus)

    # Wire notification handlers onto the bus
    notification_service.wire()

    # ── WebSocket realtime ────────────────────────────────────────────────
    ws_manager = WebSocketManager()
    realtime_service = RealtimeService(
        bus,
        ws_manager,
        user_repo=user_repo,
        space_repo=space_repo,
        conversation_repo=conversation_repo,
        media_transcode_repo=repos.media_transcode,
    )
    realtime_service.wire()

    # ── Web Push ──────────────────────────────────────────────────────────
    vapid = load_or_create_vapid(config.data_dir)
    push_service = PushService(sub_repo=push_sub_repo, vapid=vapid)
    # Hook push fan-out into the notification service (§25.3 — title only).
    notification_service.attach_push_service(push_service)
    # Let the DM notification path skip recipients with the thread
    # open — see :meth:`NotificationService.on_dm_message_created`.
    notification_service.attach_ws_manager(ws_manager)

    # ── Search (FTS5) ─────────────────────────────────────────────────────
    search_service = SearchService(bus, search_repo)
    search_service.wire()
    # Access filtering (§23.2.6): drop hits the caller can't see.
    search_service.attach_access_repos(
        space_repo=space_repo,
        user_repo=user_repo,
        conversation_repo=conversation_repo,
    )

    # ── Themes ────────────────────────────────────────────────────────────
    theme_service = ThemeService(theme_repo, space_repo)

    # ── Storage quota ─────────────────────────────────────────────────────
    storage_quota = StorageQuotaService(
        media_path=config.media_path,
        quota_bytes=config.max_storage_bytes,
    )

    # ── Backup (HA-mode only) ─────────────────────────────────────────────
    # Backup service — adapter-agnostic. HA Supervisor calls pre/post
    # snapshot; standalone operators call via API or cron.
    backup_service = BackupService(db, config.media_path, schema_version=1)
    recovery_kit_service = RecoveryKitService(db, config.data_dir)

    # ── Idempotency + reconnect orchestration ────────────────────────────
    idempotency_cache = IdempotencyCache(ttl_seconds=3600)
    reconnect_queue = ReconnectSyncQueue()

    # ── GFS connection service (§24) ────────────────────────────────────
    gfs_connection_service = GfsConnectionService(repos.gfs_connection)
    # Hook space_service so flipping a space's space_type to/from 'global'
    # auto-publishes / unpublishes to every active GFS (§D1).
    space_service.attach_gfs_connection_service(gfs_connection_service)

    # ── Public space discovery (GFS poll) ────────────────────────────────
    public_space_discovery = PublicSpaceDiscoveryService(
        repos.public_space,
        gfs_connection_repo=repos.gfs_connection,
    )

    # ── Background video-transcode scheduler ─────────────────────────────
    # Drains ``media_transcode_jobs`` — the upload endpoints stash source
    # bytes + enqueue a row, this service transcodes in the background so
    # uploads return immediately with a "processing" placeholder. Started
    # in ``_on_startup`` / stopped in cleanup alongside the DM media sync.
    media_transcode_service = MediaTranscodeService(
        repo=repos.media_transcode,
        media_dir=pathlib.Path(config.media_path),
        processor=VideoProcessor(),
        bus=bus,
    )

    # ── Gallery service ──────────────────────────────────────────────────
    gallery_service = GalleryService(
        gallery_repo,
        space_repo,
        bus,
        config,
        media_transcode_repo=repos.media_transcode,
        media_transcode_service=media_transcode_service,
    )

    # ── System "Posts" album bridge (§Gallery) ──────────────────────────
    # Subscribes to feed-post lifecycle events and mirrors photo/video
    # media into the auto-managed "Posts" album per scope. Lazy
    # creation: the album row only appears once a member shares
    # something with media.
    system_album_bridge = SystemAlbumBridge(gallery_service, bus)
    system_album_bridge.wire()

    # ── Child protection service ────────────────────────────────────────
    child_protection_service = ChildProtectionService(repos.cp, user_repo, bus)
    # Wire the space repo so `kick_from_space` can drop members directly
    # (bypassing the admin-or-self guard on SpaceService.remove_member).
    child_protection_service.attach_space_repo(space_repo)
    child_protection_service.attach_conversation_repo(conversation_repo)

    # ── Personal user aliases (§4.1.6) ──────────────────────────────────
    # Viewer-private renames of other users (local or remote). Used by
    # the member-list endpoint and any future render path that needs
    # alias resolution. Aliases never federate.
    alias_service = AliasService(repos.alias, repos.user)
    alias_resolver = AliasResolver(repos.alias)

    # ── Preferences service (household-wide + per-user toggles) ─────────
    preferences_service = PreferencesService(
        repo=repos.preferences,
        bus=bus,
    )

    # ── App service (Social Home Apps install / uninstall / enable) ──────
    app_service = AppService(
        repo=repos.app,
        catalog=AppCatalogService(
            session_factory=lambda: aiohttp.ClientSession(),
            catalog_url=config.apps_catalog_url,
        ),
        apps_path=pathlib.Path(config.apps_path),
        downloader=_download_bytes,
        bus=bus,
        cp_repo=repos.cp,
    )

    # Feature gating for §18: wire household toggle enforcement into
    # every service that owns a toggleable surface. Disabling
    # ``feat_tasks`` immediately makes POST /api/tasks return 403.
    feed_service.attach_household_features(preferences_service)
    task_service.attach_household_features(preferences_service)
    calendar_service.attach_household_features(preferences_service)
    # Space-calendar event creation walks the same tz resolution chain
    # as personal events. Wire the helpers it needs: the household
    # service (final UTC fallback) and the space repo (per-space tz).
    space_cal_service.attach_household_features(preferences_service)
    space_cal_service.attach_space_repo(space_repo)
    feed_service.attach_storage_quota(storage_quota)

    # Schedule-poll → space calendar bridge (§9 / §23.53). Needs both
    # the space calendar service and the household-features toggle
    # service, so it wires in here (after both are built).
    schedule_calendar_bridge = ScheduleCalendarBridge(
        bus=bus,
        space_calendar_service=space_cal_service,
        household_features=preferences_service,
    )
    schedule_calendar_bridge.wire()

    # Phase B: surface calendar events in the space feed. Subscribes to
    # CalendarEventCreated/Updated/Deleted on the bus and writes a
    # PostType.EVENT post via the post repo. Idempotent — a duplicate
    # CalendarEventCreated (e.g. local + federation replay) is a no-op.
    calendar_feed_bridge = CalendarFeedBridge(
        bus=bus,
        post_repo=space_post_repo,
        calendar_repo=space_cal_repo,
        space_repo=space_repo,
    )
    calendar_feed_bridge.wire()

    # Personal-calendar mirror for space RSVPs (§23.7 follow-up):
    # accepting "going" on a space event drops a mirror onto the
    # member's personal calendar so they see their commitments
    # alongside household events. Subscribes to SpaceRsvpChanged +
    # CalendarEventUpdated/Deleted on the bus.
    space_rsvp_mirror_bridge = SpaceRsvpMirrorBridge(
        bus=bus,
        calendar_repo=calendar_repo,
        space_calendar_repo=space_cal_repo,
        user_repo=user_repo,
    )
    space_rsvp_mirror_bridge.wire()

    # Phase D: per-user space-event reminder scheduler. Polls fire_at
    # on a 30 s cadence and emits EventReminderDue events that the
    # notification service translates into push + in-app rows.
    # Started/stopped from app's on_startup / on_cleanup hooks below.
    space_calendar_reminder_scheduler = SpaceCalendarReminderScheduler(
        calendar_repo=space_cal_repo,
        bus=bus,
    )
    notification_service.attach_calendar_repo(space_cal_repo)
    notification_service.attach_personal_calendar_repo(calendar_repo)

    # ── Per-user data export (§25.8.7) ──────────────────────────────────
    data_export_service = DataExportService(db)

    # ── DM relay routing (§12.5) ────────────────────────────────────────
    dm_routing_service = DmRoutingService(
        repos.dm_routing,
        federation_repo,
        child_protection_service=child_protection_service,
        visibility_repo=repos.peer_user_visibility,
    )

    # ── Page conflict resolution (§4.4.4.1) ─────────────────────────────
    page_conflict_service = PageConflictService(page_repo)

    # ── Presence service (local + remote) ──────────────────────────────
    presence_service = PresenceService(repos.presence, repos.user, bus)

    # ── Online status (session presence) ───────────────────────────────
    online_status_service = OnlineStatusService(
        ws_manager=ws_manager,
        user_repo=repos.user,
        bus=bus,
    )

    # ── Per-space zone catalogue (§23.8.7) ─────────────────────────────
    space_zone_service = SpaceZoneService(
        repos.space_zone,
        repos.space,
        repos.user,
        bus,
    )

    # ── Poll + schedule-poll service (§9) ──────────────────────────────
    poll_service = PollService(repos.poll, bus)
    space_poll_service = PollService(repos.space_poll, bus)

    # ── Bazaar service + expiry scheduler (§9, §23.15) ─────────────────
    # Bazaar listings are space-scoped: the wrapper post lives in
    # ``space_posts`` (not the household feed), so we wire SpaceService
    # rather than FeedService. Membership / writability / moderation
    # gates are inherited from SpaceService.create_post.
    bazaar_service = BazaarService(bazaar_repo, bus)
    bazaar_service.attach_spaces(space_service)
    bazaar_expiry_scheduler = BazaarExpiryScheduler(bazaar_service)

    # ── Highlights (§Highlights) ────────────────────────────────────────────
    highlight_service = HighlightService(
        highlight_repo,
        user_repo,
        bus,
        media_dir=pathlib.Path(config.media_path),
    )
    highlight_retention_scheduler = HighlightRetentionScheduler(highlight_service)
    # Public-publish service. ``attach_session`` + ``attach_identity``
    # are called from the startup hook once the shared aiohttp client
    # and federation signing key are available — same lifecycle as
    # ``ReportService``.
    highlight_publication_service = HighlightPublicationService(
        highlight_repo,
        repos.gfs_connection,
    )
    # Author-side signalling answerer for the public-highlight flow.
    # ``attach_session`` + ``attach_identity`` happen in the startup
    # hook (mirrors ``HighlightPublicationService``); the WS supervisor
    # forwards every ``highlight_signal`` frame here.
    highlight_signaling_handler = HighlightSignalingHandler(
        highlight_repo,
        repos.gfs_connection,
        media_dir=str(config.media_path),
    )
    # Author-side signalling answerer for the public-moments live index
    # (§Momentum-public). Same shape as ``highlight_signaling_handler``:
    # ``attach_session`` + ``attach_identity`` happen in the startup hook,
    # and the WS supervisor forwards every ``moment_signal`` frame here.
    moment_public_signaling_handler = MomentPublicSignalingHandler(
        moment_repo,
        repos.gfs_connection,
        media_dir=str(config.media_path),
    )

    # ── Momentum (§Momentum) ───────────────────────────────────────────
    # ``own_instance_id`` is bound after federation identity is loaded
    # in the startup hook (see ``moment_service.attach_instance_id``).
    # ``moment_repo`` is shared with the federation inbound handler so
    # remote-author rows land in the same table.
    moment_service = MomentService(
        moment_repo,
        user_repo,
        bus,
        media_dir=pathlib.Path(config.media_path),
    )
    moment_retention_scheduler = MomentRetentionScheduler(moment_service)

    # ── Public Momentum via GFS (§Momentum-public) ─────────────────────
    moment_public_registration_repo = SqliteMomentPublicRegistrationRepo(db)
    moment_public_follow_repo = SqliteMomentPublicFollowRepo(db)
    moment_public_service = MomentPublicService(
        moment_public_registration_repo,
        moment_public_follow_repo,
        user_repo,
        repos.gfs_connection,
        profile_picture_repo=profile_picture_repo,
    )
    moment_public_outbound = MomentPublicOutbound(
        bus=bus,
        moment_repo=moment_repo,
        registration_repo=moment_public_registration_repo,
        user_repo=user_repo,
        gfs_repo=repos.gfs_connection,
    )
    moment_public_outbound.wire()
    moment_public_inbound = MomentPublicInbound(
        bus=bus,
        moment_repo=moment_repo,
        follow_repo=moment_public_follow_repo,
    )
    # Public space-content relay (Phase 5a2) is constructed in
    # ``_on_startup`` — its producer/consumer both depend on
    # ``space_crypto`` (SpaceContentEncryption), which is only built once
    # the identity seed + KEK are available at startup.
    space_public_outbound: SpacePublicOutbound | None = None
    space_public_inbound: SpacePublicInbound | None = None
    # Phase 5b-b: deliver the per-space content key to a GFS subscriber so it
    # can decrypt the Phase-5a public relay. Same late-build reason as above
    # (both depend on ``space_crypto``).
    space_subscriber_key_outbound: SpaceSubscriberKeyOutbound | None = None
    space_subscriber_key_inbound: SpaceSubscriberKeyInbound | None = None
    profile_sync_service = ProfileSyncService(
        bus=bus,
        registration_repo=moment_public_registration_repo,
        public_service=moment_public_service,
    )
    profile_sync_service.wire()

    # ── My Corner aggregator (§23) ─────────────────────────────────────
    corner_service = CornerService(
        notification_repo=notification_repo,
        conversation_repo=conversation_repo,
        calendar_repo=calendar_repo,
        presence_service=presence_service,
        task_repo=task_repo,
        bazaar_repo=bazaar_repo,
        user_repo=user_repo,
        space_repo=space_repo,
        space_post_repo=space_post_repo,
    )

    # ── Typing service (relay typing indicators) ────────────────────────
    typing_service = TypingService(
        conversation_repo=conversation_repo,
        user_repo=user_repo,
        ws_manager=ws_manager,
        space_repo=space_repo,
        visibility_repo=repos.peer_user_visibility,
    )

    # ── Platform adapter (HA vs standalone) ──────────────────────────────
    platform_adapter = build_platform_adapter(config.mode, db, config)

    # Voice-note transcription depends on ``adapter.stt``; fail-silent
    # when the platform doesn't expose one (standalone v1, HA without
    # ``stt_entity_id``). Wired into DmService via the late-binder
    # because the service itself was constructed long before
    # ``platform_adapter`` exists.
    audio_transcription_service = AudioTranscriptionService(platform_adapter)
    dm_service.attach_audio_transcription(audio_transcription_service)

    # Fan notifications through the adapter's push channel too (§25.3) —
    # HA mode calls ``notify.mobile_app_<user>``, standalone POSTs to
    # ``platform_users.notify_endpoint``.
    notification_service.attach_platform_adapter(platform_adapter)

    # HA event bridge — only when running on HA. Lets users automate on
    # socialhome.* events from the HA side.
    # Calendar import — ICS-file path is always available; the AI paths
    # (photo / prompt) surface a 503 at request time when the adapter
    # lacks generate_ai_data.
    calendar_import_service = CalendarImportService(platform_adapter)

    # STT — adapter-agnostic wrapper; the route checks supports_stt and
    # closes with an error frame when the adapter has no STT backing
    # (standalone mode today).
    stt_service = SttService(platform_adapter)

    # First-boot wizard — gates `/api/setup/*` and feeds
    # `setup_required` into `/api/instance/config` so the SPA can
    # redirect to `/setup` until the operator completes the flow.
    setup_service = SetupService(db)

    # ── Auth middleware ───────────────────────────────────────────────────
    # Order matters: signed-URL checks first so browser-loaded media (img,
    # video, download links) authenticate via ``?exp=&sig=`` without ever
    # surfacing the bearer token. Bearer + HA ingress remain as fallbacks
    # for fetch()-driven traffic.
    #
    # ``HaIngressStrategy`` only makes sense when the platform adapter
    # actually advertises :data:`Capability.INGRESS` (i.e. ``haos`` mode
    # behind the HA Supervisor ingress proxy). Wiring it on standalone or
    # the basic ``ha`` adapter would noisily log the
    # "token validation is disabled" warning on every cold start AND
    # leave a tiny attack surface — a request smuggling
    # ``X-Remote-User-Name`` past a non-ingress reverse proxy could bypass
    # bearer auth. Gate on the capability and the strategy disappears
    # entirely outside HAOS.
    bearer_strategy = BearerTokenStrategy(user_repo)
    signed_media_strategy = SignedMediaStrategy()
    strategies: list = [signed_media_strategy]
    if Capability.INGRESS in platform_adapter.capabilities:
        # ``haos`` mode runs behind the HA Supervisor ingress proxy.
        # Supervisor authenticates the user upstream and stamps
        # ``X-Remote-User-Name`` on the proxied request; we trust that
        # header the same way every other HA add-on does (node-red,
        # vscode, file-editor, ESPHome…). Capability gating keeps the
        # strategy off in ``standalone`` / ``ha`` modes where there is
        # no Supervisor in front of us.
        strategies.append(HaIngressStrategy(user_repo))
    strategies.append(bearer_strategy)
    chained_strategy = ChainedStrategy(*strategies)
    auth_middleware = require_auth(chained_strategy)

    # ── Rate-limit + hardening middleware (§25.7) ────────────────────────
    limiter = RateLimiter()
    (
        security_headers_middleware,
        body_size_middleware,
        cors_middleware,
        rate_middleware,
    ) = _build_middleware(config, limiter)

    # ── Application ───────────────────────────────────────────────────────
    # Order matters: hardening runs first (cheap rejects), then auth,
    # then per-route rate limiting.
    app = web.Application(
        middlewares=[
            security_headers_middleware,
            body_size_middleware,
            cors_middleware,
            auth_middleware,
            rate_middleware,
        ]
    )

    # ── Federation infrastructure (KEK + federation + outbox processor) ──
    # The KEK protects the Ed25519 identity seed at rest; the seed is needed
    # by FederationService for envelope signing. Both are loaded in
    # _on_startup once the DB is open.
    key_manager: KeyManager | None = None
    federation_service: FederationService | None = None
    outbox_processor: OutboxProcessor | None = None
    stale_call_scheduler: StaleCallCleanupScheduler | None = None
    gfs_ws_supervisor: GfsWebSocketSupervisor | None = None
    replay_cache_scheduler: ReplayCachePruneScheduler | None = None
    app_pending_session_scheduler: AppPendingSessionPruneScheduler | None = None
    audio_transcript_scheduler: AudioTranscriptScheduler | None = None
    dm_relay_seen_scheduler: DmRelaySeenPruneScheduler | None = None
    password_reset_cleanup_scheduler: PasswordResetCleanupScheduler | None = None
    auth_audit_cleanup_scheduler: AuthAuditCleanupScheduler | None = None
    notification_cleanup_scheduler: NotificationCleanupScheduler | None = None
    pairing_relay_scheduler: PairingRelayRetentionScheduler | None = None
    pairing_session_prune_scheduler: PairingSessionPruneScheduler | None = None
    dm_gc_scheduler: DmGcScheduler | None = None
    media_sweep_scheduler: MediaOrphanSweepScheduler | None = None
    page_lock_scheduler: PageLockExpiryScheduler | None = None
    space_retention_scheduler: SpaceRetentionScheduler | None = None
    post_draft_scheduler: PostDraftCleanupScheduler | None = None
    calendar_reminder_scheduler: CalendarReminderScheduler | None = None
    task_deadline_scheduler: TaskDeadlineScheduler | None = None
    task_recurrence_scheduler: TaskRecurrenceScheduler | None = None
    app_update_scheduler: AppUpdateScheduler | None = None

    # Store services / repos in app using typed AppKeys (no warnings)
    app[K.config_key] = config
    # Expose the same limiter so public endpoints (e.g. /api/auth/token)
    # can implement IP-bucket brute-force protection without rebuilding
    # a second instance.
    app[K.rate_limiter_key] = limiter
    app[K.db_key] = db
    app[K.event_bus_key] = bus
    app[K.ws_manager_key] = ws_manager
    app[K.push_service_key] = push_service
    app[K.push_subscription_repo_key] = push_sub_repo
    app[K.search_service_key] = search_service
    app[K.theme_service_key] = theme_service
    app[K.storage_quota_service_key] = storage_quota
    app[K.backup_service_key] = backup_service
    app[K.recovery_kit_service_key] = recovery_kit_service
    app[K.idempotency_cache_key] = idempotency_cache
    app[K.reconnect_queue_key] = reconnect_queue
    app[K.gfs_connection_service_key] = gfs_connection_service
    app[K.gfs_connection_repo_key] = repos.gfs_connection
    app[K.public_space_discovery_key] = public_space_discovery
    app[K.peer_space_directory_repo_key] = repos.peer_space_directory
    app[K.gallery_service_key] = gallery_service
    app[K.gallery_repo_key] = gallery_repo
    app[K.media_transcode_repo_key] = repos.media_transcode
    app[K.media_transcode_service_key] = media_transcode_service
    app[K.child_protection_service_key] = child_protection_service
    app[K.typing_service_key] = typing_service
    app[K.preferences_service_key] = preferences_service
    app[K.app_service_key] = app_service
    app[K.alias_service_key] = alias_service
    app[K.alias_resolver_key] = alias_resolver
    app[K.data_export_service_key] = data_export_service
    app[K.i18n_key] = i18n
    app[K.platform_adapter_key] = platform_adapter
    app[K.calendar_import_service_key] = calendar_import_service
    app[K.stt_service_key] = stt_service
    app[K.setup_service_key] = setup_service
    app[K.user_service_key] = user_service
    app[K.feed_service_key] = feed_service
    app[K.space_service_key] = space_service
    app[K.space_approval_service_key] = space_approval_service
    app[K.notification_service_key] = notification_service
    app[K.dm_service_key] = dm_service
    app[K.report_repo_key] = report_repo
    app[K.report_service_key] = report_service
    app[K.task_service_key] = task_service
    app[K.space_task_service_key] = space_task_service
    app[K.calendar_service_key] = calendar_service
    app[K.space_cal_service_key] = space_cal_service
    app[K.shopping_service_key] = shopping_service
    # Bot-bridge stack — HA automations post into spaces/DMs via a thin
    # inbound service; SpaceBotService handles the admin/member CRUD.
    bot_bridge_service = BotBridgeService(
        space_post_repo,
        space_repo,
        conversation_repo,
        bus,
    )
    space_bot_service = SpaceBotService(
        space_bot_repo,
        space_repo,
        user_repo,
        bus,
    )
    app[K.space_bot_repo_key] = space_bot_repo
    app[K.space_bot_service_key] = space_bot_service
    app[K.bot_bridge_service_key] = bot_bridge_service
    app[K.user_repo_key] = user_repo
    app[K.password_reset_repo_key] = repos.password_reset
    app[K.auth_audit_log_repo_key] = repos.auth_audit_log
    app[K.profile_picture_repo_key] = profile_picture_repo
    app[K.space_cover_repo_key] = space_cover_repo
    app[K.space_icon_repo_key] = space_icon_repo
    app[K.post_repo_key] = post_repo
    app[K.space_repo_key] = space_repo
    app[K.space_remote_member_repo_key] = repos.space_remote_member
    app[K.space_remote_location_repo_key] = repos.space_remote_location
    app[K.notification_repo_key] = notification_repo
    app[K.conversation_repo_key] = conversation_repo
    app[K.outbox_repo_key] = outbox_repo
    app[K.federation_repo_key] = federation_repo
    app[K.peer_user_visibility_repo_key] = repos.peer_user_visibility
    app[K.page_repo_key] = page_repo
    app[K.page_conflict_service_key] = page_conflict_service
    app[K.presence_service_key] = presence_service
    app[K.online_status_service_key] = online_status_service
    app[K.space_zone_service_key] = space_zone_service
    app[K.space_zone_repo_key] = repos.space_zone
    app[K.poll_service_key] = poll_service
    app[K.space_poll_service_key] = space_poll_service
    app[K.bazaar_service_key] = bazaar_service
    app[K.corner_service_key] = corner_service
    app[K.sticky_repo_key] = sticky_repo
    app[K.bazaar_repo_key] = bazaar_repo
    app[K.shopping_repo_key] = shopping_repo
    app[K.highlight_repo_key] = highlight_repo
    app[K.highlight_service_key] = highlight_service
    app[K.highlight_retention_scheduler_key] = highlight_retention_scheduler
    app[K.highlight_publication_service_key] = highlight_publication_service
    app[K.moment_repo_key] = moment_repo
    app[K.moment_service_key] = moment_service
    app[K.moment_retention_scheduler_key] = moment_retention_scheduler
    app[K.moment_public_registration_repo_key] = moment_public_registration_repo
    app[K.moment_public_follow_repo_key] = moment_public_follow_repo
    app[K.moment_public_service_key] = moment_public_service
    app[K.moment_public_outbound_key] = moment_public_outbound
    app[K.moment_public_inbound_key] = moment_public_inbound

    # ── Mount routes ─────────────────────────────────────────────────────
    setup_routes(app)

    # ── Startup / cleanup hooks ───────────────────────────────────────────

    async def _on_startup(app: web.Application) -> None:  # noqa: RUF029
        nonlocal key_manager, federation_service, outbox_processor
        log.info("socialhome: starting up (mode=%s)", config.mode)
        await db.startup()

        # Shared aiohttp client session — every HTTP caller in the app
        # (HA adapter, Supervisor client, federation, GFS, standalone
        # push) reuses its connection pool. Closed in _on_cleanup.
        http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        app[K.http_session_key] = http_session
        gfs_connection_service.attach_session(http_session)
        public_space_discovery.attach_session(http_session)

        # 1. KEK — encrypts identity_private_key at rest.
        key_manager = KeyManager.from_data_dir(config.data_dir)
        app[K.key_manager_key] = key_manager
        # The space repo wraps each space's Ed25519 seed under this KEK; it
        # was built in ``_build_repos`` before the KEK existed, so wire it now.
        space_repo.attach_key_manager(key_manager)
        # The user repo unwraps each user's Ed25519 identity seed under the
        # same KEK (proto-v_25 per-user identity binding) — same late-wiring.
        user_repo.attach_key_manager(key_manager)

        # 2. Identity bootstrap — generates row on first start, returns
        #    decrypted seed + public key + derived instance_id. When the
        #    configured sig_suite includes a PQ algorithm the bundle
        #    also carries ML-DSA-65 seed + public key.
        identity = await ensure_instance_identity(
            db,
            key_manager,
            display_name=config.instance_name,
            sig_suite=config.federation_sig_suite,
        )
        # Backfill a KEK-wrapped Ed25519 identity key for every local user
        # lacking one (e.g. rows created before this feature, or by a bare
        # UserService during early boot). Runs right after the instance
        # identity + KEK exist; new users get theirs at provision time.
        await ensure_user_identities(
            db,
            key_manager,
            sig_suite=config.federation_sig_suite,
        )

        identity_seed = identity.identity_seed
        identity_pk = identity.identity_public_key
        real_instance_id = identity.instance_id
        app[K.instance_id_key] = real_instance_id
        app[K.instance_signing_key_key] = identity_seed
        app[K.instance_public_key_key] = identity_pk
        app[K.instance_keywrap_public_key_key] = identity.keywrap_public_key
        app[K.instance_keywrap_sig_key] = identity.keywrap_sig
        # Stamp Momentum rows with the local instance_id so the 3-hop
        # relay can guard against echo loops (origin_instance_id check).
        moment_service.attach_instance_id(real_instance_id)

        # Wire the GFS publish context so global-space metadata + an
        # Ed25519 signature ride the publish call (otherwise the GFS
        # only sees ``{space_id}`` and lands a pending row).
        gfs_connection_service.attach_publish_context(
            space_repo=repos.space,
            own_instance_id=real_instance_id,
            own_signing_key=identity_seed,
            theme_repo=repos.theme,
            cover_repo=repos.space_cover,
            icon_repo=repos.space_icon,
        )

        # Short-lived signed URLs for browser-loaded media — see §23.21
        # ``media_signer.py``. The HMAC key is HKDF-derived from the
        # identity seed so it never reuses the federation Ed25519 key
        # material directly. Stashed here (rather than at app-build time)
        # because the seed only becomes available after this bootstrap.
        media_signer = MediaUrlSigner(key=derive_signing_key(identity_seed))
        app[K.media_signer_key] = media_signer
        # WebSocket frames for ``post.created`` / ``comment.added`` etc.
        # need the same signed URL shape as the REST responses, so the
        # SPA can render `<img src={post.media_url}>` straight from the
        # frame without a follow-up REST hydrate.
        realtime_service.attach_media_signer(media_signer)

        # Report service auto-forwards fraud reports to every paired GFS.
        # Identity seed is the Ed25519 signing key used on /gfs/report.
        report_service.attach_gfs(
            gfs_connection_service,
            signing_key=identity_seed,
        )
        # Same identity binding for the public-highlight publish service —
        # needs the signing key for the GFS publish/revoke envelopes
        # plus the shared aiohttp client for the round-trip itself.
        highlight_publication_service.attach_session(http_session)
        highlight_publication_service.attach_identity(
            own_instance_id=real_instance_id,
            signing_key=identity_seed,
        )
        # And the matching wiring for the answerer side. The handler
        # will receive ``highlight_signal`` frames once the supervisor
        # gets ``attach_highlight_signal_handler`` — see below where the
        # supervisor is constructed.
        # Author-side answerers need the operator's STUN/TURN so a guest
        # behind NAT can complete the direct DataChannel; without them the
        # peer offers host candidates only and most cross-NAT viewers fall
        # back to the GFS relay. Use the same per-instance HMAC TURN identity
        # the federation transport uses.
        public_ice_servers = _default_ice_servers(config, hmac_user_id=real_instance_id)
        highlight_signaling_handler.attach_session(http_session)
        highlight_signaling_handler.attach_identity(
            own_instance_id=real_instance_id,
            signing_key=identity_seed,
        )
        highlight_signaling_handler.attach_ice_servers(public_ice_servers)
        # Matching wiring for the public-moments answerer side.
        moment_public_signaling_handler.attach_session(http_session)
        moment_public_signaling_handler.attach_identity(
            own_instance_id=real_instance_id,
            signing_key=identity_seed,
        )
        moment_public_signaling_handler.attach_ice_servers(public_ice_servers)
        # Public-Momentum service + outbound subscriber. Same shape as
        # ``highlight_publication_service``: shared session + signing
        # key wired up after federation identity loads.
        moment_public_service.attach_session(http_session)
        moment_public_service.attach_identity(
            own_instance_id=real_instance_id,
            signing_key=identity_seed,
        )
        moment_public_outbound.attach_session(http_session)
        moment_public_outbound.attach_identity(
            own_instance_id=real_instance_id,
            signing_key=identity_seed,
        )

        # 3. Replace UserService with one carrying the real public key.
        real_user_service = UserService(
            user_repo,
            bus,
            own_instance_public_key=identity_pk,
            profile_picture_repo=profile_picture_repo,
            key_manager=key_manager,
        )
        app[K.user_service_key] = real_user_service

        # 4. Replace SpaceService with one carrying the real instance_id.
        real_space_service = SpaceService(
            space_repo,
            space_post_repo,
            user_repo,
            bus,
            own_instance_id=real_instance_id,
            media_dir=pathlib.Path(config.media_path),
        )
        # §CP.F1: hook child-protection age gate into add_member.
        real_space_service.attach_child_protection(child_protection_service)
        real_space_service.attach_profile_picture_repo(profile_picture_repo)
        real_space_service.attach_cover_repo(space_cover_repo)
        real_space_service.attach_icon_repo(space_icon_repo)
        real_space_service.attach_gallery_repo(gallery_repo)
        real_space_service.attach_bazaar_repo(bazaar_repo)
        real_space_service.attach_gfs_connection_service(gfs_connection_service)
        # ``attach_federation`` is deferred until just after
        # ``_wire_federation_stack`` returns the live ``federation_service``
        # (see below). Calling it here would bind ``_federation`` to the
        # ``None`` placeholder declared at the top of ``create_app``,
        # which makes ``invite_remote_user`` (§D1b cross-household
        # invites) raise ``RuntimeError: federation not attached`` with
        # a 500.
        app[K.space_service_key] = real_space_service

        # 5a. SpaceContentEncryption — per-space epoch keys, KEK-protected.
        # Wires the bus so ``import_key`` publishes
        # :class:`SpaceContentKeyImported` for the
        # :class:`PendingDecryptsCache` to drain stashed sync chunks (#122).
        space_crypto = SpaceContentEncryption(
            space_key_repo,
            key_manager,
            bus=bus,
            # ``identity_seed`` signs the sealed-sender ``outer_signature``
            # so a GFS-relayed public/global-space event is authenticated
            # to the recipient; ``federation_repo`` resolves a decrypted
            # sender_instance_id → its registered Ed25519 pubkey to verify
            # that signature. Both wired so the GFS path is secure out of
            # the box (no unauthenticated sealed sender).
            identity_seed=identity_seed,
            federation_repo=federation_repo,
            # Phase 4b — stamp every locally-minted epoch with this household's
            # id so concurrent delegated-admin rotations converge deterministically.
            own_instance_id=identity.instance_id,
        )
        app[K.space_crypto_service_key] = space_crypto
        pending_decrypts_cache = PendingDecryptsCache(bus=bus)
        app[K.pending_decrypts_cache_key] = pending_decrypts_cache
        # #117 — wire content-key export/import into the §D1b paths
        # so new remote members can actually decrypt the events they
        # receive. Without this, the joiner's local space_keys is
        # empty and every SPACE_POST_CREATED inbound raises.
        real_space_service.attach_space_crypto_service(space_crypto)

        # Public space-content relay (Phase 5a2). Producer fans a
        # PUBLIC/GLOBAL space post out to the GFS as an encrypted,
        # authority-signed envelope (GFS stays content-blind); consumer
        # verifies + decrypts + dedupes the relayed envelope on receive.
        # Built here (not in create_app) because both depend on
        # ``space_crypto``, which only exists once the seed/KEK are wired.
        nonlocal space_public_outbound, space_public_inbound
        nonlocal space_subscriber_key_outbound, space_subscriber_key_inbound
        space_public_outbound = SpacePublicOutbound(
            bus=bus,
            space_repo=space_repo,
            space_crypto=space_crypto,
            user_repo=user_repo,
            gfs_service=gfs_connection_service,
        )
        space_public_outbound.attach_identity(
            own_instance_id=real_instance_id,
            own_instance_public_key=identity_pk,
            own_identity_seed=identity_seed,
        )
        space_public_outbound.wire()
        space_public_inbound = SpacePublicInbound(
            bus=bus,
            space_repo=space_repo,
            space_crypto=space_crypto,
            space_post_repo=space_post_repo,
        )
        # Phase 5b-b — subscriber content-key delivery. Outbound: a seed-holder
        # seals + relays the content key on a GFS ``new_subscriber`` notify.
        # Inbound: the subscriber unseals + imports the relayed handoff.
        space_subscriber_key_outbound = SpaceSubscriberKeyOutbound(
            space_repo=space_repo,
            space_crypto=space_crypto,
            gfs_service=gfs_connection_service,
        )
        space_subscriber_key_outbound.attach_identity(
            own_instance_id=real_instance_id,
        )
        # Phase 5b-c reconcile deps: the GFS-connection repo (to enumerate
        # published spaces) + the shared HTTP session (to pull the subscriber
        # list). Hooked to the GFS-WS on_connected below.
        space_subscriber_key_outbound.attach_reconcile_context(
            gfs_conn_repo=repos.gfs_connection,
            http_session=http_session,
        )
        space_subscriber_key_inbound = SpaceSubscriberKeyInbound(
            space_repo=space_repo,
            space_crypto=space_crypto,
        )
        space_subscriber_key_inbound.attach_identity(
            own_instance_id=real_instance_id,
            keywrap_private_key=identity.keywrap_private_key,
        )

        # 5. Federation stack — FederationService + sync manager + typing/dm/
        #    presence attach + inbound bridge + pairing-relay queue.
        fed = _wire_federation_stack(
            app=app,
            config=config,
            db=db,
            bus=bus,
            http_session=http_session,
            key_manager=key_manager,
            identity=identity,
            federation_repo=federation_repo,
            outbox_repo=outbox_repo,
            conversation_repo=conversation_repo,
            space_post_repo=space_post_repo,
            space_repo=space_repo,
            peer_space_directory_repo=repos.peer_space_directory,
            space_remote_member_repo=repos.space_remote_member,
            space_remote_location_repo=repos.space_remote_location,
            space_cover_repo=space_cover_repo,
            space_icon_repo=space_icon_repo,
            user_repo=user_repo,
            profile_picture_repo=profile_picture_repo,
            page_repo=page_repo,
            sticky_repo=sticky_repo,
            highlight_repo=highlight_repo,
            moment_repo=moment_repo,
            space_task_repo=space_task_repo,
            space_calendar_repo=space_cal_repo,
            calendar_repo=calendar_repo,
            dm_contact_repo=dm_contact_repo,
            space_poll_repo=repos.space_poll,
            gallery_repo=repos.gallery,
            bazaar_repo=bazaar_repo,
            space_crypto=space_crypto,
            reconnect_queue=reconnect_queue,
            idempotency_cache=idempotency_cache,
            typing_service=typing_service,
            dm_service=dm_service,
            dm_media_sync_service=dm_media_sync_service,
            space_media_sync_service=space_media_sync_service,
            dm_routing_service=dm_routing_service,
            dm_routing_repo=repos.dm_routing,
            presence_service=presence_service,
            online_status_service=online_status_service,
            report_service=report_service,
            report_repo=report_repo,
            pairing_relay_repo=repos.pairing_relay,
            space_zone_repo=repos.space_zone,
            presence_repo=repos.presence,
            ws_manager=ws_manager,
            peer_user_visibility_repo=repos.peer_user_visibility,
        )
        federation_service = fed.federation_service
        sync_manager = fed.sync_manager
        # Cross-household private-space invites (§D1b) need a live
        # ``FederationService``. Wire it now that ``_wire_federation_stack``
        # has returned the real one — see the matching deferral note
        # above the ``app[K.space_service_key] = real_space_service``
        # registration.
        real_space_service.attach_federation(
            federation_service=federation_service,
            federation_repo=federation_repo,
            remote_member_repo=repos.space_remote_member,
        )
        # #114 phase 2 — the SPACE_REMOTE_ADMIN_KICK inbound handler
        # was constructed inside ``_wire_federation_stack`` before
        # ``real_space_service`` existed; wire it now so the host
        # can actually dispatch the validated kick into the service.
        app[K.private_invite_handler_key].attach_space_service(
            real_space_service,
        )
        # Multi-admin approval (quorum) — rebuild with the real instance_id
        # (mirrors the real_space_service rebuild) and wire federation + the
        # real space_service it executes approved actions through.
        real_space_approval_service = SpaceApprovalService(
            repos.space_proposal,
            space_repo,
            repos.space_remote_member,
            user_repo,
            bus,
            own_instance_id=real_instance_id,
        )
        real_space_approval_service.attach(
            federation_service=federation_service,
            space_service=real_space_service,
        )
        app[K.space_approval_service_key] = real_space_approval_service
        # The private-invite handler dispatches the propose / vote verbs and
        # the SPACE_ADMIN_PROPOSAL_UPDATED mirror into the approval service.
        app[K.private_invite_handler_key].attach_approval_service(
            real_space_approval_service,
        )
        # §D2 PR 2 — federation-mesh routing primitives. The discovery
        # service runs BFS-flooded probes to find a chain of confirmed
        # peers leading to an instance we aren't directly paired with;
        # the routed-envelope handler wraps an inner event for
        # multi-hop forwarding along the discovered chain. Both wire
        # into the federation event registry via attach_to().
        route_discovery = RouteDiscoveryService(
            federation_service=federation_service,
            federation_repo=federation_repo,
            max_hops=config.max_route_hops,
        )
        route_discovery.attach_to(federation_service)
        routed_handler = SpaceRoutedHandler(
            federation_service=federation_service,
            federation_repo=federation_repo,
            event_dispatcher=federation_service._dispatch_event,  # noqa: SLF001
            # The discovery service holds the target-side ephemeral
            # privates it minted in response to FIND_ROUTE probes;
            # the routed handler asks it for the matching priv when
            # an inbound SPACE_ROUTED forward leg lands here.
            target_eph_lookup=route_discovery.lookup_target_eph_priv,
        )
        routed_handler.attach_to(federation_service)
        # Mesh lives on the federation service so every outbound that
        # goes through ``send_with_mesh_fallback`` (private invites,
        # space-content broadcasts, future per-peer fanouts) sees the
        # same direct-then-mesh logic without each service rewiring.
        federation_service.attach_mesh(
            route_service=route_discovery,
            routed_handler=routed_handler,
        )
        # §D2 cross-instance invite-token redeem — wires the
        # ``SPACE_INVITE_TOKEN_REDEEM*`` family into the federation
        # event registry and gives ``space_service`` the driver it
        # delegates to when a local user pastes a peer's token. PR 2
        # injects the mesh-routing pair so REDEEMs against unpaired
        # issuers transparently route through a discovered chain
        # rather than fail-fast with "pair first".
        invite_redeem_coordinator = SpaceInviteTokenRedeemCoordinator(
            bus=bus,
            federation_service=federation_service,
            space_repo=space_repo,
            space_remote_member_repo=repos.space_remote_member,
            user_repo=user_repo,
            federation_repo=federation_repo,
            route_service=route_discovery,
            routed_handler=routed_handler,
            cover_repo=space_cover_repo,
            icon_repo=space_icon_repo,
            space_crypto_service=space_crypto,
            child_protection_service=child_protection_service,
        )
        invite_redeem_coordinator.attach_to(federation_service)
        real_space_service.attach_redeem_coordinator(invite_redeem_coordinator)
        # #117 followup — federate SPACE_POST_CREATED outbound so
        # remote members on other households actually receive posts
        # in spaces they belong to. The inbound side was already
        # wired in federation_inbound_service; this is the missing
        # producer.
        space_post_outbound = SpacePostOutbound(
            bus=bus,
            federation_service=federation_service,
            space_repo=space_repo,
            user_repo=user_repo,
            media_sync=space_media_sync_service,
            federation_repo=repos.federation,
        )
        space_post_outbound.attach_identity(
            own_instance_id=real_instance_id,
            own_instance_public_key=identity_pk,
            own_identity_seed=identity_seed,
        )
        # Federate per-edit config changes (rename, emoji, feature
        # toggles, location_mode flips, retention bumps) to remote
        # member stubs in realtime. Before this, SPACE_CONFIG_CHANGED
        # only shipped via the §D1b catch-up reply, so toggling
        # ``location_mode`` on the host left every remote stub with
        # the prior mode — the receiver-side strict ``mode_filter``
        # in the space-map API then dropped pin rows that had
        # otherwise been validated + persisted.
        SpaceConfigOutbound(
            bus=bus,
            federation_service=federation_service,
            space_repo=space_repo,
        ).wire()
        # Wire RSVP propagation onto the calendar service. Done after
        # federation_service is built so the service can broadcast on
        # rsvp() / remove_rsvp() (§Phase A).
        space_cal_service.attach_federation(federation_service)
        # Late-bind realtime into the federation inbound — the
        # DM_MEDIA_BLOB handler uses it to fan ``dm.media_ready``
        # frames to local participants when the full bytes for a
        # cross-household media DM land.
        fed.inbound_service.attach_realtime(realtime_service)
        # Personal calendar federation (§23.60). Cross-household invites
        # ride on regular send_event envelopes; attendee → instance
        # routing happens inside the service via the user/federation
        # repos.
        calendar_service.attach_federation(
            federation_service,
            federation_repo=federation_repo,
            user_repo=user_repo,
        )
        # Spec §24.10.7 — provider asks the paired GFS for a least-loaded
        # signaling node before generating SPACE_SYNC_OFFER, releases on
        # DIRECT_READY / DIRECT_FAILED.
        federation_service.attach_gfs_connection_service(gfs_connection_service)
        await federation_service.warm_replay_cache()

        # Federation transport facade (§24.12.5): WebRTC DataChannel
        # primary, HTTPS HTTPS inbox fallback. The signalling callback is
        # send_event itself — SDP offers/answers/ICE ride on top of the
        # existing signed envelope path.
        async def _signaling_send(
            to_instance_id: str,
            event_type,
            payload,
        ):
            return await federation_service.send_event(
                to_instance_id=to_instance_id,
                event_type=event_type,
                payload=payload,
            )

        # Pass the local ``instance_id`` as the HMAC-TURN user so a
        # coturn-style REST credential (when ``webrtc_turn_secret`` is
        # set) is bound to this instance. Without it the federation
        # transport would silently drop down to static TURN creds —
        # which a security-conscious operator running coturn with
        # ``--use-auth-secret`` would refuse to accept.
        fed_ice_servers = _default_ice_servers(
            config,
            hmac_user_id=real_instance_id,
        )
        warn_if_no_turn(fed_ice_servers)
        warn_if_turn_unusable(fed_ice_servers)
        fed_transport = FederationTransport(
            own_instance_id=real_instance_id,
            https_inbox=HttpsInboxTransport(
                client_factory=federation_service._get_http_client,
            ),
            signaling_send=_signaling_send,
            ice_servers=fed_ice_servers,
            inbound_handler=federation_service.handle_inbound_rtc,
            media_inbound_handler=federation_service.handle_inbound_media_frame,
            app_inbound_handler=federation_service._app_inbound_handler,
            bus=bus,
        )
        federation_service.attach_transport(fed_transport)
        app[K.federation_transport_key] = fed_transport

        # SH's HA platform adapter (HaAdapter / HaosAdapter) pulls HA
        # Core's ``web_rtc/ice_servers`` list over the HA WebSocket on
        # startup and refreshes daily — see
        # :mod:`socialhome.platform.ha.ice_servers_sync`. Until that
        # first fetch lands the federation transport uses the
        # ``_default_ice_servers(config)`` list (Config-level
        # ``webrtc_*`` fields). Standalone mode never pulls; the Config
        # defaults are the steady state there.

        app[K.federation_service_key] = federation_service
        app[K.sync_session_manager_key] = sync_manager
        app[K.dm_routing_service_key] = dm_routing_service

        # PeerHomeSharingService — flips remote_instances.share_home and fires
        # a one-shot LOCAL_HOME_LOCATION_CHANGED to the affected peer so its map
        # updates immediately (null coords on OFF, current coords on ON).
        peer_home_sharing_service = PeerHomeSharingService(
            federation_repo=federation_repo,
            federation_service=federation_service,
        )
        app[K.peer_home_sharing_service_key] = peer_home_sharing_service

        # CallSignalingService — backend relay for WebRTC voice/video.
        call_signaling = CallSignalingService(
            call_repo=repos.call,
            conversation_repo=conversation_repo,
            user_repo=user_repo,
            own_identity_seed=identity_seed,
            federation_service=federation_service,
            ws_manager=ws_manager,
        )
        federation_service.attach_call_signaling(call_signaling)
        call_signaling.attach_push_service(push_service)
        app[K.call_signaling_service_key] = call_signaling
        app[K.call_repo_key] = repos.call

        # AppFederationService — bridges inbound APP_SESSION / APP_MESSAGE
        # federation events and binary fed-app-v1 DataChannel frames into
        # WebSocket pushes for local users. Constructed after FederationService
        # and FederationTransport so it can call attach_apps() which registers
        # both the JSON event-registry handlers and the binary-channel path
        # (already threaded into the transport via app_inbound_handler above).
        app_federation_service = AppFederationService(
            app_repo=repos.app,
            user_repo=user_repo,
            ws=ws_manager,
            federation=federation_service,
            federation_repo=federation_repo,
            cp_repo=repos.cp,
            bus=bus,
        )
        federation_service.attach_apps(app_federation_service)
        app[K.app_federation_service_key] = app_federation_service

        # Stale-call cleanup scheduler (§26.8).
        nonlocal stale_call_scheduler
        stale_call_scheduler = StaleCallCleanupScheduler(call_signaling)
        await stale_call_scheduler.start()

        # GFS WebSocket supervisor (§24.12) — opens a persistent
        # ``wss://`` connection to every paired GFS so relay events
        # arrive without an HTTPS callback. SH→GFS REST stays unchanged.
        # Inbound relay frames are logged here today; integration into the
        # federation inbound pipeline is a follow-up.
        async def _on_gfs_relay(frame: dict) -> None:
            log.info(
                "gfs.relay.received: space=%s event=%s from=%s",
                frame.get("space_id"),
                frame.get("event_type"),
                frame.get("from_instance"),
            )
            # Public space-content relay (Phase 5a2): a ``space_post_public``
            # frame carries an encrypted, authority-signed post envelope.
            # The inbound consumer verifies + decrypts + dedupes; other
            # event types remain logged-only until their consumers land.
            if (
                frame.get("event_type") == AUTHORITY_EVENT_SPACE_POST_PUBLIC
                and space_public_inbound is not None
            ):
                await space_public_inbound.handle(frame)
            # Phase 5b-b subscriber content-key handoff: the subscriber unseals
            # + imports the relayed content key so it can decrypt the relay.
            elif (
                frame.get("event_type") == AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
                and space_subscriber_key_inbound is not None
            ):
                await space_subscriber_key_inbound.handle(frame)

        # Re-fetch the GFS's current server_name on each WS (re)connect and
        # refresh the stored display_name if the operator renamed the
        # server (a rename typically restarts the GFS → forces a reconnect).
        async def _on_gfs_connected(gfs_id: str) -> None:
            await gfs_connection_service.refresh_connection_metadata(gfs_id)
            # Self-heal the household name on the GFS: a rename is pushed only
            # best-effort on edit (and never re-pushed), so a GFS that was down
            # at rename time — or that re-created our client row — keeps showing
            # the stale name forever. Re-push the *current* federated
            # display_name (instance_identity self-row, the same source the
            # pairing QR + peers use) on every (re)connect. Fail-soft: a push
            # failure must never break the metadata refresh or key reconcile.
            try:
                local_identity = await federation_repo.get_local_identity()
                current_name = (local_identity or {}).get("display_name")
                if current_name:
                    await gfs_connection_service.push_display_name(gfs_id, current_name)
            except Exception:  # noqa: BLE001 — best-effort self-heal
                log.warning(
                    "GFS %s: re-pushing household display_name on reconnect failed",
                    gfs_id,
                    exc_info=True,
                )
            # Phase 5b-c reconcile: pull this GFS's subscriber list for every
            # seed-held public/global space and re-seal the content key to each
            # — delivering keys missed while no seed-holder was online (works
            # owner-offline via any delegated admin). Fail-soft (never raises).
            if space_subscriber_key_outbound is not None:
                await space_subscriber_key_outbound.reconcile(gfs_id)

        nonlocal gfs_ws_supervisor
        gfs_ws_supervisor = GfsWebSocketSupervisor(
            repo=repos.gfs_connection,
            instance_id=real_instance_id,
            signing_key=identity_seed,
            session_factory=lambda: http_session,
            on_relay=_on_gfs_relay,
            on_highlight_signal=highlight_signaling_handler.handle_signal,
            on_moment_signal=moment_public_signaling_handler.handle_signal,
            on_moment_public=moment_public_inbound.handle,
            on_new_subscriber=space_subscriber_key_outbound.handle,
            on_connected=_on_gfs_connected,
        )
        await gfs_ws_supervisor.start()
        app[K.gfs_ws_supervisor_key] = gfs_ws_supervisor

        # 6. OutboxProcessor — drains federation_outbox in the background.
        async def _deliver(entry):
            """Re-deliver an outbox entry via FederationService.

            The outbox stores the full envelope JSON (signed + encrypted)
            from the original send_event() call. On retry we POST the same
            bytes verbatim — no re-encryption.
            """
            return await _redeliver_envelope(
                federation_service,
                federation_repo,
                entry,
            )

        outbox_processor = OutboxProcessor(outbox_repo, _deliver)
        await outbox_processor.start()
        app[K.outbox_processor_key] = outbox_processor

        # Reconnect queue — drains backlog work in priority order.
        await reconnect_queue.start()

        # Replay-cache pruner (§24.11) — keeps federation_replay_cache
        # bounded so a long-running instance doesn't accumulate years of
        # signed-envelope ids on disk.
        nonlocal replay_cache_scheduler
        replay_cache_scheduler = ReplayCachePruneScheduler(
            federation_repo, window=REPLAY_CACHE_WINDOW
        )
        await replay_cache_scheduler.start()

        # App-pending-session pruner — sweeps TTL-expired inbound app-session
        # invites so the table can't accumulate abandoned invites forever
        # (the per-pair cap bounds a flood; this clears the slow leak).
        nonlocal app_pending_session_scheduler
        app_pending_session_scheduler = AppPendingSessionPruneScheduler(repos.app)
        await app_pending_session_scheduler.start()

        # DM-relay-seen pruner (§12.5.3) — same shape as the replay-cache
        # pruner, but for the DM_RELAY dedup ring. Without this the
        # ``dm_relay_seen`` table grows unbounded for the lifetime of
        # the instance.
        nonlocal dm_relay_seen_scheduler
        dm_relay_seen_scheduler = DmRelaySeenPruneScheduler(dm_routing_service)
        await dm_relay_seen_scheduler.start()

        # DM media outbox scheduler (§DM-media) — flushes
        # ``dm_media_outbox`` rows by reading the full bytes off
        # disk + dispatching ``DM_MEDIA_BLOB`` to the target peer.
        # Sleeps between ticks; an immediate first tick happens on
        # ``start()`` so a fresh boot picks up rows queued before
        # the previous run was killed.
        await dm_media_sync_service.start()
        # Same shape for the space-post media outbox scheduler.
        await space_media_sync_service.start()
        # Background video-transcode scheduler — drains
        # ``media_transcode_jobs`` so async uploads resolve their
        # "processing" placeholder. Reclaims orphaned rows on start.
        await media_transcode_service.start()

        # Voice-note receiver-side fallback STT. Runs only when the
        # adapter advertises ``Capability.STT`` — otherwise the
        # scheduler would burn CPU on every tick decoding blobs whose
        # transcription would fail-silent anyway.
        nonlocal audio_transcript_scheduler
        if platform_adapter.supports_stt:
            audio_transcript_scheduler = AudioTranscriptScheduler(
                conversation_repo=conversation_repo,
                user_repo=user_repo,
                transcribe=audio_transcription_service,
                bus=bus,
                media_dir=pathlib.Path(config.media_path),
            )
            await audio_transcript_scheduler.start()

        # Password-reset cleanup — drops expired admin-issued reset
        # tokens so the table doesn't accumulate one row per reset
        # forever (1h TTL, runs hourly).
        nonlocal password_reset_cleanup_scheduler
        password_reset_cleanup_scheduler = PasswordResetCleanupScheduler(
            repos.password_reset,
        )
        await password_reset_cleanup_scheduler.start()

        # Auth-audit cleanup — drops aged rows so the append-only trail
        # can't be grown without bound by repeated failed logins
        # (90-day retention, runs hourly).
        nonlocal auth_audit_cleanup_scheduler
        auth_audit_cleanup_scheduler = AuthAuditCleanupScheduler(
            repos.auth_audit_log,
        )
        await auth_audit_cleanup_scheduler.start()

        # Notification cleanup — drops in-app notification rows older than
        # the 90-day retention window so inactive users' rows don't pile up
        # forever (per-user cap only prunes on fresh inserts; runs hourly).
        nonlocal notification_cleanup_scheduler
        notification_cleanup_scheduler = NotificationCleanupScheduler(
            repos.notification,
        )
        await notification_cleanup_scheduler.start()

        # Online-status idle scanner — promotes online → idle after 5
        # minutes of WS-frame silence and back to online on activity.
        await online_status_service.start()

        # Pairing-relay retention (§11.9) — drops approved/declined
        # rows after a week and pending rows after a month so the
        # admin queue table stays bounded.
        nonlocal pairing_relay_scheduler
        pairing_relay_scheduler = PairingRelayRetentionScheduler(
            repos.pairing_relay,
        )
        await pairing_relay_scheduler.start()

        # Pairing-session retention (§11) — drops ``pending_pairings``
        # rows past their ``expires_at`` and the orphan PENDING
        # ``remote_instances`` they pointed at, so the SPA's pending
        # handshake list doesn't grow forever when a peer never
        # completes the SAS step.
        nonlocal pairing_session_prune_scheduler
        pairing_session_prune_scheduler = PairingSessionPruneScheduler(
            federation_repo,
        )
        await pairing_session_prune_scheduler.start()

        # DM GC (§23.47c) — hard-deletes conversations whose every
        # local member has soft-left and which have no remote members.
        nonlocal dm_gc_scheduler
        dm_gc_scheduler = DmGcScheduler(
            conversation_repo,
            media_dir=pathlib.Path(config.media_path),
        )
        await dm_gc_scheduler.start()

        # Media orphan sweep — backstop that removes media-dir files no DB
        # row references (e.g. media whose post was deleted remotely). 24 h
        # grace + skip-patterns guard in-flight transfers + DM intermediates.
        nonlocal media_sweep_scheduler
        media_sweep_scheduler = MediaOrphanSweepScheduler(
            MediaOrphanSweepService(
                media_dir=pathlib.Path(config.media_path),
                reference_repo=SqliteMediaReferenceRepo(db),
                media_transcode_repo=repos.media_transcode,
            ),
        )
        await media_sweep_scheduler.start()

        # Bazaar auction expiry — closes due auctions on a 60-s cadence.
        await bazaar_expiry_scheduler.start()

        # Highlights retention — drops expired + over-max highlights per author.
        await highlight_retention_scheduler.start()

        # Momentum retention — drops moments past the absolute 7-day cap.
        await moment_retention_scheduler.start()

        # Page-lock + retention + draft cleanup schedulers.
        nonlocal page_lock_scheduler, space_retention_scheduler
        nonlocal post_draft_scheduler, calendar_reminder_scheduler
        nonlocal task_deadline_scheduler, task_recurrence_scheduler
        page_lock_scheduler = PageLockExpiryScheduler(page_repo)
        await page_lock_scheduler.start()

        space_retention_scheduler = SpaceRetentionScheduler(db)
        await space_retention_scheduler.start()

        post_draft_scheduler = PostDraftCleanupScheduler(db)
        await post_draft_scheduler.start()

        calendar_reminder_scheduler = CalendarReminderScheduler(
            calendar_repo=calendar_repo,
            user_repo=user_repo,
            notif_service=notification_service,
        )
        await calendar_reminder_scheduler.start()

        # Phase D: per-user space-event reminders.
        await space_calendar_reminder_scheduler.start()

        task_deadline_scheduler = TaskDeadlineScheduler(
            repo=task_repo,
            db=db,
            bus=bus,
        )
        await task_deadline_scheduler.start()

        task_recurrence_scheduler = TaskRecurrenceScheduler(task_service)
        await task_recurrence_scheduler.start()

        # App-update background checker — polls the app catalog once per day
        # so the admin can see available updates without a manual refresh.
        nonlocal app_update_scheduler
        app_update_scheduler = AppUpdateScheduler(app_service)
        await app_update_scheduler.start()

        # Public-space discovery poller (no-op when no GFS connections).
        await public_space_discovery.start()

        # §25.6 space-sync scheduler (periodic + event-driven).
        sync_sched = app.get(K.space_sync_scheduler_key)
        if sync_sched is not None:
            await sync_sched.start()

        # 7. Platform adapter startup — HA adapter runs bootstrap + wires
        #    HaBridgeService; standalone adapter is a no-op.
        await platform_adapter.on_startup(app)
        # Wire any extra services the adapter provides into the app dict.
        for key, svc in platform_adapter.get_extra_services().items():
            app[key] = svc

        # 7b. One-shot post-restore peer reconnect. Runs after the adapter's
        #     federation base is set (above) and after the federation stack is
        #     wired (url_update_outbound exists). On the FIRST boot after a
        #     Recovery Kit restore it fans URL_UPDATED out to every confirmed
        #     peer so they update our inbox URL; guarded to run exactly once.
        recovery_reconnect_service = RecoveryReconnectService(
            db, app[K.url_update_outbound_key], platform_adapter
        )
        app[K.recovery_reconnect_service_key] = recovery_reconnect_service
        try:
            await recovery_reconnect_service.maybe_reconnect()
        except Exception:
            log.warning("post-restore reconnect hook failed", exc_info=True)

        # 8. Default-calendar backfill. Runs after the adapter (so the
        #    headless ``provision_admin`` path is included) and after the
        #    HA bootstrap (so synced persons are visible). Idempotent —
        #    a no-op on the steady state, picks up upgraders + any user
        #    whose creation path bypassed the ``UserProvisioned`` event.
        active_users = await user_service.list_active()
        created = await calendar_service.backfill_default_calendars(
            [u.username for u in active_users],
        )
        if created:
            log.info(
                "calendar: seeded %d default calendar(s) for existing users",
                created,
            )

        # 9. Advertise our protocol_version to every confirmed peer.
        # Fire-and-forget — per-peer failures land in the outbox retry
        # queue, and peers that haven't yet replayed our announcement
        # default to ``proto_version=1``, so outbound senders gating on
        # ``peer_supports(...)`` stay safe while the first exchange is
        # in flight. Idempotent across restarts.
        cap_outbound = app.get(K.capabilities_outbound_key)
        if cap_outbound is not None:
            try:
                await cap_outbound.publish()
            except Exception as exc:  # pragma: no cover
                log.warning("capabilities outbound at startup failed: %s", exc)

        # 9b. After an upgrade (OURS increased since last boot), ask confirmed
        # peers to re-advertise their capabilities so our cached proto_version
        # for each peer refreshes promptly. Capabilities-scope only — no
        # content replay — so it can't cause a resync storm. One-shot per
        # upgrade, guarded by the persisted last-OURS on the self-row.
        try:
            await request_capability_resync_if_upgraded(
                federation=federation_service,
                federation_repo=federation_repo,
                identity_repo=federation_repo,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("capability-resync on upgrade failed: %s", exc)

    async def _on_shutdown(app: web.Application) -> None:  # noqa: RUF029
        """Tell every connected WebSocket client we're going away.

        Runs before ``on_cleanup`` so the handler tasks can exit their
        ``async for msg in ws`` loops and run their ``finally`` blocks
        (which call ``ws_manager.unregister``) while the rest of the
        app is still alive. Without this, Ctrl-C hangs as long as any
        browser tab still has the SPA open.
        """
        log.info("socialhome: shutdown — closing live WebSockets")
        await ws_manager.close_all()

    async def _on_cleanup(app: web.Application) -> None:  # noqa: RUF029
        log.info("socialhome: shutting down")
        await platform_adapter.on_cleanup(app)
        if outbox_processor is not None:
            await outbox_processor.stop()
        if stale_call_scheduler is not None:
            await stale_call_scheduler.stop()
        if gfs_ws_supervisor is not None:
            await gfs_ws_supervisor.stop()
        # Wind down any in-flight public-viewer sessions before
        # closing the shared aiohttp client below.
        await highlight_signaling_handler.stop()
        await moment_public_signaling_handler.stop()
        if replay_cache_scheduler is not None:
            await replay_cache_scheduler.stop()
        if app_pending_session_scheduler is not None:
            await app_pending_session_scheduler.stop()
        if dm_relay_seen_scheduler is not None:
            await dm_relay_seen_scheduler.stop()
        if audio_transcript_scheduler is not None:
            await audio_transcript_scheduler.stop()
        # DM media outbox scheduler — drain any in-flight blob send
        # so we don't leave a row marked ``in_flight`` past the
        # restart (the next boot would see it stuck and never retry).
        await dm_media_sync_service.stop()
        await space_media_sync_service.stop()
        await media_transcode_service.stop()
        if password_reset_cleanup_scheduler is not None:
            await password_reset_cleanup_scheduler.stop()
        if auth_audit_cleanup_scheduler is not None:
            await auth_audit_cleanup_scheduler.stop()
        if notification_cleanup_scheduler is not None:
            await notification_cleanup_scheduler.stop()
        await online_status_service.stop()
        if pairing_relay_scheduler is not None:
            await pairing_relay_scheduler.stop()
        if pairing_session_prune_scheduler is not None:
            await pairing_session_prune_scheduler.stop()
        if dm_gc_scheduler is not None:
            await dm_gc_scheduler.stop()
        if media_sweep_scheduler is not None:
            await media_sweep_scheduler.stop()
        if page_lock_scheduler is not None:
            await page_lock_scheduler.stop()
        if space_retention_scheduler is not None:
            await space_retention_scheduler.stop()
        if post_draft_scheduler is not None:
            await post_draft_scheduler.stop()
        if calendar_reminder_scheduler is not None:
            await calendar_reminder_scheduler.stop()
        await space_calendar_reminder_scheduler.stop()
        if task_deadline_scheduler is not None:
            await task_deadline_scheduler.stop()
        if task_recurrence_scheduler is not None:
            await task_recurrence_scheduler.stop()
        if app_update_scheduler is not None:
            await app_update_scheduler.stop()
        sync_sched = app.get(K.space_sync_scheduler_key)
        if sync_sched is not None:
            await sync_sched.stop()
        await bazaar_expiry_scheduler.stop()
        await highlight_retention_scheduler.stop()
        await moment_retention_scheduler.stop()
        # Close all RTC DataChannels so the peers see a clean EOF.
        fed_svc = app.get(K.federation_service_key)
        if fed_svc is not None and getattr(fed_svc, "_transport", None) is not None:
            await fed_svc._transport.close_all()
        await reconnect_queue.stop()
        await public_space_discovery.stop()
        await db.shutdown()
        # Close the shared HTTP session last — every other shutdown step
        # above may still want to issue a final HTTP call.
        http_session = app.get(K.http_session_key)
        if http_session is not None:
            await http_session.close()

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    app.on_cleanup.append(_on_cleanup)

    return app


if __name__ == "__main__":
    from .access_log import RedactingAccessLogger

    cfg = Config.from_env()
    web.run_app(
        create_app(cfg),
        host=cfg.listen_host,
        port=cfg.listen_port,
        access_log_class=RedactingAccessLogger,
    )
