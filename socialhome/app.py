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
from .config import Config
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
from .media_signer import MediaUrlSigner, derive_signing_key
from .infrastructure import (
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
from .infrastructure.pairing_relay_scheduler import PairingRelayRetentionScheduler
from .infrastructure.replay_cache_scheduler import ReplayCachePruneScheduler
from .infrastructure.space_retention_scheduler import SpaceRetentionScheduler
from .platform import build_platform_adapter
from .platform.adapter import Capability
from .rate_limiter import RateLimiter, build_rate_limit_middleware
from .repositories import (
    SqliteBazaarRepo,
    SqliteCalendarRepo,
    SqliteConversationRepo,
    SqliteFederationRepo,
    SqliteNotificationRepo,
    SqliteOutboxRepo,
    SqlitePageRepo,
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
from .repositories.dm_routing_repo import SqliteDmRoutingRepo
from .repositories.gallery_repo import SqliteGalleryRepo
from .repositories.alias_repo import SqliteAliasRepo
from .repositories.household_features_repo import SqliteHouseholdFeaturesRepo
from .repositories.pairing_relay_repo import SqlitePairingRelayRepo
from .repositories.poll_repo import SqlitePollRepo
from .repositories.space_poll_repo import SqliteSpacePollRepo
from .repositories.profile_picture_repo import SqliteProfilePictureRepo
from .repositories.space_bot_repo import SqliteSpaceBotRepo
from .repositories.space_cover_repo import SqliteSpaceCoverRepo
from .repositories.space_zone_repo import SqliteSpaceZoneRepo
from .repositories.presence_repo import SqlitePresenceRepo
from .repositories.peer_space_directory_repo import SqlitePeerSpaceDirectoryRepo
from .repositories.public_space_repo import SqlitePublicSpaceRepo
from .repositories.space_remote_member_repo import SqliteSpaceRemoteMemberRepo
from .repositories.report_repo import SqliteReportRepo
from .repositories.search_repo import SqliteSearchRepo
from .repositories.space_key_repo import SqliteSpaceKeyRepo
from .repositories.storage_stats_repo import SqliteStorageStatsRepo
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
from .services.bot_bridge_service import BotBridgeService
from .services.space_bot_service import SpaceBotService
from .services.calendar_import_service import CalendarImportService
from .services.calendar_service import CalendarService, SpaceCalendarService
from .services.child_protection_service import ChildProtectionService
from .services.data_export_service import DataExportService
from .services.dm_routing_service import DmRoutingService
from .services.federation_inbound_service import FederationInboundService
from .services.poll_federation_outbound import PollFederationOutbound
from .services.calendar_feed_bridge import CalendarFeedBridge
from .services.schedule_calendar_bridge import ScheduleCalendarBridge
from .services.space_calendar_reminder_scheduler import (
    SpaceCalendarReminderScheduler,
)
from .services.schedule_federation_outbound import ScheduleFederationOutbound
from .services.comment_federation_outbound import CommentFederationOutbound
from .services.corner_service import CornerService
from .federation.peer_directory_handler import PeerDirectoryHandler
from .federation.private_invite_handler import PrivateSpaceInviteHandler
from .services.peer_directory_service import PeerDirectoryService
from .services.profile_federation_outbound import ProfileFederationOutbound
from .services.url_update_outbound import UrlUpdateOutbound
from .services.space_member_profile_federation_outbound import (
    SpaceMemberProfileFederationOutbound,
)
from .services.gallery_federation_outbound import GalleryFederationOutbound
from .services.sticky_federation_outbound import StickyFederationOutbound
from .services.space_location_outbound import SpaceLocationOutbound
from .services.space_zone_outbound import SpaceZoneOutbound
from .services.space_zone_service import SpaceZoneService
from .services.task_federation_outbound import TaskFederationOutbound
from .services.federation_inbound import (
    PairingInboundHandlers,
    SpaceContentInboundHandlers,
    SpaceInviteInboundHandlers,
    SpaceMembershipInboundHandlers,
)
from .federation.sync import (
    BansExporter,
    CalendarExporter,
    ChunkBuilder,
    CommentsExporter,
    GalleryExporter,
    MembersExporter,
    PagesExporter,
    PollsExporter,
    PostsExporter,
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
from .services.pairing_relay_queue import PairingRelayQueue
from .services.alias_service import AliasResolver, AliasService
from .services.household_features_service import HouseholdFeaturesService
from .services.page_conflict_service import PageConflictService
from .services.poll_service import PollService
from .services.presence_service import PresenceService
from .services.gfs_connection_service import GfsConnectionService
from .services.public_space_discovery_service import PublicSpaceDiscoveryService
from .services.push_service import PushService, load_or_create_vapid
from .services.report_service import ReportService
from .services.realtime_service import RealtimeService
from .services.search_service import SearchService
from .services.shopping_service import ShoppingService
from .services.space_crypto_service import SpaceContentEncryption
from .services.storage_quota_service import StorageQuotaService
from .services.setup_service import SetupService
from .services.stt_service import SttService
from .services.task_service import SpaceTaskService, TaskService
from .services.theme_service import ThemeService
from .services.typing_service import TypingService
from .services.call_service import CallSignalingService, StaleCallCleanupScheduler

log = logging.getLogger(__name__)


async def _redeliver_envelope(
    federation_service: FederationService,
    federation_repo,
    entry,
) -> bool:
    """Re-POST a previously-built envelope from an :class:`OutboxEntry`.

    The envelope JSON stored in ``payload_json`` is already signed and
    encrypted from the original :meth:`FederationService.send_event`
    call — we just need to look up the peer inbox and POST again.
    Returns ``True`` on 2xx, ``False`` otherwise.
    """
    instance = await federation_repo.get_instance(entry.instance_id)
    if instance is None:
        log.warning("outbox: unknown instance %s — dropping", entry.instance_id)
        return False

    try:
        client = await federation_service._get_http_client()
        async with client.post(
            instance.remote_inbox_url,
            data=entry.payload_json,
            headers={"Content-Type": "application/json"},
            timeout=_aiohttp_timeout(10),
        ) as resp:
            if 200 <= resp.status < 300:
                await federation_repo.mark_reachable(entry.instance_id)
                return True
            log.warning(
                "outbox: %s returned HTTP %d for %s",
                entry.instance_id,
                resp.status,
                entry.id,
            )
            return False
    except Exception as exc:
        log.debug("outbox: redelivery error %s: %s", entry.id, exc)
        return False


def _default_ice_servers(config: Config) -> list[dict]:
    """Build the WebRTC ICE-server list from :class:`Config`.

    The STUN URL is always included; TURN credentials are added only
    when the operator has configured them (TURN typically requires a
    paid relay). Returned in the form expected by both
    ``RTCPeerConnection`` and ``aiolibdatachannel``.
    """
    servers: list[dict] = []
    if config.webrtc_stun_url:
        servers.append({"urls": [config.webrtc_stun_url]})
    if config.webrtc_turn_url:
        entry: dict = {"urls": [config.webrtc_turn_url]}
        if config.webrtc_turn_user:
            entry["username"] = config.webrtc_turn_user
        if config.webrtc_turn_cred:
            entry["credential"] = config.webrtc_turn_cred
        servers.append(entry)
    return servers


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
        bazaar=SqliteBazaarRepo(db),
        push_sub=SqlitePushSubscriptionRepo(db),
        gallery=SqliteGalleryRepo(db),
        space_key=SqliteSpaceKeyRepo(db),
        search=SqliteSearchRepo(db),
        theme=SqliteThemeRepo(db),
        cp=SqliteCpRepo(db),
        dm_routing=SqliteDmRoutingRepo(db),
        dm_contact=SqliteDmContactRepo(db),
        household_features=SqliteHouseholdFeaturesRepo(db),
        presence=SqlitePresenceRepo(db),
        public_space=SqlitePublicSpaceRepo(db),
        peer_space_directory=SqlitePeerSpaceDirectoryRepo(db),
        space_remote_member=SqliteSpaceRemoteMemberRepo(db),
        storage_stats=SqliteStorageStatsRepo(db),
        poll=SqlitePollRepo(db),
        space_poll=SqliteSpacePollRepo(db),
        gfs_connection=SqliteGfsConnectionRepo(db),
        call=SqliteCallRepo(db),
        profile_picture=SqliteProfilePictureRepo(db),
        space_cover=SqliteSpaceCoverRepo(db),
        space_bot=SqliteSpaceBotRepo(db),
        alias=SqliteAliasRepo(db),
        pairing_relay=SqlitePairingRelayRepo(db),
        space_zone=SqliteSpaceZoneRepo(db),
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
    user_repo,
    profile_picture_repo,
    page_repo,
    sticky_repo,
    space_task_repo,
    space_calendar_repo,
    dm_contact_repo,
    space_poll_repo,
    gallery_repo,
    space_crypto,
    reconnect_queue,
    idempotency_cache,
    typing_service,
    dm_service,
    dm_routing_service,
    dm_routing_repo,
    presence_service,
    report_service,
    pairing_relay_repo,
    space_zone_repo,
    presence_repo,
    ws_manager,
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
        ice_servers=_default_ice_servers(config),
        own_pq_seed=identity.pq_seed,
        own_pq_pk=identity.pq_public_key,
        sig_suite=config.federation_sig_suite,
    )
    federation_service.attach_session(http_session)

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

    sync_manager = SyncSessionManager(
        federation_repo,
        get_max_seq=_get_max_seq,
        check_member=_check_member,
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
    federation_service.attach_dm_routing(dm_routing_service)
    federation_service.attach_presence_service(presence_service)

    inbound_service = FederationInboundService(
        bus=bus,
        conversation_repo=conversation_repo,
        space_post_repo=space_post_repo,
        space_repo=space_repo,
        user_repo=user_repo,
        profile_picture_repo=profile_picture_repo,
        report_service=report_service,
        dm_routing_repo=dm_routing_repo,
    )
    inbound_service.attach_to(federation_service)

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
    PrivateSpaceInviteHandler(
        bus=bus,
        space_repo=space_repo,
        remote_member_repo=space_remote_member_repo,
    ).attach_to(federation_service)
    SpaceContentInboundHandlers(
        bus=bus,
        page_repo=page_repo,
        sticky_repo=sticky_repo,
        task_repo=space_task_repo,
        calendar_repo=space_calendar_repo,
        poll_repo=space_poll_repo,
        gallery_repo=gallery_repo,
        zone_repo=space_zone_repo,
    ).attach_to(federation_service)

    # §25.6 Direct Space Sync — content transfer over DataChannel.
    exporters: dict = {
        "bans": BansExporter(space_repo),
        "members": MembersExporter(space_repo),
        "posts": PostsExporter(space_post_repo),
        "comments": CommentsExporter(space_post_repo),
        "tasks": TasksExporter(space_task_repo),
        "tasks_archived": TasksArchivedExporter(space_task_repo),
        "pages": PagesExporter(page_repo),
        "stickies": StickiesExporter(sticky_repo),
        "calendar": CalendarExporter(space_calendar_repo),
        "gallery": GalleryExporter(gallery_repo),
        "polls": PollsExporter(space_poll_repo, space_post_repo),
        "space_zones": ZonesExporter(space_zone_repo),
    }
    chunk_builder = ChunkBuilder(
        encoder=federation_service._encoder,
        crypto=space_crypto,
    )
    space_sync_service = SpaceSyncService(
        builder=chunk_builder,
        exporters=exporters,
        sig_suite=config.federation_sig_suite,
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
    )
    federation_service.attach_space_sync(
        service=space_sync_service,
        receiver=space_sync_receiver,
    )
    app[K.space_sync_service_key] = space_sync_service
    app[K.space_sync_receiver_key] = space_sync_receiver

    # Scheduler: periodic tick + subscribe to PairingConfirmed.
    space_sync_scheduler = SpaceSyncScheduler(
        bus=bus,
        federation=federation_service,
        federation_repo=federation_repo,
        space_repo=space_repo,
        queue=reconnect_queue,
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
        space_repo=space_repo,
    )
    sticky_federation_outbound.wire()

    task_federation_outbound = TaskFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        space_repo=space_repo,
    )
    task_federation_outbound.wire()

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
        space_repo=space_repo,
    )
    space_zone_outbound.wire()

    schedule_federation_outbound = ScheduleFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        space_repo=space_repo,
    )
    schedule_federation_outbound.wire()

    poll_federation_outbound = PollFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        space_repo=space_repo,
    )
    poll_federation_outbound.wire()

    comment_federation_outbound = CommentFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        space_repo=space_repo,
    )
    comment_federation_outbound.wire()

    profile_federation_outbound = ProfileFederationOutbound(
        bus=bus,
        federation_service=federation_service,
        federation_repo=federation_repo,
    )
    profile_federation_outbound.wire()

    # §11 URL rotation fan-out. Triggered by
    # PATCH /api/ha/integration/federation-base when the HA integration
    # reports a new externally-reachable base URL.
    url_update_outbound = UrlUpdateOutbound(
        federation_service=federation_service,
        federation_repo=federation_repo,
    )
    app[K.url_update_outbound_key] = url_update_outbound

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
    )
    gallery_federation_outbound.wire()

    # DM history sync: reconcile missed messages when a peer reconnects.
    dm_history_provider = DmHistoryProvider(
        conversation_repo=conversation_repo,
        federation_service=federation_service,
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

    return SimpleNamespace(
        federation_service=federation_service,
        sync_manager=sync_manager,
        inbound_service=inbound_service,
        pairing_relay_queue=pairing_relay_queue,
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
    dm_contact_repo = repos.dm_contact
    bazaar_repo = repos.bazaar
    push_sub_repo = repos.push_sub
    gallery_repo = repos.gallery
    space_key_repo = repos.space_key
    search_repo = repos.search
    theme_repo = repos.theme
    profile_picture_repo = repos.profile_picture
    space_cover_repo = repos.space_cover
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
    feed_service = FeedService(post_repo, user_repo, bus)
    space_service = SpaceService(
        space_repo,
        space_post_repo,
        user_repo,
        bus,
        own_instance_id="unknown",  # patched on startup
    )
    space_service.attach_profile_picture_repo(profile_picture_repo)
    space_service.attach_cover_repo(space_cover_repo)
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
    dm_service = DmService(
        conversation_repo,
        user_repo,
        bus,
        dm_routing_repo=repos.dm_routing,
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
    )
    realtime_service.wire()

    # ── Web Push ──────────────────────────────────────────────────────────
    vapid = load_or_create_vapid(config.data_dir)
    push_service = PushService(sub_repo=push_sub_repo, vapid=vapid)
    # Hook push fan-out into the notification service (§25.3 — title only).
    notification_service.attach_push_service(push_service)

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
        repos.storage_stats,
        quota_bytes=config.max_storage_bytes,
    )

    # ── Backup (HA-mode only) ─────────────────────────────────────────────
    # Backup service — adapter-agnostic. HA Supervisor calls pre/post
    # snapshot; standalone operators call via API or cron.
    backup_service = BackupService(db, config.media_path, schema_version=1)

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

    # ── Gallery service ──────────────────────────────────────────────────
    gallery_service = GalleryService(
        gallery_repo,
        space_repo,
        bus,
        config,
    )

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

    # ── Household feature toggles (§22) ─────────────────────────────────
    household_features_service = HouseholdFeaturesService(
        repos.household_features,
        bus=bus,
    )

    # Feature gating for §18: wire household toggle enforcement into
    # every service that owns a toggleable surface. Disabling
    # ``feat_tasks`` immediately makes POST /api/tasks return 403.
    feed_service.attach_household_features(household_features_service)
    task_service.attach_household_features(household_features_service)
    calendar_service.attach_household_features(household_features_service)
    feed_service.attach_storage_quota(storage_quota)

    # Schedule-poll → space calendar bridge (§9 / §23.53). Needs both
    # the space calendar service and the household-features toggle
    # service, so it wires in here (after both are built).
    schedule_calendar_bridge = ScheduleCalendarBridge(
        bus=bus,
        space_calendar_service=space_cal_service,
        household_features=household_features_service,
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
    )
    calendar_feed_bridge.wire()

    # Phase D: per-user space-event reminder scheduler. Polls fire_at
    # on a 30 s cadence and emits EventReminderDue events that the
    # notification service translates into push + in-app rows.
    # Started/stopped from app's on_startup / on_cleanup hooks below.
    space_calendar_reminder_scheduler = SpaceCalendarReminderScheduler(
        calendar_repo=space_cal_repo,
        bus=bus,
    )
    notification_service.attach_calendar_repo(space_cal_repo)

    # ── Per-user data export (§25.8.7) ──────────────────────────────────
    data_export_service = DataExportService(db)

    # ── DM relay routing (§12.5) ────────────────────────────────────────
    dm_routing_service = DmRoutingService(
        repos.dm_routing,
        federation_repo,
        child_protection_service=child_protection_service,
    )

    # ── Page conflict resolution (§4.4.4.1) ─────────────────────────────
    page_conflict_service = PageConflictService(page_repo)

    # ── Presence service (local + remote) ──────────────────────────────
    presence_service = PresenceService(repos.presence, bus)

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
    )

    # ── Platform adapter (HA vs standalone) ──────────────────────────────
    platform_adapter = build_platform_adapter(config.mode, db, config)

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
    # ``X-Ingress-User`` past a non-ingress reverse proxy could bypass
    # bearer auth. Gate on the capability and the strategy disappears
    # entirely outside HAOS.
    bearer_strategy = BearerTokenStrategy(user_repo)
    signed_media_strategy = SignedMediaStrategy()
    strategies: list = [signed_media_strategy]
    if Capability.INGRESS in platform_adapter.capabilities:
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
    pairing_relay_scheduler: PairingRelayRetentionScheduler | None = None
    dm_gc_scheduler: DmGcScheduler | None = None
    page_lock_scheduler: PageLockExpiryScheduler | None = None
    space_retention_scheduler: SpaceRetentionScheduler | None = None
    post_draft_scheduler: PostDraftCleanupScheduler | None = None
    calendar_reminder_scheduler: CalendarReminderScheduler | None = None
    task_deadline_scheduler: TaskDeadlineScheduler | None = None
    task_recurrence_scheduler: TaskRecurrenceScheduler | None = None

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
    app[K.idempotency_cache_key] = idempotency_cache
    app[K.reconnect_queue_key] = reconnect_queue
    app[K.gfs_connection_service_key] = gfs_connection_service
    app[K.gfs_connection_repo_key] = repos.gfs_connection
    app[K.public_space_discovery_key] = public_space_discovery
    app[K.peer_space_directory_repo_key] = repos.peer_space_directory
    app[K.gallery_service_key] = gallery_service
    app[K.gallery_repo_key] = gallery_repo
    app[K.child_protection_service_key] = child_protection_service
    app[K.typing_service_key] = typing_service
    app[K.household_features_service_key] = household_features_service
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
    app[K.profile_picture_repo_key] = profile_picture_repo
    app[K.space_cover_repo_key] = space_cover_repo
    app[K.post_repo_key] = post_repo
    app[K.space_repo_key] = space_repo
    app[K.notification_repo_key] = notification_repo
    app[K.conversation_repo_key] = conversation_repo
    app[K.outbox_repo_key] = outbox_repo
    app[K.federation_repo_key] = federation_repo
    app[K.page_repo_key] = page_repo
    app[K.page_conflict_service_key] = page_conflict_service
    app[K.presence_service_key] = presence_service
    app[K.space_zone_service_key] = space_zone_service
    app[K.space_zone_repo_key] = repos.space_zone
    app[K.poll_service_key] = poll_service
    app[K.space_poll_service_key] = space_poll_service
    app[K.bazaar_service_key] = bazaar_service
    app[K.corner_service_key] = corner_service
    app[K.sticky_repo_key] = sticky_repo
    app[K.bazaar_repo_key] = bazaar_repo
    app[K.shopping_repo_key] = shopping_repo

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
        identity_seed = identity.identity_seed
        identity_pk = identity.identity_public_key
        real_instance_id = identity.instance_id
        app[K.instance_id_key] = real_instance_id
        app[K.instance_signing_key_key] = identity_seed

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

        # 3. Replace UserService with one carrying the real public key.
        real_user_service = UserService(
            user_repo,
            bus,
            own_instance_public_key=identity_pk,
            profile_picture_repo=profile_picture_repo,
        )
        app[K.user_service_key] = real_user_service

        # 4. Replace SpaceService with one carrying the real instance_id.
        real_space_service = SpaceService(
            space_repo,
            space_post_repo,
            user_repo,
            bus,
            own_instance_id=real_instance_id,
        )
        # §CP.F1: hook child-protection age gate into add_member.
        real_space_service.attach_child_protection(child_protection_service)
        real_space_service.attach_profile_picture_repo(profile_picture_repo)
        real_space_service.attach_cover_repo(space_cover_repo)
        real_space_service.attach_gfs_connection_service(gfs_connection_service)
        real_space_service.attach_federation(
            federation_service=federation_service,
            federation_repo=federation_repo,
            remote_member_repo=repos.space_remote_member,
        )
        app[K.space_service_key] = real_space_service

        # 5a. SpaceContentEncryption — per-space epoch keys, KEK-protected.
        space_crypto = SpaceContentEncryption(space_key_repo, key_manager)
        app[K.space_crypto_service_key] = space_crypto

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
            user_repo=user_repo,
            profile_picture_repo=profile_picture_repo,
            page_repo=page_repo,
            sticky_repo=sticky_repo,
            space_task_repo=space_task_repo,
            space_calendar_repo=space_cal_repo,
            dm_contact_repo=dm_contact_repo,
            space_poll_repo=repos.space_poll,
            gallery_repo=repos.gallery,
            space_crypto=space_crypto,
            reconnect_queue=reconnect_queue,
            idempotency_cache=idempotency_cache,
            typing_service=typing_service,
            dm_service=dm_service,
            dm_routing_service=dm_routing_service,
            dm_routing_repo=repos.dm_routing,
            presence_service=presence_service,
            report_service=report_service,
            pairing_relay_repo=repos.pairing_relay,
            space_zone_repo=repos.space_zone,
            presence_repo=repos.presence,
            ws_manager=ws_manager,
        )
        federation_service = fed.federation_service
        sync_manager = fed.sync_manager
        # Wire RSVP propagation onto the calendar service. Done after
        # federation_service is built so the service can broadcast on
        # rsvp() / remove_rsvp() (§Phase A).
        space_cal_service.attach_federation(federation_service)
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

        fed_transport = FederationTransport(
            own_instance_id=real_instance_id,
            https_inbox=HttpsInboxTransport(
                client_factory=federation_service._get_http_client,
            ),
            signaling_send=_signaling_send,
            ice_servers=_default_ice_servers(config),
            inbound_handler=federation_service.handle_inbound_rtc,
        )
        federation_service.attach_transport(fed_transport)

        app[K.federation_service_key] = federation_service
        app[K.sync_session_manager_key] = sync_manager
        app[K.dm_routing_service_key] = dm_routing_service

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

        nonlocal gfs_ws_supervisor
        gfs_ws_supervisor = GfsWebSocketSupervisor(
            repo=repos.gfs_connection,
            instance_id=real_instance_id,
            signing_key=identity_seed,
            session_factory=lambda: http_session,
            on_relay=_on_gfs_relay,
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
        replay_cache_scheduler = ReplayCachePruneScheduler(federation_repo)
        await replay_cache_scheduler.start()

        # Pairing-relay retention (§11.9) — drops approved/declined
        # rows after a week and pending rows after a month so the
        # admin queue table stays bounded.
        nonlocal pairing_relay_scheduler
        pairing_relay_scheduler = PairingRelayRetentionScheduler(
            repos.pairing_relay,
        )
        await pairing_relay_scheduler.start()

        # DM GC (§23.47c) — hard-deletes conversations whose every
        # local member has soft-left and which have no remote members.
        nonlocal dm_gc_scheduler
        dm_gc_scheduler = DmGcScheduler(conversation_repo)
        await dm_gc_scheduler.start()

        # Bazaar auction expiry — closes due auctions on a 60-s cadence.
        await bazaar_expiry_scheduler.start()

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
        if replay_cache_scheduler is not None:
            await replay_cache_scheduler.stop()
        if pairing_relay_scheduler is not None:
            await pairing_relay_scheduler.stop()
        if dm_gc_scheduler is not None:
            await dm_gc_scheduler.stop()
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
        sync_sched = app.get(K.space_sync_scheduler_key)
        if sync_sched is not None:
            await sync_sched.stop()
        await bazaar_expiry_scheduler.stop()
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
