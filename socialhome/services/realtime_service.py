"""RealtimeService — bridges domain events to WebSocket clients.

Subscribes to the in-process :class:`EventBus`, translates each event
into a ``{type, data}`` JSON frame, and pushes via
:class:`WebSocketManager` to the affected users.

The translation is intentionally minimal — the frontend does its own
state hydration from the REST API on receipt. WS frames are signals,
not full state replication. This keeps payloads small and avoids
encoding subtle data shapes in two places.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from ..domain.events import (
    BazaarBidPlaced,
    BazaarBidWithdrawn,
    BazaarListingCancelled,
    BazaarListingCreated,
    BazaarListingExpired,
    BazaarListingUpdated,
    BazaarOfferAccepted,
    BazaarOfferRejected,
    CalendarEventCreated,
    ConnectionReachable,
    ConnectionUnreachable,
    GalleryAlbumCreated,
    GalleryAlbumDeleted,
    GalleryAlbumUpdated,
    GalleryItemDeleted,
    GalleryItemUploaded,
    CalendarEventDeleted,
    CalendarEventUpdated,
    AutoPairRequestIncoming,
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    PairingAborted,
    PairingAcceptReceived,
    PairingConfirmed,
    PairingIntroReceived,
    PeerTransportChanged,
    PeerUnpaired,
    SpaceMemberProfileUpdated,
    UserCameOnline,
    UserProfileUpdated,
    UserResumedActive,
    UserWentIdle,
    UserWentOffline,
    CpBlockAdded,
    CpBlockRemoved,
    CpGuardianAdded,
    CpGuardianRemoved,
    CpProtectionDisabled,
    CpProtectionEnabled,
    CpSpaceAgeGateChanged,
    DmConversationCreated,
    DmGroupRosterChanged,
    DmMessageCreated,
    DmMessageReactionChanged,
    DmMessageUpdated,
    HouseholdConfigChanged,
    LocalHomeLocationUpdated,
    NotificationCreated,
    NotificationReadChanged,
    PageConflictEmitted,
    PageProposalSettled,
    PageEditLockAcquired,
    PageEditLockReleased,
    PeerHomeChanged,
    PostCreated,
    PostDeleted,
    PostEdited,
    PostReactionChanged,
    StickyCreated,
    StickyDeleted,
    StickyUpdated,
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
    LocalSpaceInviteCreated,
    MediaTranscodeFailed,
    MediaTranscodeReady,
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
    PresenceUpdated,
    RemoteSpaceDissolved,
    ShoppingItemAdded,
    ShoppingItemRemoved,
    ShoppingItemsCleared,
    ShoppingItemToggled,
    ShoppingItemUpdated,
    ShoppingStoreDeleted,
    ShoppingStoreRenamed,
    ShoppingStoresReordered,
    SpaceConfigChanged,
    SpaceProposalUpdated,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceModerationApproved,
    SpaceModerationExpired,
    SpaceModerationQueued,
    SpaceModerationRejected,
    SpacePostCreated,
    SpacePostModerated,
    SpaceZoneDeleted,
    SpaceZoneUpserted,
    PollClosed,
    PollCreated,
    PollVoted,
    SchedulePollFinalized,
    SchedulePollResponded,
    TaskAssigned,
    TaskCompleted,
    TaskCreated,
    TaskDeadlineDue,
    TaskDeleted,
    TaskListCreated,
    TaskListDeleted,
    TaskListUpdated,
    TaskUpdated,
    TimetableDeleted,
    TimetableSaved,
    UserPreferencesChanged,
    UserStatusChanged,
)
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    SETTINGS_AUTHORITY_ROLES,
    SpaceModerationItem,
)
from ..domain.timetable import to_wire_dict as timetable_to_wire_dict
from ..infrastructure.event_bus import EventBus
from ..infrastructure.ws_manager import WebSocketManager
from ..media_signer import MediaUrlSigner, sign_media_urls_in
from .inbound_media_store import verbatim_local_media_ref
from .space_bot_service import (
    SpaceBotCreated,
    SpaceBotDeleted,
    SpaceBotTokenRotated,
    SpaceBotUpdated,
)

if TYPE_CHECKING:
    from ..repositories.cp_repo import AbstractCpRepo
    from ..repositories.media_transcode_repo import AbstractMediaTranscodeRepo

log = logging.getLogger(__name__)

#: Space roles that may act on join requests (settings authority).
_SPACE_ADMIN_ROLES = SETTINGS_AUTHORITY_ROLES

#: Space roles that may read the moderation queue (content authority, v_41).
_SPACE_MODERATOR_ROLES = CONTENT_AUTHORITY_ROLES


class RealtimeService:
    """Push domain events to connected WebSocket clients.

    Parameters
    ----------
    bus:
        The shared in-process event bus.
    ws:
        The WebSocketManager that owns user → connection state.
    user_repo:
        Used to fan out post events to all active household members.
    space_repo:
        Used to enumerate space members for SpacePostCreated events.
    """

    __slots__ = (
        "_bus",
        "_ws",
        "_user_repo",
        "_space_repo",
        "_conversation_repo",
        "_media_signer",
        "_media_transcode_repo",
        "_cp_repo",
    )

    def __init__(
        self,
        bus: EventBus,
        ws: WebSocketManager,
        *,
        user_repo,
        space_repo,
        conversation_repo=None,
        media_signer: MediaUrlSigner | None = None,
        media_transcode_repo: "AbstractMediaTranscodeRepo | None" = None,
        cp_repo: "AbstractCpRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._ws = ws
        self._user_repo = user_repo
        self._space_repo = space_repo
        # Needed by ``broadcast_dm_media_ready`` to enumerate the
        # local participants of a conversation. Optional for older
        # callers / test stacks that don't exercise the media path.
        self._conversation_repo = conversation_repo
        # Lets WS broadcast frames carry the same signed ``media_url`` /
        # ``picture_url`` / ``cover_url`` shape the REST API returns, so
        # browsers can drop the fields straight into ``<img src>``
        # without a follow-up REST hydrate. Optional only because
        # ``__init__`` runs before the signer is constructed in
        # ``_on_startup``; ``attach_media_signer`` wires it in once
        # available.
        self._media_signer = media_signer
        # Lets ``*.created`` / ``*.edited`` frames stamp ``media_status`` on a
        # freshly-posted video so the SPA renders the 'Processing…' placeholder
        # until the ``media.ready`` frame swaps in the player — matching the
        # ``media_status`` field the REST list endpoints already serve.
        # Optional for back-compat callers / test stacks that don't exercise
        # the video path.
        self._media_transcode_repo = media_transcode_repo
        # §CP: child-protection frames name a protected account, so they go
        # to admins, the account itself and its guardians only — never the
        # whole household. Optional for test stacks without CP.
        self._cp_repo = cp_repo

    def attach_media_signer(self, signer: MediaUrlSigner) -> None:
        """Late binding — signer is built after RealtimeService.__init__."""
        self._media_signer = signer

    # ─── Wiring ───────────────────────────────────────────────────────────

    def wire(self) -> None:
        """Subscribe handlers on the bus.  Idempotent."""
        self._bus.subscribe(PostCreated, self._on_post_created)
        self._bus.subscribe(PostEdited, self._on_post_edited)
        self._bus.subscribe(PostDeleted, self._on_post_deleted)
        self._bus.subscribe(PostReactionChanged, self._on_post_reaction)
        self._bus.subscribe(CommentAdded, self._on_comment_added)
        self._bus.subscribe(CommentUpdated, self._on_comment_updated)
        self._bus.subscribe(CommentDeleted, self._on_comment_deleted)
        self._bus.subscribe(
            UserProfileUpdated,
            self._on_user_profile_updated,
        )
        self._bus.subscribe(
            SpaceMemberProfileUpdated,
            self._on_space_member_profile_updated,
        )
        self._bus.subscribe(
            PairingAcceptReceived,
            self._on_pairing_accept_received,
        )
        self._bus.subscribe(
            PairingConfirmed,
            self._on_pairing_confirmed,
        )
        self._bus.subscribe(
            PairingAborted,
            self._on_pairing_aborted,
        )
        self._bus.subscribe(
            PeerTransportChanged,
            self._on_peer_transport_changed,
        )
        self._bus.subscribe(
            PairingIntroReceived,
            self._on_pairing_intro_received,
        )
        self._bus.subscribe(
            AutoPairRequestIncoming,
            self._on_auto_pair_requested,
        )
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)
        self._bus.subscribe(SpaceMemberJoined, self._on_space_member_joined)
        # Bot persona lifecycle — frontend uses these to refresh the
        # "Bots" tab in space settings and to invalidate cached bot data
        # for feed posts when a bot is renamed/deleted.
        self._bus.subscribe(SpaceBotCreated, self._on_space_bot_created)
        self._bus.subscribe(SpaceBotUpdated, self._on_space_bot_updated)
        self._bus.subscribe(SpaceBotDeleted, self._on_space_bot_deleted)
        self._bus.subscribe(SpaceBotTokenRotated, self._on_space_bot_token_rotated)
        self._bus.subscribe(SpaceMemberLeft, self._on_space_member_left)
        self._bus.subscribe(SpaceJoinRequested, self._on_space_join_requested)
        self._bus.subscribe(
            LocalSpaceInviteCreated,
            self._on_local_space_invite_created,
        )
        self._bus.subscribe(SpaceJoinApproved, self._on_space_join_approved)
        self._bus.subscribe(SpaceJoinDenied, self._on_space_join_denied)
        self._bus.subscribe(SpacePostModerated, self._on_space_post_moderated)
        self._bus.subscribe(SpaceModerationQueued, self._on_space_mod_queued)
        self._bus.subscribe(SpaceModerationApproved, self._on_space_mod_approved)
        self._bus.subscribe(SpaceModerationRejected, self._on_space_mod_rejected)
        self._bus.subscribe(SpaceModerationExpired, self._on_space_mod_expired)
        self._bus.subscribe(SpaceConfigChanged, self._on_space_config_changed)
        self._bus.subscribe(SpaceProposalUpdated, self._on_space_proposal_updated)
        self._bus.subscribe(RemoteSpaceDissolved, self._on_remote_space_dissolved)
        self._bus.subscribe(SpaceZoneUpserted, self._on_space_zone_upserted)
        self._bus.subscribe(SpaceZoneDeleted, self._on_space_zone_deleted)
        self._bus.subscribe(TaskAssigned, self._on_task_assigned)
        self._bus.subscribe(TaskCompleted, self._on_task_completed)
        self._bus.subscribe(TaskDeadlineDue, self._on_task_deadline)
        self._bus.subscribe(TaskCreated, self._on_task_created)
        self._bus.subscribe(TaskUpdated, self._on_task_updated)
        self._bus.subscribe(TaskDeleted, self._on_task_deleted)
        self._bus.subscribe(TaskListCreated, self._on_task_list_created)
        self._bus.subscribe(TaskListUpdated, self._on_task_list_updated)
        self._bus.subscribe(TaskListDeleted, self._on_task_list_deleted)
        self._bus.subscribe(TimetableSaved, self._on_timetable_saved)
        self._bus.subscribe(TimetableDeleted, self._on_timetable_deleted)
        self._bus.subscribe(
            SchedulePollResponded,
            self._on_schedule_responded,
        )
        self._bus.subscribe(
            SchedulePollFinalized,
            self._on_schedule_finalized,
        )
        self._bus.subscribe(PollCreated, self._on_poll_created)
        self._bus.subscribe(PollVoted, self._on_poll_voted)
        self._bus.subscribe(PollClosed, self._on_poll_closed)
        self._bus.subscribe(CalendarEventCreated, self._on_calendar_created)
        self._bus.subscribe(CalendarEventUpdated, self._on_calendar_updated)
        self._bus.subscribe(CalendarEventDeleted, self._on_calendar_deleted)
        self._bus.subscribe(ConnectionReachable, self._on_connection_reachable)
        self._bus.subscribe(ConnectionUnreachable, self._on_connection_unreachable)
        self._bus.subscribe(PeerUnpaired, self._on_peer_unpaired)
        self._bus.subscribe(GalleryAlbumCreated, self._on_gallery_album_created)
        self._bus.subscribe(GalleryAlbumUpdated, self._on_gallery_album_updated)
        self._bus.subscribe(GalleryAlbumDeleted, self._on_gallery_album_deleted)
        self._bus.subscribe(GalleryItemUploaded, self._on_gallery_item_uploaded)
        self._bus.subscribe(GalleryItemDeleted, self._on_gallery_item_deleted)
        self._bus.subscribe(UserStatusChanged, self._on_user_status)
        self._bus.subscribe(PresenceUpdated, self._on_presence_updated)
        # Online status (session presence) — orthogonal to physical
        # ``presence.updated``. Drives the green/amber dot on avatars.
        self._bus.subscribe(UserCameOnline, self._on_user_came_online)
        self._bus.subscribe(UserResumedActive, self._on_user_resumed_active)
        self._bus.subscribe(UserWentIdle, self._on_user_went_idle)
        self._bus.subscribe(UserWentOffline, self._on_user_went_offline)
        self._bus.subscribe(ShoppingItemAdded, self._on_shopping_added)
        self._bus.subscribe(ShoppingItemToggled, self._on_shopping_toggled)
        self._bus.subscribe(ShoppingItemUpdated, self._on_shopping_updated)
        self._bus.subscribe(ShoppingItemRemoved, self._on_shopping_removed)
        self._bus.subscribe(ShoppingItemsCleared, self._on_shopping_cleared)
        self._bus.subscribe(
            ShoppingStoresReordered,
            self._on_shopping_stores_reordered,
        )
        self._bus.subscribe(
            ShoppingStoreRenamed,
            self._on_shopping_store_renamed,
        )
        self._bus.subscribe(
            ShoppingStoreDeleted,
            self._on_shopping_store_deleted,
        )
        self._bus.subscribe(NotificationCreated, self._on_notification_new)
        self._bus.subscribe(
            NotificationReadChanged,
            self._on_notification_read_changed,
        )
        self._bus.subscribe(BazaarBidPlaced, self._on_bazaar_bid_placed)
        self._bus.subscribe(
            BazaarListingExpired,
            self._on_bazaar_listing_closed,
        )
        self._bus.subscribe(
            BazaarListingCreated,
            self._on_bazaar_listing_created,
        )
        self._bus.subscribe(
            BazaarListingUpdated,
            self._on_bazaar_listing_updated,
        )
        self._bus.subscribe(
            BazaarListingCancelled,
            self._on_bazaar_listing_cancelled,
        )
        self._bus.subscribe(
            BazaarOfferAccepted,
            self._on_bazaar_offer_accepted,
        )
        self._bus.subscribe(
            BazaarOfferRejected,
            self._on_bazaar_offer_rejected,
        )
        self._bus.subscribe(
            BazaarBidWithdrawn,
            self._on_bazaar_bid_withdrawn,
        )
        self._bus.subscribe(DmMessageCreated, self._on_dm_message_created)
        self._bus.subscribe(DmMessageUpdated, self._on_dm_message_updated)
        self._bus.subscribe(
            DmMessageReactionChanged,
            self._on_dm_message_reaction_changed,
        )
        self._bus.subscribe(
            DmConversationCreated,
            self._on_dm_conversation_created,
        )
        self._bus.subscribe(DmGroupRosterChanged, self._on_dm_group_roster_changed)
        self._bus.subscribe(
            HouseholdConfigChanged,
            self._on_household_config_changed,
        )
        self._bus.subscribe(CpProtectionEnabled, self._on_cp_protection_enabled)
        self._bus.subscribe(CpProtectionDisabled, self._on_cp_protection_disabled)
        self._bus.subscribe(CpGuardianAdded, self._on_cp_guardian_added)
        self._bus.subscribe(CpGuardianRemoved, self._on_cp_guardian_removed)
        self._bus.subscribe(CpBlockAdded, self._on_cp_block_added)
        self._bus.subscribe(CpBlockRemoved, self._on_cp_block_removed)
        self._bus.subscribe(PageEditLockAcquired, self._on_page_lock_acquired)
        self._bus.subscribe(PageEditLockReleased, self._on_page_lock_released)
        self._bus.subscribe(PageConflictEmitted, self._on_page_conflict)
        self._bus.subscribe(PageProposalSettled, self._on_page_sequenced)
        self._bus.subscribe(StickyCreated, self._on_sticky_created)
        self._bus.subscribe(StickyUpdated, self._on_sticky_updated)
        self._bus.subscribe(StickyDeleted, self._on_sticky_deleted)
        self._bus.subscribe(CpSpaceAgeGateChanged, self._on_cp_age_gate_changed)
        # Highlights — both local-write and inbound-federation paths publish
        # these on the bus, so the same handler covers "I posted a frame"
        # and "a paired peer's frame just landed". The SPA fetches
        # ``/api/highlights`` on receipt to pull the audience-filtered list,
        # so the WS frames stay narrow.
        self._bus.subscribe(HighlightFrameAdded, self._on_highlight_frame_added)
        self._bus.subscribe(HighlightFrameRemoved, self._on_highlight_frame_removed)
        self._bus.subscribe(HighlightRemoved, self._on_highlight_removed)
        # Highlight view / reaction back-channel — only the author needs
        # the update so we narrow the broadcast to their WS sessions.
        self._bus.subscribe(HighlightFrameViewed, self._on_highlight_frame_viewed)
        self._bus.subscribe(
            HighlightFrameReactionChanged,
            self._on_highlight_frame_reaction_changed,
        )
        # Momentum — broadcast new moments / deletions to the whole
        # household; reaction-changed unicasts to the author.
        self._bus.subscribe(MomentCreated, self._on_moment_created)
        self._bus.subscribe(MomentDeleted, self._on_moment_deleted)
        self._bus.subscribe(
            MomentReactionChanged,
            self._on_moment_reaction_changed,
        )
        # Federation map — home location signals (§federation-map)
        self._bus.subscribe(
            LocalHomeLocationUpdated,
            self._on_local_home_location_updated,
        )
        self._bus.subscribe(
            PeerHomeChanged,
            self._on_peer_home_changed,
        )
        # User preferences — unicast to the owner only (not household-wide)
        self._bus.subscribe(
            UserPreferencesChanged,
            self._on_user_preferences_changed,
        )
        # Background video transcode finished — unicast to the uploader
        self._bus.subscribe(
            MediaTranscodeReady,
            self._on_media_transcode_ready,
        )
        # Background video transcode permanently failed — unicast to the
        # uploader so the placeholder flips to the failed state at once.
        self._bus.subscribe(
            MediaTranscodeFailed,
            self._on_media_transcode_failed,
        )

    # ─── Async video transcode readiness ─────────────────────────────────

    async def _annotate_media_status(self, item: dict | None) -> None:
        """If ``item`` is a video with a media_url, stamp ``media_status`` from
        the transcode queue so a freshly-posted (still-transcoding) video
        renders the 'Processing…' placeholder until the ``media.ready`` frame
        swaps it. Mutates in place.

        Mirrors the REST list endpoints (``routes/feed.py`` etc.): a local
        just-enqueued video has a ``media_transcode_jobs`` row →
        ``'processing'``; a federated video with no transcode job is absent
        from ``status_for`` → ``'ready'`` (so federated videos aren't wrongly
        stuck processing). Fail-soft — never let annotation break the
        broadcast.
        """
        if not item or self._media_transcode_repo is None:
            return
        if (
            item.get("type") != "video"
            and item.get("media_type") != "video"
            and item.get("item_type") != "video"
        ):
            return
        media_url = item.get("media_url") or item.get("url")
        fn = _media_filename(media_url)
        if not fn:
            return
        try:
            statuses = await self._media_transcode_repo.status_for([fn])
        except Exception:  # noqa: BLE001 — annotation must not break the frame
            log.warning("media_status annotation failed for %s", fn, exc_info=True)
            return
        item["media_status"] = statuses.get(fn, "ready")
        # Server-derived poster — the ``.webp`` sibling of the ``.webm``
        # ``media_url`` (shared UUID stem). Set the *unsigned* path so
        # the broadcast helper's ``sign_media_urls_in`` pass signs it
        # alongside ``media_url`` (``media_thumbnail_url`` is signable).
        poster = _video_poster_path(media_url)
        if poster is not None:
            item["media_thumbnail_url"] = poster

    # ─── Household feed events ────────────────────────────────────────────

    async def _on_post_created(self, event: PostCreated) -> None:
        post = _safe(event.post)
        await self._annotate_media_status(post)
        await self._broadcast_household(
            {
                "type": "post.created",
                "post": post,
            }
        )

    async def _on_post_edited(self, event: PostEdited) -> None:
        post = _safe(event.post)
        await self._annotate_media_status(post)
        await self._broadcast_household(
            {
                "type": "post.edited",
                "post": post,
            }
        )

    async def _on_post_deleted(self, event: PostDeleted) -> None:
        await self._broadcast_household(
            {
                "type": "post.deleted",
                "post_id": event.post_id,
            }
        )

    async def _on_post_reaction(self, event: PostReactionChanged) -> None:
        # Household-feed reactions only: a space reaction (published for the
        # v_49 member relay) must not reach non-members' sockets.
        if event.space_id:
            return
        await self._broadcast_household(
            {
                "type": "post.reaction_changed",
                "post": _safe(event.post),
            }
        )

    async def _on_comment_added(self, event: CommentAdded) -> None:
        frame = {
            "type": "comment.added",
            "post_id": event.post_id,
            "space_id": event.space_id,
            "comment": _safe(event.comment),
        }
        if event.space_id:
            await self._broadcast_space(event.space_id, frame)
        else:
            await self._broadcast_household(frame)

    async def _on_comment_updated(self, event: CommentUpdated) -> None:
        frame = {
            "type": "comment.updated",
            "post_id": event.post_id,
            "space_id": event.space_id,
            "comment": _safe(event.comment),
        }
        if event.space_id:
            await self._broadcast_space(event.space_id, frame)
        else:
            await self._broadcast_household(frame)

    async def _on_comment_deleted(self, event: CommentDeleted) -> None:
        frame = {
            "type": "comment.deleted",
            "post_id": event.post_id,
            "space_id": event.space_id,
            "comment_id": event.comment_id,
        }
        if event.space_id:
            await self._broadcast_space(event.space_id, frame)
        else:
            await self._broadcast_household(frame)

    async def _on_user_profile_updated(
        self,
        event: UserProfileUpdated,
    ) -> None:
        """Household-wide broadcast (bytes excluded — clients render
        ``picture_url`` directly so they don't have to know the
        signing scheme)."""
        picture_url = (
            f"api/users/{event.user_id}/picture?v={event.picture_hash}"
            if event.picture_hash
            else None
        )
        # ``_broadcast_household`` signs ``picture_url`` automatically via
        # ``sign_media_urls_in()``, so the SPA can drop the value into
        # ``<img src>`` directly.
        await self._broadcast_household(
            {
                "type": "user.profile_updated",
                "user_id": event.user_id,
                "username": event.username,
                "display_name": event.display_name,
                "bio": event.bio,
                "picture_hash": event.picture_hash,
                "picture_url": picture_url,
            }
        )

    async def _on_space_member_profile_updated(
        self,
        event: SpaceMemberProfileUpdated,
    ) -> None:
        picture_url = (
            f"/api/spaces/{event.space_id}/members/{event.user_id}"
            f"/picture?v={event.picture_hash}"
            if event.picture_hash
            else None
        )
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.member.profile_updated",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "space_display_name": event.space_display_name,
                "picture_hash": event.picture_hash,
                "picture_url": picture_url,
            },
        )

    async def _on_pairing_accept_received(
        self,
        event: PairingAcceptReceived,
    ) -> None:
        """Peer accepted our QR invite — forward SAS to the waiting UI."""
        await self._broadcast_household(
            {
                "type": "pairing.accept_received",
                "from_instance": event.from_instance,
                "token": event.token,
                "verification_code": event.verification_code,
            }
        )

    async def _on_pairing_confirmed(
        self,
        event: PairingConfirmed,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "pairing.confirmed",
                "instance_id": event.instance_id,
            }
        )

    async def _on_pairing_aborted(
        self,
        event: PairingAborted,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "pairing.aborted",
                "instance_id": event.instance_id,
                "reason": event.reason,
            }
        )

    async def _on_peer_transport_changed(
        self,
        event: PeerTransportChanged,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "peer.transport_changed",
                "instance_id": event.instance_id,
                "transport": event.transport,
            }
        )

    # ─── Federation map — home location (§federation-map) ────────────────

    async def _on_local_home_location_updated(
        self,
        event: LocalHomeLocationUpdated,
    ) -> None:
        """Broadcast our own home coords to every WS client."""
        await self._broadcast_household(
            {
                "type": "local.home_changed",
                "latitude": event.latitude,
                "longitude": event.longitude,
            }
        )

    async def _on_peer_home_changed(self, event: PeerHomeChanged) -> None:
        """Broadcast a peer's new home coords to every WS client."""
        await self._broadcast_household(
            {
                "type": "peer.home_changed",
                "instance_id": event.instance_id,
                "latitude": event.latitude,
                "longitude": event.longitude,
            }
        )

    # ─── User preferences (§preferences) ─────────────────────────────────

    async def _on_user_preferences_changed(
        self,
        event: UserPreferencesChanged,
    ) -> None:
        """Broadcast user.preferences_changed to the owner user only."""
        await self._ws.broadcast_to_users(
            [event.user_id],
            {
                "type": "user.preferences_changed",
                "user_id": event.user_id,
                "changed": dict(event.changed),
            },
        )

    async def _on_pairing_intro_received(
        self,
        event: PairingIntroReceived,
    ) -> None:
        await self._broadcast_household_admins(
            {
                "type": "pairing.intro_received",
                "from_instance": event.from_instance,
                "via_instance_id": event.via_instance_id,
                "message": event.message,
            }
        )

    async def _on_auto_pair_requested(
        self,
        event: AutoPairRequestIncoming,
    ) -> None:
        await self._broadcast_household_admins(
            {
                "type": "pairing.auto_pair_requested",
                "request_id": event.request_id,
                "from_a_id": event.from_a_id,
                "from_a_display": event.from_a_display,
                "via_b_id": event.via_b_id,
                "via_b_display": event.via_b_display,
            }
        )

    # ─── Space events ─────────────────────────────────────────────────────

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        post = _safe(event.post)
        await self._annotate_media_status(post)
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.post.created",
                "space_id": event.space_id,
                "post": post,
            },
        )

    async def _on_space_post_moderated(self, event: SpacePostModerated) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.post.moderated",
                "space_id": event.space_id,
                "post": _safe(event.post),
                "moderated_by": event.moderated_by,
            },
        )

    # ``space.moderation.*`` frames carry the full queued item (the
    # pending body, the rejection reason). Only the people who can read
    # the queue (``GET …/moderation`` is owner / admin / moderator) get them;
    # the submitter additionally learns the outcome of their own item.

    async def _on_space_mod_queued(self, event: SpaceModerationQueued) -> None:
        item = event.item
        await self._broadcast_space_moderators(
            item.space_id,
            {
                "type": "space.moderation.queued",
                "space_id": item.space_id,
                "item": _safe(item),
            },
        )
        await self._notify_submitter(item)

    async def _notify_submitter(self, item: SpaceModerationItem) -> None:
        """``space.moderation.mine`` — only to the submitter while they are
        still a member, only the receipt (no content): their pending strip
        refetches ``…/mine``."""
        members = await self._space_repo.list_members(item.space_id)
        if not any(m.user_id == item.submitted_by for m in members):
            return
        await self._ws.broadcast_to_users(
            [item.submitted_by],
            {
                "type": "space.moderation.mine",
                "space_id": item.space_id,
                "item_id": item.id,
                "feature": item.feature,
                "action": item.action,
                "status": item.status.value,
            },
        )

    async def _on_space_mod_approved(self, event: SpaceModerationApproved) -> None:
        item = event.item
        await self._broadcast_space_moderators(
            item.space_id,
            {
                "type": "space.moderation.approved",
                "space_id": item.space_id,
                "item": _safe(item),
            },
            also=item.submitted_by,
        )
        await self._notify_submitter(item)

    async def _on_space_mod_rejected(self, event: SpaceModerationRejected) -> None:
        item = event.item
        await self._broadcast_space_moderators(
            item.space_id,
            {
                "type": "space.moderation.rejected",
                "space_id": item.space_id,
                "item": _safe(item),
            },
            also=item.submitted_by,
        )
        await self._notify_submitter(item)

    async def _on_space_mod_expired(self, event: SpaceModerationExpired) -> None:
        item = event.item
        await self._broadcast_space_moderators(
            item.space_id,
            {
                "type": "space.moderation.expired",
                "space_id": item.space_id,
                "item": _safe(item),
            },
            also=item.submitted_by,
        )
        await self._notify_submitter(item)

    async def _on_space_config_changed(self, event: SpaceConfigChanged) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.config.changed",
                "space_id": event.space_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
            },
        )

    async def _on_space_proposal_updated(self, event: SpaceProposalUpdated) -> None:
        """Realtime fan-out for a critical-action approval proposal (v_16):
        opened / voted / resolved. The SPA renders the pending banner +
        live tally and updates Approve/Reject state from this frame."""
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.proposal.updated",
                "space_id": event.space_id,
                "proposal_id": event.proposal_id,
                "proposal": event.view,
            },
        )

    async def _on_remote_space_dissolved(self, event: RemoteSpaceDissolved) -> None:
        """A host dissolved a space we're a member of — tell connected tabs
        to drop it, using the same ``dissolved`` frame the host path emits
        so the client handles both uniformly. Fanned out *before* the
        inbound handler purges the membership rows this resolves against.
        """
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.config.changed",
                "space_id": event.space_id,
                "event_type": "dissolved",
            },
        )

    # ─── Space zones (§23.8.7) ───────────────────────────────────────────

    async def _on_space_zone_upserted(self, event: SpaceZoneUpserted) -> None:
        """Local fan-out for an admin's zone CRUD. Federation to remote
        member instances is owned by :class:`SpaceZoneOutbound`; this
        handler is just the WS frame for clients on this instance.
        """
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space_zone_changed",
                "data": {
                    "space_id": event.space_id,
                    "action": "upsert",
                    "zone_id": event.zone_id,
                    "zone": {
                        "id": event.zone_id,
                        "space_id": event.space_id,
                        "name": event.name,
                        "latitude": event.latitude,
                        "longitude": event.longitude,
                        "radius_m": event.radius_m,
                        "color": event.color,
                        "created_by": event.created_by,
                        "updated_at": event.updated_at,
                    },
                },
            },
        )

    async def _on_space_zone_deleted(self, event: SpaceZoneDeleted) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space_zone_changed",
                "data": {
                    "space_id": event.space_id,
                    "action": "delete",
                    "zone_id": event.zone_id,
                    "zone": None,
                },
            },
        )

    # ─── Space membership (§23.52) ───────────────────────────────────────

    async def _on_space_member_joined(self, event: SpaceMemberJoined) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.member.joined",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "role": event.role,
            },
        )

    async def _on_space_member_left(self, event: SpaceMemberLeft) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.member.left",
                "space_id": event.space_id,
                "user_id": event.user_id,
            },
        )

    # ── Bot persona lifecycle ───────────────────────────────────────────

    async def _on_space_bot_created(self, event: SpaceBotCreated) -> None:
        # token_hash is on the SpaceBot dataclass; _safe uses the
        # security.sanitise_for_api filter so it's stripped on the wire.
        await self._broadcast_space(
            event.bot.space_id,
            {
                "type": "space.bot.created",
                "space_id": event.bot.space_id,
                "bot": _safe(event.bot),
            },
        )

    async def _on_space_bot_updated(self, event: SpaceBotUpdated) -> None:
        await self._broadcast_space(
            event.bot.space_id,
            {
                "type": "space.bot.updated",
                "space_id": event.bot.space_id,
                "bot": _safe(event.bot),
            },
        )

    async def _on_space_bot_deleted(self, event: SpaceBotDeleted) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.bot.deleted",
                "space_id": event.space_id,
                "bot_id": event.bot_id,
            },
        )

    async def _on_space_bot_token_rotated(self, event: SpaceBotTokenRotated) -> None:
        # No token in the payload — just a nudge for the HA integration to
        # re-auth (it holds the old token and will start getting 401s).
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.bot.token_rotated",
                "space_id": event.space_id,
                "bot_id": event.bot_id,
            },
        )

    async def _on_space_join_requested(
        self,
        event: SpaceJoinRequested,
    ) -> None:
        # Only notify admins + owner so the notification isn't a leak
        # about a space the requester isn't in yet.
        await self._ws.broadcast_to_users(
            await self._space_admin_ids(event.space_id),
            {
                "type": "space.join.requested",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "request_id": event.request_id,
            },
        )

    async def _on_space_join_approved(
        self,
        event: SpaceJoinApproved,
    ) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.join.approved",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "request_id": event.request_id,
            },
        )

    async def _on_space_join_denied(self, event: SpaceJoinDenied) -> None:
        # Only tell the requester + the admins.
        await self._ws.broadcast_to_users(
            await self._space_admin_ids(event.space_id) + [event.user_id],
            {
                "type": "space.join.denied",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "request_id": event.request_id,
            },
        )

    async def _on_local_space_invite_created(
        self,
        event: LocalSpaceInviteCreated,
    ) -> None:
        """Push the invite to the recipient's session so the inbox
        banner appears immediately. The SPA already polls
        ``/api/local_invites`` on mount; this WS frame just collapses
        the latency from "user opens the SPA" to "user sees the
        prompt the instant the admin clicks Invite"."""
        await self._ws.broadcast_to_user(
            event.invited_user_id,
            {
                "type": "space.local_invite_received",
                "space_id": event.space_id,
                "invitation_id": event.invitation_id,
                "invited_by": event.invited_by,
            },
        )

    # ─── Tasks ────────────────────────────────────────────────────────────

    async def _on_task_assigned(self, event: TaskAssigned) -> None:
        # Defence in depth: a space task's frame carries its title +
        # description, so it never reaches a non-member.
        if event.space_id is not None and event.assigned_to not in (
            await self._space_repo.list_local_member_user_ids(event.space_id)
        ):
            return
        await self._ws.broadcast_to_user(
            event.assigned_to,
            {
                "type": "task.assigned",
                "task": _safe(event.task),
            },
        )

    async def _on_task_completed(self, event: TaskCompleted) -> None:
        payload = {
            "type": "task.completed",
            "task_id": event.task.id,
            "completed_by": event.completed_by,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_deadline(self, event: TaskDeadlineDue) -> None:
        for assignee in event.task.assignees or ():
            await self._ws.broadcast_to_user(
                assignee,
                {
                    "type": "task.deadline_due",
                    "task_id": event.task.id,
                    "due_date": event.due_date.isoformat(),
                },
            )

    async def _on_task_created(self, event: TaskCreated) -> None:
        payload = {
            "type": "task.created",
            "space_id": event.space_id,
            "task": _safe(event.task),
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_updated(self, event: TaskUpdated) -> None:
        payload = {
            "type": "task.updated",
            "space_id": event.space_id,
            "task": _safe(event.task),
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_deleted(self, event: TaskDeleted) -> None:
        payload = {
            "type": "task.deleted",
            "task_id": event.task_id,
            "list_id": event.list_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_timetable_saved(self, event: TimetableSaved) -> None:
        payload = {
            "type": "timetable.changed",
            "space_id": event.space_id,
            "timetable": timetable_to_wire_dict(event.timetable),
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_timetable_deleted(self, event: TimetableDeleted) -> None:
        payload = {
            "type": "timetable.deleted",
            "timetable_id": event.timetable_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_list_created(self, event: TaskListCreated) -> None:
        payload = {
            "type": "task_list.created",
            "list_id": event.list_id,
            "name": event.name,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_list_updated(self, event: TaskListUpdated) -> None:
        payload = {
            "type": "task_list.updated",
            "list_id": event.list_id,
            "name": event.name,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_list_deleted(self, event: TaskListDeleted) -> None:
        payload = {
            "type": "task_list.deleted",
            "list_id": event.list_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    # ─── Schedule polls (§9 / §23.53) ────────────────────────────────────

    async def _on_schedule_responded(
        self,
        event: SchedulePollResponded,
    ) -> None:
        payload = {
            "type": "schedule_poll.responded",
            "post_id": event.post_id,
            "slot_id": event.slot_id,
            "user_id": event.user_id,
            "response": event.response,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_poll_created(self, event: PollCreated) -> None:
        payload = {
            "type": "poll.created",
            "post_id": event.post_id,
            "question": event.question,
            "allow_multiple": event.allow_multiple,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_poll_voted(self, event: PollVoted) -> None:
        payload = {
            "type": "poll.voted",
            "post_id": event.post_id,
            "voter_user_id": event.voter_user_id,
            "option_ids": list(event.option_ids),
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_poll_closed(self, event: PollClosed) -> None:
        payload = {
            "type": "poll.closed",
            "post_id": event.post_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_schedule_finalized(
        self,
        event: SchedulePollFinalized,
    ) -> None:
        payload = {
            "type": "schedule_poll.finalized",
            "post_id": event.post_id,
            "slot_id": event.slot_id,
            "slot_date": event.slot_date,
            "start_time": event.start_time,
            "end_time": event.end_time,
            "title": event.title,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    # ─── Calendar ─────────────────────────────────────────────────────────

    async def _on_calendar_created(self, event: CalendarEventCreated) -> None:
        await self._broadcast_calendar_event(
            {"type": "calendar.created", "event": _calendar_event_wire(event.event)},
            calendar_id=event.event.calendar_id,
        )

    async def _on_calendar_updated(self, event: CalendarEventUpdated) -> None:
        await self._broadcast_calendar_event(
            {"type": "calendar.updated", "event": _calendar_event_wire(event.event)},
            calendar_id=event.event.calendar_id,
        )

    async def _on_calendar_deleted(self, event: CalendarEventDeleted) -> None:
        frame = {
            "type": "calendar.deleted",
            "event_id": event.event_id,
            # Scope hint for the SPA: the space id for a space calendar,
            # else None (household / personal).
            "calendar_id": event.space_id,
        }
        if event.space_id:
            await self._broadcast_space(event.space_id, frame)
        else:
            await self._broadcast_household(frame)

    async def _broadcast_calendar_event(self, frame: dict, *, calendar_id: str) -> None:
        """Route a calendar WS frame by scope.

        Space-scoped calendars use ``calendar_id == space_id``; those
        frames fan out to that space's members only — never the whole
        household — so a non-member local user can't see a space's
        calendar content (matches the §encryption-first non-member rule
        the post / comment handlers already follow). Personal / household
        calendars (``calendar_id`` is not a space) fan to the household.
        """
        if calendar_id and await self._space_repo.get(calendar_id) is not None:
            await self._broadcast_space(calendar_id, frame)
        else:
            await self._broadcast_household(frame)

    # ─── Connections (peer reachability) ──────────────────────────────────

    async def _on_connection_reachable(self, event: ConnectionReachable) -> None:
        await self._broadcast_household(
            {"type": "connection.reachable", "instance_id": event.instance_id},
        )

    async def _on_connection_unreachable(self, event: ConnectionUnreachable) -> None:
        await self._broadcast_household(
            {"type": "connection.unreachable", "instance_id": event.instance_id},
        )

    async def _on_peer_unpaired(self, event: PeerUnpaired) -> None:
        # Household-wide like the other ``connection.*`` frames: every
        # signed-in member can read the connections list. A new pairing
        # needs no sibling frame — ``pairing.confirmed`` is that signal.
        await self._broadcast_household(
            {"type": "connection.removed", "instance_id": event.instance_id},
        )

    # ─── Gallery ──────────────────────────────────────────────────────────
    #
    # Thin frames (ids only) — the SPA refetches the affected album/list so
    # it always renders the canonical GET shape. Space albums fan out to
    # that space's members only (non-member rule); household albums
    # (``space_id is None``) fan to the household.

    async def _on_gallery_album_created(self, event: GalleryAlbumCreated) -> None:
        await self._broadcast_gallery(
            {
                "type": "gallery.album_created",
                "album_id": event.album_id,
                "space_id": event.space_id,
            },
            event.space_id,
        )

    async def _on_gallery_album_updated(self, event: GalleryAlbumUpdated) -> None:
        # Rename / description / cover change — local edits and the ones
        # a federation inbound handler applied (``origin_instance_id``)
        # alike, so an open album header follows a remote rename too.
        await self._broadcast_gallery(
            {
                "type": "gallery.album_updated",
                "album_id": event.album_id,
                "space_id": event.space_id,
            },
            event.space_id,
        )

    async def _on_gallery_album_deleted(self, event: GalleryAlbumDeleted) -> None:
        await self._broadcast_gallery(
            {
                "type": "gallery.album_deleted",
                "album_id": event.album_id,
                "space_id": event.space_id,
            },
            event.space_id,
        )

    async def _on_gallery_item_uploaded(self, event: GalleryItemUploaded) -> None:
        await self._broadcast_gallery(
            {
                "type": "gallery.item_uploaded",
                "album_id": event.album_id,
                "item_id": event.item_id,
                "space_id": event.space_id,
            },
            event.space_id,
        )

    async def _on_gallery_item_deleted(self, event: GalleryItemDeleted) -> None:
        await self._broadcast_gallery(
            {
                "type": "gallery.item_deleted",
                "album_id": event.album_id,
                "item_id": event.item_id,
                "space_id": event.space_id,
            },
            event.space_id,
        )

    async def _broadcast_gallery(self, frame: dict, space_id: str | None) -> None:
        if space_id:
            await self._broadcast_space(space_id, frame)
        else:
            await self._broadcast_household(frame)

    # ─── User status ──────────────────────────────────────────────────────

    async def _on_user_status(self, event: UserStatusChanged) -> None:
        await self._broadcast_household(
            {
                "type": "user.status_changed",
                "user_id": event.user_id,
                "status": _safe(event.status) if event.status else None,
            }
        )

    # ─── Household feature toggles (§18 / §23.13) ────────────────────────

    async def _on_household_config_changed(
        self,
        event: HouseholdConfigChanged,
    ) -> None:
        """Broadcast toggle changes so every connected client can
        refresh its nav + post-type allowlist without a page reload."""
        await self._broadcast_household(
            {
                "type": "household.config_changed",
                "changed": dict(event.changed),
            }
        )

    # ─── Child Protection (§23.107) ──────────────────────────────────────

    async def _on_cp_protection_enabled(self, event: CpProtectionEnabled) -> None:
        # No ``declared_age`` (SENSITIVE_FIELDS): listeners refetch.
        minor = await self._user_repo.get(event.minor_username)
        await self._broadcast_cp(
            minor.user_id if minor is not None else None,
            {
                "type": "cp.protection_enabled",
                "minor_username": event.minor_username,
            },
        )

    async def _on_cp_protection_disabled(self, event: CpProtectionDisabled) -> None:
        minor = await self._user_repo.get(event.minor_username)
        await self._broadcast_cp(
            minor.user_id if minor is not None else None,
            {
                "type": "cp.protection_disabled",
                "minor_username": event.minor_username,
            },
        )

    async def _on_cp_guardian_added(self, event: CpGuardianAdded) -> None:
        await self._broadcast_cp(
            event.minor_user_id,
            {
                "type": "cp.guardian_added",
                "minor_user_id": event.minor_user_id,
                "guardian_user_id": event.guardian_user_id,
            },
            also=(event.guardian_user_id,),
        )

    async def _on_cp_guardian_removed(self, event: CpGuardianRemoved) -> None:
        await self._broadcast_cp(
            event.minor_user_id,
            {
                "type": "cp.guardian_removed",
                "minor_user_id": event.minor_user_id,
                "guardian_user_id": event.guardian_user_id,
            },
            # The removed guardian is no longer listed — tell them directly.
            also=(event.guardian_user_id,),
        )

    async def _on_cp_block_added(self, event: CpBlockAdded) -> None:
        await self._broadcast_cp(
            event.minor_user_id,
            {
                "type": "cp.block_added",
                "minor_user_id": event.minor_user_id,
                "blocked_user_id": event.blocked_user_id,
            },
        )

    async def _on_cp_block_removed(self, event: CpBlockRemoved) -> None:
        await self._broadcast_cp(
            event.minor_user_id,
            {
                "type": "cp.block_removed",
                "minor_user_id": event.minor_user_id,
                "blocked_user_id": event.blocked_user_id,
            },
        )

    async def _broadcast_cp(
        self,
        minor_user_id: str | None,
        payload: dict,
        *,
        also: tuple[str, ...] = (),
    ) -> int:
        """§CP fan-out: household admins, the protected account itself and
        its guardians (plus ``also``). A child-protection frame names a
        protected account, so the rest of the household must not see it.

        The account itself also gets ``me.protection_changed`` — no data,
        only that account — so its SPA reloads ``/api/me`` and
        ``/api/me/protection`` instead of waiting for the next page load.
        """
        if minor_user_id:
            await self._ws.broadcast_to_user(
                minor_user_id, {"type": "me.protection_changed"}
            )
        users = await self._user_repo.list_active()
        ids = {u.user_id for u in users if u.is_admin}
        ids.update(also)
        if minor_user_id:
            ids.add(minor_user_id)
            if self._cp_repo is not None:
                ids.update(await self._cp_repo.list_guardians(minor_user_id))
        return await self._ws.broadcast_to_users(sorted(ids), payload)

    async def _on_cp_age_gate_changed(self, event: CpSpaceAgeGateChanged) -> None:
        await self._broadcast_household(
            {
                "type": "cp.age_gate_changed",
                "space_id": event.space_id,
                "min_age": event.min_age,
            }
        )

    # ─── Pages (§23.72) ───────────────────────────────────────────────────

    async def _on_page_lock_acquired(self, event: PageEditLockAcquired) -> None:
        """Broadcast ``page.editing`` so other viewers disable Edit."""
        payload = {
            "type": "page.editing",
            "page_id": event.page_id,
            "space_id": event.space_id,
            "locked_by": event.locked_by,
            "lock_expires_at": event.lock_expires_at,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_page_lock_released(self, event: PageEditLockReleased) -> None:
        payload = {
            "type": "page.editing_done",
            "page_id": event.page_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_page_conflict(self, event: PageConflictEmitted) -> None:
        payload = {
            "type": "page.conflict",
            "page_id": event.page_id,
            "space_id": event.space_id,
            "theirs": event.theirs,
            "theirs_by": event.theirs_by,
            "federated": event.federated,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_page_sequenced(self, event: PageProposalSettled) -> None:
        """v_48: the space's host answered this household's page edit —
        an open viewer drops its "waiting for the host" pill, or shows the
        refusal (``reason``)."""
        await self._broadcast_space(
            event.space_id,
            {
                "type": "page.sequenced",
                "page_id": event.page_id,
                "space_id": event.space_id,
                "outcome": event.outcome,
                "reason": event.reason,
            },
        )

    # ─── Stickies (§19) ───────────────────────────────────────────────────

    async def _on_sticky_created(self, event: StickyCreated) -> None:
        payload = {
            "type": "sticky.created",
            "id": event.sticky_id,
            "space_id": event.space_id,
            "author": event.author,
            "content": event.content,
            "color": event.color,
            "position_x": event.position_x,
            "position_y": event.position_y,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_sticky_updated(self, event: StickyUpdated) -> None:
        payload = {
            "type": "sticky.updated",
            "id": event.sticky_id,
            "space_id": event.space_id,
            "content": event.content,
            "color": event.color,
            "position_x": event.position_x,
            "position_y": event.position_y,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_sticky_deleted(self, event: StickyDeleted) -> None:
        payload = {
            "type": "sticky.deleted",
            "id": event.sticky_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    # ─── Highlights (§Highlights) ───────────────────────────────────────────────

    async def _on_highlight_frame_added(self, event: HighlightFrameAdded) -> None:
        await self._broadcast_household(
            {
                "type": "highlight.frame_added",
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "author_user_id": event.author_user_id,
                "highlight_date": event.highlight_date,
                "is_first_frame": event.is_first_frame,
            }
        )

    async def _on_highlight_frame_removed(self, event: HighlightFrameRemoved) -> None:
        await self._broadcast_household(
            {
                "type": "highlight.frame_removed",
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "author_user_id": event.author_user_id,
            }
        )

    async def _on_highlight_removed(self, event: HighlightRemoved) -> None:
        await self._broadcast_household(
            {
                "type": "highlight.removed",
                "highlight_id": event.highlight_id,
                "author_user_id": event.author_user_id,
            }
        )

    # ─── Highlight view / reaction back-channel ──────────────────────────────

    async def _on_highlight_frame_viewed(self, event: HighlightFrameViewed) -> None:
        # Only the author cares about a view receipt — narrow the WS
        # frame to their sessions so unrelated household members don't
        # see the noise.
        await self._ws.broadcast_to_users(
            [event.author_user_id],
            {
                "type": "highlight.frame_viewed",
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "viewer_user_id": event.viewer_user_id,
            },
        )

    async def _on_highlight_frame_reaction_changed(
        self,
        event: HighlightFrameReactionChanged,
    ) -> None:
        await self._ws.broadcast_to_users(
            [event.author_user_id],
            {
                "type": "highlight.frame_reaction_changed",
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "reactor_user_id": event.reactor_user_id,
                "emoji": event.emoji,  # None when cleared
            },
        )

    # ─── Momentum (§Momentum) ─────────────────────────────────────────────

    async def _on_moment_created(self, event: MomentCreated) -> None:
        # Whole-household broadcast — the SPA refetches ``/api/moments``
        # to pick up the audience-filtered shape (block-aware, etc.).
        await self._broadcast_household(
            {
                "type": "moment.created",
                "moment_id": event.moment_id,
                "author_user_id": event.author_user_id,
                "parent_moment_id": event.parent_moment_id,
            }
        )

    async def _on_moment_deleted(self, event: MomentDeleted) -> None:
        await self._broadcast_household(
            {
                "type": "moment.deleted",
                "moment_id": event.moment_id,
                "author_user_id": event.author_user_id,
            }
        )

    async def _on_moment_reaction_changed(
        self,
        event: MomentReactionChanged,
    ) -> None:
        # Author-only — unrelated household members don't need the noise.
        await self._ws.broadcast_to_users(
            [event.author_user_id],
            {
                "type": "moment.reaction_changed",
                "moment_id": event.moment_id,
                "reactor_user_id": event.reactor_user_id,
                "emoji": event.emoji,
            },
        )

    # ─── Fan-out helpers ──────────────────────────────────────────────────

    async def _broadcast_household(self, payload: dict) -> int:
        if self._media_signer is not None:
            sign_media_urls_in(payload, self._media_signer, extra_fields=("url",))
        users = await self._user_repo.list_active()
        return await self._ws.broadcast_to_users(
            [u.user_id for u in users],
            payload,
        )

    async def _broadcast_space(self, space_id: str, payload: dict) -> int:
        if self._media_signer is not None:
            sign_media_urls_in(payload, self._media_signer, extra_fields=("url",))
        ids = await self._space_repo.list_local_member_user_ids(space_id)
        return await self._ws.broadcast_to_users(ids, payload)

    async def _broadcast_household_admins(self, payload: dict) -> int:
        """Household fan-out narrowed to admins — for frames about
        actions only an admin can take (pairing management)."""
        users = await self._user_repo.list_active()
        return await self._ws.broadcast_to_users(
            [u.user_id for u in users if u.is_admin],
            payload,
        )

    async def _space_admin_ids(self, space_id: str) -> list[str]:
        members = await self._space_repo.list_members(space_id)
        return [m.user_id for m in members if m.role in _SPACE_ADMIN_ROLES]

    async def _broadcast_space_moderators(
        self,
        space_id: str,
        payload: dict,
        *,
        also: str | None = None,
    ) -> int:
        """Space fan-out narrowed to the owner, admins and moderators, plus
        ``also`` (e.g. a submitter) while that user is still a member."""
        members = await self._space_repo.list_members(space_id)
        ids = [m.user_id for m in members if m.role in _SPACE_MODERATOR_ROLES]
        if also is not None and also not in ids:
            if any(m.user_id == also for m in members):
                ids.append(also)
        return await self._ws.broadcast_to_users(ids, payload)

    # ─── Shopping list (§23.120 — local household only) ─────────────────

    async def _on_shopping_added(self, event: ShoppingItemAdded) -> None:
        # Shape matches the REST response so the client store can
        # append the event payload directly to ``items.value``.
        await self._broadcast_household(
            {
                "type": "shopping_list.item_added",
                "id": event.item_id,
                "text": event.text,
                "completed": False,
                "created_by": event.created_by,
                "created_at": event.created_at,
                "store": event.store,
            }
        )

    async def _on_shopping_toggled(self, event: ShoppingItemToggled) -> None:
        await self._broadcast_household(
            {
                "type": "shopping_list.item_updated",
                "id": event.item_id,
                "completed": event.completed,
            }
        )

    async def _on_shopping_updated(self, event: ShoppingItemUpdated) -> None:
        # Same frame type as the toggle broadcast — the client store
        # patches whatever keys are present, leaving the rest of the
        # cached row untouched. Carrying the new ``text`` + ``store``
        # lets every connected tab reflect an inline edit live.
        await self._broadcast_household(
            {
                "type": "shopping_list.item_updated",
                "id": event.item_id,
                "text": event.text,
                "store": event.store,
            }
        )

    async def _on_shopping_removed(self, event: ShoppingItemRemoved) -> None:
        await self._broadcast_household(
            {
                "type": "shopping_list.item_removed",
                "id": event.item_id,
            }
        )

    async def _on_shopping_cleared(self, event: ShoppingItemsCleared) -> None:
        await self._broadcast_household(
            {
                "type": "shopping_list.cleared",
                "count": event.count,
            }
        )

    async def _on_shopping_stores_reordered(
        self,
        event: ShoppingStoresReordered,
    ) -> None:
        # Push the canonical post-reorder name sequence; clients re-sort
        # their local ``stores`` signal by index in this list. ``sort_order``
        # itself isn't on the wire — the index in ``order`` is the order.
        await self._broadcast_household(
            {
                "type": "shopping_list.stores_reordered",
                "order": list(event.order),
            }
        )

    async def _on_shopping_store_renamed(
        self,
        event: ShoppingStoreRenamed,
    ) -> None:
        # Receivers patch both their catalogue (rename the row) and
        # every item whose ``store`` matched ``old_name`` (point it
        # at ``new_name``). One frame covers both.
        await self._broadcast_household(
            {
                "type": "shopping_list.store_renamed",
                "old_name": event.old_name,
                "new_name": event.new_name,
            }
        )

    async def _on_shopping_store_deleted(
        self,
        event: ShoppingStoreDeleted,
    ) -> None:
        # Items at this store have already had ``store`` cleared to
        # NULL server-side; SPA receivers mirror that locally so the
        # rows drop into the "No store" bucket without a refetch.
        await self._broadcast_household(
            {
                "type": "shopping_list.store_deleted",
                "name": event.name,
            }
        )

    # ─── Presence (§22) ──────────────────────────────────────────────────

    async def _on_presence_updated(self, event: PresenceUpdated) -> None:
        await self._broadcast_household(
            {
                "type": "presence.updated",
                "username": event.username,
                "state": event.state,
                "zone_name": event.zone_name,
                "latitude": event.latitude,
                "longitude": event.longitude,
            }
        )

    # ─── Online status (session presence) ─────────────────────────────────
    #
    # Frame names sit in the ``user.*`` namespace, not ``presence.*``, to
    # keep them visibly distinct from physical presence — the two signals
    # are orthogonal and clients should react to them independently.

    async def _on_user_came_online(self, event: UserCameOnline) -> None:
        await self._broadcast_user_status(
            user_id=event.user_id,
            frame_type="user.online",
            last_seen_at=None,
        )

    async def _on_user_resumed_active(self, event: UserResumedActive) -> None:
        await self._broadcast_user_status(
            user_id=event.user_id,
            frame_type="user.online",
            last_seen_at=None,
        )

    async def _on_user_went_idle(self, event: UserWentIdle) -> None:
        await self._broadcast_user_status(
            user_id=event.user_id,
            frame_type="user.idle",
            last_seen_at=event.last_active_at.isoformat(),
        )

    async def _on_user_went_offline(self, event: UserWentOffline) -> None:
        await self._broadcast_user_status(
            user_id=event.user_id,
            frame_type="user.offline",
            last_seen_at=event.last_seen_at.isoformat(),
        )

    async def _broadcast_user_status(
        self,
        *,
        user_id: str,
        frame_type: str,
        last_seen_at: str | None,
    ) -> None:
        """Fan a session-presence frame to every household member except
        the subject themselves (self-frame suppression — the user's own
        UI hydrates from ``/api/presence`` on mount)."""
        payload = {
            "type": frame_type,
            "user_id": user_id,
            "last_seen_at": last_seen_at,
        }
        users = await self._user_repo.list_active()
        recipients = [u.user_id for u in users if u.user_id != user_id]
        if recipients:
            await self._ws.broadcast_to_users(recipients, payload)

    # ─── Notifications (§21) ─────────────────────────────────────────────

    async def _on_notification_new(self, event: NotificationCreated) -> None:
        await self._ws.broadcast_to_user(
            event.user_id,
            {
                "type": "notification.new",
                "notification_id": event.notification_id,
                "notif_type": event.type,
                "title": event.title,
                "link_url": event.link_url,
            },
        )

    async def _on_notification_read_changed(
        self,
        event: NotificationReadChanged,
    ) -> None:
        await self._ws.broadcast_to_user(
            event.user_id,
            {
                "type": "notification.unread_count",
                "unread_count": event.unread_count,
            },
        )

    # ─── Bazaar (§9) ─────────────────────────────────────────────────────

    async def _on_bazaar_bid_placed(self, event: BazaarBidPlaced) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.bid_placed",
                "listing_post_id": event.listing_post_id,
                "amount": event.amount,
                "new_end_time": event.new_end_time,
            }
        )

    async def _on_bazaar_listing_closed(
        self,
        event: BazaarListingExpired,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.listing_closed",
                "listing_post_id": event.listing_post_id,
                "final_status": event.final_status,
            }
        )

    async def _on_bazaar_listing_created(
        self,
        event: BazaarListingCreated,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.listing_created",
                "listing_post_id": event.listing_post_id,
                "seller_user_id": event.seller_user_id,
                "mode": event.mode,
                "title": event.title,
            }
        )

    async def _on_bazaar_listing_updated(
        self,
        event: BazaarListingUpdated,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.listing_updated",
                "listing_post_id": event.listing_post_id,
            }
        )

    async def _on_bazaar_listing_cancelled(
        self,
        event: BazaarListingCancelled,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.listing_cancelled",
                "listing_post_id": event.listing_post_id,
            }
        )

    async def _on_bazaar_offer_accepted(
        self,
        event: BazaarOfferAccepted,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.offer_accepted",
                "listing_post_id": event.listing_post_id,
                "buyer_user_id": event.buyer_user_id,
                "price": event.price,
            }
        )

    async def _on_bazaar_offer_rejected(
        self,
        event: BazaarOfferRejected,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.offer_rejected",
                "listing_post_id": event.listing_post_id,
                "bid_id": event.bid_id,
                "bidder_user_id": event.bidder_user_id,
            }
        )

    async def _on_bazaar_bid_withdrawn(
        self,
        event: BazaarBidWithdrawn,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.bid_withdrawn",
                "listing_post_id": event.listing_post_id,
                "bid_id": event.bid_id,
                "bidder_user_id": event.bidder_user_id,
            }
        )

    # ─── DMs (§23.47) ────────────────────────────────────────────────────

    async def _on_dm_message_created(self, event: DmMessageCreated) -> None:
        """Push new DM messages to every recipient's WS sessions.

        §25.3: content is included on the in-process bus and intra-device
        WS (trusted transport). Push notifications separately apply the
        title-only redaction rule via :class:`NotificationService`.

        The frame ships a fully-formed ``message`` object (matching the
        REST :py:meth:`/api/conversations/{id}/messages` shape) so the
        client can append it directly to the thread without a follow-up
        fetch — the sender, recipient *and* every other open session land
        on the same row in the same render tick.
        """
        # Sign the media URL inside the WS frame just like the REST
        # ``GET /api/conversations/{id}/messages`` response does. The
        # canonical event carries the *unsigned* form (the bus / DB
        # never store a signed URL — signatures expire); the SPA
        # drops the URL straight into ``<img src>`` so it must be
        # the signed variant. Without this, the optimistic-bubble
        # reconciliation overwrites the sender's signed preview URL
        # with the unsigned canonical and the picture renders as
        # broken-image.
        signed_media_url: str | None = event.media_url
        if signed_media_url and self._media_signer is not None:
            signed_media_url = self._media_signer.sign(signed_media_url)
        payload = {
            "type": "dm.message",
            "conversation_id": event.conversation_id,
            "sender_display": event.sender_display_name,
            "message": {
                "id": event.message_id,
                "sender_user_id": event.sender_user_id,
                "content": event.content,
                "type": event.message_type,
                "media_url": signed_media_url,
                # v_3 media metadata. Mirror the REST GET shape so
                # the SPA's optimistic-bubble reconcile keeps the
                # filename + size on the receiver's bubble — the
                # file-pill render branches on these fields.
                "file_name": event.file_name,
                "mime_type": event.mime_type,
                "file_size_bytes": event.file_size_bytes,
                "reply_to_id": event.reply_to_id,
                "deleted": False,
                "created_at": event.occurred_at.isoformat(),
                "edited_at": None,
            },
        }
        # Sender's own sessions get the frame too — open thread tabs
        # show the sent message without the round-trip GET that
        # ``handleSend`` used to do.
        seen: set[str] = set()
        for user_id in (event.sender_user_id, *event.recipient_user_ids):
            if user_id in seen:
                continue
            seen.add(user_id)
            await self._ws.broadcast_to_user(user_id, payload)

    async def _on_dm_message_updated(self, event: DmMessageUpdated) -> None:
        """Push the in-place ``dm.message_updated`` frame.

        Two senders trigger this today, both for voice notes: the
        sender-side STT completing after the audio bubble has already
        shipped (``DmService._transcribe_and_patch``) and the
        receiver-side fallback STT (:class:`AudioTranscriptScheduler`).
        The frame carries only the fields that change so the SPA can
        merge into the existing bubble — no need to re-render the
        whole message.

        Fan-out covers the sender's own sessions too so a desktop
        composer sees the transcript appear on its mobile mirror.
        """
        payload = {
            "type": "dm.message_updated",
            "conversation_id": event.conversation_id,
            "message_id": event.message_id,
            "content": event.content,
            "edited_at": event.edited_at.isoformat(),
        }
        seen: set[str] = set()
        for user_id in (event.sender_user_id, *event.recipient_user_ids):
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            await self._ws.broadcast_to_user(user_id, payload)

    async def _on_dm_message_reaction_changed(
        self,
        event: DmMessageReactionChanged,
    ) -> None:
        """Push ``dm.message_reaction`` to every conversation member.

        The frame is a patch — the SPA applies it to the message in
        the active thread (or to the cached list if the thread is
        open elsewhere) and re-renders the reaction strip without
        a follow-up fetch.
        """
        payload = {
            "type": "dm.message_reaction",
            "conversation_id": event.conversation_id,
            "message_id": event.message_id,
            "user_id": event.user_id,
            "emoji": event.emoji,
            "action": event.action,
        }
        seen: set[str] = set()
        for user_id in event.recipient_user_ids:
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            await self._ws.broadcast_to_user(user_id, payload)

    async def broadcast_dm_media_ready(
        self,
        *,
        message_id: str,
        conversation_id: str,
        media_url: str,
        exclude_user_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Push ``dm.media_ready`` to local members of a conversation
        (never to ``exclude_user_ids`` — §CP.F2 guardian blocks).

        Called by ``FederationInboundService._on_dm_media_blob`` once
        the full bytes for a cross-household media message have
        landed and the row's ``media_url`` has flipped to point at
        the local full file. The SPA listens for this frame on
        :class:`DmThreadPage` and swaps the bubble's preview
        ``<img src>`` for the full media.

        Fan-out: every local participant of the conversation. The
        sender household's broadcast happens locally for free on the
        original send; this method is the *receiver* fan-out, so we
        only need the local members of *this* household (members
        from other households see the same WS frame via their own
        instance's matching DM_MEDIA_BLOB handler).
        """
        if not message_id or not conversation_id or not media_url:
            return
        members = await self._conversation_repo.list_members(conversation_id)
        # Resolve usernames → user_ids so the WS broker can route.
        user_ids: list[str] = []
        for m in members:
            if m.deleted_at is not None:
                continue
            u = await self._user_repo.get(m.username)
            if u is None or u.user_id in exclude_user_ids:
                continue
            user_ids.append(u.user_id)
        if not user_ids:
            return
        # Sign the URL for the same reason the dm.message frame does:
        # the SPA drops this value straight into ``<img src>`` on
        # the receiver side, so it must be the short-lived signed
        # variant rather than the unsigned canonical.
        signed = media_url
        if self._media_signer is not None:
            signed = self._media_signer.sign(media_url)
        await self._ws.broadcast_to_users(
            user_ids,
            {
                "type": "dm.media_ready",
                "conversation_id": conversation_id,
                "message_id": message_id,
                "media_url": signed,
            },
        )

    async def _on_media_transcode_ready(self, event: MediaTranscodeReady) -> None:
        """Tell the uploader's SPA a background video transcode is done so it
        can swap the 'Processing…' placeholder for the player. Other viewers
        pick up readiness via the media_status field on their next list fetch
        (broadcasting to the full post/item audience is a documented
        follow-up)."""
        if not event.owner_user_id:
            return
        media_url = f"api/media/{event.output_filename}"
        thumb_url = f"api/media/{event.thumbnail_filename}"
        if self._media_signer is not None:
            media_url = self._media_signer.sign(media_url)
            thumb_url = self._media_signer.sign(thumb_url)
        await self._ws.broadcast_to_user(
            event.owner_user_id,
            {
                "type": "media.ready",
                "output_filename": event.output_filename,
                "media_url": media_url,
                "thumbnail_url": thumb_url,
            },
        )

    async def _on_media_transcode_failed(self, event: MediaTranscodeFailed) -> None:
        """Tell the uploader's SPA a background video transcode permanently
        failed so it flips the 'Processing…' placeholder to the failed state
        at once. Other viewers pick up the ``'failed'`` status via the
        media_status field on their next list fetch (mirrors the
        ``media.ready`` path). No media_url is needed — the SPA just flips
        state."""
        if not event.owner_user_id:
            return
        await self._ws.broadcast_to_user(
            event.owner_user_id,
            {
                "type": "media.failed",
                "output_filename": event.output_filename,
            },
        )

    async def _on_dm_conversation_created(
        self,
        event: DmConversationCreated,
    ) -> None:
        """Tell every member's open inbox to refresh.

        The frame stays minimal — the inbox does a fresh GET so
        ordering, last-message stamps, and badge counts come from a
        single source of truth (the REST list endpoint).
        """
        payload = {
            "type": "dm.conversation.created",
            "conversation_id": event.conversation_id,
            "conversation_type": event.conversation_type,
            "name": event.name,
        }
        for user_id in event.member_user_ids:
            await self._ws.broadcast_to_user(user_id, payload)

    async def _on_dm_group_roster_changed(
        self,
        event: DmGroupRosterChanged,
    ) -> None:
        """Tell every local member (before and after the change) to refetch.

        Minimal like ``dm.conversation.created``: the thread / inbox pull
        the roster from ``GET /api/conversations/{id}/members`` — a member
        who was just removed gets a 403 there and leaves the thread.
        """
        payload = {
            "type": "dm.group.updated",
            "conversation_id": event.conversation_id,
            "name": event.name,
        }
        for user_id in event.notify_user_ids:
            await self._ws.broadcast_to_user(user_id, payload)


# ─── Serialisation helper ────────────────────────────────────────────────


def _media_filename(url: str | None) -> str | None:
    """Last path segment of a media URL, minus any signature query.

    The transcode repo's ``status_for`` is keyed by the *output filename* —
    the canonical media key. Mirrors ``routes/media_status.media_filename``;
    inlined here so the service layer carries no dependency on ``routes``.
    """
    if not url:
        return None
    return url.split("?", 1)[0].rsplit("/", 1)[-1] or None


def _video_poster_path(media_url: str | None) -> str | None:
    """Unsigned poster path for a transcoded video — the ``.webp``
    sibling of a ``.webm`` ``media_url`` (shared UUID stem). Returns
    ``None`` for a missing URL or any non-``.webm`` media. Mirrors
    ``routes/media_status.video_poster_path``; inlined here so the
    service layer carries no dependency on ``routes``.
    """
    if not media_url:
        return None
    base = media_url.split("?", 1)[0]
    if not base.endswith(".webm"):
        return None
    return base[: -len(".webm")] + ".webp"


def _calendar_event_wire(event: Any) -> Any:
    """:func:`_safe` of a calendar event with its cover cut to a local
    media reference — like every REST read (``routes/calendar.py``), a
    cover stored before the local-only rule is ``null``, never an
    ``<img>`` source (docs/principles.md "No third-party fetches")."""
    wire = _safe(event)
    if isinstance(wire, dict) and "cover_url" in wire:
        wire["cover_url"] = verbatim_local_media_ref(wire["cover_url"])
    return wire


def _safe(value: Any) -> Any:
    """Convert a domain dataclass into a JSON-serialisable dict.

    Handles ``datetime`` / ``date`` (ISO-8601), ``frozenset`` / ``set``
    / ``tuple`` (lists), and nested dataclasses.  Anything else is
    passed through.
    """
    if value is None:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return _safe(asdict(value))
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
