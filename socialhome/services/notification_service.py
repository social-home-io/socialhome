"""Notification service — event-driven notification dispatch (§17.2).

Subscribes to :class:`DomainEvent` types via the :class:`EventBus` and
creates :class:`Notification` entries in the notification repo for the
relevant users. The route layer reads these via ``GET /api/notifications``
and the bell-badge via ``GET /api/notifications/unread-count``.

Push delivery (HA mobile notifications, ntfy, etc.) is out of scope for
this first service slice — it will ride on top of this same bus wiring
when the platform adapter's push API is ready. The notification row
itself is the persistence layer; push is fire-and-forget on top.

**Which events produce notifications:**

| Event                 | Who is notified                       | Title pattern                                  |
|----------------------|---------------------------------------|-------------------------------------------------|
| PostCreated          | All active household members          | "{author} posted"                              |
| CommentAdded         | Household users (feed) / space members (space comment, level + mentions as below) | "{commenter} commented on a post" / "{author} commented in {space}" |
| TaskAssigned         | Each assignee (not the assigner)      | "You were assigned: {task title}"              |
| TaskDeadlineDue      | All assignees                         | "Task due today: {task title}"                 |
| SpacePostCreated     | Space members with notifications on   | "{author} posted in {space}"                   |
| … mentioning a member | That member (level all / mentions)   | "{author} mentioned you in {space}"            |
| PostEdited / CommentUpdated (space) | Only members the edit newly mentions | "{author} mentioned you in {space}" |
| DmMessageCreated     | Other members (group level all / mentions, not muted) | "{sender} messaged you" / "{sender} mentioned you in {chat}" |
| DmMessageUpdated     | Only members the edit newly mentions (groups) | "{sender} mentioned you in {chat}"   |
| SpaceModerationQueued| Space admins                          | "New content pending review in {space}"        |
| SpaceModeration{Approved,Rejected,Expired} | The submitter     | "Your submission in {space} was approved"…      |
| CallRinging          | Each local callee (push only, no row) | "{caller} is calling" / "… in {chat}"           |
| CallMissed           | Each local callee who missed it       | "Missed call from {caller}" / "… in {chat}"     |
| CallRingStopped      | Callees who answered / declined (no row, the ring push is withdrawn) | —            |
| DmConversationCreated (group) | People added to a group chat | "{name} added you to {chat}"                    |
| PollClosed           | Local voters (not the author)         | "{author} closed a poll you voted in"…          |
| ReportResolved       | The local reporter                    | "Your report in {space} was reviewed"           |
| AppInstalled         | Members (not the installer, not the age-gated; once a day per app; in app) | "{name} added {app}" |
| AppRequested         | Admins (a protected asker's guardians) — Needs you, grouped per app | "{name} asked for {app}" |
| AppUpdateAvailable   | Admins — Needs you, one row per app (re-opens for a newer version) | "{app} has an update" |
| AppTurn              | The player whose turn it is (not while the app is on screen; Games, in app by default) — one row per game, re-opens on the next turn | "Your turn with {name} in {app}" |

Body is intentionally omitted for privacy-sensitive events (DMs,
location, UGC content) per §25.3.

Every row passes one audience gate, :meth:`NotificationService._save_notif`:
the recipient is a local user, and never someone who blocked the actor or
whom a guardian block (§CP.F2) separates from them. Each call names its
``actor_user_id`` (``None`` only for a notice nobody caused).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from string import Formatter

from ..domain.apps import (
    PENDING_SESSION_TTL_SECONDS,
    SESSION_INVITED,
    app_age_allows,
    app_view_key,
    is_newer_version,
)
from ..domain.calendar import RSVPStatus
from ..domain.conversation import ConversationType, SystemChatScope, mute_active
from ..domain.events import (
    AppChallengeDeclined,
    AppChallengeEnded,
    AppChallengeReceived,
    AppChallengeWaiting,
    AppEnabledChanged,
    AppGameEnded,
    AppInstalled,
    AppRequestDismissed,
    AppRequested,
    AppTurn,
    AppTurnsSeen,
    AppUninstalled,
    AppUpdateAvailable,
    AppUpdated,
    AppUpdateGone,
    AutoPairRequestAnswered,
    AutoPairRequestIncoming,
    BazaarBidPlaced,
    BazaarListingExpired,
    BazaarOfferAccepted,
    BazaarOfferRejected,
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    CallMissed,
    CallRinging,
    CallRingStopped,
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    DmConversationCreated,
    DmMessageCreated,
    DmMessageDeleted,
    DmMessageUpdated,
    EventReminderDue,
    HighlightFrameReactionChanged,
    HighlightRemoved,
    LocalSpaceInviteCreated,
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
    NotificationCreated,
    NotificationReadChanged,
    PollClosed,
    PostCreated,
    PostDeleted,
    PostEdited,
    PostReactionChanged,
    RemoteJoinRequestApproved,
    RemoteJoinRequestDenied,
    RemoteSchedulePollFinalized,
    RemoteSpaceDissolved,
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    RemoteSpaceInviteReceived,
    RemoteSpaceMemberBanned,
    ReportFiled,
    ReportResolved,
    SchedulePollFinalized,
    SpaceAdminSeedsRetiredAfterRestore,
    SpaceConfigChanged,
    SpaceInviteAnswered,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceLocationFeatureEnabled,
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceModerationApproved,
    SpaceModerationExpired,
    SpaceModerationQueued,
    SpaceModerationRejected,
    SpacePostCreated,
    SpacePostModerated,
    SpaceRemoteSeatLive,
    TaskAssigned,
    TaskCompleted,
    TaskDeadlineDue,
    TaskDeleted,
    UserFollowed,
)
from ..domain.federation import PairingStatus
from ..domain.federation_capabilities import FederationCapability
from ..domain.mention import Mention, MentionType
from ..domain.notification import (
    GROUP_QUIET_DAYS,
    HOUSEHOLD_FEED_ID,
    RETIRED_ACTION_TYPES,
    Notification,
    NotificationActor,
    NotificationHandled,
    NotificationObject,
    NotificationSubject,
    SpaceNotifLevel,
    default_space_notif_level,
    first_params,
    object_key,
    regroup,
)
from ..domain.notification_kinds import (
    NotifDelivery,
    NotifKind,
    SpaceNotifPref,
    notif_kind,
)
from ..domain.post import PostType
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    SETTINGS_AUTHORITY_ROLES,
    WRITER_ROLES,
    SpaceConfigEventType,
    SpaceFeatureAccess,
    SpaceRole,
    join_request_flags,
)
from ..domain.user import User
from ..i18n import Catalog
from ..infrastructure.event_bus import E, EventBus, Handler
from ..repositories._spec import Spec
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.notification_repo import (
    AbstractNotificationRepo,
    GroupKey,
    new_notification,
)
from ..repositories.poll_repo import AbstractPollRepo
from ..repositories.post_repo import AbstractPostRepo
from ..repositories.space_author_profile_repo import AbstractSpaceAuthorProfileRepo
from ..repositories.space_post_repo import AbstractSpacePostRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .notification_delivery_worker import NotificationDeliveryWorker
from .notification_links import (
    HOUSEHOLD_CALENDAR_LINK,
    HOUSEHOLD_FEED_LINK,
    app_about_link,
    app_link,
    bazaar_listing_link,
    household_post_link,
    link_target,
    space_about_link,
    space_calendar_link,
    space_feed_link,
    space_members_link,
    space_moderation_link,
    space_post_link,
    task_link,
)
from .protection_gate import ProtectionGateMixin
from .push_service import PushPayload
from .user_preferences import NOTIF_MUTED_POSTS_KEY, parse_locale, parse_notif_settings

log = logging.getLogger(__name__)

#: One @here per space per this many seconds pages anyone. Persisted: the
#: window is read back from the ``space_here`` rows it wrote, so a restart
#: does not reopen it.
HERE_COOLDOWN_SECONDS = 600

#: D8 — a chat that stayed quiet this long alerts again on its next message
#: (every other grouped row alerts once until it is read).
CHAT_REALERT_SECONDS = 300

#: Chat types: their repeats re-alert after :data:`CHAT_REALERT_SECONDS`
#: of quiet even while unread (D8).
_CHAT_TYPES: frozenset[str] = frozenset({"dm_message", "dm_mention", "bazaar_question"})

#: A missed call replaces the ringing push on the device (same tag), so it
#: always goes out — even as a repeat on a row still unread — or the lock
#: screen would keep saying someone is calling. The service worker swaps
#: it in quietly (``renotify: false``): the ring already buzzed.
_ALWAYS_PUSH_TYPES: frozenset[str] = frozenset({"call_missed"})

#: A new app is announced once a day at most (D13): installing it again
#: within this long — after an uninstall, by another admin — adds nothing.
APP_ADDED_QUIET = timedelta(days=1)

#: The types opening a conversation reads (``mark_read_for_dm``).
_CONVERSATION_TYPES: tuple[str, ...] = (
    "dm_message",
    "dm_mention",
    "bazaar_question",
    "call_missed",
    "chat_added",
)


def _call_tag(link: str) -> str:
    """The device tag a call's pushes share: the ring (``call_incoming``)
    and the missed call that replaces it (``call_missed``'s row tag)."""
    return f"call_missed:{link}"


def _push_tag(note: Notification) -> str:
    """The device tag a row's push carries — one per bell item, so a repeat
    replaces itself and reading it can withdraw it
    (:class:`~socialhome.services.push_withdraw_service.PushWithdrawService`)."""
    return f"{note.type}:{note.link_url or note.id}"


def _conversation_tags(link: str) -> list[str]:
    """Every push a conversation's pushes can carry: its rows'
    (:data:`_CONVERSATION_TYPES`) and the row-less ``dm_message`` push."""
    return list(
        dict.fromkeys(f"{t}:{link}" for t in ("dm_message", *_CONVERSATION_TYPES))
    )


@dataclass(slots=True, frozen=True)
class _SeatPrefs:
    """Service-local: one conversation's per-member bell settings."""

    muted: frozenset[str] = frozenset()
    mentions_only: frozenset[str] = frozenset()
    group: bool = False
    name: str | None = None
    #: Set for a system chat (household / space chat).
    system_scope: SystemChatScope | None = None
    #: A space chat's space and its name (the bell reads "… in {space}").
    space_id: str | None = None
    space_name: str | None = None
    #: An "Ask the seller" thread (v_74): its listing's title, seller and
    #: space — the seller's bell row names the listing.
    listing_title: str | None = None
    listing_seller_user_id: str | None = None
    listing_space_id: str | None = None
    listing_space_name: str | None = None


@dataclass(slots=True, frozen=True)
class _Pending:
    """Service-local: one row that passed the gate and waits for its
    job's batched write (:meth:`NotificationService.run_batched`)."""

    note: Notification
    about: NotificationSubject
    user: User
    actor: NotificationActor | None
    push: bool
    push_params: dict[str, str | None] | None
    flags: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class _DirectPush:
    """Service-local: a push with no row (a chat message, a ring) that
    waits its turn in a delivery job."""

    user: User
    ntype: str
    title: str
    link_url: str | None
    tag: str


@dataclass(slots=True)
class _DeliveryBatch:
    """Service-local: what one delivery job gathered — rows to write in
    one go, and the people it already looked up (``None``: not a local
    user), so a fan-out reads each of them once."""

    items: list[_Pending | _DirectPush] = field(default_factory=list)
    users: dict[str, User | None] = field(default_factory=dict)
    #: Keyed by (user id, space id — ``None`` outside a space).
    names: dict[tuple[str, str | None], str | None] = field(default_factory=dict)
    space_prefs: dict[str, dict[str, SpaceNotifPref]] = field(default_factory=dict)


#: The batch of the delivery job running in this task, if any.
_BATCH: ContextVar[_DeliveryBatch | None] = ContextVar(
    "notification_delivery_batch", default=None
)

#: Where a household-chat bell / push leads: the feed's Chat tab.
HOUSEHOLD_CHAT_LINK = "/?tab=chat"


#: §25.3 / D11 — what a push (lock screen, push service, HA Companion) may
#: say: **who and where only** — names of people, spaces and chats. Never
#: message text, a task title, an event name, an emoji or post text.
#:
#: Bell types whose in-app title embeds such content push one of these
#: titles instead (the bell row keeps its full text). Each entry lists
#: variants, most specific first, as ``(catalog key, English fallback)``;
#: :meth:`NotificationService._push_title` takes the first whose
#: placeholders the handler's ``push_params`` fill.
_PUSH_TITLES: dict[str, tuple[tuple[str, str], ...]] = {
    "task_assigned": (
        (
            "notification.push.task_assigned_in",
            "{name} assigned you a task in {space_name}",
        ),
        ("notification.push.task_assigned_by", "{name} assigned you a task"),
        ("notification.push.task_assigned", "You were assigned a task"),
    ),
    "task_deadline": (("notification.push.task_deadline", "A task is due today"),),
    "task_completed": (
        (
            "notification.push.task_completed_in",
            "{name} completed a task in {space_name}",
        ),
        ("notification.push.task_completed_by", "{name} completed a task"),
        ("notification.push.task_completed", "A task was completed"),
    ),
    "calendar_event_created": (
        (
            "notification.push.calendar_created_in",
            "{name} added an event in {space_name}",
        ),
        (
            "notification.push.calendar_created_by",
            "{name} added an event to your calendar",
        ),
        ("notification.push.calendar_created", "New event"),
    ),
    "calendar_event_updated": (
        (
            "notification.push.calendar_updated_by",
            "{name} changed an event you're going to",
        ),
        ("notification.push.calendar_updated", "An event was changed"),
    ),
    "calendar_event_cancelled": (
        (
            "notification.push.calendar_cancelled_by",
            "{name} cancelled an event you're going to",
        ),
        ("notification.push.calendar_cancelled", "An event was cancelled"),
    ),
    "calendar_reminder": (("notification.push.calendar_reminder", "Event reminder"),),
    "moment_reacted": (
        ("notification.push.moment_reacted", "{name} reacted to your moment"),
    ),
    "highlight_reacted": (
        ("notification.push.highlight_reacted", "{name} reacted to your highlight"),
    ),
    # An invite can come from a household the recipient never paired with:
    # its push names the space only for this household's own invite or a
    # paired household's (D11); otherwise the app shows which one.
    "space_invite": (
        ("notification.push.space_invite_to", "{name} invited you to {space_name}"),
        ("notification.push.space_invite", "{name} invited you to a space"),
    ),
    # "Ask the seller" (v_74): the listing's title is the seller's own
    # words — never on a lock screen; who and where only.
    "bazaar_question": (
        (
            "notification.push.bazaar_question_in",
            "{name} asked about your listing in {space_name}",
        ),
        ("notification.push.bazaar_question", "{name} asked about your listing"),
    ),
    "post_reacted": (
        (
            "notification.push.post_reacted_in",
            "{name} reacted to your post in {space_name}",
        ),
        ("notification.push.post_reacted", "{name} reacted to your post"),
    ),
}

#: Rows that are a decision about the recipient's own content: they stay
#: when the thing itself is deleted (the author still learns what happened).
_DECISION_TYPES: frozenset[str] = frozenset({"post_moderated", "moderation_decided"})

#: The notice that a space ended — it outlives the space's other rows.
_SPACE_ENDED_TYPES: frozenset[str] = frozenset({"space_dissolved"})

#: The push title when no variant of a :data:`_PUSH_TITLES` entry fits.
_PUSH_GENERIC: tuple[str, str] = ("notification.push.generic", "New notification")

#: Bell types whose title already names only people, spaces and chats (or
#: nothing at all): their push carries the bell title as is. Every type the
#: service creates is in exactly one of this set and :data:`_PUSH_TITLES`
#: (a test walks the module), so a new type must decide.
_PUSH_AS_IS: frozenset[str] = frozenset(
    {
        "post_created",
        "comment_added",
        "post_replied",
        "comment_replied",
        "space_post_created",
        "space_comment_added",
        "space_mention",
        "space_here",
        "dm_message",
        "dm_mention",
        "call_incoming",
        "call_missed",
        "chat_added",
        "poll_result",
        "poll_scheduled",
        "report_answer",
        "app_challenge",
        "app_challenge_continue",
        "app_challenge_waiting",
        "app_added",
        "app_challenge_declined",
        "app_game_ended",
        "app_turn",
        "app_request",
        "app_update",
        "bazaar_bid_placed",
        "bazaar_offer_accepted",
        "bazaar_offer_rejected",
        "bazaar_listing_sold",
        "bazaar_listing_expired",
        "moderation_pending",
        "space_report",
        "moderation_unavailable",
        "moderation_decided",
        "post_moderated",
        "space_member_joined",
        "space_dissolved",
        "space_admins_reconfirm",
        "space_join_requested",
        "space_join_approved",
        "space_ownership_transferred",
        "space_join_denied",
        "space_remote_invite_accepted",
        "space_remote_invite_declined",
        "pairing_request",
        "moment_replied",
        "user_followed",
        "space_location_enabled",
    }
)


def _conversation_link(
    conversation_id: str,
    scope: SystemChatScope | None,
    space_id: str | None = None,
) -> str:
    """The SPA path a conversation's bell / push opens: the feed's Chat
    tab for the household chat, the space's Chat section for a space chat,
    the DM thread otherwise."""
    if scope is SystemChatScope.HOUSEHOLD:
        return HOUSEHOLD_CHAT_LINK
    if scope is SystemChatScope.SPACE and space_id:
        return f"/spaces/{space_id}/chat"
    return f"/dms/{conversation_id}"


#: English names of the access-levelled features (catalog fallback).
_FEATURE_FALLBACK: dict[str, str] = {
    "pages": "Pages",
    "tasks": "Tasks",
    "stickies": "Sticky notes",
    "calendar": "Calendar events",
}


def _as_utc(stamp: str | datetime | None) -> datetime | None:
    """*stamp* (a UTC time — ISO, naive SQLite, or a datetime) as an aware
    datetime; ``None`` when missing or unreadable."""
    if stamp is None:
        return None
    try:
        when = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _quiet_for(
    stamp: str | datetime | None,
    seconds: int,
    *,
    until: str | datetime | None = None,
) -> bool:
    """Whether at least *seconds* passed from *stamp* to *until* (default:
    now). An unreadable stamp counts as quiet, so a doubt alerts rather
    than swallowing a push."""
    since = _as_utc(stamp)
    if since is None:
        return True
    end = _as_utc(until) or datetime.now(UTC)
    return end - since >= timedelta(seconds=seconds)


def _reply_kind(
    user_id: str,
    *,
    post_author: str | None,
    parent_author: str | None,
    in_space: bool = False,
) -> tuple[str, str, str] | None:
    """``(type, catalog key, English fallback)`` when a new comment is a
    reply *to* ``user_id`` (D6): an answer to their comment, else a comment
    on their post; ``None`` when it is neither."""
    if parent_author and user_id == parent_author:
        if in_space:
            return (
                "comment_replied",
                "notification.space.comment.replied",
                "{author} replied to your comment in {space_name}",
            )
        return (
            "comment_replied",
            "notification.comment.replied",
            "{author} replied to your comment",
        )
    if post_author and user_id == post_author:
        if in_space:
            return (
                "post_replied",
                "notification.space.post.replied",
                "{author} commented on your post in {space_name}",
            )
        return (
            "post_replied",
            "notification.post.replied",
            "{author} commented on your post",
        )
    return None


def _group_since() -> str:
    """The oldest last activity a row may have and still take a new event
    into its group (D7: :data:`GROUP_QUIET_DAYS`)."""
    return (datetime.now(UTC) - timedelta(days=GROUP_QUIET_DAYS)).isoformat()


def _grouped(
    note: Notification,
    group: Notification | None,
    about: NotificationSubject,
    actor: NotificationActor | None,
    push: bool,
    flags: tuple[str, ...],
) -> tuple[Notification, bool, bool]:
    """``(row to write, reopened, silent)`` for *note* joining *group*.

    No group → *note* starts one. Else (D7) the group's row takes the new
    title, link and activity, comes back unread, and its params move on
    (:func:`regroup`); a read row **re-opens**. Silent (D8): a repeat on a
    row still unread — the badge already counts it and the device already
    buzzed — unless the type always pushes or a chat went quiet for
    :data:`CHAT_REALERT_SECONDS`; and anything with ``push`` off."""
    if group is None:
        saved = replace(
            note,
            object_key=about.object_key,
            space_id=about.space_id,
            params=first_params(actor, space_name=about.space_name, flags=flags),
        )
        return saved, False, not push
    saved = replace(
        group,
        title=note.title,
        body=note.body,
        link_url=note.link_url,
        created_at=note.created_at,
        read_at=None,
        space_id=about.space_id or group.space_id,
        params=regroup(
            group.params,
            actor,
            reopen=group.is_read,
            space_name=about.space_name,
            flags=flags,
        ),
    )
    reopened = group.is_read
    repeat = (
        not reopened
        and saved.type not in _ALWAYS_PUSH_TYPES
        and not (
            saved.type in _CHAT_TYPES
            and _quiet_for(group.created_at, CHAT_REALERT_SECONDS)
        )
    )
    return saved, reopened, repeat or not push


def _beyond_the_circle(recipient, received_via: str | None, hop_count: int) -> bool:
    """§CP.R (decision 14): whether something that reached a **protected**
    *recipient* came from beyond its circle — its household and paired
    households — i.e. relayed over the mesh (hop ≥ 2) or by a GFS. Such a
    thing never reaches a protected account as a notification naming a
    stranger. Always ``False`` for an unprotected recipient."""
    if not getattr(recipient, "child_protection_enabled", False):
        return False
    return received_via == "gfs" or hop_count > 1


class NotificationService(ProtectionGateMixin):
    """Creates notification-centre entries in response to domain events.

    Call :meth:`wire` once during app startup to bind the handlers to the
    event bus. The handler methods are public so integration tests can
    invoke them directly without going through the bus.

    When constructed with ``i18n`` (a :class:`~socialhome.i18n.Catalog`),
    notification titles are translated to each recipient's locale; when
    ``i18n`` is ``None`` the legacy English-only titles are used so
    existing tests stay green.
    """

    __slots__ = (
        "_adapter",
        "_app_catalog",
        "_apps",
        "_author_profiles",
        "_bus",
        "_calendar_repo",
        "_child_protection",
        "_convos",
        "_i18n",
        "_instances",
        "_listings",
        "_notifs",
        "_pair_inbox",
        "_personal_calendar_repo",
        "_polls",
        "_posts",
        "_push",
        "_remote_members",
        "_reports",
        "_space_polls",
        "_space_posts",
        "_spaces",
        "_users",
        "_withdrawals",
        "_worker",
        "_ws_manager",
    )

    def __init__(
        self,
        notification_repo: AbstractNotificationRepo,
        user_repo: AbstractUserRepo,
        space_repo: AbstractSpaceRepo,
        bus: EventBus,
        *,
        i18n: Catalog | None = None,
        conversation_repo: AbstractConversationRepo | None = None,
    ) -> None:
        self._notifs = notification_repo
        #: Per-member conversation mutes (``on_dm_message_created``).
        #: Without it no conversation reads as muted.
        self._convos = conversation_repo
        self._users = user_repo
        #: §CP.F2 — nobody hears from someone a guardian block separates
        #: them from (mentions, @here, DMs, reactions).
        self._child_protection = None
        self._spaces = space_repo
        self._bus = bus
        self._i18n = i18n
        # Household and space posts — optional; they name a comment's post
        # author, the comment it answers and who else commented (D6).
        # Without them a comment is an in-app row for its audience only.
        self._posts: AbstractPostRepo | None = None
        self._space_posts: AbstractSpacePostRepo | None = None
        self._push = None  # attach_push_service(PushService)
        self._adapter = None  # attach_platform_adapter(PlatformAdapter)
        # attach_push_withdraw(PushWithdrawService) — takes a push off the
        # device once its row is read / its item handled / its ring ends.
        self._withdrawals = None
        self._calendar_repo = None  # attach_calendar_repo(...) Phase D
        # Personal calendars (per-user). Used by ``on_calendar_event_created``
        # to dispatch the notification audience: a personal-calendar event
        # only notifies the calendar's owner (and not the creator
        # themselves). Space calendar events fall through to
        # ``_spaces.list_members`` instead. Optional — without it the
        # handler degrades to "no personal notifications", which is safer
        # than the old "fan to every household member" behavior.
        self._personal_calendar_repo = None
        # WebSocketManager — optional. Used by :meth:`on_dm_message_created`
        # to skip the bell row + push when the recipient has the DM
        # thread open in any of their tabs. Without it the service
        # degrades to the pre-fix behaviour (always notify).
        self._ws_manager = None
        # The app catalog's held copy — optional; names an app in the
        # reader's language on its rows (else the install's own names).
        self._app_catalog = None
        # Remote seats per household — optional; without it the
        # "Reviewed isn't available here any more" warning is skipped.
        self._remote_members = None
        # Paired households' advertised ``proto_version`` — optional; it
        # limits that warning to households below v_43 (federated
        # moderation). Without it every new household warns.
        self._instances = None
        # Reports — optional; it tells when a space's last open report was
        # handled, so every authority's "New report" row reads as read.
        self._reports = None
        # The auto-pair inbox — optional; "Needs you" checks a pairing
        # request still waits there (it lives in memory and expires).
        self._pair_inbox = None
        # Household and space polls — optional; they name who voted, so a
        # closed poll tells its voters (``poll_result``).
        self._polls: AbstractPollRepo | None = None
        self._space_polls: AbstractPollRepo | None = None
        # Runs the handlers off the publisher's path — optional; without
        # it each handler runs inline in the publisher (still batched).
        self._worker: NotificationDeliveryWorker | None = None
        # Game invites — optional; "Needs you" checks a game invite still
        # waits. Without it such a row stays pinned until it is answered.
        self._apps = None
        # Space author profiles a GFS follower holds — optional; the last
        # place a space's author from a household this one never met is
        # named (:meth:`_held_name`).
        self._author_profiles: AbstractSpaceAuthorProfileRepo | None = None
        # Bazaar listings — optional; an "Ask the seller" thread (v_74)
        # gives its seller a ``bazaar_question`` row naming the listing.
        # Without it such a message pushes like any 1:1.
        self._listings = None

    def attach_bazaar_repo(self, bazaar_repo) -> None:
        """Wire the bazaar listings ("Ask the seller" rows)."""
        self._listings = bazaar_repo

    def attach_post_repos(
        self,
        post_repo: AbstractPostRepo | None,
        space_post_repo: AbstractSpacePostRepo | None,
    ) -> None:
        """Wire the household and space post repos (D6 comment audience,
        D13 post reactions)."""
        self._posts = post_repo
        self._space_posts = space_post_repo

    def attach_poll_repos(
        self,
        poll_repo: AbstractPollRepo | None,
        space_poll_repo: AbstractPollRepo | None,
    ) -> None:
        """Wire the household and space poll repos (``on_poll_closed``)."""
        self._polls = poll_repo
        self._space_polls = space_poll_repo

    def attach_delivery_worker(self, worker: NotificationDeliveryWorker) -> None:
        """Run the bus handlers on *worker* (call before :meth:`wire`)."""
        self._worker = worker

    def attach_pair_inbox(self, inbox) -> None:
        """Wire the auto-pair inbox (:meth:`pending_actions`)."""
        self._pair_inbox = inbox

    def attach_app_repo(self, app_repo) -> None:
        """Wire the app registry and its waiting game invites
        (:meth:`pending_actions`)."""
        self._apps = app_repo

    def attach_app_catalog(self, catalog) -> None:
        """Wire the app catalog's held copy (``cached_entry``): every app
        row names the app in the reader's language (:meth:`_app_name`).
        Never fetched for a row; without it (or before the first answer)
        the installed bundle's own names are used."""
        self._app_catalog = catalog

    def attach_report_repo(self, report_repo) -> None:
        """Wire the report repo (``on_report_resolved``)."""
        self._reports = report_repo

    def attach_author_profile_repo(
        self, author_profiles: AbstractSpaceAuthorProfileRepo
    ) -> None:
        """Wire the space author profiles (v_60) — names for a space's
        authors this household holds no roster seat for."""
        self._author_profiles = author_profiles

    def attach_remote_member_repo(self, remote_member_repo) -> None:
        """Wire the remote-seat mirror (``on_remote_seat_live``)."""
        self._remote_members = remote_member_repo

    def attach_federation_repo(self, federation_repo) -> None:
        """Wire the paired households' versions (``on_remote_seat_live``)."""
        self._instances = federation_repo

    def attach_push_service(self, push_service) -> None:
        """Attach a :class:`PushService` to fan out Web Push alongside the
        in-app notification rows. Safe to call once; subsequent calls
        replace the previous reference.
        """
        self._push = push_service

    def attach_push_withdraw(self, withdrawals) -> None:
        """Attach the :class:`PushWithdrawService`: what goes out is
        remembered, and what was read or handled comes back off the
        devices."""
        self._withdrawals = withdrawals

    def _withdraw(self, user_id: str, tags: Iterable[str]) -> None:
        """Take these pushes off *user_id*'s devices (coalesced; only the
        ones actually pushed go out)."""
        if self._withdrawals is not None:
            self._withdrawals.withdraw(user_id, tags)

    def attach_calendar_repo(self, calendar_repo) -> None:
        """Wire :class:`AbstractSpaceCalendarRepo` so update-push handlers
        can resolve the affected RSVP cohort. Optional — without it the
        update-push handler is a no-op (Phase D)."""
        self._calendar_repo = calendar_repo

    def attach_personal_calendar_repo(self, calendar_repo) -> None:
        """Wire :class:`AbstractCalendarRepo` (the personal-calendar repo)
        so ``on_calendar_event_created`` can resolve the owning user of
        the event's calendar. Without it personal-calendar events emit
        no notifications (the broader space-member branch still works
        via ``_spaces``).
        """
        self._personal_calendar_repo = calendar_repo

    def attach_platform_adapter(self, adapter) -> None:
        """Attach the :class:`PlatformAdapter` so push notifications also
        reach HA mobile apps (`notify.mobile_app_<user>`) or the
        standalone ``notify_endpoint``. Optional — if both this adapter
        and the Web Push ``PushService`` are wired, we fan out to both
        so the user gets the notification on every registered surface.
        """
        self._adapter = adapter

    def attach_ws_manager(self, ws_manager) -> None:
        """Attach the :class:`WebSocketManager` so DM notifications can
        skip recipients who have the thread open right now. See the
        docstring on :meth:`on_dm_message_created` for the rationale.
        """
        self._ws_manager = ws_manager

    async def _save_notif(
        self,
        note: Notification,
        *,
        about: NotificationSubject,
        actor_user_id: str | None = None,
        actor_name: str | None = None,
        withheld: frozenset[str] | None = None,
        push: bool = True,
        push_params: dict[str, str | None] | None = None,
        flags: tuple[str, ...] = (),
    ) -> Notification | None:
        """The one gate every notification passes, then persist + publish
        ``NotificationCreated`` + fire title-only pushes to every registered
        surface (Web Push + HA mobile app). Returns the saved row, or
        ``None`` when the gate refused it — and inside a delivery job
        (:meth:`run_batched`), where the row is written with the rest of
        the job's rows once its handler is done.

        The gate — so a new type inherits it rather than copying it:

        * the recipient is a **local user** — a remote or unknown id never
          gets a row (``notifications.user_id`` FKs into ``users``; a
          failing insert used to abort the rest of a fan-out);
        * ``actor_user_id`` is who caused it (a poster, assigner, joiner,
          bidder…; ``None`` only for a notice nobody caused, such as a
          deadline or an expiry). A recipient who blocked the actor, or
          whom a guardian block (§CP.F2) separates from the actor, gets
          nothing. A fan-out that already holds :meth:`_withheld_from` for
          the actor passes it as ``withheld`` to spare a lookup per row.

        Per §25.3 we never put the body on the wire; subscribers
        translate the title and tap-open the app to see the full row.
        The push title follows D11 (who and where only, see
        :meth:`_push_title`); ``push_params`` names the people and the
        space a content-free push title may mention.

        ``about`` names the thing the row is about and its space (0129) —
        required, so a new type can't forget it. One row per (recipient,
        type, thing) while it stays active (D7): an event for a thing whose
        row saw activity in the last :data:`GROUP_QUIET_DAYS` days updates
        that row — fresh title and link, to the top, unread again, the
        counts and people in its params moved on (:func:`regroup`) — and
        only after that many quiet days does a fresh row start. A row that
        was still unread keeps the unread count as is; a read one re-opens
        and every device hears the new count. ``actor_name`` is the name
        the row remembers for ``actor_user_id`` (looked up when not given).

        Push (D8): the event that starts or re-opens a row alerts; a repeat
        on a row still unread is **silent** — no second push, no second
        Home Assistant event — so one thing buzzes once until it is read. A
        chat row (:data:`_CHAT_TYPES`) alerts again after
        :data:`CHAT_REALERT_SECONDS` of quiet. ``push=False`` writes an
        in-app row only: silent too.

        ``flags`` say why a "Needs you" item can't be decided with one tap
        (:data:`~socialhome.domain.notification.JOIN_REQUEST_FLAGS`).

        What the recipient chose (D4, D10, :meth:`_heard`) comes last: a
        muted post or a kind set to Off → no row; a kind set to In app → a
        row, no push. Reactions and activity in spaces (D5, D6, D13) are In
        app by default — a handler never decides that itself, so someone
        who sets them to Push gets the push.
        """
        user = await self._local_user(note.user_id)
        if user is None:
            return None  # not a local user: their own household tells them
        if actor_user_id and actor_user_id != note.user_id:
            if withheld is not None:
                hidden = note.user_id in withheld
            else:
                hidden = await self._hidden_from(note.user_id, actor_user_id)
            if hidden:
                return None
        heard = await self._heard(user, note.type, about)
        if heard is NotifDelivery.OFF:
            return None
        if heard is NotifDelivery.IN_APP:
            push = False
        actor = (
            NotificationActor(
                actor_user_id,
                actor_name
                or await self._held_name(actor_user_id, space_id=about.space_id),
            )
            if actor_user_id
            else None
        )
        batch = _BATCH.get()
        if batch is not None:
            # A delivery job: the row joins the job's one batched write.
            batch.items.append(
                _Pending(note, about, user, actor, push, push_params, flags)
            )
            return None
        group = await self._notifs.latest_in_group(
            user_id=note.user_id,
            type=note.type,
            object_key=about.object_key,
            since=_group_since(),
        )
        saved, reopened, silent = _grouped(note, group, about, actor, push, flags)
        if group is None:
            await self._notifs.save(saved)
        else:
            await self._notifs.update_group(saved)
        await self._publish_created(
            saved, bumped=group is not None, reopened=reopened, silent=silent
        )
        if reopened:
            await self._announce_unread(saved.user_id)
        if not silent:
            await self._push_row(saved, user, push_params)
        return saved

    async def run_batched(self, handler: Handler[E], event: E) -> None:
        """Run one bus handler as **one delivery job** (the
        :class:`~socialhome.services.notification_delivery_worker.NotificationDeliveryWorker`
        runs it off the publisher's path).

        Every row the handler saves passes :meth:`_save_notif`'s gate as it
        goes, but is written when the handler is done: one group lookup and
        one transaction for the whole fan-out (:meth:`_flush`), then the
        ``NotificationCreated`` frames and the pushes in the order the
        handler made them. The people a fan-out names are looked up once
        (:meth:`_prefetch_users`). Grouping (D7) and silent repeats (D8)
        come out exactly as one save at a time would make them. A handler
        that fails still writes what it gathered before failing."""
        if _BATCH.get() is not None:
            await handler(event)
            return
        batch = _DeliveryBatch()
        token = _BATCH.set(batch)
        try:
            await handler(event)
        finally:
            _BATCH.reset(token)
            await self._flush(batch)

    async def _flush(self, batch: _DeliveryBatch) -> None:
        """Write one job's rows (:meth:`run_batched`): one group lookup and
        one transaction, then the ``NotificationCreated`` frames, the new
        unread counts of re-opened rows, and the pushes — each in the order
        the handler made them."""
        rows = [i for i in batch.items if isinstance(i, _Pending)]
        groups: dict[GroupKey, Notification] = (
            await self._notifs.latest_in_groups(
                {(p.note.user_id, p.note.type, p.about.object_key) for p in rows},
                since=_group_since(),
            )
            if rows
            else {}
        )
        fresh: dict[str, Notification] = {}
        changed: dict[str, Notification] = {}
        done: dict[int, tuple[Notification, bool, bool, bool]] = {}
        for item in rows:
            key = (item.note.user_id, item.note.type, item.about.object_key)
            group = groups.get(key)
            saved, reopened, silent = _grouped(
                item.note, group, item.about, item.actor, item.push, item.flags
            )
            # A second event for the same group in this job joins the row
            # the first one made, as it would one save at a time.
            groups[key] = saved
            if group is None or saved.id in fresh:
                fresh[saved.id] = saved
            else:
                changed[saved.id] = saved
            done[id(item)] = (saved, group is not None, reopened, silent)
        failed = await self._notifs.save_batch(
            list(fresh.values()), list(changed.values())
        )
        landed = {k: v for k, v in done.items() if v[0].id not in failed}
        for saved, bumped, reopened, silent in landed.values():
            await self._publish_created(
                saved, bumped=bumped, reopened=reopened, silent=silent
            )
        reopened_users = list(
            dict.fromkeys(v[0].user_id for v in landed.values() if v[2])
        )
        if reopened_users:
            counts = await self._notifs.count_unread_many(reopened_users)
            for user_id in reopened_users:
                await self._bus.publish(
                    NotificationReadChanged(
                        user_id=user_id, unread_count=counts.get(user_id, 0)
                    )
                )
        for entry in batch.items:
            if isinstance(entry, _DirectPush):
                await self._push_now(
                    entry.user,
                    ntype=entry.ntype,
                    title=entry.title,
                    link_url=entry.link_url,
                    tag=entry.tag,
                )
            elif (row := landed.get(id(entry))) is not None and not row[3]:
                await self._push_row(row[0], entry.user, entry.push_params)

    async def _publish_created(
        self, saved: Notification, *, bumped: bool, reopened: bool, silent: bool
    ) -> None:
        await self._bus.publish(
            NotificationCreated(
                user_id=saved.user_id,
                notification_id=saved.id,
                type=saved.type,
                title=saved.title,
                link_url=saved.link_url,
                bumped=bumped,
                reopened=reopened,
                silent=silent,
                object_key=saved.object_key,
                space_id=saved.space_id,
                params=saved.params,
            )
        )

    async def _push_row(
        self,
        saved: Notification,
        user: User,
        push_params: dict[str, str | None] | None,
    ) -> None:
        await self._push_now(
            user,
            ntype=saved.type,
            title=self._push_title(saved, user, push_params),
            link_url=saved.link_url,
            # One tag per bell item: a repeat replaces itself on the device;
            # a different item never silently replaces it.
            tag=_push_tag(saved),
        )

    async def _heard(
        self, user, ntype: str, about: NotificationSubject
    ) -> NotifDelivery:
        """How *user* hears about a *ntype* row about *about* (D4, D10).

        * The post it is about is muted (a row's "Mute this post") →
          ``OFF``. Activity on it keeps the mute alive: its stamp moves (at
          most daily) so it lapses only after
          :data:`~socialhome.domain.notification_kinds.MUTED_POST_QUIET_DAYS`
          quiet days.
        * Else the choice for the type's kind (Settings → Notifications) —
          theirs, or the kind's default: ``PUSH`` (a row and a push),
          ``IN_APP`` (a row only) or ``OFF``. A type no kind files keeps
          the handler's own choice (``PUSH``)."""
        settings = parse_notif_settings(user.preferences_json)
        now = datetime.now(UTC)
        key = about.object_key
        if settings.post_muted(key, now=now):
            if settings.needs_refresh(key, now=now):
                try:
                    await self._users.touch_preference_entry(
                        user.user_id, NOTIF_MUTED_POSTS_KEY, key, now.isoformat()
                    )
                except Exception:
                    log.debug("muted post refresh failed", exc_info=True)
            return NotifDelivery.OFF
        kind = notif_kind(ntype, about.space_id)
        return settings.delivery(kind) if kind is not None else NotifDelivery.PUSH

    async def _send_push(
        self, user, *, ntype: str, title: str, link_url: str | None, tag: str
    ) -> None:
        """A push with no row (a chat message, a ring). In a delivery job it
        waits its turn behind the rows saved before it (:meth:`_flush`), so
        pushes leave in the order the handler made them."""
        push = _DirectPush(user, ntype, title, link_url, tag)
        batch = _BATCH.get()
        if batch is not None:
            batch.items.append(push)
            return
        await self._push_now(user, ntype=ntype, title=title, link_url=link_url, tag=tag)

    async def _push_now(
        self, user, *, ntype: str, title: str, link_url: str | None, tag: str
    ) -> None:
        """One title-only push to every surface *user* registered: Web Push
        and the platform adapter (HA mobile app / standalone inbox)."""
        if self._withdrawals is not None:
            self._withdrawals.note_pushed(user.user_id, tag)
        if self._push is not None:
            try:
                await self._push.push_to_user(
                    user.user_id,
                    PushPayload(title=title, click_url=link_url, tag=tag),
                )
            except Exception as exc:
                log.debug("web push fan-out failed: %s", exc, exc_info=True)
        if self._adapter is not None:
            try:
                await self._adapter.send_push(
                    user,
                    title,
                    "",
                    data={"type": ntype, "url": link_url, "tag": tag},
                )
            except Exception as exc:
                log.debug("platform push fan-out failed: %s", exc, exc_info=True)

    def _push_title(
        self, saved, user, params: dict[str, str | None] | None = None
    ) -> str:
        """The title a push carries for *saved* (§25.3, D11: who and where).

        A bell whose title embeds user content — a task title, an event
        summary, a reaction emoji — pushes the first :data:`_PUSH_TITLES`
        variant whose placeholders *params* fill, in the recipient's
        language; the in-app row keeps the full text. A type in
        :data:`_PUSH_AS_IS` names only people, spaces and chats and goes
        out as is. Anything else (a type nobody classified) pushes the
        generic title — never its bell text.
        """
        if saved.type in _PUSH_AS_IS:
            return saved.title
        return self._content_free_title(saved.type, self._locale(user), params)

    def _content_free_title(
        self, ntype: str, locale: str | None, params: dict[str, str | None] | None
    ) -> str:
        """*ntype*'s who-and-where title (:data:`_PUSH_TITLES`) in *locale*:
        the first variant whose placeholders *params* fill, else the
        generic one."""
        filled = {k: v for k, v in (params or {}).items() if v}
        for key, fallback in _PUSH_TITLES.get(ntype, ()):
            fields = {f for _, f, _, _ in Formatter().parse(fallback) if f}
            if fields <= filled.keys():
                return self._t(
                    key,
                    locale=locale,
                    fallback=fallback,
                    **{f: filled[f] for f in fields},
                )
        key, fallback = _PUSH_GENERIC
        return self._t(key, locale=locale, fallback=fallback)

    def _someone(self, locale: str | None) -> str:
        """ "Someone" in the recipient's language — for a person this
        household holds no name for."""
        return self._t("notification.someone", locale=locale, fallback="Someone")

    def _t(self, key: str, *, locale: str | None, fallback: str, **fmt) -> str:
        if self._i18n is None:
            try:
                return fallback.format(**fmt)
            except KeyError, IndexError:
                return fallback
        translated = self._i18n.gettext(key, locale=locale, **fmt)
        # ``Catalog.gettext`` returns the raw key when the locale catalog
        # is missing the translation.  That looked like
        # ``notification.calendar.updated`` in the inbox UI — clearly
        # not the warm copy we wanted.  Detect the miss and fall back
        # to the formatted fallback so the row reads naturally even
        # before the catalog catches up.
        if translated == key:
            try:
                return fallback.format(**fmt)
            except KeyError, IndexError:
                return fallback
        return translated

    @staticmethod
    def _locale(user) -> str | None:
        """The language a row / push for *user* is written in: the one they
        picked in the app (``preferences.locale``, what the SPA saves — it
        never writes ``users.locale``), else the ``users.locale`` column,
        else the catalog's default."""
        if user is None:
            return None
        return parse_locale(getattr(user, "preferences_json", None)) or (
            getattr(user, "locale", None) or None
        )

    def _subscribe(self, event_type: type[E], handler: Handler[E]) -> None:
        """Subscribe *handler* as one delivery job per event
        (:meth:`run_batched`) — on the delivery worker when one is
        attached, so the publisher only queues the event and returns."""

        async def job(event: E) -> None:
            await self.run_batched(handler, event)

        job.__qualname__ = getattr(handler, "__qualname__", repr(handler))
        self._bus.subscribe(
            event_type,
            self._worker.deferred(job) if self._worker is not None else job,
        )

    def wire(self) -> None:
        """Register all event handlers on the bus. Idempotent (but
        calling twice subscribes twice — callers should call once).
        Attach the delivery worker first (:meth:`attach_delivery_worker`).
        """
        self._subscribe(PostCreated, self.on_post_created)
        self._subscribe(CommentAdded, self.on_comment_added)
        self._subscribe(TaskAssigned, self.on_task_assigned)
        self._subscribe(TaskDeadlineDue, self.on_task_deadline_due)
        self._subscribe(SpacePostCreated, self.on_space_post_created)
        self._subscribe(SpaceModerationQueued, self.on_moderation_queued)
        self._subscribe(ReportFiled, self.on_report_filed)
        self._subscribe(ReportResolved, self.on_report_resolved)
        self._subscribe(SpaceModerationApproved, self.on_moderation_decided)
        self._subscribe(SpaceModerationRejected, self.on_moderation_decided)
        self._subscribe(SpaceModerationExpired, self.on_moderation_decided)
        self._subscribe(SpaceModerationApproved, self.on_moderation_item_handled)
        self._subscribe(SpaceModerationRejected, self.on_moderation_item_handled)
        self._subscribe(SpaceModerationExpired, self.on_moderation_item_handled)
        self._subscribe(SpaceRemoteSeatLive, self.on_remote_seat_live)
        self._subscribe(DmMessageCreated, self.on_dm_message_created)
        self._subscribe(DmMessageDeleted, self.on_dm_message_deleted)
        self._subscribe(DmMessageUpdated, self.on_dm_message_updated)
        self._subscribe(DmConversationCreated, self.on_chat_added)
        self._subscribe(CallRinging, self.on_call_ringing)
        self._subscribe(CallMissed, self.on_call_missed)
        self._subscribe(CallRingStopped, self.on_call_ring_stopped)
        self._subscribe(PollClosed, self.on_poll_closed)
        self._subscribe(SchedulePollFinalized, self.on_schedule_poll_finalized)
        self._subscribe(RemoteSchedulePollFinalized, self.on_schedule_poll_finalized)
        self._subscribe(PostEdited, self.on_post_edited)
        self._subscribe(CommentUpdated, self.on_comment_updated)
        self._subscribe(BazaarBidPlaced, self.on_bazaar_bid_placed)
        self._subscribe(BazaarOfferAccepted, self.on_bazaar_offer_accepted)
        self._subscribe(BazaarOfferRejected, self.on_bazaar_offer_rejected)
        self._subscribe(BazaarListingExpired, self.on_bazaar_listing_expired)
        self._subscribe(CalendarEventCreated, self.on_calendar_event_created)
        self._subscribe(CalendarEventDeleted, self.on_calendar_event_deleted)
        self._subscribe(CalendarEventUpdated, self.on_calendar_event_updated)
        self._subscribe(EventReminderDue, self.on_event_reminder_due)
        self._subscribe(TaskCompleted, self.on_task_completed)
        self._subscribe(SpacePostModerated, self.on_space_post_moderated)
        self._subscribe(SpaceMemberJoined, self.on_space_member_joined)
        self._subscribe(SpaceJoinRequested, self.on_space_join_requested)
        self._subscribe(SpaceJoinApproved, self.on_space_join_approved)
        self._subscribe(SpaceJoinDenied, self.on_space_join_denied)
        self._subscribe(SpaceJoinApproved, self.on_join_request_handled)
        self._subscribe(SpaceJoinDenied, self.on_join_request_handled)
        self._subscribe(RemoteJoinRequestApproved, self.on_remote_join_answered)
        self._subscribe(RemoteJoinRequestDenied, self.on_remote_join_answered)
        self._subscribe(LocalSpaceInviteCreated, self.on_local_space_invite)
        self._subscribe(RemoteSpaceInviteReceived, self.on_remote_space_invite)
        self._subscribe(SpaceInviteAnswered, self.on_space_invite_answered)
        self._subscribe(AutoPairRequestIncoming, self.on_auto_pair_requested)
        self._subscribe(AutoPairRequestAnswered, self.on_auto_pair_answered)
        self._subscribe(SpaceConfigChanged, self.on_space_ownership_transferred)
        self._subscribe(
            RemoteSpaceInviteAccepted,
            self.on_remote_invite_accepted,
        )
        self._subscribe(
            RemoteSpaceInviteDeclined,
            self.on_remote_invite_declined,
        )
        self._subscribe(RemoteSpaceDissolved, self.on_remote_space_dissolved)
        self._subscribe(SpaceAdminSeedsRetiredAfterRestore, self.on_admin_seeds_retired)
        # Momentum (§Momentum) — reactions, replies, and new follows.
        self._subscribe(MomentReactionChanged, self.on_moment_reaction_changed)
        # D13: reactions on posts (household feed and spaces).
        self._subscribe(PostReactionChanged, self.on_post_reaction_changed)
        self._subscribe(MomentCreated, self.on_moment_created)
        self._subscribe(UserFollowed, self.on_user_followed)
        # Highlights — a reaction on your frame (a household member's, or
        # one a paired household's HIGHLIGHT_FRAME_REACTED brought in).
        self._subscribe(
            HighlightFrameReactionChanged, self.on_highlight_reaction_changed
        )
        # Space location feature enabled — nudge members to opt in.
        self._subscribe(
            SpaceLocationFeatureEnabled,
            self.on_space_location_feature_enabled,
        )
        # Social Home Apps — a challenge (APP_SESSION open) arrived.
        self._subscribe(
            AppChallengeReceived,
            self.on_app_challenge_received,
        )
        self._subscribe(AppChallengeWaiting, self.on_app_challenge_waiting)
        self._subscribe(AppChallengeEnded, self.on_app_challenge_ended)
        self._subscribe(AppInstalled, self.on_app_installed)
        self._subscribe(AppChallengeDeclined, self.on_app_challenge_declined)
        self._subscribe(AppGameEnded, self.on_app_game_ended)
        self._subscribe(AppTurn, self.on_app_turn)
        self._subscribe(AppTurnsSeen, self.on_app_turns_seen)
        self._subscribe(AppInstalled, self.on_app_request_answered)
        self._subscribe(AppRequested, self.on_app_requested)
        self._subscribe(AppRequestDismissed, self.on_app_request_answered)
        # An app turned back on answers the asks to turn it on (D10).
        self._subscribe(AppEnabledChanged, self.on_app_request_answered)
        self._subscribe(AppUpdateAvailable, self.on_app_update_available)
        self._subscribe(AppUpdated, self.on_app_update_handled)
        self._subscribe(AppUpdateGone, self.on_app_update_handled)
        self._subscribe(AppUninstalled, self.on_app_update_handled)
        # The thing is gone, or you left its space: so are its rows.
        self._subscribe(PostDeleted, self.on_thing_deleted)
        self._subscribe(CommentDeleted, self.on_thing_deleted)
        self._subscribe(TaskDeleted, self.on_thing_deleted)
        self._subscribe(MomentDeleted, self.on_thing_deleted)
        self._subscribe(HighlightRemoved, self.on_thing_deleted)
        self._subscribe(SpaceMemberLeft, self.on_space_seat_gone)
        self._subscribe(RemoteSpaceMemberBanned, self.on_space_seat_gone)
        self._subscribe(SpaceConfigChanged, self.on_space_dissolved_here)

    # ── Blocks ─────────────────────────────────────────────────────────

    async def _withheld_from(self, actor_user_id: str) -> frozenset[str]:
        """Who never hears about *actor_user_id*'s activity: everyone who
        blocked them (§Privacy — a personal block stays the blocker's own)
        and everyone a guardian block (§CP.F2) separates them from."""
        return await self._guardian_block_counterparts(
            actor_user_id
        ) | await self._users.list_blockers(actor_user_id)

    async def _hidden_from(self, recipient_user_id: str, actor_user_id: str) -> bool:
        """Whether *recipient_user_id* never hears about *actor_user_id* —
        the one-pair form of :meth:`_withheld_from`."""
        return await self._guardian_blocked(
            recipient_user_id, actor_user_id
        ) or await self._users.is_blocked(recipient_user_id, actor_user_id)

    # ── Handlers ───────────────────────────────────────────────────────

    async def on_post_created(self, event: PostCreated) -> None:
        """Every active household member except the author gets an in-app
        row — the household feed is "Everything" (D5): no push for a plain
        new post. The rows group on the feed, not the post
        (``feed:household``): a burst of posts is one row that counts them
        and names who posted, and it opens the feed (``/feed``)."""
        author_id = event.post.author
        users = await self._active_users()
        author = await self._local_user(author_id)
        name = author.display_name if author else None
        blocked = await self._withheld_from(author_id)
        for user in users:
            if user.user_id == author_id or user.user_id in blocked:
                continue
            await self._save_notif(
                new_notification(
                    user_id=user.user_id,
                    type="post_created",
                    title=self._t(
                        "notification.post.created",
                        locale=self._locale(user),
                        fallback="{author} posted",
                        author=name or self._someone(self._locale(user)),
                    ),
                    link_url=HOUSEHOLD_FEED_LINK,
                ),
                about=NotificationSubject.of(
                    NotificationObject.FEED, HOUSEHOLD_FEED_ID
                ),
                actor_user_id=author_id,
                actor_name=name,
                withheld=blocked,
            )

    async def on_comment_added(self, event: CommentAdded) -> None:
        """Who hears about a new comment (D6).

        With a push: the post's author ("commented on your post",
        ``post_replied``), the author of the comment it answers ("replied
        to your comment", ``comment_replied``) and anyone it mentions. In
        the app only (``comment_added`` / ``space_comment_added``, grouped
        per post): others who commented on that post, and everyone at
        "Everything" — the household feed is "Everything" for everyone
        (D5). Nobody else.

        A space comment goes to the space's members only, per their level
        (:meth:`_notify_space_members`); a household user outside the space
        never hears about it."""
        commenter_id = event.comment.author
        post_author, parent_author, commenters = await self._comment_audience(event)
        if event.space_id is not None:
            await self._notify_space_members(
                space_id=event.space_id,
                author_id=commenter_id,
                mentions=event.mentions,
                link=space_post_link(event.space_id, event.post_id, event.comment.id),
                post_id=event.post_id,
                comment_id=event.comment.id,
                generic_type="space_comment_added",
                generic_key="notification.space.comment.added",
                generic_fallback="{author} commented in {space_name}",
                mention_key="notification.space.comment.mention",
                mention_fallback="{author} mentioned you in a comment in {space_name}",
                post_author_id=post_author,
                parent_author_id=parent_author,
                commenter_ids=commenters,
            )
            return
        commenter = await self._local_user(commenter_id)
        name = commenter.display_name if commenter else None
        users = await self._active_users()
        blocked = await self._withheld_from(commenter_id)
        for user in users:
            if user.user_id == commenter_id or user.user_id in blocked:
                continue
            locale = self._locale(user)
            ntype, key, fallback = _reply_kind(
                user.user_id, post_author=post_author, parent_author=parent_author
            ) or (
                "comment_added",
                "notification.comment.added",
                "{author} commented on a post",
            )
            await self._save_notif(
                new_notification(
                    user_id=user.user_id,
                    type=ntype,
                    title=self._t(
                        key,
                        locale=locale,
                        fallback=fallback,
                        author=name or self._someone(locale),
                    ),
                    link_url=household_post_link(event.post_id, event.comment.id),
                ),
                about=NotificationSubject.of(NotificationObject.POST, event.post_id),
                actor_user_id=commenter_id,
                actor_name=name,
                withheld=blocked,
            )

    async def _comment_audience(
        self, event: CommentAdded
    ) -> tuple[str | None, str | None, frozenset[str]]:
        """The people a comment is *for* (D6): its post's author, the author
        of the comment it answers (``parent_id``) — ``None`` when not known
        here — and everyone else with a live comment on the post."""
        repo: AbstractPostRepo | AbstractSpacePostRepo | None = (
            self._space_posts if event.space_id is not None else self._posts
        )
        if repo is None:
            return None, None, frozenset()
        post_author: str | None = None
        if event.space_id is not None and self._space_posts is not None:
            held = await self._space_posts.get(event.post_id)
            post_author = held[1].author if held is not None else None
        elif self._posts is not None:
            post = await self._posts.get(event.post_id)
            post_author = post.author if post is not None else None
        parent_author: str | None = None
        if event.comment.parent_id:
            parent = await repo.get_comment(event.comment.parent_id)
            if parent is not None and not parent.deleted:
                parent_author = parent.author
        commenters = frozenset(await repo.list_comment_author_ids(event.post_id))
        return post_author, parent_author, commenters

    async def on_post_edited(self, event: PostEdited) -> None:
        """A space post edit rings only the members it newly @-mentions
        (``event.new_mentions`` — never those already mentioned, never a
        generic "posted" bell). A newly added ``@here`` pages like on
        create, still under the per-author cooldown."""
        if event.space_id is None or not event.new_mentions:
            return
        await self._notify_space_members(
            space_id=event.space_id,
            author_id=event.post.author,
            mentions=event.new_mentions,
            link=space_post_link(event.space_id, event.post.id),
            post_id=event.post.id,
            generic_type=None,
            generic_key="notification.space.post.created",
            generic_fallback="{author} posted in {space_name}",
            mention_key="notification.space.mention",
            mention_fallback="{author} mentioned you in {space_name}",
        )

    async def on_comment_updated(self, event: CommentUpdated) -> None:
        """Space comment edit — same rule as :meth:`on_post_edited`."""
        if event.space_id is None or not event.new_mentions:
            return
        await self._notify_space_members(
            space_id=event.space_id,
            author_id=event.comment.author,
            mentions=event.new_mentions,
            link=space_post_link(event.space_id, event.post_id, event.comment.id),
            post_id=event.post_id,
            comment_id=event.comment.id,
            generic_type=None,
            generic_key="notification.space.comment.added",
            generic_fallback="{author} commented in {space_name}",
            mention_key="notification.space.comment.mention",
            mention_fallback="{author} mentioned you in a comment in {space_name}",
        )

    async def on_task_assigned(self, event: TaskAssigned) -> None:
        """Notify the assignee (unless they assigned themselves, or they
        blocked whoever assigned them).

        A space task only notifies a current local member of that space —
        the title must never reach a non-member (defence in depth behind
        the service-level assignee check)."""
        assigner = event.assigned_by or event.task.created_by
        if assigner == event.assigned_to:
            return
        if (
            event.space_id is not None
            and await self._spaces.get_member(event.space_id, event.assigned_to) is None
        ):
            return
        recipient = await self._local_user(event.assigned_to)
        locale = self._locale(recipient)
        space_name = await self._space_name(event.space_id)
        await self._save_notif(
            new_notification(
                user_id=event.assigned_to,
                type="task_assigned",
                title=self._t(
                    "notification.task.assigned",
                    locale=locale,
                    fallback="You were assigned: {title}",
                    title=event.task.title,
                ),
                link_url=task_link(event.task, event.space_id),
            ),
            about=NotificationSubject.of(
                NotificationObject.TASK,
                event.task.id,
                space_id=event.space_id,
                space_name=space_name,
            ),
            actor_user_id=assigner,
            push_params={
                "name": (
                    await self._person_name(assigner, locale, space_id=event.space_id)
                    if assigner
                    else None
                ),
                "space_name": space_name,
            },
        )

    async def on_task_deadline_due(self, event: TaskDeadlineDue) -> None:
        """Notify every assignee that a task is due today — a reminder
        nobody caused, so no actor; a remote assignee is skipped by the
        gate (their own household reminds them)."""
        for assignee_id in event.task.assignees:
            recipient = await self._local_user(assignee_id)
            title = self._t(
                "notification.task.deadline_due",
                locale=self._locale(recipient),
                fallback="Task due today: {title}",
                title=event.task.title,
            )
            await self._save_notif(
                new_notification(
                    user_id=assignee_id,
                    type="task_deadline",
                    title=title,
                    # The deadline sweep reads household tasks only.
                    link_url=task_link(event.task),
                ),
                about=NotificationSubject.of(NotificationObject.TASK, event.task.id),
                actor_user_id=None,
            )

    async def on_dm_message_created(self, event: DmMessageCreated) -> None:
        """Push each recipient about a chat message — no Notifications row.

        D3: Chats keeps its own unread badge, so a plain message never
        lands in the Notifications list (it would count twice). It still
        pushes, title only (§25.3): the first unread message of a chat
        alerts, the rest of the burst stays quiet, and a chat that went
        quiet for :data:`CHAT_REALERT_SECONDS` alerts again (D8) — read
        from the conversation itself (:meth:`_chat_alerts`), so nothing is
        stored and a restart changes nothing.

        Being @-mentioned is about *you*: that one still makes a
        ``dm_mention`` row, one per conversation (D7), which
        :meth:`mark_read_for_dm` reads when the thread is opened.

        Active-viewer suppression: when the recipient already has the
        DM thread open in any of their tabs (the SPA emits
        ``{type: 'dm.active', data: {conversation_id}}`` over WS on
        mount), we skip the bell row AND the push for that recipient.
        The message itself still renders via the regular DM broadcast
        path; only the notification noise is gone. Without an attached
        ``WebSocketManager`` we degrade to the pre-fix behaviour
        (always notify) so unit tests that don't wire the manager
        don't have to mock it.

        Muted conversations: a recipient who muted this conversation
        (``conversation_members.muted_until`` still in the future) gets
        neither the bell row nor the push. The message and its unread
        count are untouched; the mute is theirs alone and never federated.

        Group mentions (``event.mentions``, resolved against the
        conversation's seats): a mentioned member gets one distinct
        ``dm_mention`` bell ("{sender} mentioned you in {chat}") instead of
        the ``dm_message`` one; a member at level ``mentions``
        (``conversation_members.notif_level``) is rung only when mentioned.
        A mute wins over both. In a 1:1 a mention is just a message.

        The household chat (a system chat) reads "{sender} in Household
        chat" (localised; no location / content hint) and opens the feed's
        Chat tab (:data:`HOUSEHOLD_CHAT_LINK`) instead of ``/dms/{id}``.
        """
        if not event.recipient_user_ids:
            return
        prefs = await self._seat_prefs(event.conversation_id)
        link = _conversation_link(
            event.conversation_id, prefs.system_scope, prefs.space_id
        )
        about = NotificationSubject.of(
            NotificationObject.CONVERSATION,
            event.conversation_id,
            space_id=prefs.space_id,
            space_name=prefs.space_name,
        )
        muted = prefs.muted
        mentioned = (
            {m.user_id for m in event.mentions if m.user_id} if prefs.group else set()
        )
        blocked = await self._withheld_from(event.sender_user_id)
        for recipient_id in event.recipient_user_ids:
            if recipient_id in blocked:
                continue
            # Notifications.user_id FK's into ``users`` (local accounts
            # only). Remote recipients' rows live in ``remote_users``;
            # they get notified on their *own* household via the
            # inbound DM federation handler. Skipping them here keeps
            # the sender-side ``DmMessageCreated`` handler from blowing
            # up with a FOREIGN KEY constraint failure for every
            # cross-household DM.
            local = await self._local_user(recipient_id)
            if local is None:
                continue
            if local.username in muted:
                # The recipient muted this conversation: the message still
                # lands and counts unread, but no bell row and no push.
                continue
            is_mentioned = recipient_id in mentioned
            if local.username in prefs.mentions_only and not is_mentioned:
                continue
            if self._ws_manager is not None and (
                self._ws_manager.is_user_active_in_conversation(
                    recipient_id,
                    event.conversation_id,
                )
            ):
                continue
            if (
                prefs.listing_seller_user_id is not None
                and recipient_id == prefs.listing_seller_user_id
            ):
                # "Ask the seller": the seller gets one row per thread
                # (D7) — a buyer asking is about them, not chat noise.
                await self._save_bazaar_question(
                    local,
                    event=event,
                    prefs=prefs,
                    link=link,
                    withheld=blocked,
                )
                continue
            if is_mentioned:
                await self._save_dm_mention(
                    local,
                    sender_id=event.sender_user_id,
                    withheld=blocked,
                    sender_name=event.sender_display_name
                    or self._someone(self._locale(local)),
                    chat_name=self._chat_name(prefs, local),
                    link=link,
                    about=about,
                )
                continue
            if (
                parse_notif_settings(local.preferences_json).delivery(
                    NotifKind.MESSAGES
                )
                is NotifDelivery.OFF
            ):
                continue  # Messages and calls: Off (D4)
            if not await self._chat_alerts(event, local):
                continue  # the same burst: the device already buzzed
            await self._send_push(
                local,
                ntype="dm_message",
                title=self._dm_title(event, prefs, local),
                link_url=link,
                tag=f"dm_message:{link}",
            )

    async def _chat_alerts(self, event: DmMessageCreated, recipient) -> bool:
        """D8 for a chat message: alert when it is the first one the
        recipient has not read, or when the chat was quiet for
        :data:`CHAT_REALERT_SECONDS` before it. Without the conversation
        repo every message alerts."""
        if self._convos is None:
            return True
        if (
            await self._convos.count_unread(event.conversation_id, recipient.username)
            <= 1
        ):
            return True
        latest = await self._convos.list_messages(event.conversation_id, limit=2)
        this = next((m for m in latest if m.id == event.message_id), None)
        previous = next((m for m in latest if m.id != event.message_id), None)
        # The gap before this message — a catch-up of old messages from a
        # household that was offline is one burst, not one alert each.
        return previous is None or _quiet_for(
            previous.created_at,
            CHAT_REALERT_SECONDS,
            until=this.created_at if this is not None else None,
        )

    async def on_dm_message_deleted(self, event: DmMessageDeleted) -> None:
        """A deleted message no longer rings: for each local member it went
        to, once nothing in the conversation is unread for them any more,
        the conversation's ``dm_message`` / ``dm_mention`` bell rows are
        marked read (bell rows are collapsed per conversation, so this is
        the row the deleted message bumped)."""
        if self._convos is None:
            return
        for user_id in dict.fromkeys(event.recipient_user_ids):
            local = await self._local_user(user_id)
            if local is None:
                continue
            if await self._convos.count_unread(event.conversation_id, local.username):
                continue
            await self.mark_read_for_dm(user_id, event.conversation_id)

    def _dm_title(self, event: DmMessageCreated, prefs: _SeatPrefs, recipient) -> str:
        """A chat message's bell (and push) title: who, and in which chat —
        never the text. A shared location says so — never where (§25.3: no
        coordinates, no label, no body)."""
        locale = self._locale(recipient)
        author = event.sender_display_name or self._someone(locale)
        if event.message_type == "location":
            return self._t(
                "notification.dm.location",
                locale=locale,
                fallback="{author} shared a location",
                author=author,
            )
        chat_name = self._chat_name(prefs, recipient)
        if chat_name:  # a system chat or a named group
            return self._t(
                "notification.household_chat.message",
                locale=locale,
                fallback="{author} in {chat_name}",
                author=author,
                chat_name=chat_name,
            )
        return self._t(
            "notification.dm.message",
            locale=locale,
            fallback="{author} messaged you",
            author=author,
        )

    def _chat_name(self, prefs: _SeatPrefs, recipient) -> str | None:
        """The name a bell gives the conversation: the group's own, or for
        the household chat its localised label, for a space chat the space's
        name."""
        if prefs.system_scope is SystemChatScope.SPACE:
            return prefs.space_name or self._t(
                "notification.space_chat.name",
                locale=self._locale(recipient),
                fallback="a space",
            )
        if prefs.system_scope is SystemChatScope.HOUSEHOLD:
            return self._t(
                "notification.household_chat.name",
                locale=self._locale(recipient),
                fallback="Household chat",
            )
        return prefs.name

    async def on_dm_message_updated(self, event: DmMessageUpdated) -> None:
        """A sender's edit rings only the group members it newly @-mentions
        (``event.new_mentions``) with a ``dm_mention`` bell — whatever
        their level, never while muted, never the sender, never someone
        already mentioned, never across a guardian block. Transcript patches
        carry no new mentions."""
        if not event.new_mentions:
            return
        prefs = await self._seat_prefs(event.conversation_id)
        if not prefs.group:
            return
        link = _conversation_link(
            event.conversation_id, prefs.system_scope, prefs.space_id
        )
        allowed = set(event.recipient_user_ids)
        blocked = await self._withheld_from(event.sender_user_id)
        for user_id in {m.user_id for m in event.new_mentions if m.user_id}:
            if (
                user_id == event.sender_user_id
                or user_id not in allowed
                or user_id in blocked
            ):
                continue
            local = await self._local_user(user_id)
            if local is None or local.username in prefs.muted:
                continue
            if self._ws_manager is not None and (
                self._ws_manager.is_user_active_in_conversation(
                    user_id, event.conversation_id
                )
            ):
                continue
            await self._save_dm_mention(
                local,
                sender_id=event.sender_user_id,
                withheld=blocked,
                sender_name=event.sender_display_name
                or await self._person_name(event.sender_user_id, self._locale(local)),
                chat_name=self._chat_name(prefs, local),
                link=link,
                about=NotificationSubject.of(
                    NotificationObject.CONVERSATION,
                    event.conversation_id,
                    space_id=prefs.space_id,
                    space_name=prefs.space_name,
                ),
            )

    async def _save_bazaar_question(
        self,
        recipient,
        *,
        event: DmMessageCreated,
        prefs: _SeatPrefs,
        link: str,
        withheld: frozenset[str],
    ) -> None:
        """The seller's row for an "Ask the seller" thread: "{author} asked
        about {listing}" in the app — one per thread, bumped by each new
        question (D7). The push names who and where only (D11): never the
        listing's title, never the text."""
        locale = self._locale(recipient)
        author = event.sender_display_name or self._someone(locale)
        await self._save_notif(
            new_notification(
                user_id=recipient.user_id,
                type="bazaar_question",
                title=self._t(
                    "notification.bazaar.question",
                    locale=locale,
                    fallback="{author} asked about {title}",
                    author=author,
                    title=prefs.listing_title or "",
                ),
                link_url=link,
            ),
            about=NotificationSubject.of(
                NotificationObject.CONVERSATION,
                event.conversation_id,
                space_id=prefs.listing_space_id,
                space_name=prefs.listing_space_name,
            ),
            actor_user_id=event.sender_user_id,
            actor_name=author,
            withheld=withheld,
            push_params={"name": author, "space_name": prefs.listing_space_name},
        )

    async def _save_dm_mention(
        self,
        recipient,
        *,
        sender_id: str,
        withheld: frozenset[str],
        sender_name: str,
        chat_name: str | None,
        link: str,
        about: NotificationSubject,
    ) -> None:
        if chat_name:
            title = self._t(
                "notification.dm.mention",
                locale=self._locale(recipient),
                fallback="{author} mentioned you in {chat_name}",
                author=sender_name,
                chat_name=chat_name,
            )
        else:
            title = self._t(
                "notification.dm.mention_unnamed",
                locale=self._locale(recipient),
                fallback="{author} mentioned you in a group chat",
                author=sender_name,
            )
        await self._save_notif(
            new_notification(
                user_id=recipient.user_id,
                type="dm_mention",
                title=title,
                link_url=link,
            ),
            about=about,
            actor_user_id=sender_id,
            actor_name=sender_name,
            withheld=withheld,
        )

    async def _person_name(
        self, user_id: str, locale: str | None, *, space_id: str | None = None
    ) -> str:
        """The name this household holds for *user_id* (:meth:`_held_name`),
        else "Someone" in the recipient's language — never the raw user
        id."""
        return await self._held_name(user_id, space_id=space_id) or self._someone(
            locale
        )

    async def _held_name(
        self, user_id: str, *, space_id: str | None = None
    ) -> str | None:
        """The name this household holds for *user_id*, or ``None``.
        Looked up once per delivery job.

        The same order the SPA names people in
        (:class:`~socialhome.services.person_summary_service.PersonSummaryService`),
        so a bell row and the member list agree. In a space (*space_id*):
        the person's per-space name, then their household's (a local
        member, a paired household's person), then the space's roster seat
        (kept after they left) and the author profile a GFS follower holds.
        Then wherever else this household met them: a seat in another
        space, a group-chat seat, an author profile in another space."""
        batch = _BATCH.get()
        key = (user_id, space_id)
        if batch is not None and key in batch.names:
            return batch.names[key]
        name = await self._lookup_name(user_id, space_id)
        if batch is not None:
            batch.names[key] = name
        return name

    async def _lookup_name(self, user_id: str, space_id: str | None) -> str | None:
        if space_id is not None:
            member = await self._spaces.get_member(space_id, user_id)
            if member is not None and member.space_display_name:
                return member.space_display_name
        local = await self._local_user(user_id)
        if local is not None:
            # One of ours: never named from another household's record.
            return local.display_name or None
        remote = await self._users.get_remote(user_id)
        if remote is not None and remote.display_name:
            return remote.display_name
        if space_id is not None:
            if self._remote_members is not None:
                seat = await self._remote_members.get_including_tombstones(
                    space_id, "", user_id
                )
                if seat is not None and seat.display_name:
                    return seat.display_name
            if self._author_profiles is not None:
                for p in await self._author_profiles.list_for(space_id, [user_id]):
                    if p.display_name:
                        return p.display_name
        if self._remote_members is not None:
            name = await self._remote_members.latest_display_name(user_id)
            if name:
                return name
        if self._convos is not None:
            name = await self._convos.remote_member_display_name(user_id)
            if name:
                return name
        if self._author_profiles is not None:
            for sid in await self._author_profiles.list_spaces_for_author(user_id):
                if sid == space_id:
                    continue
                for p in await self._author_profiles.list_for(sid, [user_id]):
                    if p.display_name:
                        return p.display_name
        return None

    async def _local_user(self, user_id: str) -> User | None:
        """The local user *user_id* (``None``: not one of ours) — looked up
        once per delivery job (:meth:`run_batched`)."""
        batch = _BATCH.get()
        if batch is not None and user_id in batch.users:
            return batch.users[user_id]
        user = await self._users.get_by_user_id(user_id)
        if batch is not None:
            batch.users[user_id] = user
        return user

    async def _prefetch_users(self, user_ids: Iterable[str]) -> None:
        """Look a fan-out's people up in **one** read, so its per-recipient
        :meth:`_local_user` calls cost nothing. Outside a delivery job
        there is nowhere to keep them, so it does nothing."""
        batch = _BATCH.get()
        if batch is None:
            return
        wanted = {uid for uid in user_ids if uid not in batch.users}
        if not wanted:
            return
        found = {u.user_id: u for u in await self._users.list_by_ids(wanted)}
        for uid in wanted:
            batch.users[uid] = found.get(uid)

    @staticmethod
    def _remember_users(users: Iterable[User]) -> None:
        """Keep users a handler already read (``list_active``) for the rest
        of its delivery job."""
        batch = _BATCH.get()
        if batch is not None:
            for user in users:
                batch.users.setdefault(user.user_id, user)

    async def _space_name(self, space_id: str | None) -> str | None:
        """The name of *space_id* (``None`` for none, or one not held)."""
        if not space_id:
            return None
        space = await self._spaces.get(space_id)
        return space.name if space is not None else None

    async def _seat_prefs(self, conversation_id: str) -> _SeatPrefs:
        """This conversation's per-member bell settings, read once per
        message: who muted it right now, who is at level ``mentions``, and
        whether it is a group (only groups have mentions / levels)."""
        if self._convos is None:
            return _SeatPrefs()
        now = datetime.now(UTC)
        members = await self._convos.list_members(conversation_id)
        conv = await self._convos.get(conversation_id)
        group = conv is not None and conv.type is ConversationType.GROUP_DM
        space_id = conv.space_id if conv is not None else None
        space = await self._spaces.get(space_id) if space_id else None
        listing = (
            await self._listings.get_listing(conv.bazaar_listing_id)
            if conv is not None and conv.bazaar_listing_id and self._listings
            else None
        )
        listing_space = (
            await self._spaces.get(listing.space_id) if listing is not None else None
        )
        return _SeatPrefs(
            muted=frozenset(
                m.username for m in members if mute_active(m.muted_until, now=now)
            ),
            mentions_only=frozenset(
                m.username for m in members if group and m.notif_level == "mentions"
            ),
            group=group,
            name=conv.name if conv is not None and group else None,
            system_scope=conv.system_scope if conv is not None else None,
            space_id=space_id,
            space_name=space.name if space is not None else None,
            listing_title=listing.title if listing is not None else None,
            listing_seller_user_id=(
                listing.seller_user_id if listing is not None else None
            ),
            listing_space_id=listing.space_id if listing is not None else None,
            listing_space_name=(
                listing_space.name if listing_space is not None else None
            ),
        )

    async def mark_read_for_dm(
        self,
        user_id: str,
        conversation_id: str,
    ) -> int:
        """Mark every unread notification about a conversation
        (:data:`_CONVERSATION_TYPES`: a message, a mention, a missed call,
        being added) as read for one user. Returns the number flipped.

        Called from ``POST /api/conversations/{id}/read`` so the bell
        clears in step with the thread's read-receipt state — opening
        the thread is the natural "I've seen these" signal, no
        separate UI gesture needed.
        """
        conv = await self._convos.get(conversation_id) if self._convos else None
        link = _conversation_link(
            conversation_id,
            conv.system_scope if conv is not None else None,
            conv.space_id if conv is not None else None,
        )
        flipped = 0
        for ntype in _CONVERSATION_TYPES:
            flipped += await self._notifs.mark_read_by_link(
                user_id=user_id, link_url=link, type=ntype
            )
        if flipped:
            await self._announce_unread(user_id)
        # The chat's pushes — its rows' and the row-less message push —
        # come off every device, read rows or not.
        self._withdraw(user_id, _conversation_tags(link))
        return flipped

    async def mark_read(self, user_id: str, notification_id: str) -> None:
        """One row of *user_id* read (``POST /api/notifications/{id}/read``):
        every device hears the new count, and its push comes off them."""
        note = await self._notifs.get(notification_id)
        await self._notifs.mark_read(notification_id, user_id)
        await self._announce_unread(user_id)
        if note is not None and note.user_id == user_id and not note.is_read:
            self._withdraw(user_id, [_push_tag(note)])

    async def mark_all_read(self, user_id: str) -> None:
        """Every row of *user_id* read (``POST /api/notifications/read-all``):
        every device hears the count, and the pushes of what flipped come
        off them."""
        unread = await self._notifs.list_unread(user_id)
        await self._notifs.mark_all_read(user_id)
        await self._announce_unread(user_id)
        self._withdraw(user_id, [_push_tag(n) for n in unread])

    async def mark_read_by_links(self, user_id: str, links: list[str]) -> int:
        """The user saw these pages: every unread row of theirs that opens
        one of them reads as read, on every device. Returns how many flipped.

        Rows match on :func:`link_target` — the page, not the part it picks
        out — so viewing ``/post/p`` reads "commented" rows that point at
        ``/post/p?comment=c`` too, while viewing a space's feed
        (``/spaces/s``) leaves its posts' rows (``/spaces/s?post=p``) alone.
        """
        targets = {link_target(link) for link in links}
        seen = [
            n
            for n in await self._notifs.list_unread(user_id)
            if n.link_url is not None and link_target(n.link_url) in targets
        ]
        flipped = await self._notifs.mark_read_ids(user_id, [n.id for n in seen])
        if flipped:
            await self._announce_unread(user_id)
            self._withdraw(user_id, [_push_tag(n) for n in seen])
        return flipped

    async def mark_read_by_objects(self, user_id: str, object_keys: list[str]) -> int:
        """The user saw these things (``post:<id>``, ``dm:<id>`` …): every
        unread row of theirs about one of them reads as read, whatever its
        type, on every device. Returns how many flipped."""
        keys = set(object_keys)
        seen: list[Notification] = (
            [n for n in await self._notifs.list_unread(user_id) if n.object_key in keys]
            if self._withdrawals is not None
            else []
        )
        flipped = await self._notifs.mark_read_by_object(user_id, object_keys)
        if flipped:
            await self._announce_unread(user_id)
            self._withdraw(user_id, [_push_tag(n) for n in seen])
        return flipped

    # ── The thing is gone: so are its rows ────────────────────────────

    async def on_thing_deleted(
        self,
        event: PostDeleted
        | CommentDeleted
        | TaskDeleted
        | MomentDeleted
        | HighlightRemoved,
    ) -> None:
        """A post, space comment, task, moment or highlight was deleted
        (here or by its household): every row about it goes, for everyone
        — except a decision about the author's own content
        (:data:`_DECISION_TYPES`). Recipients who lost an unread row hear
        their new count."""
        match event:
            case PostDeleted(space_id=None):
                keys = [
                    object_key(NotificationObject.POST, event.post_id),
                    object_key(NotificationObject.LISTING, event.post_id),
                ]
            case PostDeleted():
                keys = [object_key(NotificationObject.SPACE_POST, event.post_id)]
            case CommentDeleted(space_id=None):
                return  # household comments group on their post
            case CommentDeleted():
                keys = [object_key(NotificationObject.SPACE_COMMENT, event.comment_id)]
            case TaskDeleted():
                keys = [object_key(NotificationObject.TASK, event.task_id)]
            case MomentDeleted():
                keys = [object_key(NotificationObject.MOMENT, event.moment_id)]
            case HighlightRemoved():
                keys = [object_key(NotificationObject.HIGHLIGHT, event.highlight_id)]
        await self._forget(keys)

    async def on_space_seat_gone(
        self, event: SpaceMemberLeft | RemoteSpaceMemberBanned
    ) -> None:
        """Someone here left a space, or was removed or banned from it: their
        rows about that space go (the space's end notice stays)."""
        for user_id in await self._notifs.delete_by_space(
            event.space_id, user_id=event.user_id, keep_types=_SPACE_ENDED_TYPES
        ):
            await self._announce_unread(user_id)

    async def on_space_dissolved_here(self, event: SpaceConfigChanged) -> None:
        """A space this household hosts was dissolved: its rows go for
        everyone."""
        if event.event_type != SpaceConfigEventType.DISSOLVED.value:
            return
        await self._forget_space(event.space_id)

    async def _forget(
        self, keys: list[str], *, keep_types: frozenset[str] = _DECISION_TYPES
    ) -> None:
        for user_id in await self._notifs.delete_by_object(keys, keep_types=keep_types):
            await self._announce_unread(user_id)

    async def _forget_space(self, space_id: str) -> None:
        for user_id in await self._notifs.delete_by_space(
            space_id, keep_types=_SPACE_ENDED_TYPES
        ):
            await self._announce_unread(user_id)

    async def _announce_unread(
        self, user_id: str, *, handled: tuple[str, ...] = ()
    ) -> None:
        """Tell every device of ``user_id`` its unread count now — and
        which "Needs you" items just left the section (``handled``)."""
        await self._bus.publish(
            NotificationReadChanged(
                user_id=user_id,
                unread_count=await self._notifs.count_unread(user_id),
                handled=handled,
            )
        )

    async def _resolve(
        self,
        key: str,
        *,
        types: Iterable[str],
        outcome: str,
        by_user_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """A "Needs you" item was handled: every recipient's row about
        *key* (or only ``user_id``'s) says how it ended and who ended it,
        reads as read, and leaves the section on each of their devices."""
        by = (
            NotificationActor(by_user_id, await self._held_name(by_user_id))
            if by_user_id
            else None
        )
        kinds = frozenset(types)
        # The rows about it, to take their pushes off the devices too.
        about: list[Notification] = (
            [
                n
                for n in await self._notifs.find(Spec(where=[("object_key", "=", key)]))
                if n.type in kinds
            ]
            if self._withdrawals is not None
            else []
        )
        for uid in await self._notifs.resolve_object(
            key,
            types=kinds,
            handled=NotificationHandled(outcome=outcome, by=by),
            user_id=user_id,
        ):
            await self._announce_unread(uid, handled=(key,))
            self._withdraw(uid, [_push_tag(n) for n in about if n.user_id == uid])

    # ── Needs you: handled by one, handled for all (D12) ──────────────
    #
    # A join request, an invite and a pairing request are each their own
    # row (``join_request:<id>`` …): answering one resolves exactly that
    # row for everyone it went to. A space's review queue (posts, reports)
    # is one row per space — it resolves once the queue it announces is
    # empty. Rows from before per-request keys (a join request keyed by
    # its space) resolve the queue way too.

    async def on_join_request_handled(
        self, event: SpaceJoinApproved | SpaceJoinDenied
    ) -> None:
        """This request was answered: every admin's row about it says who
        approved or declined it. Once no request waits in the space, an
        older space-wide "wants to join" row resolves too."""
        if isinstance(event, SpaceJoinApproved):
            approved, by = True, event.approved_by
        else:
            approved, by = False, event.denied_by
        await self._resolve(
            object_key(NotificationObject.JOIN_REQUEST, event.request_id),
            types=("space_join_requested",),
            outcome="approved" if approved else "declined",
            by_user_id=by,
        )
        if await self._spaces.list_pending_join_requests(event.space_id):
            return
        await self._resolve(
            object_key(NotificationObject.SPACE, event.space_id),
            types=("space_join_requested",),
            outcome="done",
            by_user_id=by,
        )

    async def on_moderation_item_handled(
        self,
        event: SpaceModerationApproved
        | SpaceModerationRejected
        | SpaceModerationExpired,
    ) -> None:
        """Nothing left to review in the space: every moderator's "pending
        review" row says it was handled (by whoever reviewed the last
        item)."""
        space_id = event.item.space_id
        if await self._spaces.count_pending(space_id):
            return
        await self._resolve(
            object_key(NotificationObject.SPACE, space_id),
            types=("moderation_pending",),
            outcome="done",
            by_user_id=event.item.reviewed_by,
        )

    async def on_report_resolved(self, event: ReportResolved) -> None:
        """The reporter hears their report was reviewed
        (:meth:`_tell_reporter`); and once no open report is left in the
        space, every authority's "New report" row says who handled the
        last one."""
        if self._reports is None:
            return
        await self._tell_reporter(event)
        if not event.space_id:
            return
        if await self._reports.count_pending_in_space(event.space_id):
            return
        await self._resolve(
            object_key(NotificationObject.SPACE, event.space_id),
            types=("space_report",),
            outcome="done",
            by_user_id=event.resolved_by,
        )

    async def _tell_reporter(self, event: ReportResolved) -> None:
        """``report_answer``: "Your report in {space} was reviewed".

        Never says what became of it, who reviewed it or who (or what) it
        was about — not in the row, not in the push (§25.3): the
        moderator stays out of it and the reported person is never named.
        A reporter on another household is told by their own household
        once the space's host says the report was reviewed
        (``SPACE_REPORT_ANSWERED``, v_71: ``ReportService.apply_report_answer``
        publishes this event with no decider)."""
        assert self._reports is not None
        report = await self._reports.get(event.report_id)
        if report is None or report.reporter_user_id == event.resolved_by:
            return
        recipient = await self._local_user(report.reporter_user_id)
        if recipient is None:
            return
        locale = self._locale(recipient)
        space_name = await self._space_name(report.space_id)
        if space_name:
            title = self._t(
                "notification.report.answered_in",
                locale=locale,
                fallback="Your report in {space_name} was reviewed",
                space_name=space_name,
            )
        else:
            title = self._t(
                "notification.report.answered",
                locale=locale,
                fallback="Your report was reviewed",
            )
        await self._save_notif(
            new_notification(
                user_id=recipient.user_id,
                type="report_answer",
                title=title,
                link_url=(
                    space_feed_link(report.space_id)
                    if report.space_id
                    else HOUSEHOLD_FEED_LINK
                ),
            ),
            about=NotificationSubject.of(
                NotificationObject.REPORT,
                report.id,
                space_id=report.space_id,
                space_name=space_name,
            ),
            actor_user_id=None,  # a decision about your own report
        )

    # ── Calls and group chats ────────────────────────────────────────

    async def _call_parts(
        self, conversation_id: str
    ) -> tuple[_SeatPrefs, str, NotificationSubject]:
        """A call's conversation: its seat settings, its link and what a
        row about it is about (the chat, its name as the place)."""
        prefs = await self._seat_prefs(conversation_id)
        link = _conversation_link(conversation_id, prefs.system_scope, prefs.space_id)
        return (
            prefs,
            link,
            NotificationSubject.of(
                NotificationObject.CONVERSATION,
                conversation_id,
                space_id=prefs.space_id,
                space_name=prefs.space_name or prefs.name,
            ),
        )

    async def _call_audience(
        self, caller_user_id: str, callee_user_ids: Iterable[str], prefs: _SeatPrefs
    ) -> list:
        """The local people a call reaches: not the caller, not someone a
        block separates from them, not someone who muted the chat (D9 —
        a muted chat rings nobody; "Mentions only" still rings, a call is
        for everyone in it)."""
        blocked = await self._withheld_from(caller_user_id)
        out = []
        for user_id in dict.fromkeys(callee_user_ids):
            if user_id == caller_user_id or user_id in blocked:
                continue
            local = await self._local_user(user_id)
            if local is None or local.username in prefs.muted:
                continue
            out.append(local)
        return out

    def _call_title(
        self, caller: str, chat_name: str | None, locale: str | None, *, missed: bool
    ) -> str:
        """D11 — who and where: "Anna is calling (in Team)", "Missed call
        from Anna (in Team)"."""
        if not missed:
            if chat_name:
                return self._t(
                    "notification.call.incoming_in",
                    locale=locale,
                    fallback="{name} is calling in {chat_name}",
                    name=caller,
                    chat_name=chat_name,
                )
            return self._t(
                "notification.call.incoming",
                locale=locale,
                fallback="{name} is calling",
                name=caller,
            )
        if chat_name:
            return self._t(
                "notification.call.missed_in",
                locale=locale,
                fallback="Missed call from {name} in {chat_name}",
                name=caller,
                chat_name=chat_name,
            )
        return self._t(
            "notification.call.missed",
            locale=locale,
            fallback="Missed call from {name}",
            name=caller,
        )

    async def on_call_ringing(self, event: CallRinging) -> None:
        """``call_incoming``: a push — no row — for each local person a call
        rings: "Anna is calling" / "Anna is calling in Team" (D11).

        Under Messages and calls (D4): Off → nothing. A muted chat rings
        silently in the app only. The push shares its tag with the missed
        call (:func:`_call_tag`), so when nobody picks up "Missed call from
        Anna" replaces it on the device instead of stacking under it."""
        prefs, link, _ = await self._call_parts(event.conversation_id)
        for local in await self._call_audience(
            event.caller_user_id, event.callee_user_ids, prefs
        ):
            if (
                parse_notif_settings(local.preferences_json).delivery(
                    NotifKind.MESSAGES
                )
                is NotifDelivery.OFF
            ):
                continue
            locale = self._locale(local)
            await self._send_push(
                local,
                ntype="call_incoming",
                title=self._call_title(
                    await self._person_name(event.caller_user_id, locale),
                    self._chat_name(prefs, local),
                    locale,
                    missed=False,
                ),
                link_url=link,
                tag=_call_tag(link),
            )

    async def on_call_ring_stopped(self, event: CallRingStopped) -> None:
        """The call stopped ringing for these people without being missed
        (answered — on any device —, declined, invite withdrawn): the
        ringing push comes off their devices. A missed call never comes
        here — "Missed call from Anna" replaces the ring instead."""
        if self._withdrawals is None:
            return
        _, link, _ = await self._call_parts(event.conversation_id)
        for user_id in event.user_ids:
            self._withdraw(user_id, [_call_tag(link)])

    async def on_call_missed(self, event: CallMissed) -> None:
        """``call_missed``: a row — one per conversation (D7) — and a push
        for each local person who missed the call: "Missed call from Anna"
        (D3: a missed call is about you, so it is in the list although chat
        messages are not). A muted chat or Messages and calls set to Off
        gives nothing; opening the chat reads the row."""
        prefs, link, about = await self._call_parts(event.conversation_id)
        withheld = await self._withheld_from(event.caller_user_id)
        for local in await self._call_audience(
            event.caller_user_id, event.callee_user_ids, prefs
        ):
            locale = self._locale(local)
            caller = await self._person_name(event.caller_user_id, locale)
            await self._save_notif(
                new_notification(
                    user_id=local.user_id,
                    type="call_missed",
                    title=self._call_title(
                        caller, self._chat_name(prefs, local), locale, missed=True
                    ),
                    link_url=link,
                ),
                about=about,
                actor_user_id=event.caller_user_id,
                withheld=withheld,
            )

    async def on_chat_added(self, event: DmConversationCreated) -> None:
        """``chat_added``: someone put you in a group chat — "Anna added you
        to Team" (D3: being added is about you). The person who added you
        doesn't get one; a roster from another household doesn't say who
        added you ("You were added to Team"). A 1:1 chat is not "added"."""
        if event.conversation_type != ConversationType.GROUP_DM.value:
            return
        adder = event.creator_user_id or None
        prefs, link, about = await self._call_parts(event.conversation_id)
        withheld = await self._withheld_from(adder) if adder else frozenset()
        for user_id in dict.fromkeys(event.member_user_ids):
            if user_id == adder:
                continue
            local = await self._local_user(user_id)
            if local is None or local.username in prefs.muted:
                continue
            locale = self._locale(local)
            chat_name = self._chat_name(prefs, local)
            if adder:
                name = await self._person_name(adder, locale)
                title = (
                    self._t(
                        "notification.chat.added_to",
                        locale=locale,
                        fallback="{name} added you to {chat_name}",
                        name=name,
                        chat_name=chat_name,
                    )
                    if chat_name
                    else self._t(
                        "notification.chat.added",
                        locale=locale,
                        fallback="{name} added you to a group chat",
                        name=name,
                    )
                )
            else:
                title = (
                    self._t(
                        "notification.chat.added_to_none",
                        locale=locale,
                        fallback="You were added to {chat_name}",
                        chat_name=chat_name,
                    )
                    if chat_name
                    else self._t(
                        "notification.chat.added_none",
                        locale=locale,
                        fallback="You were added to a group chat",
                    )
                )
            await self._save_notif(
                new_notification(
                    user_id=local.user_id,
                    type="chat_added",
                    title=title,
                    link_url=link,
                ),
                about=about,
                actor_user_id=adder,
                withheld=withheld,
            )

    # ── Polls ────────────────────────────────────────────────────────

    async def on_poll_closed(self, event: PollClosed) -> None:
        """``poll_result``: a poll closed — everyone here who voted in it
        hears so; the row opens the post with the result. Never the
        question or the winning answer (§25.3). A close from another
        household (``SPACE_POLL_CLOSED``) tells this household's voters.

        Closed by its author: "Anna closed a poll you voted in" / "… a poll
        in Familie Alpha", not to the author. Past its deadline
        (``at_deadline``) nobody closed it: "A poll you voted in has ended"
        / "A poll in Familie Alpha has ended", to every voter — the author
        included."""
        repo = self._space_polls if event.space_id else self._polls
        if repo is None:
            return
        voters = await repo.list_voter_ids(event.post_id)
        if not voters:
            return
        author = (
            ""
            if event.at_deadline
            else event.actor_user_id or await repo.get_post_author(event.post_id)
        )
        space_name = await self._space_name(event.space_id)
        link, about = self._poll_target(event.post_id, event.space_id, space_name)
        withheld = await self._withheld_from(author) if author else frozenset()
        for voter in voters:
            if author and voter == author:
                continue
            local = await self._local_user(voter)
            if local is None:
                continue
            locale = self._locale(local)
            if event.at_deadline:
                title = (
                    self._t(
                        "notification.poll.ended_in",
                        locale=locale,
                        fallback="A poll in {space_name} has ended",
                        space_name=space_name,
                    )
                    if space_name
                    else self._t(
                        "notification.poll.ended",
                        locale=locale,
                        fallback="A poll you voted in has ended",
                    )
                )
            else:
                name = (
                    await self._person_name(author, locale, space_id=event.space_id)
                    if author
                    else self._someone(locale)
                )
                title = (
                    self._t(
                        "notification.poll.closed_in",
                        locale=locale,
                        fallback="{name} closed a poll in {space_name}",
                        name=name,
                        space_name=space_name,
                    )
                    if space_name
                    else self._t(
                        "notification.poll.closed",
                        locale=locale,
                        fallback="{name} closed a poll you voted in",
                        name=name,
                    )
                )
            await self._save_notif(
                new_notification(
                    user_id=local.user_id,
                    type="poll_result",
                    title=title,
                    link_url=link,
                ),
                about=about,
                actor_user_id=author or None,
                withheld=withheld,
            )

    async def on_schedule_poll_finalized(
        self, event: SchedulePollFinalized | RemoteSchedulePollFinalized
    ) -> None:
        """``poll_scheduled``: the organiser picked the time of a date poll
        — everyone here who answered it hears so ("Anna picked a time for a
        date poll you answered" / "… a time in Familie Alpha"), never the
        organiser. Never the title or the time (§25.3): the row opens the
        post. A peer's finalise (``SPACE_SCHEDULE_FINALIZED``) arrives as
        :class:`RemoteSchedulePollFinalized` — it never re-runs the
        calendar bridge."""
        repo = self._space_polls if event.space_id else self._polls
        if repo is None:
            return
        answered = sorted(
            {r["user_id"] for r in await repo.list_schedule_responses(event.post_id)}
        )
        organiser = event.finalized_by
        space_name = await self._space_name(event.space_id)
        link, about = self._poll_target(event.post_id, event.space_id, space_name)
        withheld = await self._withheld_from(organiser) if organiser else frozenset()
        for user_id in answered:
            if user_id == organiser:
                continue
            local = await self._local_user(user_id)
            if local is None:
                continue
            locale = self._locale(local)
            name = (
                await self._person_name(organiser, locale, space_id=event.space_id)
                if organiser
                else self._someone(locale)
            )
            title = (
                self._t(
                    "notification.poll.scheduled_in",
                    locale=locale,
                    fallback="{name} picked a time in {space_name}",
                    name=name,
                    space_name=space_name,
                )
                if space_name
                else self._t(
                    "notification.poll.scheduled",
                    locale=locale,
                    fallback="{name} picked a time for a date poll you answered",
                    name=name,
                )
            )
            await self._save_notif(
                new_notification(
                    user_id=local.user_id,
                    type="poll_scheduled",
                    title=title,
                    link_url=link,
                ),
                about=about,
                actor_user_id=organiser or None,
                withheld=withheld,
            )

    @staticmethod
    def _poll_target(
        post_id: str, space_id: str | None, space_name: str | None
    ) -> tuple[str, NotificationSubject]:
        """Where a poll row leads and what it is about: its post."""
        if space_id:
            return space_post_link(space_id, post_id), NotificationSubject.of(
                NotificationObject.SPACE_POST,
                post_id,
                space_id=space_id,
                space_name=space_name,
            )
        return household_post_link(post_id), NotificationSubject.of(
            NotificationObject.POST, post_id
        )

    async def on_space_invite_answered(self, event: SpaceInviteAnswered) -> None:
        """The invitee answered their invite: it leaves their section."""
        await self._resolve(
            object_key(NotificationObject.SPACE_INVITE, event.space_id),
            types=("space_invite",),
            outcome="accepted" if event.accepted else "declined",
            user_id=event.user_id,
        )

    async def on_auto_pair_answered(self, event: AutoPairRequestAnswered) -> None:
        """An admin answered a pairing request: every admin's row says who."""
        await self._resolve(
            object_key(NotificationObject.PAIRING_REQUEST, event.request_id),
            types=("pairing_request",),
            outcome="approved" if event.approved else "declined",
            by_user_id=event.by_user_id,
        )

    async def pending_actions(self, user_id: str) -> list[Notification]:
        """``user_id``'s "Needs you" items, newest first — each checked
        against the thing it asks about, so the section never shows work
        that is gone: a request answered or expired without an event this
        service heard, a queue emptied, a seat that lost its admin role.
        Such a row resolves (``done``) for this user on the way."""
        out: list[Notification] = []
        for note in await self._notifs.list_pending_actions(user_id):
            ended = await self._ended_as(note)
            if ended is None:
                out.append(note)
            elif note.object_key:
                await self._resolve(
                    note.object_key,
                    types=(note.type,),
                    outcome=ended,
                    user_id=user_id,
                )
        return out

    async def _ended_as(self, note: Notification) -> str | None:
        """``None`` while *note*'s request still waits for its recipient,
        else how it ended (:data:`HANDLED_OUTCOMES`)."""
        if note.type in RETIRED_ACTION_TYPES:
            return "done"
        if note.type in ("app_challenge", "app_challenge_continue"):
            return await self._challenge_ended_as(note)
        if note.type == "app_challenge_waiting":
            # Waits for its app (D10) as long as any invite does; adding
            # the app turns it into a playable one.
            cutoff = datetime.now(UTC) - timedelta(seconds=PENDING_SESSION_TTL_SECONDS)
            return "expired" if note.created_at < cutoff.isoformat() else None
        return None if await self._still_needs(note) else "done"

    async def _challenge_ended_as(self, note: Notification) -> str | None:
        """A game invite waits while its app is enabled here and the invite
        is kept for its invitee, younger than the invite TTL."""
        kind, _, oid = (note.object_key or "").partition(":")
        app_id, _, session_id = oid.rpartition("/")
        if kind != NotificationObject.APP_SESSION or not app_id or not session_id:
            # A row from before one row per invite: the app holds those.
            return "done"
        if self._apps is None:
            return None
        app = await self._apps.get(app_id)
        if app is None or not app.enabled:
            return "done"
        invite = await self._apps.get_pending_session(app_id, note.user_id, session_id)
        if invite is None or invite.state != SESSION_INVITED:
            # Gone, or played: the row stays as the game's mailbox (0131).
            return "done"
        cutoff = datetime.now(UTC) - timedelta(seconds=PENDING_SESSION_TTL_SECONDS)
        if invite.created_at < cutoff.isoformat():
            return "expired"
        return None

    async def _still_needs(self, note: Notification) -> bool:
        """Does *note*'s request still wait for its recipient?"""
        kind, _, oid = (note.object_key or "").partition(":")
        if note.type == "pairing_request":
            user = await self._local_user(note.user_id)
            if user is None or not user.is_admin:
                return False
            if self._pair_inbox is None:
                return True  # nothing to check against: keep it
            return (
                kind == NotificationObject.PAIRING_REQUEST
                and self._pair_inbox.get(oid) is not None
            )
        if note.type == "app_request":
            # Waits until the app is in the household and turned on (an
            # install the event missed, e.g. during a restart, still ends
            # it).
            if kind != NotificationObject.APP:
                return False
            if self._apps is None:
                return True
            app = await self._apps.get(oid)
            return app is None or not app.enabled
        if note.type == "app_update":
            # Waits while its admin is one, the app is still here and the
            # version the row offers (its body) is newer than the installed
            # one — an update the event missed still ends it.
            user = await self._local_user(note.user_id)
            if kind != NotificationObject.APP or user is None or not user.is_admin:
                return False
            if self._apps is None:
                return True
            app = await self._apps.get(oid)
            return app is not None and is_newer_version(note.body or "", app.version)
        if note.type == "space_invite":
            if kind != NotificationObject.SPACE_INVITE:
                return False
            invites = [
                *await self._spaces.list_pending_local_invites_for(note.user_id),
                *await self._spaces.list_pending_remote_invites_for(note.user_id),
            ]
            return any(str(i.get("space_id")) == oid for i in invites)
        space_id = note.space_id or (oid if kind == NotificationObject.SPACE else "")
        if not space_id:
            return False
        member = await self._spaces.get_member(space_id, note.user_id)
        if note.type == "space_join_requested":
            if member is None or member.role not in SETTINGS_AUTHORITY_ROLES:
                return False
            if kind == NotificationObject.JOIN_REQUEST:
                request = await self._spaces.get_join_request(oid)
                return request is not None and request.get("status") == "pending"
            return bool(await self._spaces.list_pending_join_requests(space_id))
        if member is None or member.role not in CONTENT_AUTHORITY_ROLES:
            return False
        if note.type == "moderation_pending":
            return bool(await self._spaces.count_pending(space_id))
        if note.type == "space_report":
            if self._reports is None:
                return True
            return bool(await self._reports.count_pending_in_space(space_id))
        return False

    async def on_app_challenge_ended(self, event: AppChallengeEnded) -> None:
        """Game invites stopped waiting (the app took them in, a decline, a
        withdrawal — or, for one that waited for its app, D10, the app
        arriving): their rows leave the invitee's section."""
        for session_id in event.session_ids:
            await self._resolve(
                object_key(
                    NotificationObject.APP_SESSION, f"{event.app_id}/{session_id}"
                ),
                types=(
                    "app_challenge",
                    "app_challenge_continue",
                    "app_challenge_waiting",
                ),
                outcome=event.outcome,
                user_id=event.user_id,
            )

    async def on_app_challenge_declined(self, event: AppChallengeDeclined) -> None:
        """The person you invited said no — "{name} declined your {app}
        game" (D11), so the challenger learns it even if they never open
        the app. A row only, no push: nothing waits on them. Their app
        hears the close itself (live, or from its mailbox on open)."""
        recipient = await self._local_user(event.to_user_id)
        locale = self._locale(recipient)
        app_name = await self._app_name(event.app_id, locale)
        if app_name:
            title = self._t(
                "notification.app.challenge_declined",
                locale=locale,
                fallback="{name} declined your {app} game",
                name=event.from_display,
                app=app_name,
            )
        else:
            title = self._t(
                "notification.app.challenge_declined_generic",
                locale=locale,
                fallback="{name} declined your game",
                name=event.from_display,
            )
        await self._save_notif(
            new_notification(
                user_id=event.to_user_id,
                type="app_challenge_declined",
                title=title,
                link_url=f"/apps/{event.app_id}",
            ),
            about=NotificationSubject.of(
                NotificationObject.APP_SESSION,
                f"{event.app_id}/{event.session_id}",
                space_name=app_name,
            ),
            actor_user_id=event.from_user_id,
            actor_name=event.from_display,
            push=False,
        )

    async def on_app_game_ended(self, event: AppGameEnded) -> None:
        """The other household uninstalled the app — "{name} no longer has
        {app}, so your game ended" (D9), so the player learns why the game
        is gone even if they don't open the app for weeks. About you, a
        row only, no push: nothing waits on them and nothing can be done.
        Their app hears the close itself (live, or from its mailbox)."""
        recipient = await self._local_user(event.to_user_id)
        locale = self._locale(recipient)
        app_name = await self._app_name(event.app_id, locale)
        if app_name:
            title = self._t(
                "notification.app.game_ended",
                locale=locale,
                fallback="{name} no longer has {app}, so your game ended",
                name=event.from_display,
                app=app_name,
            )
        else:
            title = self._t(
                "notification.app.game_ended_generic",
                locale=locale,
                fallback="{name} no longer has the app, so your game ended",
                name=event.from_display,
            )
        await self._save_notif(
            new_notification(
                user_id=event.to_user_id,
                type="app_game_ended",
                title=title,
                link_url=f"/apps/{event.app_id}",
            ),
            about=NotificationSubject.of(
                NotificationObject.APP_SESSION,
                f"{event.app_id}/{event.session_id}",
                space_name=app_name,
            ),
            actor_user_id=event.from_user_id,
            actor_name=event.from_display,
            push=False,
        )

    async def on_app_turn(self, event: AppTurn) -> None:
        """A move handed the turn to a local player while their app was
        closed — "Your turn with {name} in {app}" (owner decision
        2026-10-30). The app said so itself (``turn: true`` on its send);
        Social Home reads nothing of the move.

        **One row per game** (``app_session:<app>/<session>``, D7): the
        next turn in the same game moves that row on — unread again if it
        was read — and a repeat on a row still unread is silent (D8). Games
        is In app by default; it pushes only for someone who set Games to
        Push, and then the push names the person and the app only (D11).
        A player who has the app on screen right now (the SPA's
        ``dm.active`` claim, :func:`app_view_key`) gets no row: the move is
        in front of them. The gate (:meth:`_save_notif`) drops a sender
        the player blocked or a guardian block separates from them."""
        if self._ws_manager is not None and (
            self._ws_manager.is_user_active_in_conversation(
                event.to_user_id, app_view_key(event.app_id)
            )
        ):
            return
        recipient = await self._local_user(event.to_user_id)
        if recipient is None:
            return
        locale = self._locale(recipient)
        app_name = await self._app_name(event.app_id, locale)
        name = event.from_display or await self._person_name(event.from_user_id, locale)
        if app_name:
            title = self._t(
                "notification.app.turn",
                locale=locale,
                fallback="Your turn with {name} in {app}",
                name=name,
                app=app_name,
            )
        else:
            title = self._t(
                "notification.app.turn_generic",
                locale=locale,
                fallback="Your turn with {name}",
                name=name,
            )
        await self._save_notif(
            new_notification(
                user_id=event.to_user_id,
                type="app_turn",
                title=title,
                link_url=f"/apps/{event.app_id}",
            ),
            about=NotificationSubject.of(
                NotificationObject.APP_SESSION,
                f"{event.app_id}/{event.session_id}",
                space_name=app_name,
            ),
            actor_user_id=event.from_user_id,
            actor_name=event.from_display,
        )

    async def on_app_turns_seen(self, event: AppTurnsSeen) -> None:
        """The player's app opened and took its held messages: those games
        are in front of them, so their rows read as read on every device
        (the next turn re-opens a row)."""
        await self.mark_read_by_objects(
            event.user_id,
            [
                object_key(NotificationObject.APP_SESSION, f"{event.app_id}/{sid}")
                for sid in event.session_ids
            ],
        )

    async def _app_name(
        self, app_id: str, locale: str | None, fallback: str | None = None
    ) -> str | None:
        """*app_id*'s name for a reader of *locale* — every app row
        (invite, continue, waiting, declined, ended, turn, added, asked,
        update) names it this way, in the title and as the row's place:

        1. the catalog's name in their language (the held copy only —
           never a fetch for a row);
        2. else the installed bundle's own ``i18n`` name in their language
           (kept with the install, so right after a restart too), else
           its installed name;
        3. else *fallback* (the name the event carries, e.g. for an app
           this household doesn't have); ``None`` when there is none."""
        entry = (
            self._app_catalog.cached_entry(app_id)
            if self._app_catalog is not None
            else None
        )
        if entry is not None:
            return entry.name_in(locale)
        app = await self._apps.get(app_id) if self._apps is not None else None
        if app is not None and app.name:
            return app.name_in(locale)
        return fallback or None

    async def on_app_challenge_received(
        self,
        event: AppChallengeReceived,
    ) -> None:
        """A Social Home App invite arrived for a local user — bell + push.

        "{name} invited you to play {app}": the app's name is the one this
        household installed it under (any app, not just chess); an app the
        registry doesn't know (no app repo wired) reads "invited you to play
        a game". §25.3 / D11: title only — the inviter's display name (with
        their household for someone from another one) and the app's name
        are labels, not user content; no body on the wire. The app's name
        rides as the row's place (``space_name``) so the client builds the
        same sentence in the viewer's language. The deep link points at the
        app surface so a tap opens it; the SPA's open frame (delivered
        separately over WS) seats the live invite.
        """
        recipient = await self._local_user(event.to_user_id)
        locale = self._locale(recipient)
        app_name = await self._app_name(event.app_id, locale)
        if event.continued:
            # An old game picked up again: same Play / Decline, own words.
            title = self._t(
                "notification.app.continue",
                locale=locale,
                fallback="{name} wants to continue your {app} game",
                name=event.from_display,
                app=app_name or "",
            )
        elif app_name:
            title = self._t(
                "notification.app.challenge",
                locale=locale,
                fallback="{name} invited you to play {app}",
                name=event.from_display,
                app=app_name,
            )
        else:
            title = self._t(
                "notification.app.challenge_generic",
                locale=locale,
                fallback="{name} invited you to play a game",
                name=event.from_display,
            )
        about = NotificationSubject.of(
            NotificationObject.APP_SESSION,
            f"{event.app_id}/{event.session_id}",
            space_name=app_name,
        )
        # An invite that waited for its app (D10) is playable now: its
        # waiting row makes way for this one (a fresh row, so it buzzes).
        await self._resolve(
            about.object_key,
            types=("app_challenge_waiting",),
            outcome="done",
            user_id=event.to_user_id,
        )
        await self._save_notif(
            new_notification(
                user_id=event.to_user_id,
                type=("app_challenge_continue" if event.continued else "app_challenge"),
                title=title,
                link_url=f"/apps/{event.app_id}",
            ),
            # One row per invite: it stays in "Needs you" until that invite
            # is taken in, declined, withdrawn or expires.
            about=about,
            actor_user_id=event.from_user_id,
            actor_name=event.from_display,
        )

    async def on_app_challenge_waiting(self, event: AppChallengeWaiting) -> None:
        """A game invite for an app this household doesn't have or turned
        off (D10): the invitee's "Needs you" row — "{name} invited you to
        play {app}" — with "Ask an admin" / "Ask a grown-up" (admins: "See
        {app}") and Decline. ``flags`` say why it waits (``app_missing`` /
        ``app_off``); the app's name (the household's catalog's or its
        install's) rides as the row's place. The row is about the same
        ``app_session:<app>/<session>`` a playable invite uses, so a
        withdrawal or a decline finds it; it becomes a playable invite once
        the app is here (:meth:`on_app_challenge_received`). Title only
        on the push (§25.3): the inviter and the app are labels.

        **One row per (invitee, app, sender person).** A further invite
        from the same person for the same app joins their row (D7): it
        moves on to the newest session (the one Play takes in once the app
        is here), its count goes up (``params.total``: "invited you 3
        times") and nothing buzzes again."""
        recipient = await self._local_user(event.to_user_id)
        locale = self._locale(recipient)
        app_name = await self._app_name(event.app_id, locale, event.app_name)
        key = object_key(
            NotificationObject.APP_SESSION, f"{event.app_id}/{event.session_id}"
        )
        prefix = object_key(NotificationObject.APP_SESSION, f"{event.app_id}/")
        mine = [
            n
            for n in await self._notifs.find(
                Spec(
                    where=[
                        ("type", "=", "app_challenge_waiting"),
                        ("user_id", "=", event.to_user_id),
                    ]
                )
            )
            if n.is_pending_action
            and (n.object_key or "").startswith(prefix)
            and n.params is not None
            and any(a.id == event.from_user_id for a in n.params.actors)
        ]
        if mine:
            row = max(mine, key=lambda n: n.created_at)
            if row.object_key == key:
                return  # the same invite again
            moved = replace(
                row,
                object_key=key,
                created_at=datetime.now(UTC).isoformat(),
                read_at=None,
                params=regroup(
                    row.params,
                    NotificationActor(event.from_user_id, event.from_display),
                    reopen=row.is_read,
                    space_name=app_name,
                    flags=(event.reason,),
                ),
            )
            await self._notifs.update_group(moved)
            await self._publish_created(
                moved, bumped=True, reopened=row.is_read, silent=True
            )
            if row.is_read:
                await self._announce_unread(moved.user_id)
            return
        await self._save_notif(
            new_notification(
                user_id=event.to_user_id,
                type="app_challenge_waiting",
                title=self._t(
                    "notification.app.challenge",
                    locale=locale,
                    fallback="{name} invited you to play {app}",
                    name=event.from_display,
                    app=app_name or event.app_id,
                ),
                link_url=app_about_link(event.app_id),
            ),
            about=NotificationSubject.of(
                NotificationObject.APP_SESSION,
                f"{event.app_id}/{event.session_id}",
                space_name=app_name,
            ),
            actor_user_id=event.from_user_id,
            actor_name=event.from_display,
            flags=(event.reason,),
        )

    async def on_app_requested(self, event: AppRequested) -> None:
        """ "Ask an admin" (D4): one "Needs you" row per decider
        (``event.recipients``: the admins, or a protected asker's
        guardians), about ``app:<id>`` so every ask for the app joins one
        row ("Milo and Kiki asked for Chess"), opening the app's detail
        page where it can be added. Asking again while the row is unread
        is silent (one buzz per request). Adding the app or "Not now"
        resolves it (:meth:`on_app_request_answered`)."""
        asker = await self._local_user(event.requested_by)
        if asker is None:
            return
        for recipient_id in event.recipients:
            recipient = await self._local_user(recipient_id)
            if recipient is None:
                continue
            locale = self._locale(recipient)
            app_name = await self._app_name(event.app_id, locale, event.name)
            about = NotificationSubject.of(
                NotificationObject.APP, event.app_id, space_name=app_name
            )
            await self._save_notif(
                new_notification(
                    user_id=recipient_id,
                    type="app_request",
                    title=self._t(
                        "notification.app.requested",
                        locale=locale,
                        fallback="{name} asked for {app}",
                        name=asker.display_name,
                        app=app_name or event.app_id,
                    ),
                    link_url=app_about_link(event.app_id),
                ),
                about=about,
                actor_user_id=asker.user_id,
                actor_name=asker.display_name,
            )

    async def on_app_request_answered(
        self, event: AppInstalled | AppRequestDismissed | AppEnabledChanged
    ) -> None:
        """The app was added ("approved", by the installer) or turned back
        on (asked for after an admin turned it off, D10), or an admin said
        "Not now" ("declined"): every decider's request row for it is
        handled and leaves Needs you on each device."""
        if isinstance(event, AppEnabledChanged):
            if not event.enabled:
                return
            outcome, by = "approved", None
        elif isinstance(event, AppInstalled):
            outcome, by = "approved", event.installed_by or None
        else:
            outcome, by = "declined", event.dismissed_by
        await self._resolve(
            object_key(NotificationObject.APP, event.app_id),
            types=("app_request",),
            outcome=outcome,
            by_user_id=by,
        )

    async def on_app_update_available(self, event: AppUpdateAvailable) -> None:
        """A newer compatible version of an installed app (D7): **one**
        "Needs you" row per admin (``event.recipients``) per app, about
        ``app:<id>``, opening the app's detail page (What's new, Update).
        The row's body is the version it offers.

        The daily check repeats this event while the update waits: an
        admin whose row already offers this version gets nothing (no
        second row, no second push, whether or not they read it). An even
        newer version resolves the older rows and re-opens them with the
        new version — one row, never two. Updating, uninstalling or the
        update going away resolves it (:meth:`on_app_update_handled`)."""
        key = object_key(NotificationObject.APP, event.app_id)
        pending = [
            n
            for n in await self._notifs.find(Spec(where=[("object_key", "=", key)]))
            if n.type == "app_update" and n.is_pending_action
        ]
        if any(n.body != event.version for n in pending):
            await self._resolve(key, types=("app_update",), outcome="done")
            pending = []
        told = {n.user_id for n in pending}
        for recipient_id in event.recipients:
            if recipient_id in told:
                continue
            recipient = await self._local_user(recipient_id)
            if recipient is None or not recipient.is_admin:
                continue
            locale = self._locale(recipient)
            app_name = await self._app_name(event.app_id, locale, event.name)
            about = NotificationSubject.of(
                NotificationObject.APP, event.app_id, space_name=app_name
            )
            await self._save_notif(
                new_notification(
                    user_id=recipient_id,
                    type="app_update",
                    title=self._t(
                        "notification.app.update_available",
                        locale=locale,
                        fallback="{app} has an update",
                        app=app_name or event.app_id,
                    ),
                    body=event.version,
                    link_url=app_about_link(event.app_id),
                ),
                about=about,
                # Nobody caused it: the catalog has a newer version.
                actor_user_id=None,
            )

    async def on_app_update_handled(
        self, event: AppUpdated | AppUpdateGone | AppUninstalled
    ) -> None:
        """An admin updated the app ("Handled by Anna"), the update went
        away, or the app was removed: every admin's update row for it
        leaves Needs you."""
        by = event.updated_by if isinstance(event, AppUpdated) else None
        await self._resolve(
            object_key(NotificationObject.APP, event.app_id),
            types=("app_update",),
            outcome="done",
            by_user_id=by,
        )

    async def pending_app_requests(self, user_id: str) -> frozenset[str]:
        """The apps *user_id* has asked for that nobody has answered yet
        — what the Apps page shows as "Asked". Read from the deciders'
        open ``app_request`` rows (the request has no table of its own)."""
        rows = await self._notifs.find(Spec(where=[("type", "=", "app_request")]))
        out: set[str] = set()
        for n in rows:
            if not n.is_pending_action or not n.object_key or n.params is None:
                continue
            if any(a.id == user_id for a in n.params.actors):
                out.add(n.object_key.split(":", 1)[1])
        return frozenset(out)

    async def on_app_installed(self, event: AppInstalled) -> None:
        """A new app in the household (D13): "Anna added Chess".

        One in-app row per member — never the admin who installed it, and
        never someone the app's age gate keeps out (:func:`app_age_allows`,
        the gate the runtime and the app list use: a protected account
        doesn't learn of an app it can't open). Filed under Activity
        (``app_added``), In app by default: a row, no push. The row is
        about ``app:<id>`` and opens the app's entry on the Apps page; the
        app's name rides as the row's place so the client builds the
        sentence in the viewer's language. At most one a day per app
        (:data:`APP_ADDED_QUIET`): a reinstall the same day adds nothing,
        a later one joins the row like any group (D7)."""
        installer_id = event.installed_by or None
        installer = await self._local_user(installer_id) if installer_id else None
        name = installer.display_name if installer is not None else None
        blocked = (
            await self._withheld_from(installer_id) if installer_id else frozenset()
        )
        key = object_key(NotificationObject.APP, event.app_id)
        audience = [
            u
            for u in await self._active_users()
            if u.user_id != installer_id
            and u.user_id not in blocked
            and app_age_allows(
                event.min_age,
                protected=bool(u.child_protection_enabled),
                declared_age=u.declared_age,
            )
        ]
        since = (datetime.now(UTC) - APP_ADDED_QUIET).isoformat()
        told = await self._notifs.latest_in_groups(
            [(u.user_id, "app_added", key) for u in audience],
            since=since,
        )
        for user in audience:
            if (user.user_id, "app_added", key) in told:
                continue
            locale = self._locale(user)
            app_name = await self._app_name(event.app_id, locale, event.name)
            await self._save_notif(
                new_notification(
                    user_id=user.user_id,
                    type="app_added",
                    title=self._t(
                        "notification.app.added",
                        locale=locale,
                        fallback="{name} added {app}",
                        name=name or self._someone(locale),
                        app=app_name or event.app_id,
                    ),
                    link_url=app_link(event.app_id),
                ),
                about=NotificationSubject.of(
                    NotificationObject.APP, event.app_id, space_name=app_name
                ),
                actor_user_id=installer_id,
                actor_name=name,
                withheld=blocked,
            )

    async def on_bazaar_bid_placed(self, event: BazaarBidPlaced) -> None:
        """Notify the seller that a bid landed (or was raised)."""
        if event.bidder_user_id == event.seller_user_id:
            return
        recipient = await self._local_user(event.seller_user_id)
        title = self._t(
            "notification.bazaar.bid_placed",
            locale=self._locale(recipient),
            fallback="New bid on your listing",
        )
        await self._save_notif(
            new_notification(
                user_id=event.seller_user_id,
                type="bazaar_bid_placed",
                title=title,
                link_url=bazaar_listing_link(event.listing_post_id),
            ),
            about=NotificationSubject.of(
                NotificationObject.LISTING, event.listing_post_id
            ),
            actor_user_id=event.bidder_user_id,
        )

    async def on_bazaar_offer_accepted(
        self,
        event: BazaarOfferAccepted,
    ) -> None:
        """Notify the buyer that the seller accepted their offer."""
        recipient = await self._local_user(event.buyer_user_id)
        title = self._t(
            "notification.bazaar.offer_accepted",
            locale=self._locale(recipient),
            fallback="Your offer was accepted",
        )
        await self._save_notif(
            new_notification(
                user_id=event.buyer_user_id,
                type="bazaar_offer_accepted",
                title=title,
                link_url=bazaar_listing_link(event.listing_post_id),
            ),
            about=NotificationSubject.of(
                NotificationObject.LISTING, event.listing_post_id
            ),
            actor_user_id=event.seller_user_id,
        )

    async def on_bazaar_offer_rejected(
        self,
        event: BazaarOfferRejected,
    ) -> None:
        recipient = await self._local_user(event.bidder_user_id)
        title = self._t(
            "notification.bazaar.offer_rejected",
            locale=self._locale(recipient),
            fallback="Your offer was declined",
        )
        await self._save_notif(
            new_notification(
                user_id=event.bidder_user_id,
                type="bazaar_offer_rejected",
                title=title,
                link_url=bazaar_listing_link(event.listing_post_id),
            ),
            about=NotificationSubject.of(
                NotificationObject.LISTING, event.listing_post_id
            ),
            actor_user_id=event.seller_user_id,
        )

    async def on_bazaar_listing_expired(
        self,
        event: BazaarListingExpired,
    ) -> None:
        """Notify the seller whenever a listing transitions to sold/expired."""
        recipient = await self._local_user(event.seller_user_id)
        if event.final_status == "sold":
            title = self._t(
                "notification.bazaar.sold",
                locale=self._locale(recipient),
                fallback="Your listing sold",
            )
        else:
            title = self._t(
                "notification.bazaar.expired",
                locale=self._locale(recipient),
                fallback="Your listing expired without a buyer",
            )
        await self._save_notif(
            new_notification(
                user_id=event.seller_user_id,
                type=f"bazaar_listing_{event.final_status}",
                title=title,
                link_url=bazaar_listing_link(event.listing_post_id),
            ),
            about=NotificationSubject.of(
                NotificationObject.LISTING, event.listing_post_id
            ),
            actor_user_id=None,  # the listing's own clock
        )

    async def on_space_post_created(self, event: SpacePostCreated) -> None:
        """Notify space members (except the author). Space name is included
        in the title for context. Body is omitted per §25.3.

        The plain "new post" row groups on the space's feed
        (``space_feed:<space_id>``, opening ``/spaces/<id>``): a burst of
        posts is one row that counts them and names who posted (D5). A
        mention or an ``@here`` stays about the post itself.

        See :meth:`_notify_space_members` for how the per-member
        :table:`space_notif_prefs` level and ``event.mentions`` combine.

        A calendar event's feed card (``PostType.EVENT`` with a
        ``linked_event_id``, minted by :class:`CalendarFeedBridge` on every
        household) rings nobody: the event already rang its own
        ``calendar_event_created`` bell — one bell per event.
        """
        if event.post.type is PostType.EVENT and event.post.linked_event_id:
            return
        await self._notify_space_members(
            space_id=event.space_id,
            author_id=event.post.author,
            mentions=event.mentions,
            link=space_post_link(event.space_id, event.post.id),
            post_id=event.post.id,
            generic_feed=True,
            generic_type="space_post_created",
            generic_key="notification.space.post.created",
            generic_fallback="{author} posted in {space_name}",
            mention_key="notification.space.mention",
            mention_fallback="{author} mentioned you in {space_name}",
        )

    async def _notify_space_members(
        self,
        *,
        space_id: str,
        author_id: str,
        mentions: tuple[Mention, ...],
        link: str,
        post_id: str,
        comment_id: str | None = None,
        generic_feed: bool = False,
        generic_type: str | None,
        generic_key: str,
        generic_fallback: str,
        mention_key: str,
        mention_fallback: str,
        post_author_id: str | None = None,
        parent_author_id: str | None = None,
        commenter_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Fan a space-content row out to this household's space members.

        Per member (never the author, so a self-mention is silent), at their
        level (:meth:`_space_level` — their choice, else the D5 default):

        * ``muted`` → nothing, mentioned or not.
        * mentioned (a resolved ``MentionType.USER`` in *mentions*) → one
          ``space_mention`` row with a push — never also the generic one.
        * a comment answering their comment / on their post (D6) →
          ``comment_replied`` / ``post_replied`` with a push, at ``all`` and
          ``mentions`` alike.
        * ``@here`` → ``space_here`` with a push, members only — never a
          follower (subscriber).
        * otherwise the generic row (``generic_type``) **in the app only**,
          no push: at ``all``, or — for a comment — at ``mentions`` when they
          commented on that post too ("also commented").

        ``@here`` (a ``MentionType.HERE`` entry — the resolver keeps it only
        when the space allows it and the author is owner/admin by this
        household's roster) pages at most once per space per
        :data:`HERE_COOLDOWN_SECONDS`; a later one inside the window
        notifies like a plain post. The window is read back from the
        ``space_here`` rows themselves, so it survives a restart.

        ``generic_type=None`` (an edit, whose *mentions* are only the newly
        added ones) → no generic row and no reply row for anyone: only the
        mentioned (and a newly added, still-allowed, not-rate-limited
        ``@here``) are rung.

        The member list is the local ``space_members`` roster, so a mention can
        only ever reach a member — the resolver never resolves outsiders.

        ``link`` is the post (and comment) every row of this fan-out opens.
        Every row is about the post (``post_id``) — a mention in a comment
        about that comment (``comment_id``), so deleting it drops the row.
        ``generic_feed`` (a new post): the generic row is about the space's
        feed instead and opens it — one row for a burst of posts (D5).
        """
        space = await self._spaces.get(space_id)
        if space is None:
            return
        author = await self._local_user(author_id)
        # A post federated from another household names a remote author.
        remote_author = (
            await self._users.get_remote(author_id) if author is None else None
        )
        name = (
            author.display_name
            if author
            else remote_author.display_name
            if remote_author and remote_author.display_name
            else None
        )
        mentioned = {m.user_id for m in mentions if m.user_id}
        here = any(m.type is MentionType.HERE for m in mentions) and (
            await self._here_window_open(space_id)
        )
        post_about = NotificationSubject.of(
            NotificationObject.SPACE_POST,
            post_id,
            space_id=space_id,
            space_name=space.name,
        )
        mention_about = (
            NotificationSubject.of(
                NotificationObject.SPACE_COMMENT,
                comment_id,
                space_id=space_id,
                space_name=space.name,
            )
            if comment_id
            else post_about
        )
        generic_about, generic_link = (
            (
                NotificationSubject.of(
                    NotificationObject.SPACE_FEED,
                    space_id,
                    space_id=space_id,
                    space_name=space.name,
                ),
                space_feed_link(space_id),
            )
            if generic_feed
            else (post_about, link)
        )
        is_comment = comment_id is not None and generic_type is not None
        members = await self._space_members(space_id)
        blocked = await self._withheld_from(author_id)
        for member in members:
            if member.user_id == author_id or member.user_id in blocked:
                continue
            level = await self._space_level(member, space)
            if level is SpaceNotifLevel.MUTED:
                continue
            reply = (
                _reply_kind(
                    member.user_id,
                    post_author=post_author_id,
                    parent_author=parent_author_id,
                    in_space=True,
                )
                if is_comment
                else None
            )
            about, row_link = post_about, link
            if member.user_id in mentioned:
                ntype, key, fallback = "space_mention", mention_key, mention_fallback
            elif reply is not None:
                ntype, key, fallback = reply
            elif here and member.role in WRITER_ROLES:
                ntype, key, fallback = (
                    "space_here",
                    "notification.space.here",
                    "{author} notified everyone in {space_name}",
                )
            elif generic_type is not None and (
                level is SpaceNotifLevel.ALL
                or (is_comment and member.user_id in commenter_ids)
            ):
                ntype, key, fallback = generic_type, generic_key, generic_fallback
                about, row_link = generic_about, generic_link
            else:
                continue
            recipient = await self._local_user(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type=ntype,
                    title=self._t(
                        key,
                        locale=self._locale(recipient),
                        fallback=fallback,
                        author=name or self._someone(self._locale(recipient)),
                        space_name=space.name,
                    ),
                    link_url=row_link,
                ),
                about=mention_about if ntype == "space_mention" else about,
                actor_user_id=author_id,
                actor_name=name,
                withheld=blocked,
            )

    async def _space_prefs(
        self, batch: _DeliveryBatch, space_id: str
    ) -> dict[str, SpaceNotifPref]:
        """Every member's choice in *space_id*, read once per job."""
        prefs = batch.space_prefs.get(space_id)
        if prefs is None:
            prefs = await self._notifs.space_notif_prefs_in(space_id)
            batch.space_prefs[space_id] = prefs
        return prefs

    async def _space_members(self, space_id: str) -> list:
        """*space_id*'s local roster, its people looked up in one read for
        the rest of the delivery job (:meth:`_prefetch_users`)."""
        members = await self._spaces.list_members(space_id)
        await self._prefetch_users(m.user_id for m in members)
        return members

    async def _active_users(self) -> list[User]:
        """Every active household member, kept for the rest of the delivery
        job so the gate does not read each of them again."""
        users = await self._users.list_active()
        self._remember_users(users)
        return users

    async def _space_level(self, member, space) -> SpaceNotifLevel:
        """*member*'s level in *space*: the one they chose, else the default
        for the space's type and their seat (D5) — "Everything" in a
        household or private space, "Replies and mentions" in a public or
        global one and wherever they only follow.

        In a delivery job the space's choices are read once for all its
        members (:meth:`_space_prefs`)."""
        batch = _BATCH.get()
        if batch is not None:
            pref = (await self._space_prefs(batch, space.id)).get(member.user_id)
            chosen = pref.effective(datetime.now(UTC)) if pref else None
        else:
            chosen = await self._notifs.get_space_notif_level(
                user_id=member.user_id, space_id=space.id
            )
        if chosen:
            try:
                return SpaceNotifLevel(chosen)
            except ValueError:
                pass
        return default_space_notif_level(space.space_type, member.role)

    async def _here_window_open(self, space_id: str) -> bool:
        """Whether an ``@here`` in *space_id* may page now: nobody here got a
        ``space_here`` row about the space in the last
        :data:`HERE_COOLDOWN_SECONDS` (per space, whoever sent it)."""
        since = datetime.now(UTC) - timedelta(seconds=HERE_COOLDOWN_SECONDS)
        return not await self._notifs.exists_in_space_since(
            type="space_here", space_id=space_id, since=since.isoformat()
        )

    async def on_moderation_queued(self, event: SpaceModerationQueued) -> None:
        """Notify everyone who works the queue (owner / admin / moderator)
        that content is pending review."""
        space = await self._spaces.get(event.item.space_id)
        if space is None:
            return
        members = await self._space_members(event.item.space_id)
        for member in members:
            if member.role in CONTENT_AUTHORITY_ROLES:
                recipient = await self._local_user(member.user_id)
                await self._save_notif(
                    new_notification(
                        user_id=member.user_id,
                        type="moderation_pending",
                        title=self._t(
                            "notification.space.moderation.queued",
                            locale=self._locale(recipient),
                            fallback="New content pending review in {space_name}",
                            space_name=space.name,
                        ),
                        link_url=space_moderation_link(event.item.space_id),
                    ),
                    about=NotificationSubject.of(
                        NotificationObject.SPACE,
                        event.item.space_id,
                        space_id=event.item.space_id,
                        space_name=space.name,
                    ),
                    # A duty, not news about a person: the title names
                    # nobody, and blocking a submitter must not hide the
                    # queue from its moderators.
                    actor_user_id=None,
                )

    async def on_report_filed(self, event: ReportFiled) -> None:
        """A space-scoped report: tell the space's content authority
        (owner / admin / moderator) on this household — except the reporter
        and the report's subject (the reported member / the item's author,
        unless they are the space's sole authority) — title only, never the category, notes or
        reporter (§25.3). Household-level reports
        notify nobody (household admins look in the admin page)."""
        if not event.space_id:
            return
        space = await self._spaces.get(event.space_id)
        if space is None or space.dissolved:
            return
        members = await self._space_members(event.space_id)
        authority = [m for m in members if m.role in CONTENT_AUTHORITY_ROLES]
        subject = event.subject_user_id
        # The subject reviews a report about themself only as the space's
        # sole content authority (its owner, nobody else anywhere) — the
        # ``ReportService.review_space`` rule.
        subject_reviews = (
            subject is not None
            and len(authority) == 1
            and authority[0].user_id == subject
            and authority[0].role == SpaceRole.OWNER
            and not (
                self._remote_members is not None
                and await self._remote_members.list_instances_with_roles(
                    event.space_id,
                    frozenset({SpaceRole.ADMIN.value, SpaceRole.MODERATOR.value}),
                )
            )
        )
        for member in authority:
            if member.user_id == event.reporter_user_id:
                continue
            if member.user_id == subject and not subject_reviews:
                continue  # never told about a report on themself
            recipient = await self._local_user(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_report",
                    title=self._t(
                        "notification.space.report.filed",
                        locale=self._locale(recipient),
                        fallback="New report in {space_name}",
                        space_name=space.name,
                    ),
                    link_url=space_moderation_link(event.space_id),
                ),
                about=NotificationSubject.of(
                    NotificationObject.SPACE,
                    event.space_id,
                    space_id=event.space_id,
                    space_name=space.name,
                ),
                actor_user_id=None,  # a duty; the reporter stays unnamed
            )

    async def on_remote_seat_live(self, event: SpaceRemoteSeatLive) -> None:
        """A household's FIRST member just took a seat in a space that keeps
        a feature other than posts "Reviewed", and that household is too old
        to submit its members' changes for review (below v_43, federated
        moderation): every receiver refuses those changes. Tell the space's
        local owner / admins once, so they ask that household to update or
        pick another level. A v_43 household needs no warning."""
        if self._remote_members is None:
            return
        if self._instances is not None:
            peer = await self._instances.get_instance(event.instance_id)
            if (
                peer is not None
                and peer.proto_version
                >= FederationCapability.MIN_FOR_FEDERATED_MODERATION
            ):
                return
        space = await self._spaces.get(event.space_id)
        if space is None or space.dissolved:
            return
        reviewed = [
            f
            for f in ("pages", "tasks", "stickies", "calendar")
            if space.features.access_level(f) is SpaceFeatureAccess.MODERATED
        ]
        if not reviewed:
            return
        seats = await self._remote_members.list_for_instance(
            event.space_id, event.instance_id, include_tombstoned=False
        )
        if len(seats) > 1:
            return  # not the household's first member — already told
        for member in await self._space_members(event.space_id):
            if member.role not in SETTINGS_AUTHORITY_ROLES:
                continue
            recipient = await self._local_user(member.user_id)
            locale = self._locale(recipient)
            names = ", ".join(
                self._t(
                    f"notification.space.feature.{f}",
                    locale=locale,
                    fallback=_FEATURE_FALLBACK[f],
                )
                for f in reviewed
            )
            # One push per (space, household): the link names the household,
            # so its rejoin bumps the existing bell in-app (read or not) and
            # only a household that is new here pushes again. The link opens
            # Settings › Members & joining, where the access levels are. A
            # row stored before that (``/settings?household=…``, which the
            # SPA redirects there) stays the household's key: a rejoin
            # keeps using that link, so it does not push again and an
            # unread row is bumped rather than doubled.
            link = (
                f"/spaces/{event.space_id}/settings/members"
                f"?household={event.instance_id}"
            )
            legacy = f"/spaces/{event.space_id}/settings?household={event.instance_id}"
            told = await self._told(member.user_id, "moderation_unavailable", link)
            if not told and await self._told(
                member.user_id, "moderation_unavailable", legacy
            ):
                told, link = True, legacy
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="moderation_unavailable",
                    title=self._t(
                        "notification.space.moderation.unavailable",
                        locale=locale,
                        fallback=(
                            "Reviewed isn't available for {features} in "
                            "{space_name} for members of a household that "
                            "needs an update — their changes are refused until "
                            "it updates or you choose another level"
                        ),
                        features=names,
                        space_name=space.name,
                    ),
                    link_url=link,
                ),
                about=NotificationSubject.of(
                    NotificationObject.SPACE_HOUSEHOLD,
                    f"{event.space_id}/{event.instance_id}",
                    space_id=event.space_id,
                    space_name=space.name,
                ),
                actor_user_id=None,
                push=not told,
            )

    async def _told(self, user_id: str, kind: str, link: str) -> bool:
        """Has ``user_id`` ever had a ``kind`` notification with ``link``?"""
        return bool(
            await self._notifs.find(
                Spec(
                    where=[
                        ("user_id", "=", user_id),
                        ("type", "=", kind),
                        ("link_url", "=", link),
                    ],
                    limit=1,
                )
            )
        )

    async def on_moderation_decided(
        self,
        event: SpaceModerationApproved
        | SpaceModerationRejected
        | SpaceModerationExpired,
    ) -> None:
        """Tell the submitter what became of their queued item
        (``moderation_decided``). Title only — never the content, never a
        moderator's rejection reason (§25.3); the app shows those."""
        item = event.item
        space = await self._spaces.get(item.space_id)
        if space is None:
            return
        if await self._spaces.get_member(item.space_id, item.submitted_by) is None:
            return  # they left: nothing of theirs to come back to
        recipient = await self._local_user(item.submitted_by)
        if recipient is None:
            return
        outcome = item.status.value  # approved | rejected | expired
        fallback = {
            "approved": "Your submission in {space_name} was approved",
            "rejected": "Your submission in {space_name} was not approved",
        }.get(outcome, "Your submission in {space_name} expired without a review")
        await self._save_notif(
            new_notification(
                user_id=item.submitted_by,
                type="moderation_decided",
                title=self._t(
                    f"notification.space.moderation.{outcome}",
                    locale=self._locale(recipient),
                    fallback=fallback,
                    space_name=space.name,
                ),
                link_url=f"/spaces/{item.space_id}",
            ),
            about=NotificationSubject.of(
                NotificationObject.MODERATION_ITEM,
                item.id,
                space_id=item.space_id,
                space_name=space.name,
            ),
            actor_user_id=None,  # a decision about your own content
        )

    async def on_calendar_event_created(
        self,
        event: CalendarEventCreated,
    ) -> None:
        """Notify only the people the event is actually for.

        Two paths depending on which calendar the event lives on:

        * **Personal calendar.** The ``calendars`` table holds one row
          per user's personal calendar; the event's ``calendar_id``
          points at one of those rows. Only the calendar's owner needs
          a bell, and only when *someone else* added the event — a
          user adding a reminder to their own calendar should never
          notify themselves.

          Something done *to you* ("Plans and tasks"): it pushes.

        * **Space calendar.** Space events live in
          ``space_calendar_events`` and don't have a row in
          ``calendars``; their ``calendar_id`` is the
          :class:`Space` id directly. Members of that space (minus the
          creator) at "Everything" get an in-app row, no push — activity in
          a space, like a new post (D5); "Replies and mentions" and "Nothing"
          get none.

        The old behavior fanned out to every active household member
        regardless of whose calendar the event lived on, which spammed
        users about events they had nothing to do with. The user
        report that drove the rewrite is on file in the PR notes.
        """
        cal_event = event.event
        # Personal-calendar branch — only fires if the calendar row
        # exists in ``calendars`` (i.e. the event lives on a personal
        # calendar, not a space one).
        if self._personal_calendar_repo is not None:
            try:
                cal = await self._personal_calendar_repo.get_calendar(
                    cal_event.calendar_id,
                )
            except Exception:
                log.debug(
                    "NotificationService.on_calendar_event_created: fail-soft error ignored",
                    exc_info=True,
                )
                cal = None
            if cal is not None:
                owner = await self._users.get(cal.owner_username)
                if owner is None:
                    return
                if owner.user_id == cal_event.created_by:
                    # The owner added the event to their own calendar —
                    # no self-notification.
                    return
                await self._save_notif(
                    new_notification(
                        user_id=owner.user_id,
                        type="calendar_event_created",
                        title=self._t(
                            "notification.calendar.created",
                            locale=self._locale(owner),
                            fallback="New event: {summary}",
                            summary=cal_event.summary,
                        ),
                        link_url=HOUSEHOLD_CALENDAR_LINK,
                    ),
                    about=NotificationSubject.of(
                        NotificationObject.EVENT, cal_event.id
                    ),
                    actor_user_id=cal_event.created_by,
                    push_params={
                        "name": await self._person_name(
                            cal_event.created_by, self._locale(owner)
                        )
                    },
                )
                return
        # Space-calendar branch — ``calendar_id`` is the space_id.
        # Notify members of that space (except the creator). If the
        # space doesn't exist either (e.g., a misrouted federation
        # event), bail silently rather than reverting to the
        # household-wide fanout.
        space = await self._spaces.get(cal_event.calendar_id)
        if space is None:
            return
        members = await self._space_members(cal_event.calendar_id)
        blocked = await self._withheld_from(cal_event.created_by)
        for member in members:
            if member.user_id == cal_event.created_by:
                continue
            if await self._space_level(member, space) is not SpaceNotifLevel.ALL:
                continue
            recipient = await self._local_user(member.user_id)
            if recipient is None:
                continue
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="calendar_event_created",
                    title=self._t(
                        "notification.calendar.created",
                        locale=self._locale(recipient),
                        fallback="New event: {summary}",
                        summary=cal_event.summary,
                    ),
                    link_url=space_calendar_link(cal_event.calendar_id),
                ),
                about=NotificationSubject.of(
                    NotificationObject.EVENT,
                    cal_event.id,
                    space_id=space.id,
                    space_name=space.name,
                ),
                actor_user_id=cal_event.created_by,
                withheld=blocked,
            )

    async def on_calendar_event_deleted(
        self,
        event: CalendarEventDeleted,
    ) -> None:
        """Phase D: tell this household's people who were coming that the
        event is cancelled.

        ``notify_user_ids`` is the cohort captured before the FK CASCADE
        wiped the RSVP rows — by :meth:`SpaceCalendarService.delete_event`
        for a local cancel, by the inbound ``SPACE_CALENDAR_EVENT_DELETED``
        handler for one made in another household.
        """
        # The event is gone: its own rows go too (its "new event" row, a
        # reminder, an earlier change) — the cancellation below replaces them.
        await self._forget([object_key(NotificationObject.EVENT, event.event_id)])
        await self._tell_still_coming(
            event.notify_user_ids,
            about=NotificationSubject.of(
                NotificationObject.EVENT, event.event_id, space_id=event.space_id
            ),
            actor_user_id=event.actor_user_id,
            ntype="calendar_event_cancelled",
            key="notification.calendar.cancelled",
            fallback="Event cancelled: {summary}",
            summary=event.summary,
            link_url=(
                space_calendar_link(event.space_id)
                if event.space_id
                else HOUSEHOLD_CALENDAR_LINK
            ),
        )

    async def on_calendar_event_updated(
        self,
        event: CalendarEventUpdated,
    ) -> None:
        """Phase D: tell this household's people who are coming when the
        event moves — only on material changes.

        Material = start / end / summary / capacity-down. Cosmetic
        updates (description, attendees, rrule, all_day) stay silent so
        members don't get notification spam from incidental edits. Fires
        for a local edit and for one made in another household alike.
        """
        if not event.material_changes:
            return
        if self._calendar_repo is None:
            return
        cal_event = event.event
        try:
            rsvps = await self._calendar_repo.list_rsvps(cal_event.id)
        except Exception:
            log.debug(
                "NotificationService.on_calendar_event_updated: fail-soft error ignored",
                exc_info=True,
            )
            return
        # A space event's ``calendar_id`` is the space id; anything else
        # is a personal calendar on the household calendar page.
        space = await self._spaces.get(cal_event.calendar_id)
        link = (
            space_calendar_link(cal_event.calendar_id)
            if space is not None
            else HOUSEHOLD_CALENDAR_LINK
        )
        await self._tell_still_coming(
            [r.user_id for r in rsvps if r.status in RSVPStatus.STILL_COMING],
            about=NotificationSubject.of(
                NotificationObject.EVENT,
                cal_event.id,
                space_id=space.id if space is not None else None,
                space_name=space.name if space is not None else None,
            ),
            actor_user_id=event.actor_user_id,
            ntype="calendar_event_updated",
            key="notification.calendar.updated",
            fallback="Event updated: {summary}",
            summary=cal_event.summary,
            link_url=link,
        )

    async def _tell_still_coming(
        self,
        cohort: Iterable[str],
        *,
        about: NotificationSubject,
        actor_user_id: str | None,
        ntype: str,
        key: str,
        fallback: str,
        summary: str | None,
        link_url: str,
    ) -> None:
        """One bell per person in *cohort* who lives in this household.

        The bell names the event (*summary*); one without a name (an old
        delete that carried none) reads like its push. The push names only
        who made the change (D11).

        The cohort holds every household's answers (RSVPs federate), so
        each household tells its own people and nobody is told twice — a
        person with answers for several occurrences included. Never the
        person who made the change (the gate lets one's own action through,
        so that is checked here); :meth:`_save_notif` keeps out anyone who
        blocked them or whom a guardian block separates from them.
        """
        withheld = await self._withheld_from(actor_user_id) if actor_user_id else None
        for uid in dict.fromkeys(cohort):
            if uid == actor_user_id:
                continue
            recipient = await self._local_user(uid)
            if recipient is None:
                continue  # another household's person: their household tells them
            locale = self._locale(recipient)
            name = (
                await self._person_name(actor_user_id, locale, space_id=about.space_id)
                if actor_user_id
                else None
            )
            if summary:
                title = self._t(key, locale=locale, fallback=fallback, summary=summary)
            else:
                title = self._content_free_title(ntype, locale, {"name": name})
            await self._save_notif(
                new_notification(
                    user_id=uid,
                    type=ntype,
                    title=title,
                    link_url=link_url,
                ),
                about=about,
                actor_user_id=actor_user_id,
                actor_name=name,
                withheld=withheld,
                push_params={"name": name},
            )

    async def on_event_reminder_due(self, event: EventReminderDue) -> None:
        """Phase D: deliver the user's chosen reminder."""
        recipient = await self._local_user(event.user_id)
        if recipient is None:
            return
        await self._save_notif(
            new_notification(
                user_id=event.user_id,
                type="calendar_reminder",
                title=self._t(
                    "notification.calendar.reminder",
                    locale=self._locale(recipient),
                    fallback="Reminder: {summary}",
                    summary=event.summary,
                ),
                link_url=space_calendar_link(event.space_id),
            ),
            about=NotificationSubject.of(
                NotificationObject.EVENT, event.event_id, space_id=event.space_id
            ),
            actor_user_id=None,  # the user's own reminder
        )

    async def notify_upcoming_event(
        self, user, *, event_id: str, summary: str, minutes: int
    ) -> None:
        """The household calendar's "starts soon" bell for *user*
        (:class:`CalendarReminderScheduler`): "{summary} — in {minutes} min",
        or "— now" once it starts, in their language. The push says only
        "Event reminder" (D11)."""
        locale = self._locale(user)
        if minutes <= 0:
            title = self._t(
                "notification.calendar.starting_now",
                locale=locale,
                fallback="{summary} — now",
                summary=summary,
            )
        else:
            title = self._t(
                "notification.calendar.starting_soon",
                locale=locale,
                fallback="{summary} — in {minutes} min",
                summary=summary,
                minutes=minutes,
            )
        await self._save_notif(
            new_notification(
                user_id=user.user_id,
                type="calendar_reminder",
                title=title,
                link_url=f"{HOUSEHOLD_CALENDAR_LINK}?event={event_id}",
            ),
            about=NotificationSubject.of(NotificationObject.EVENT, event_id),
            actor_user_id=None,  # the clock
        )

    async def on_task_completed(self, event: TaskCompleted) -> None:
        """Notify task assignees when a task is completed.

        A space task notifies only assignees who are still local members
        of the space and links to the space's tasks tab."""
        task = event.task
        completed_by = event.completed_by
        link_url = task_link(task, event.space_id)
        blocked = await self._withheld_from(completed_by)
        members: set[str] | None = None
        if event.space_id is not None:
            members = set(await self._spaces.list_local_member_user_ids(event.space_id))
        space_name = await self._space_name(event.space_id)
        for uid in getattr(task, "assignees", ()):
            if uid == completed_by:
                continue
            if members is not None and uid not in members:
                continue
            recipient = await self._local_user(uid)
            if recipient is None or uid in blocked:
                continue
            name = await self._person_name(
                completed_by, self._locale(recipient), space_id=event.space_id
            )
            await self._save_notif(
                new_notification(
                    user_id=uid,
                    type="task_completed",
                    title=self._t(
                        "notification.task.completed",
                        locale=self._locale(recipient),
                        fallback="{name} completed: {title}",
                        name=name,
                        title=task.title,
                    ),
                    link_url=link_url,
                ),
                about=NotificationSubject.of(
                    NotificationObject.TASK,
                    task.id,
                    space_id=event.space_id,
                    space_name=space_name,
                ),
                actor_user_id=completed_by,
                actor_name=name,
                withheld=blocked,
                push_params={"name": name, "space_name": space_name},
            )

    async def on_space_post_moderated(
        self,
        event: SpacePostModerated,
    ) -> None:
        """Notify the post author that their post was moderated (a remote
        author is skipped by the gate — their household tells them)."""
        post = event.post
        recipient = await self._local_user(post.author)
        if recipient is None:
            return  # a remote author: their household tells them
        space_name = await self._space_name(event.space_id)
        locale = self._locale(recipient)
        if space_name:
            title = self._t(
                "notification.space.post.moderated",
                locale=locale,
                fallback="Your post in {space_name} was moderated",
                space_name=space_name,
            )
        else:
            title = self._t(
                "notification.post.moderated",
                locale=locale,
                fallback="Your post was moderated",
            )
        await self._save_notif(
            new_notification(
                user_id=post.author,
                type="post_moderated",
                title=title,
                link_url=f"/spaces/{event.space_id}",
            ),
            about=NotificationSubject.of(
                NotificationObject.SPACE_POST,
                post.id,
                space_id=event.space_id,
                space_name=space_name,
            ),
            actor_user_id=None,  # a decision about your own content
        )

    async def on_space_member_joined(self, event: SpaceMemberJoined) -> None:
        """Tell existing members that a new person joined (§23.52) — an
        in-app row for members at "Everything", no push: activity in a space
        (D5). Not for an automatic household seat: everyone in the home is a
        member of a household space, so "X joined" would be noise."""
        if event.automatic:
            return
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        members = await self._space_members(event.space_id)
        blocked = await self._withheld_from(event.user_id)
        # A follower's reduced roster lists the members, never another
        # follower (owner decision 13): a follower joining is news to the
        # members only.
        followers_too = event.role in WRITER_ROLES
        for member in members:
            if member.user_id == event.user_id:
                continue
            if not followers_too and member.role not in WRITER_ROLES:
                continue
            if member.user_id in blocked:
                continue
            if await self._space_level(member, space) is not SpaceNotifLevel.ALL:
                continue
            recipient = await self._local_user(member.user_id)
            if recipient is None:
                continue
            # A remote joiner is named by the name this household holds,
            # never by their raw user id.
            name = await self._person_name(
                event.user_id, self._locale(recipient), space_id=event.space_id
            )
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_member_joined",
                    title=self._t(
                        "notification.space.member.joined",
                        locale=self._locale(recipient),
                        fallback="{name} joined {space_name}",
                        name=name,
                        space_name=space.name,
                    ),
                    link_url=f"/spaces/{event.space_id}",
                ),
                about=NotificationSubject.of(
                    NotificationObject.SPACE,
                    event.space_id,
                    space_id=event.space_id,
                    space_name=space.name,
                ),
                actor_user_id=event.user_id,
                actor_name=name,
                withheld=blocked,
            )

    async def on_remote_space_dissolved(self, event: RemoteSpaceDissolved) -> None:
        """The owner host ended our membership in a space; the local copy was
        archived read-only. Tell each local member once that their copy is now
        a read-only archive (the space is still viewable).

        Reason-aware: both ``_on_dissolved`` (reason='dissolved') and
        ``_on_sync_rejected`` (reason='dissolved' | 'removed') publish this
        event, and the space row's ``archived_reason`` is already set by the
        time we run. 'removed' means the space still exists on the host but we
        were dropped from it; 'dissolved' (or anything else) keeps the
        original 'was dissolved' wording."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        removed = space.archived_reason == "removed"
        # The space's other rows go (the copy is an archive now); the notice
        # below is what stays.
        await self._forget_space(event.space_id)
        members = await self._space_members(event.space_id)
        for member in members:
            locale = self._locale(await self._local_user(member.user_id))
            if removed:
                title = self._t(
                    "notification.space.removed",
                    locale=locale,
                    fallback="You're no longer a member of “{space_name}”",
                    space_name=space.name,
                )
                body = self._t(
                    "notification.space.removed.body",
                    locale=locale,
                    fallback="Your copy is now a read-only archive.",
                )
            else:
                title = self._t(
                    "notification.space.dissolved",
                    locale=locale,
                    fallback="“{space_name}” was dissolved",
                    space_name=space.name,
                )
                body = self._t(
                    "notification.space.dissolved.body",
                    locale=locale,
                    fallback=(
                        "The space's owner dissolved it. Your copy is now a "
                        "read-only archive."
                    ),
                )
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_dissolved",
                    title=title,
                    body=body,
                    link_url=f"/spaces/{event.space_id}",
                ),
                # A re-broadcast of SPACE_DISSOLVED (fresh msg_id) must not
                # re-notify every member: it lands on the same row, and a
                # repeat of this type on an unread row is silent.
                about=NotificationSubject.of(
                    NotificationObject.SPACE,
                    event.space_id,
                    space_id=event.space_id,
                    space_name=space.name,
                ),
                actor_user_id=None,
            )

    async def on_admin_seeds_retired(
        self, event: SpaceAdminSeedsRetiredAfterRestore
    ) -> None:
        """v_44: a restore rotated a space's authority key, shared the new
        seed with nobody and turned delegated admin OFF. Tell the space's
        OWNER (only) to review its admins; turning delegation back on shares
        the seed with the admins seated then."""
        for member in await self._space_members(event.space_id):
            if member.role != SpaceRole.OWNER:
                continue
            owner = await self._local_user(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_admins_reconfirm",
                    title=self._t(
                        "notification.space.admins_reconfirm",
                        locale=self._locale(owner),
                        fallback=(
                            "Restored from backup: review space admins, then "
                            "turn delegated admin back on"
                        ),
                    ),
                    body=None,
                    link_url=f"/spaces/{event.space_id}",
                ),
                about=NotificationSubject.of(
                    NotificationObject.SPACE, event.space_id, space_id=event.space_id
                ),
                actor_user_id=None,
            )

    async def on_space_join_requested(
        self,
        event: SpaceJoinRequested,
    ) -> None:
        """Notify space admins + owner that a new join request is pending."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        members = await self._space_members(event.space_id)
        blocked = await self._withheld_from(event.user_id)
        flags = await self._join_request_flags(space, event.request_id)
        for member in members:
            if member.role not in SETTINGS_AUTHORITY_ROLES:
                continue
            if member.user_id in blocked:
                continue
            recipient = await self._local_user(member.user_id)
            if recipient is None:
                continue
            # A remote applicant is named by the name this household holds,
            # never by their raw user id.
            name = await self._person_name(
                event.user_id, self._locale(recipient), space_id=event.space_id
            )
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_join_requested",
                    title=self._t(
                        "notification.space.join.requested",
                        locale=self._locale(recipient),
                        fallback="{name} wants to join {space_name}",
                        name=name,
                        space_name=space.name,
                    ),
                    link_url=space_moderation_link(event.space_id),
                ),
                # One row per request (D12): answering it resolves exactly
                # this row for every admin.
                about=NotificationSubject.of(
                    NotificationObject.JOIN_REQUEST,
                    event.request_id,
                    space_id=event.space_id,
                    space_name=space.name,
                ),
                actor_user_id=event.user_id,
                actor_name=name,
                withheld=blocked,
                flags=flags,
            )

    async def _join_request_flags(self, space, request_id: str) -> tuple[str, ...]:
        """Why this request needs the review page rather than one tap
        (:func:`~socialhome.domain.space.join_request_flags` — the same rule
        the approve endpoint enforces on a one-tap approval)."""
        request = await self._spaces.get_join_request(request_id)
        household = (request or {}).get("remote_applicant_instance_id")
        return join_request_flags(
            request,
            min_age=getattr(space, "min_age", 0),
            household_paired=bool(household) and await self._paired(str(household)),
        )

    async def _paired(self, instance_id: str) -> bool:
        """Is *instance_id* a household this one is paired with?"""
        if not instance_id or self._instances is None:
            return False
        peer = await self._instances.get_instance(instance_id)
        return peer is not None and peer.status is PairingStatus.CONFIRMED

    async def on_space_join_approved(
        self,
        event: SpaceJoinApproved,
    ) -> None:
        """Tell the requester their join was approved."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        recipient = await self._local_user(event.user_id)
        if recipient is None:
            # A §D2 cross-household applicant (or an admin/mod elevation of
            # one): the approved user lives on another household, so there
            # is no LOCAL user to notify — and a notification row for a
            # non-local user_id would fail the users FK. The applicant is
            # told over federation, not by a local notification here.
            return
        await self._save_notif(
            new_notification(
                user_id=event.user_id,
                type="space_join_approved",
                title=self._t(
                    "notification.space.join.approved",
                    locale=self._locale(recipient),
                    fallback="You're in: {space_name}",
                    space_name=space.name,
                ),
                link_url=f"/spaces/{event.space_id}",
            ),
            about=NotificationSubject.of(
                NotificationObject.SPACE,
                event.space_id,
                space_id=event.space_id,
                space_name=space.name,
            ),
            actor_user_id=None,  # an answer to your own request
        )

    async def on_space_ownership_transferred(
        self,
        event: SpaceConfigChanged,
    ) -> None:
        """Tell the new owner the space is now theirs (§23.48).

        Only an ``ownership_transferred`` change, and only for a LOCAL
        recipient — on a member household the new owner lives on the
        host, so there is nobody here to tell.
        """
        if event.event_type != SpaceConfigEventType.OWNERSHIP_TRANSFERRED.value:
            return
        new_owner = str(event.payload.get("new_owner_user_id") or "")
        if not new_owner:
            return
        recipient = await self._local_user(new_owner)
        space = await self._spaces.get(event.space_id)
        if recipient is None or space is None:
            return
        await self._save_notif(
            new_notification(
                user_id=new_owner,
                type="space_ownership_transferred",
                title=self._t(
                    "notification.space.ownership.transferred",
                    locale=self._locale(recipient),
                    fallback="You now own {space_name}",
                    space_name=space.name,
                ),
                link_url=f"/spaces/{event.space_id}/settings",
            ),
            about=NotificationSubject.of(
                NotificationObject.SPACE,
                event.space_id,
                space_id=event.space_id,
                space_name=space.name,
            ),
            actor_user_id=None,  # the space is yours now, whoever handed it
        )

    async def on_space_join_denied(
        self,
        event: SpaceJoinDenied,
    ) -> None:
        """§D2 — tell the requester their join was declined.

        Per §25.3 title-only rule: space name is omitted from the body
        because the notification may surface on a lock screen.
        """
        recipient = await self._local_user(event.user_id)
        if recipient is None:
            # Cross-household applicant — no local user to notify, and a row
            # for a non-local user_id would fail the users FK.
            return
        await self._save_notif(
            new_notification(
                user_id=event.user_id,
                type="space_join_denied",
                title=self._t(
                    "notification.space.join.denied",
                    locale=self._locale(recipient),
                    fallback="Your join request was declined.",
                ),
                # The title can't name the space (lock screen, §25.3): the
                # space's landing page says which one.
                link_url=space_about_link(event.space_id),
            ),
            # No space name: the requester may not know the space's
            # household (the push and title don't name it either).
            about=NotificationSubject.of(
                NotificationObject.SPACE, event.space_id, space_id=event.space_id
            ),
            actor_user_id=None,  # an answer to your own request
        )

    async def on_remote_join_answered(
        self, event: RemoteJoinRequestApproved | RemoteJoinRequestDenied
    ) -> None:
        """§D2 — another household's space answered our member's join
        request: tell them, from the answer this household already
        receives (no new federation message). Only the person who asked,
        and only a request this household filed for them."""
        request = await self._spaces.get_join_request(event.request_id)
        if request is None or not request.get("remote_applicant_instance_id"):
            return
        user_id = str(request.get("user_id") or "")
        recipient = await self._local_user(user_id)
        if recipient is None:
            return
        space_id = str(request.get("space_id") or event.space_id)
        about = NotificationSubject.of(
            NotificationObject.SPACE, space_id, space_id=space_id
        )
        locale = self._locale(recipient)
        if isinstance(event, RemoteJoinRequestDenied):
            await self._save_notif(
                new_notification(
                    user_id=user_id,
                    type="space_join_denied",
                    title=self._t(
                        "notification.space.join.denied",
                        locale=locale,
                        fallback="Your join request was declined.",
                    ),
                    link_url=space_about_link(space_id),
                ),
                about=about,
                actor_user_id=None,  # an answer to your own request
            )
            return
        space_name = await self._space_name(space_id)
        title = (
            self._t(
                "notification.space.join.approved_remote",
                locale=locale,
                fallback="Your request to join {space_name} was approved",
                space_name=space_name,
            )
            if space_name
            else self._t(
                "notification.space.join.approved_remote_unnamed",
                locale=locale,
                fallback="Your join request was approved",
            )
        )
        await self._save_notif(
            new_notification(
                user_id=user_id,
                type="space_join_approved",
                title=title,
                link_url=f"/spaces/{space_id}",
            ),
            about=NotificationSubject.of(
                NotificationObject.SPACE,
                space_id,
                space_id=space_id,
                space_name=space_name,
            ),
            actor_user_id=None,  # an answer to your own request
        )

    async def on_local_space_invite(self, event: LocalSpaceInviteCreated) -> None:
        """An admin of this household invited a member here to a space:
        a "Needs you" row for the invitee. The row opens the invite (where
        a local community asks for location consent) — never a one-tap
        join."""
        space = await self._spaces.get(event.space_id)
        await self._save_invite(
            invitee=event.invited_user_id,
            inviter=event.invited_by,
            space_id=event.space_id,
            space_name=space.name if space is not None else None,
            name_on_lock_screen=True,  # this household's own space
        )

    async def on_remote_space_invite(self, event: RemoteSpaceInviteReceived) -> None:
        """Another household invited a member here to a space. Its push
        names the space only when that household is paired with this one
        (D11): an invite can come from a stranger."""
        space = await self._spaces.get(event.space_id)
        await self._save_invite(
            invitee=event.invitee_user_id,
            inviter=event.inviter_user_id,
            space_id=event.space_id,
            space_name=event.space_name or (space.name if space else None),
            name_on_lock_screen=await self._paired(event.inviter_instance_id),
        )

    async def _save_invite(
        self,
        *,
        invitee: str,
        inviter: str,
        space_id: str,
        space_name: str | None,
        name_on_lock_screen: bool,
    ) -> None:
        recipient = await self._local_user(invitee)
        if recipient is None:
            return
        locale = self._locale(recipient)
        name = await self._person_name(inviter, locale, space_id=space_id)
        title = (
            self._t(
                "notification.space.invite.received",
                locale=locale,
                fallback="{name} invited you to {space_name}",
                name=name,
                space_name=space_name,
            )
            if space_name
            else self._t(
                "notification.space.invite.received_unnamed",
                locale=locale,
                fallback="{name} invited you to a space",
                name=name,
            )
        )
        await self._save_notif(
            new_notification(
                user_id=invitee,
                type="space_invite",
                title=title,
                # The Spaces page holds the invite card: accept (with the
                # location consent a local community asks for) or decline.
                link_url="/spaces",
            ),
            about=NotificationSubject.of(
                NotificationObject.SPACE_INVITE,
                space_id,
                space_id=space_id,
                space_name=space_name,
            ),
            actor_user_id=inviter,
            actor_name=name,
            push_params={
                "name": name,
                "space_name": space_name if name_on_lock_screen else None,
            },
        )

    async def on_auto_pair_requested(self, event: AutoPairRequestIncoming) -> None:
        """A household asks to pair with this one, vouched for by a paired
        household: a "Needs you" row for every admin here. It always opens
        the review (Connections) — pairing with a stranger is never one
        tap from a list."""
        for admin in await self._active_users():
            if not admin.is_admin:
                continue
            await self._save_notif(
                new_notification(
                    user_id=admin.user_id,
                    type="pairing_request",
                    title=self._t(
                        "notification.pairing.requested",
                        locale=self._locale(admin),
                        fallback="{household} wants to pair with your household",
                        household=event.from_a_display,
                    ),
                    link_url="/connections",
                ),
                about=NotificationSubject.of(
                    NotificationObject.PAIRING_REQUEST, event.request_id
                ),
                actor_user_id=None,  # a household, not a person
            )

    async def on_remote_invite_accepted(
        self,
        event: RemoteSpaceInviteAccepted,
    ) -> None:
        """§D1b — the inviter (only) learns the remote person accepted:
        "{name} accepted your invite to {space_name}"."""
        await self._tell_inviter(
            event,
            ntype="space_remote_invite_accepted",
            key="notification.space.remote_invite.accepted",
            fallback="{name} accepted your invite to {space_name}",
            link=f"/spaces/{event.space_id}",
            display_name=event.invitee_display_name,
        )

    async def on_remote_invite_declined(
        self,
        event: RemoteSpaceInviteDeclined,
    ) -> None:
        """§D1b — the inviter (only) learns the remote person declined."""
        await self._tell_inviter(
            event,
            ntype="space_remote_invite_declined",
            key="notification.space.remote_invite.declined",
            fallback="{name} declined your invite to {space_name}",
            # Members settings: the invites, to try someone else.
            link=space_members_link(event.space_id),
            display_name=None,
        )

    async def _tell_inviter(
        self,
        event: RemoteSpaceInviteAccepted | RemoteSpaceInviteDeclined,
        *,
        ntype: str,
        key: str,
        fallback: str,
        link: str,
        display_name: str | None,
    ) -> None:
        """An invite answer goes to the person who sent the invitation —
        never to every admin — while they still hold a seat in the space.
        The title names the invitee (the name their accept carried, else
        the one this household holds, else "Someone"; never the raw id)
        and the space."""
        inviter = event.inviter_user_id
        if not inviter:
            return
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        if await self._spaces.get_member(event.space_id, inviter) is None:
            return  # they left the space: nothing of theirs to come back to
        recipient = await self._local_user(inviter)
        if recipient is None:
            return
        locale = self._locale(recipient)
        name = display_name or await self._person_name(
            event.invitee_user_id, locale, space_id=event.space_id
        )
        await self._save_notif(
            new_notification(
                user_id=inviter,
                type=ntype,
                title=self._t(
                    key,
                    locale=locale,
                    fallback=fallback,
                    name=name,
                    space_name=space.name,
                ),
                link_url=link,
            ),
            about=NotificationSubject.of(
                NotificationObject.SPACE,
                event.space_id,
                space_id=event.space_id,
                space_name=space.name,
            ),
            actor_user_id=event.invitee_user_id,
            actor_name=name,
        )

    # ── Momentum (§Momentum) ──────────────────────────────────────────

    async def on_moment_reaction_changed(
        self,
        event: MomentReactionChanged,
    ) -> None:
        """Notify the moment author when someone reacts.

        Cleared reactions (``emoji is None``) intentionally don't fire
        a notification — the author already saw the original. Self-
        reactions are silent. The recipient must live on this instance.

        §CP.R (decision 14), the same gate as a reply: a protected account
        sees moments from its household and paired households only — a
        reaction relayed from further away (mesh hop ≥ 2, or a GFS) never
        reaches it as a notification naming a stranger.
        """
        if event.emoji is None:
            return
        if event.reactor_user_id == event.author_user_id:
            return
        author = await self._local_user(event.author_user_id)
        if author is None:
            return  # remote author — their instance fires the local notif
        if _beyond_the_circle(author, event.received_via, event.hop_count):
            return
        locale = self._locale(author)
        name = await self._person_name(event.reactor_user_id, locale)
        await self._save_notif(
            new_notification(
                user_id=event.author_user_id,
                type="moment_reacted",
                title=self._t(
                    "notification.moment.reacted",
                    locale=locale,
                    fallback="{name} reacted {emoji} to your moment",
                    name=name,
                    emoji=event.emoji,
                ),
                link_url=f"/momentum/{event.moment_id}",
            ),
            about=NotificationSubject.of(NotificationObject.MOMENT, event.moment_id),
            actor_user_id=event.reactor_user_id,
            actor_name=name,
        )

    async def on_moment_created(self, event: MomentCreated) -> None:
        """Notify the parent author when this moment is a reply.

        Top-level moments don't fire a notification per recipient —
        broadcast posts are picked up via the WS frame and the inbox
        refresh; pinging every household member would be too noisy.
        """
        if event.parent_moment_id is None:
            return
        if not event.parent_author_user_id:
            return
        if event.parent_author_user_id == event.author_user_id:
            return  # author replied to their own thread
        recipient = await self._local_user(event.parent_author_user_id)
        if recipient is None:
            return  # parent author lives on a peer instance
        if _beyond_the_circle(recipient, event.received_via, event.hop_count):
            # §CP.R (decision 14): a protected account sees moments from its
            # household and paired households only — a reply from further
            # away never reaches it, not as a notification naming its author.
            return
        locale = self._locale(recipient)
        await self._save_notif(
            new_notification(
                user_id=event.parent_author_user_id,
                type="moment_replied",
                title=self._t(
                    "notification.moment.replied",
                    locale=locale,
                    fallback="{name} replied to your moment",
                    name=await self._person_name(event.author_user_id, locale),
                ),
                # The parent, with this reply picked out and scrolled to.
                link_url=(
                    f"/momentum/{event.parent_moment_id}?reply={event.moment_id}"
                ),
            ),
            about=NotificationSubject.of(
                NotificationObject.MOMENT, event.parent_moment_id
            ),
            actor_user_id=event.author_user_id,
        )

    # ── Highlights (§Highlights) ──────────────────────────────────────

    async def on_highlight_reaction_changed(
        self,
        event: HighlightFrameReactionChanged,
    ) -> None:
        """Notify the highlight's author when someone reacts to a frame.

        A local row from an event that already arrives — the reactor's
        household sends ``HIGHLIGHT_FRAME_REACTED`` to the author's home
        only, so nothing new crosses the wire. Same rules as a moment
        reaction: a cleared reaction, your own, a blocked reactor and an
        author on another household (their household notifies them) stay
        silent.
        """
        if event.emoji is None:
            return
        if event.reactor_user_id == event.author_user_id:
            return
        author = await self._local_user(event.author_user_id)
        if author is None:
            return
        locale = self._locale(author)
        name = await self._person_name(event.reactor_user_id, locale)
        await self._save_notif(
            new_notification(
                user_id=event.author_user_id,
                type="highlight_reacted",
                title=self._t(
                    "notification.highlight.reacted",
                    locale=locale,
                    fallback="{name} reacted {emoji} to your highlight",
                    name=name,
                    emoji=event.emoji,
                ),
                link_url=f"/highlights/{event.highlight_id}",
            ),
            about=NotificationSubject.of(
                NotificationObject.HIGHLIGHT, event.highlight_id
            ),
            actor_user_id=event.reactor_user_id,
            actor_name=name,
        )

    async def on_post_reaction_changed(self, event: PostReactionChanged) -> None:
        """A reaction on your post — household feed or space: an in-app row
        grouped per post, no push (D13). A removed reaction, your own, a
        reactor you blocked and an author on another household (theirs
        tells them) stay silent; a space post's author must still hold a
        seat there."""
        if not event.added or not event.emoji or not event.reactor_user_id:
            return
        post = event.post
        if event.reactor_user_id == post.author:
            return
        author = await self._local_user(post.author)
        if author is None:
            return
        space_name: str | None = None
        if event.space_id is not None:
            if await self._spaces.get_member(event.space_id, post.author) is None:
                return
            space_name = await self._space_name(event.space_id)
            about = NotificationSubject.of(
                NotificationObject.SPACE_POST,
                post.id,
                space_id=event.space_id,
                space_name=space_name,
            )
            link = space_post_link(event.space_id, post.id)
        else:
            about = NotificationSubject.of(NotificationObject.POST, post.id)
            link = household_post_link(post.id)
        locale = self._locale(author)
        name = await self._person_name(
            event.reactor_user_id, locale, space_id=event.space_id
        )
        if space_name:
            title = self._t(
                "notification.space.post.reacted",
                locale=locale,
                fallback="{name} reacted {emoji} to your post in {space_name}",
                name=name,
                emoji=event.emoji,
                space_name=space_name,
            )
        else:
            title = self._t(
                "notification.post.reacted",
                locale=locale,
                fallback="{name} reacted {emoji} to your post",
                name=name,
                emoji=event.emoji,
            )
        await self._save_notif(
            new_notification(
                user_id=post.author,
                type="post_reacted",
                title=title,
                link_url=link,
            ),
            about=about,
            actor_user_id=event.reactor_user_id,
            actor_name=name,
            push_params={"name": name, "space_name": space_name},
        )

    async def on_user_followed(self, event: UserFollowed) -> None:
        """Notify the followed user when someone starts following them."""
        if event.follower_user_id == event.followed_user_id:
            return
        recipient = await self._local_user(event.followed_user_id)
        if recipient is None:
            return
        locale = self._locale(recipient)
        await self._save_notif(
            new_notification(
                user_id=event.followed_user_id,
                type="user_followed",
                title=self._t(
                    "notification.user.followed",
                    locale=locale,
                    fallback="{name} started following you",
                    name=await self._person_name(event.follower_user_id, locale),
                ),
                link_url="/momentum",
            ),
            about=NotificationSubject.of(
                NotificationObject.FOLLOWERS, event.followed_user_id
            ),
            actor_user_id=event.follower_user_id,
        )

    # ── Space location feature (§23.8.6) ──────────────────────────────

    async def on_space_location_feature_enabled(
        self,
        event: SpaceLocationFeatureEnabled,
    ) -> None:
        """Nudge the space's members (except the actor) to opt in.

        Fired only on the OFF→ON transition — see
        :class:`SpaceService.update_config`. The link points at the
        Personal Settings → Privacy → 'Space location sharing' panel
        so members can discover and manage their per-space sharing in
        one place.

        Never to a follower (a subscriber can't share location in the
        space) nor to a protected account (§CP: nothing nudges a child to
        share where they are).
        """
        members = await self._space_members(event.space_id)
        blocked = await self._withheld_from(event.actor_user_id)
        for m in members:
            if m.user_id == event.actor_user_id or m.role not in WRITER_ROLES:
                continue
            recipient = await self._local_user(m.user_id)
            if recipient is None or recipient.child_protection_enabled:
                continue
            if await self._is_protected(m.user_id):
                continue
            await self._save_notif(
                new_notification(
                    user_id=m.user_id,
                    type="space_location_enabled",
                    title=self._t(
                        "notification.space.location_enabled",
                        locale=self._locale(recipient),
                        fallback="Location sharing turned on in {space_name}",
                        space_name=event.space_name,
                    ),
                    link_url="/settings#privacy",
                ),
                about=NotificationSubject.of(
                    NotificationObject.SPACE,
                    event.space_id,
                    space_id=event.space_id,
                    space_name=event.space_name,
                ),
                actor_user_id=event.actor_user_id,
                withheld=blocked,
            )
