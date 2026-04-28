"""Route handlers — thin aiohttp handlers that delegate to services.

Each submodule exposes one or more :class:`BaseView` subclasses grouped
by REST resource.  :func:`setup_routes` mounts them all on an
:class:`aiohttp.web.Application` via ``router.add_view()``.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .. import app_keys as K
from .backup import (
    BackupExportView,
    BackupImportView,
    BackupPostView,
    BackupPreView,
)
from .bazaar import (
    BazaarBidAcceptView,
    BazaarBidCollectionView,
    BazaarBidDetailView,
    BazaarBidRejectView,
    BazaarCollectionView,
    BazaarDetailView,
    BazaarOfferAcceptView,
    BazaarOfferCollectionView,
    BazaarOfferDetailView,
    BazaarOfferRejectView,
    BazaarSaveView,
    MySavedBazaarView,
)
from .calendar import (
    CalendarCollectionView,
    CalendarEventApprovalView,
    CalendarEventDeleteView,
    CalendarEventPendingView,
    CalendarEventRemindersView,
    CalendarEventRsvpsView,
    CalendarEventRsvpView,
    CalendarEventsView,
    CalendarImportIcsView,
    CalendarImportImageView,
    CalendarImportPromptView,
    SpaceCalendarEventDetailView,
    SpaceCalendarEventsView,
)
from .calendar_ics import (
    CalendarEventIcsView,
    SpaceCalendarFeedTokenView,
    SpaceCalendarFeedView,
)
from .calls import (
    CallActiveView,
    CallAnswerView,
    CallCollectionView,
    CallDeclineView,
    CallHangupView,
    CallIceView,
    CallJoinView,
    CallQualityView,
    ConversationCallHistoryView,
    IceServersView,
)
from .child_protection import (
    CPAgeGateView,
    CPAuditLogView,
    CPBlockCollectionView,
    CPBlockView,
    CPConversationCollectionView,
    CPDmContactCollectionView,
    CPKickView,
    CPMembershipAuditView,
    CPMinorsForGuardianView,
    CPSpaceCollectionView,
    CPGuardiansView,
    CPProtectionView,
)
from .conversations import (
    ConversationCollectionView,
    ConversationDeliveryStatesView,
    ConversationDmView,
    ConversationGapsView,
    ConversationGroupView,
    ConversationMessageDeliveryView,
    ConversationMessageView,
    ConversationReadView,
    ConversationUnreadView,
)
from .federation import FederationInboxView
from .ha_integration import HaIntegrationFederationBaseView
from .feed import (
    FeedCollectionView,
    FeedReadWatermarkView,
    PostCollectionView,
    PostCommentDetailView,
    PostCommentView,
    PostDetailView,
    PostReactionCollectionView,
    PostReactionDetailView,
    PostSaveView,
    SavedPostsView,
)
from .gfs import (
    GfsAppealView,
    GfsConnectionCollectionView,
    GfsConnectionDetailView,
    GfsPublicationsView,
    GfsSpacePublishView,
)
from .gallery import (
    AlbumDetailView,
    AlbumItemCollectionView,
    AlbumRetentionView,
    GalleryItemDetailView,
    HouseholdAlbumCollectionView,
    SpaceAlbumCollectionView,
)
from .health import HealthView
from .household import HouseholdFeaturesView
from .media import MediaServeView, MediaUploadView
from .notifications import (
    NotificationCollectionView,
    NotificationReadAllView,
    NotificationReadView,
    NotificationUnreadCountView,
)
from .pages import (
    PageCollectionView,
    PageConflictView,
    PageDeleteApproveView,
    PageDeleteCancelView,
    PageDeleteRequestView,
    PageDetailView,
    PageLockRefreshView,
    PageLockView,
    PageRevertView,
    PageVersionView,
    SpacePageCollectionView,
    SpacePageDetailView,
)
from .pairing import (
    AutoPairInboxApproveView,
    AutoPairInboxCollectionView,
    AutoPairInboxDeclineView,
    AutoPairViaView,
    PairingAcceptView,
    PairingConfirmView,
    PairingConnectionCollectionView,
    PairingConnectionDetailView,
    PairingInitiateView,
    PairingIntroduceView,
    PairingRelayApproveView,
    PairingRelayDeclineView,
    PairingRelayRequestCollectionView,
)
from .pairing_peer import (
    PairingPeerAcceptView,
    PairingPeerConfirmView,
)
from .polls import (
    PollCloseView,
    PollSummaryView,
    PollVoteView,
    SchedulePollCollectionView,
    SchedulePollFinalizeView,
    SchedulePollRespondView,
    SchedulePollSlotResponseView,
    SchedulePollSummaryView,
)
from .space_polls import (
    SpacePollCloseView,
    SpacePollSummaryView,
    SpacePollVoteView,
    SpaceSchedulePollCollectionView,
    SpaceSchedulePollFinalizeView,
    SpaceSchedulePollRespondView,
    SpaceSchedulePollSlotResponseView,
    SpaceSchedulePollSummaryView,
)
from .presence import PresenceCollectionView, PresenceLocationView
from .peer_spaces import PeerSpaceCollectionView
from .public_spaces import (
    PublicSpaceBlockInstanceView,
    PublicSpaceCollectionView,
    PublicSpaceHideView,
    PublicSpaceJoinRequestView,
    PublicSpacesRefreshView,
)
from .push import PushSubscribeView, PushSubscriptionListView, PushVapidKeyView
from .search import SearchView
from .shopping import (
    ShoppingClearCompletedView,
    ShoppingCollectionView,
    ShoppingItemCompleteView,
    ShoppingItemDetailView,
    ShoppingItemUncompleteView,
)
from .bot_bridge import (
    BotBridgeConversationPostView,
    BotBridgeSpacePostView,
)
from .space_bots import (
    SpaceBotCollectionView,
    SpaceBotDetailView,
    SpaceBotTokenView,
)
from .spaces import (
    AdminSpaceCollectionView,
    MySubscriptionsView,
    SpaceBanListView,
    SpaceBanView,
    SpaceCollectionView,
    SpaceDetailView,
    SpaceCoverView,
    SpaceFeedView,
    SpaceSubscribeView,
    RemoteInviteCollectionView,
    RemoteInviteDecisionView,
    SpaceInviteTokenView,
    SpaceLinkCollectionView,
    SpaceLinkDetailView,
    SpaceNotifPrefsView,
    SpaceMemberLocationSharingView,
    SpacePresenceView,
    SpaceJoinRequestCollectionView,
    SpaceJoinRequestDetailView,
    SpaceJoinView,
    SpaceRemoteInviteView,
    SpaceMemberDetailView,
    SpaceMemberMePictureView,
    SpaceMemberMeProfileView,
    SpaceMemberPictureView,
    SpaceMembersView,
    SpaceModerationApproveView,
    SpaceModerationQueueView,
    SpaceModerationRejectView,
    SpaceOwnershipView,
    SpacePostCollectionView,
    SpacePostCommentDetailView,
    SpacePostCommentView,
    SpacePostReactionView,
    SpaceSyncTriggerView,
    SpaceUnbanView,
)
from .space_zones import SpaceZoneDetailView, SpaceZonesCollectionView
from .stickies import (
    SpaceStickyCollectionView,
    SpaceStickyDetailView,
    StickyCollectionView,
    StickyDetailView,
)
from .ha_users import HaUserProvisionView, HaUsersCollectionView
from .reports import (
    AdminReportCollectionView,
    AdminReportResolveView,
    ReportCollectionView,
)
from .storage import StorageQuotaView, StorageUsageView
from .tasks import (
    SpaceTaskArchiveView,
    SpaceTaskDetailView,
    SpaceTaskListCollectionView,
    SpaceTaskListDetailView,
    SpaceTaskListTasksView,
    TaskArchiveView,
    TaskAttachmentCollectionView,
    TaskAttachmentDetailView,
    TaskCommentCollectionView,
    TaskCommentDetailView,
    TaskDetailView,
    TaskListCollectionView,
    TaskListDetailView,
    TaskListReorderView,
    TaskListTasksView,
)
from .themes import HouseholdThemeView, SpaceThemeView
from .corner import CornerView
from .users import (
    AdminTokenCollectionView,
    AdminTokenDetailView,
    AdminUserCollectionView,
    AuthTokenView,
    MeExportView,
    MePictureRefreshFromHaView,
    MePictureView,
    MeView,
    TokenCollectionView,
    TokenDetailView,
    UserCollectionView,
    UserDetailView,
    UserExportView,
    UserPictureView,
)
from .aliases import AliasCollectionView, AliasItemView
from .instance import InstanceConfigView
from .setup import (
    HaOwnerSetupView,
    HaPersonsSetupView,
    HaosCompleteSetupView,
    StandaloneSetupView,
)
from .stt import SttStreamView
from .ws import WebSocketView

log = logging.getLogger(__name__)


def setup_routes(app: web.Application) -> None:  # noqa: C901
    """Add all route definitions to *app*."""

    # ── Health (public — no auth) ───────────────────────────────────────
    app.router.add_view("/healthz", HealthView)

    # ── Users / auth ────────────────────────────────────────────────────
    app.router.add_view("/api/me", MeView)
    app.router.add_view("/api/me/corner", CornerView)
    app.router.add_view("/api/me/picture", MePictureView)
    app.router.add_view(
        "/api/me/picture/refresh-from-ha",
        MePictureRefreshFromHaView,
    )
    app.router.add_view("/api/me/tokens", TokenCollectionView)
    app.router.add_view("/api/me/tokens/{id}", TokenDetailView)
    app.router.add_view("/api/admin/tokens", AdminTokenCollectionView)
    app.router.add_view("/api/admin/tokens/{id}", AdminTokenDetailView)
    app.router.add_view("/api/admin/users", AdminUserCollectionView)
    app.router.add_view("/api/me/export", MeExportView)
    app.router.add_view("/api/users", UserCollectionView)
    app.router.add_view("/api/users/{user_id}", UserDetailView)
    app.router.add_view("/api/users/{user_id}/picture", UserPictureView)
    app.router.add_view("/api/users/{user_id}/export", UserExportView)
    # Personal user aliases (§4.1.6) — viewer-private renames.
    app.router.add_view("/api/aliases/users", AliasCollectionView)
    app.router.add_view("/api/aliases/users/{user_id}", AliasItemView)
    app.router.add_view("/api/auth/token", AuthTokenView)

    # ── Instance metadata + first-boot setup wizard ─────────────────────
    # Public paths (see auth._DEFAULT_PUBLIC_PATHS) — the SPA needs them
    # before it has a token.
    app.router.add_view("/api/instance/config", InstanceConfigView)
    app.router.add_view("/api/setup/standalone", StandaloneSetupView)
    app.router.add_view("/api/setup/ha/persons", HaPersonsSetupView)
    app.router.add_view("/api/setup/ha/owner", HaOwnerSetupView)
    app.router.add_view("/api/setup/haos/complete", HaosCompleteSetupView)

    # ── Feed / posts ────────────────────────────────────────────────────
    app.router.add_view("/api/feed", FeedCollectionView)
    app.router.add_view("/api/feed/posts", PostCollectionView)
    app.router.add_view("/api/feed/posts/{id}", PostDetailView)
    app.router.add_view("/api/feed/posts/{id}/reactions", PostReactionCollectionView)
    app.router.add_view(
        "/api/feed/posts/{id}/reactions/{emoji}", PostReactionDetailView
    )
    app.router.add_view("/api/feed/posts/{id}/comments", PostCommentView)
    app.router.add_view(
        "/api/feed/posts/{id}/comments/{cid}",
        PostCommentDetailView,
    )
    app.router.add_view("/api/feed/posts/{id}/save", PostSaveView)
    app.router.add_view("/api/feed/saved", SavedPostsView)
    app.router.add_view("/api/me/feed/read", FeedReadWatermarkView)

    # ── Spaces ──────────────────────────────────────────────────────────
    app.router.add_view("/api/admin/spaces", AdminSpaceCollectionView)
    app.router.add_view("/api/spaces", SpaceCollectionView)
    app.router.add_view("/api/spaces/join", SpaceJoinView)
    app.router.add_view("/api/spaces/{id}", SpaceDetailView)
    app.router.add_view("/api/spaces/{id}/members", SpaceMembersView)
    app.router.add_view(
        "/api/spaces/{id}/members/me",
        SpaceMemberMeProfileView,
    )
    app.router.add_view(
        "/api/spaces/{id}/members/me/picture",
        SpaceMemberMePictureView,
    )
    # Route ordering: picture (concrete child) before the dynamic
    # {user_id} detail view, so "me/picture" doesn't match user_id="me"
    # with a trailing /picture suffix (it wouldn't anyway — aiohttp's
    # radix-tree matcher is exact — but this keeps the reading order
    # obvious).
    app.router.add_view(
        "/api/spaces/{id}/members/{user_id}/picture",
        SpaceMemberPictureView,
    )
    app.router.add_view("/api/spaces/{id}/members/{user_id}", SpaceMemberDetailView)
    app.router.add_view("/api/spaces/{id}/ban", SpaceBanView)
    app.router.add_view("/api/spaces/{id}/invite-tokens", SpaceInviteTokenView)
    app.router.add_view("/api/spaces/{id}/presence", SpacePresenceView)
    app.router.add_view("/api/spaces/{id}/zones", SpaceZonesCollectionView)
    app.router.add_view(
        "/api/spaces/{id}/zones/{zone_id}",
        SpaceZoneDetailView,
    )
    app.router.add_view(
        "/api/spaces/{id}/members/me/location-sharing",
        SpaceMemberLocationSharingView,
    )
    app.router.add_view(
        "/api/spaces/{id}/remote-invites",
        SpaceRemoteInviteView,
    )
    app.router.add_view("/api/remote_invites", RemoteInviteCollectionView)
    app.router.add_view(
        "/api/remote_invites/{token}/{decision}",
        RemoteInviteDecisionView,
    )
    app.router.add_view("/api/spaces/{id}/feed", SpaceFeedView)
    app.router.add_view("/api/spaces/{id}/cover", SpaceCoverView)
    app.router.add_view("/api/spaces/{id}/posts", SpacePostCollectionView)
    app.router.add_view("/api/spaces/{id}/sync", SpaceSyncTriggerView)
    app.router.add_view("/api/spaces/{id}/subscribe", SpaceSubscribeView)
    app.router.add_view("/api/me/subscriptions", MySubscriptionsView)
    # Space customisation — admin-configured sidebar links + per-user
    # notification preferences (§23).
    app.router.add_view("/api/spaces/{id}/links", SpaceLinkCollectionView)
    app.router.add_view(
        "/api/spaces/{id}/links/{link_id}",
        SpaceLinkDetailView,
    )
    app.router.add_view(
        "/api/spaces/{id}/notif-prefs",
        SpaceNotifPrefsView,
    )
    # Bot personas (named bots that post into a space via the bot-bridge).
    app.router.add_view("/api/spaces/{id}/bots", SpaceBotCollectionView)
    app.router.add_view("/api/spaces/{id}/bots/{bot_id}", SpaceBotDetailView)
    app.router.add_view("/api/spaces/{id}/bots/{bot_id}/token", SpaceBotTokenView)
    # Bot-bridge (HA → SH). Space posts auth via per-bot Bearer tokens
    # (route marked public in auth._DEFAULT_PUBLIC_PATHS; inline auth).
    # Conversation posts use the normal user API token path.
    app.router.add_view("/api/bot-bridge/spaces/{id}", BotBridgeSpacePostView)
    app.router.add_view(
        "/api/bot-bridge/conversations/{id}", BotBridgeConversationPostView
    )
    app.router.add_view("/api/spaces/{id}/moderation", SpaceModerationQueueView)
    app.router.add_view(
        "/api/spaces/{id}/moderation/{item_id}/approve",
        SpaceModerationApproveView,
    )
    app.router.add_view(
        "/api/spaces/{id}/moderation/{item_id}/reject",
        SpaceModerationRejectView,
    )
    app.router.add_view("/api/spaces/{id}/bans", SpaceBanListView)
    app.router.add_view("/api/spaces/{id}/bans/{user_id}", SpaceUnbanView)
    # Ownership transfer (owner-only).
    app.router.add_view("/api/spaces/{id}/ownership", SpaceOwnershipView)
    # Join-request review.
    app.router.add_view(
        "/api/spaces/{id}/join-requests",
        SpaceJoinRequestCollectionView,
    )
    app.router.add_view(
        "/api/spaces/{id}/join-requests/{request_id}/{action}",
        SpaceJoinRequestDetailView,
    )
    # Space-post reactions + comments.
    app.router.add_view(
        "/api/spaces/{id}/posts/{post_id}/reactions",
        SpacePostReactionView,
    )
    app.router.add_view(
        "/api/spaces/{id}/posts/{post_id}/reactions/{emoji}",
        SpacePostReactionView,
    )
    app.router.add_view(
        "/api/spaces/{id}/posts/{post_id}/comments",
        SpacePostCommentView,
    )
    app.router.add_view(
        "/api/spaces/{id}/posts/{post_id}/comments/{cid}",
        SpacePostCommentDetailView,
    )

    # ── Conversations (DMs) ─────────────────────────────────────────────
    app.router.add_view("/api/conversations", ConversationCollectionView)
    app.router.add_view("/api/conversations/dm", ConversationDmView)
    app.router.add_view("/api/conversations/group", ConversationGroupView)
    app.router.add_view("/api/conversations/{id}/messages", ConversationMessageView)
    app.router.add_view(
        "/api/conversations/{id}/messages/{mid}/delivered",
        ConversationMessageDeliveryView,
    )
    app.router.add_view(
        "/api/conversations/{id}/delivery-states",
        ConversationDeliveryStatesView,
    )
    app.router.add_view(
        "/api/conversations/{id}/gaps",
        ConversationGapsView,
    )
    app.router.add_view("/api/conversations/{id}/read", ConversationReadView)
    app.router.add_view("/api/conversations/{id}/unread", ConversationUnreadView)

    # ── Notifications ───────────────────────────────────────────────────
    app.router.add_view("/api/notifications", NotificationCollectionView)
    app.router.add_view("/api/notifications/unread-count", NotificationUnreadCountView)
    app.router.add_view("/api/notifications/{id}/read", NotificationReadView)
    app.router.add_view("/api/notifications/read-all", NotificationReadAllView)

    # ── Presence ────────────────────────────────────────────────────────
    app.router.add_view("/api/presence", PresenceCollectionView)
    app.router.add_view("/api/presence/location", PresenceLocationView)

    # ── Shopping ────────────────────────────────────────────────────────
    app.router.add_view("/api/shopping", ShoppingCollectionView)
    app.router.add_view("/api/shopping/clear-completed", ShoppingClearCompletedView)
    app.router.add_view("/api/shopping/{id}", ShoppingItemDetailView)
    app.router.add_view("/api/shopping/{id}/complete", ShoppingItemCompleteView)
    app.router.add_view("/api/shopping/{id}/uncomplete", ShoppingItemUncompleteView)

    # ── Tasks ───────────────────────────────────────────────────────────
    app.router.add_view("/api/tasks/lists", TaskListCollectionView)
    app.router.add_view("/api/tasks/lists/{id}", TaskListDetailView)
    app.router.add_view("/api/tasks/lists/{id}/tasks", TaskListTasksView)
    app.router.add_view(
        "/api/tasks/lists/{id}/reorder",
        TaskListReorderView,
    )
    app.router.add_view("/api/tasks/{id}", TaskDetailView)
    app.router.add_view("/api/tasks/{id}/archive", TaskArchiveView)
    app.router.add_view(
        "/api/tasks/{id}/comments",
        TaskCommentCollectionView,
    )
    app.router.add_view(
        "/api/tasks/{id}/comments/{comment_id}",
        TaskCommentDetailView,
    )
    app.router.add_view(
        "/api/tasks/{id}/attachments",
        TaskAttachmentCollectionView,
    )
    app.router.add_view(
        "/api/tasks/{id}/attachments/{attachment_id}",
        TaskAttachmentDetailView,
    )
    app.router.add_view(
        "/api/spaces/{id}/tasks/lists",
        SpaceTaskListCollectionView,
    )
    app.router.add_view(
        "/api/spaces/{id}/tasks/lists/{lid}",
        SpaceTaskListDetailView,
    )
    app.router.add_view(
        "/api/spaces/{id}/tasks/lists/{lid}/tasks",
        SpaceTaskListTasksView,
    )
    app.router.add_view(
        "/api/spaces/{id}/tasks/{tid}",
        SpaceTaskDetailView,
    )
    app.router.add_view(
        "/api/spaces/{id}/tasks/{tid}/archive",
        SpaceTaskArchiveView,
    )

    # ── Calendar ────────────────────────────────────────────────────────
    app.router.add_view("/api/calendars", CalendarCollectionView)
    app.router.add_view("/api/calendars/{id}/events", CalendarEventsView)
    app.router.add_view("/api/calendars/{id}/import_ics", CalendarImportIcsView)
    app.router.add_view("/api/calendars/{id}/import_image", CalendarImportImageView)
    app.router.add_view("/api/calendars/{id}/import_prompt", CalendarImportPromptView)
    app.router.add_view("/api/calendars/events/{id}", CalendarEventDeleteView)
    app.router.add_view("/api/calendars/events/{id}/rsvp", CalendarEventRsvpView)
    app.router.add_view("/api/calendars/events/{id}/rsvps", CalendarEventRsvpsView)
    app.router.add_view(
        "/api/calendars/events/{id}/approve",
        CalendarEventApprovalView,
    )
    app.router.add_view(
        "/api/calendars/events/{id}/pending",
        CalendarEventPendingView,
    )
    app.router.add_view(
        "/api/calendars/events/{id}/reminders",
        CalendarEventRemindersView,
    )
    # Phase F — iCal export. Path uses a sub-segment to avoid colliding
    # with `/api/calendars/events/{id}` (aiohttp's `{id}` would otherwise
    # eat ``eid.ics`` as the dynamic segment).
    app.router.add_view(
        "/api/calendars/events/{id}/export.ics",
        CalendarEventIcsView,
    )
    app.router.add_view(
        "/api/spaces/{id}/calendar/export.ics",
        SpaceCalendarFeedView,
    )
    app.router.add_view(
        "/api/spaces/{id}/calendar/feed-token",
        SpaceCalendarFeedTokenView,
    )
    app.router.add_view("/api/spaces/{id}/calendar/events", SpaceCalendarEventsView)
    app.router.add_view(
        "/api/spaces/{id}/calendar/events/{eid}",
        SpaceCalendarEventDetailView,
    )

    # ── Pages ───────────────────────────────────────────────────────────
    app.router.add_view("/api/pages", PageCollectionView)
    app.router.add_view("/api/pages/{id}", PageDetailView)
    app.router.add_view("/api/pages/{id}/lock", PageLockView)
    app.router.add_view("/api/pages/{id}/lock/refresh", PageLockRefreshView)
    app.router.add_view("/api/pages/{id}/versions", PageVersionView)
    app.router.add_view("/api/pages/{id}/revert", PageRevertView)
    app.router.add_view(
        "/api/pages/{id}/delete-request",
        PageDeleteRequestView,
    )
    app.router.add_view(
        "/api/pages/{id}/delete-approve",
        PageDeleteApproveView,
    )
    app.router.add_view(
        "/api/pages/{id}/delete-cancel",
        PageDeleteCancelView,
    )
    app.router.add_view("/api/spaces/{id}/pages", SpacePageCollectionView)
    app.router.add_view("/api/spaces/{id}/pages/{pid}", SpacePageDetailView)
    app.router.add_view(
        "/api/spaces/{id}/pages/{pid}/resolve-conflict",
        PageConflictView,
    )

    # ── Stickies ────────────────────────────────────────────────────────
    app.router.add_view("/api/stickies", StickyCollectionView)
    app.router.add_view("/api/stickies/{id}", StickyDetailView)
    app.router.add_view(
        "/api/spaces/{id}/stickies",
        SpaceStickyCollectionView,
    )
    app.router.add_view(
        "/api/spaces/{id}/stickies/{sid}",
        SpaceStickyDetailView,
    )

    # ── Bazaar ──────────────────────────────────────────────────────────
    app.router.add_view("/api/bazaar", BazaarCollectionView)
    app.router.add_view("/api/bazaar/{id}", BazaarDetailView)
    app.router.add_view("/api/bazaar/{id}/bids", BazaarBidCollectionView)
    app.router.add_view(
        "/api/bazaar/{id}/bids/{bid_id}",
        BazaarBidDetailView,
    )
    app.router.add_view(
        "/api/bazaar/{id}/bids/{bid_id}/accept",
        BazaarBidAcceptView,
    )
    app.router.add_view(
        "/api/bazaar/{id}/bids/{bid_id}/reject",
        BazaarBidRejectView,
    )
    # Fixed-price offers + saved-listing bookmarks (§23.23).
    app.router.add_view(
        "/api/bazaar/{id}/offers",
        BazaarOfferCollectionView,
    )
    app.router.add_view(
        "/api/bazaar/{id}/offers/{offer_id}",
        BazaarOfferDetailView,
    )
    app.router.add_view(
        "/api/bazaar/{id}/offers/{offer_id}/accept",
        BazaarOfferAcceptView,
    )
    app.router.add_view(
        "/api/bazaar/{id}/offers/{offer_id}/reject",
        BazaarOfferRejectView,
    )
    app.router.add_view("/api/bazaar/{id}/save", BazaarSaveView)
    app.router.add_view("/api/me/bazaar/saved", MySavedBazaarView)

    # ── Media ───────────────────────────────────────────────────────────
    app.router.add_view("/api/media/upload", MediaUploadView)
    app.router.add_view("/api/media/{filename}", MediaServeView)

    # ── Gallery ─────────────────────────────────────────────────────────
    app.router.add_view("/api/gallery/albums", HouseholdAlbumCollectionView)
    app.router.add_view("/api/gallery/albums/{album_id}", AlbumDetailView)
    app.router.add_view("/api/gallery/albums/{album_id}/retention", AlbumRetentionView)
    app.router.add_view("/api/gallery/albums/{album_id}/items", AlbumItemCollectionView)
    app.router.add_view("/api/gallery/items/{item_id}", GalleryItemDetailView)
    app.router.add_view(
        "/api/spaces/{space_id}/gallery/albums",
        SpaceAlbumCollectionView,
    )

    # ── Federation ──────────────────────────────────────────────────────
    app.router.add_view("/federation/inbox/{inbox_id}", FederationInboxView)

    # ── HA integration bridge (§7.9) ────────────────────────────────────
    app.router.add_view(
        "/api/ha/integration/federation-base",
        HaIntegrationFederationBaseView,
    )

    # ── Pairing / connections ───────────────────────────────────────────
    app.router.add_view("/api/pairing/initiate", PairingInitiateView)
    app.router.add_view("/api/pairing/accept", PairingAcceptView)
    app.router.add_view("/api/pairing/confirm", PairingConfirmView)
    # §11 peer-to-peer bootstrap transport (plaintext, Ed25519-signed).
    app.router.add_view("/api/pairing/peer-accept", PairingPeerAcceptView)
    app.router.add_view("/api/pairing/peer-confirm", PairingPeerConfirmView)
    app.router.add_view("/api/pairing/introduce", PairingIntroduceView)
    app.router.add_view("/api/pairing/auto-pair-via", AutoPairViaView)
    app.router.add_view(
        "/api/pairing/auto-pair-requests",
        AutoPairInboxCollectionView,
    )
    app.router.add_view(
        "/api/pairing/auto-pair-requests/{request_id}/approve",
        AutoPairInboxApproveView,
    )
    app.router.add_view(
        "/api/pairing/auto-pair-requests/{request_id}/decline",
        AutoPairInboxDeclineView,
    )
    app.router.add_view("/api/pairing/connections", PairingConnectionCollectionView)
    app.router.add_view(
        "/api/pairing/connections/{instance_id}",
        PairingConnectionDetailView,
    )
    app.router.add_view("/api/connections", PairingConnectionCollectionView)
    app.router.add_view(
        "/api/pairing/relay-requests",
        PairingRelayRequestCollectionView,
    )
    app.router.add_view(
        "/api/pairing/relay-requests/{id}/approve",
        PairingRelayApproveView,
    )
    app.router.add_view(
        "/api/pairing/relay-requests/{id}/decline",
        PairingRelayDeclineView,
    )
    # ── GFS connections ────────────────────────────────────────────────
    app.router.add_view("/api/gfs/connections", GfsConnectionCollectionView)
    app.router.add_view("/api/gfs/publications", GfsPublicationsView)
    app.router.add_view("/api/gfs/connections/{id}", GfsConnectionDetailView)
    app.router.add_view(
        "/api/gfs/connections/{gfs_id}/appeal",
        GfsAppealView,
    )
    app.router.add_view(
        "/api/spaces/{id}/publish/{gfs_id}",
        GfsSpacePublishView,
    )

    # ── Calls / WebRTC ──────────────────────────────────────────────────
    app.router.add_view("/api/calls", CallCollectionView)
    app.router.add_view("/api/calls/active", CallActiveView)
    app.router.add_view("/api/calls/{call_id}/answer", CallAnswerView)
    app.router.add_view("/api/calls/{call_id}/ice", CallIceView)
    app.router.add_view("/api/calls/{call_id}/hangup", CallHangupView)
    app.router.add_view("/api/calls/{call_id}/decline", CallDeclineView)
    app.router.add_view("/api/calls/{call_id}/join", CallJoinView)
    app.router.add_view("/api/calls/{call_id}/quality", CallQualityView)
    app.router.add_view(
        "/api/conversations/{id}/calls",
        ConversationCallHistoryView,
    )
    app.router.add_view("/api/webrtc/ice_servers", IceServersView)
    app.router.add_view("/api/calls/ice-servers", IceServersView)

    # ── WebSocket ───────────────────────────────────────────────────────
    app.router.add_view("/api/ws", WebSocketView)
    app.router.add_view("/api/stt/stream", SttStreamView)

    # ── Push ────────────────────────────────────────────────────────────
    app.router.add_view("/api/push/vapid_public_key", PushVapidKeyView)
    app.router.add_view("/api/push/subscribe", PushSubscribeView)
    app.router.add_view("/api/push/subscribe/{sub_id}", PushSubscribeView)
    app.router.add_view("/api/push/subscriptions", PushSubscriptionListView)

    # ── Search ──────────────────────────────────────────────────────────
    app.router.add_view("/api/search", SearchView)

    # ── Themes ──────────────────────────────────────────────────────────
    app.router.add_view("/api/theme", HouseholdThemeView)
    app.router.add_view("/api/spaces/{space_id}/theme", SpaceThemeView)

    # ── Storage ─────────────────────────────────────────────────────────
    app.router.add_view("/api/storage/usage", StorageUsageView)
    app.router.add_view("/api/admin/storage/quota", StorageQuotaView)

    # ── Household features ──────────────────────────────────────────────
    app.router.add_view("/api/household/features", HouseholdFeaturesView)

    # ── Backup (adapter-agnostic) ────────────────────────────────────────
    app.router.add_view("/api/backup/pre_backup", BackupPreView)
    app.router.add_view("/api/backup/post_backup", BackupPostView)
    app.router.add_view("/api/backup/export", BackupExportView)
    app.router.add_view("/api/backup/import", BackupImportView)

    # ── Public spaces ───────────────────────────────────────────────────
    app.router.add_view("/api/public_spaces", PublicSpaceCollectionView)
    app.router.add_view("/api/public_spaces/refresh", PublicSpacesRefreshView)
    app.router.add_view(
        "/api/public_spaces/{space_id}/join-request",
        PublicSpaceJoinRequestView,
    )
    app.router.add_view("/api/public_spaces/{space_id}/hide", PublicSpaceHideView)
    app.router.add_view(
        "/api/public_spaces/blocked_instances/{instance_id}",
        PublicSpaceBlockInstanceView,
    )

    # ── Peer-public-space directory (§D1a) ──────────────────────────────
    app.router.add_view("/api/peer_spaces", PeerSpaceCollectionView)

    # ── Child protection ────────────────────────────────────────────────
    app.router.add_view("/api/cp/users/{username}/protection", CPProtectionView)
    app.router.add_view("/api/cp/users/{minor_id}/guardians", CPGuardiansView)
    app.router.add_view(
        "/api/cp/users/{minor_id}/guardians/{guardian_id}",
        CPGuardiansView,
    )
    app.router.add_view(
        "/api/cp/minors/{minor_id}/blocks/{blocked_id}",
        CPBlockView,
    )
    app.router.add_view(
        "/api/cp/minors/{minor_id}/blocks",
        CPBlockCollectionView,
    )
    app.router.add_view(
        "/api/cp/minors/{minor_id}/spaces",
        CPSpaceCollectionView,
    )
    app.router.add_view(
        "/api/cp/minors/{minor_id}/spaces/{space_id}/kick",
        CPKickView,
    )
    app.router.add_view(
        "/api/cp/minors/{minor_id}/conversations",
        CPConversationCollectionView,
    )
    app.router.add_view(
        "/api/cp/minors/{minor_id}/dm-contacts",
        CPDmContactCollectionView,
    )
    app.router.add_view("/api/cp/minors", CPMinorsForGuardianView)
    app.router.add_view("/api/cp/spaces/{space_id}/age-gate", CPAgeGateView)
    app.router.add_view("/api/cp/minors/{minor_id}/audit-log", CPAuditLogView)
    app.router.add_view(
        "/api/cp/minors/{minor_id}/membership-audit",
        CPMembershipAuditView,
    )

    # ── Polls ───────────────────────────────────────────────────────────
    app.router.add_view("/api/posts/{id}/poll", PollSummaryView)
    app.router.add_view("/api/posts/{id}/poll/vote", PollVoteView)
    app.router.add_view("/api/posts/{id}/poll/close", PollCloseView)
    app.router.add_view(
        "/api/posts/{id}/schedule-poll",
        SchedulePollCollectionView,
    )
    app.router.add_view(
        "/api/schedule-polls/{id}/finalize",
        SchedulePollFinalizeView,
    )
    app.router.add_view("/api/schedule-polls/{id}/respond", SchedulePollRespondView)
    app.router.add_view(
        "/api/schedule-polls/{id}/slots/{slot_id}/response",
        SchedulePollSlotResponseView,
    )
    app.router.add_view("/api/schedule-polls/{id}/summary", SchedulePollSummaryView)
    # ── Space-scoped polls + schedule polls ────────────────────────────
    app.router.add_view(
        "/api/spaces/{id}/posts/{pid}/poll",
        SpacePollSummaryView,
    )
    app.router.add_view(
        "/api/spaces/{id}/posts/{pid}/poll/vote",
        SpacePollVoteView,
    )
    app.router.add_view(
        "/api/spaces/{id}/posts/{pid}/poll/close",
        SpacePollCloseView,
    )
    app.router.add_view(
        "/api/spaces/{id}/posts/{pid}/schedule-poll",
        SpaceSchedulePollCollectionView,
    )
    app.router.add_view(
        "/api/spaces/{id}/schedule-polls/{pid}/respond",
        SpaceSchedulePollRespondView,
    )
    app.router.add_view(
        "/api/spaces/{id}/schedule-polls/{pid}/slots/{slot_id}/response",
        SpaceSchedulePollSlotResponseView,
    )
    app.router.add_view(
        "/api/spaces/{id}/schedule-polls/{pid}/finalize",
        SpaceSchedulePollFinalizeView,
    )
    app.router.add_view(
        "/api/spaces/{id}/schedule-polls/{pid}/summary",
        SpaceSchedulePollSummaryView,
    )

    # ── Reports (user-filed) ───────────────────────────────────────────
    app.router.add_view("/api/reports", ReportCollectionView)
    app.router.add_view("/api/admin/reports", AdminReportCollectionView)
    app.router.add_view("/api/admin/reports/{id}/resolve", AdminReportResolveView)

    # ── HA user sync (admin only — 501 in standalone mode) ────────────
    app.router.add_view("/api/admin/ha-users", HaUsersCollectionView)
    app.router.add_view(
        "/api/admin/ha-users/{username}/provision",
        HaUserProvisionView,
    )

    # ── Calendar export (iCal) ─────────────────────────────────────────
    from .calendar_export import CalendarExportView

    app.router.add_view("/api/calendar/{calendar_id}/export.ics", CalendarExportView)

    # ── Adapter-provided routes ────────────────────────────────────────
    adapter = app.get(K.platform_adapter_key)
    if adapter is not None:
        for path, view_cls in adapter.get_extra_routes():
            app.router.add_view(path, view_cls)


__all__ = ["setup_routes"]
