"""Space and household feature domain types (§4.3 / §13).

Defines:

* :class:`SpaceRole` — membership roles (§4.2.3).
* :class:`SpaceFeatureAccess` — per-feature permission levels.
* :class:`SpaceFeatures` — per-space feature toggles + access levels.
* :class:`HouseholdFeatures` — feature toggles for the local HA household.
* :class:`ModerationStatus`, :class:`SpaceModerationItem` — moderation queue.
* :class:`SpaceConfigEventType`, :class:`SpaceConfigEvent` — signed,
  monotonically-ordered space config events (§4.3).
* :class:`Space`, :class:`SpaceMember`, :class:`SpacePublicProfile`.
* :class:`SpacePermissionError`, :class:`PublicSpaceLimitError`,
  :class:`SpaceConfigGapError` — domain exceptions.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .post import PostType


# ─── Discovery categories (§23.50) ────────────────────────────────────────

#: Discovery categories (§23.50). A space's ``category`` is one of these;
#: anything else (legacy ``target_audience`` values, a remote peer's invented
#: category, ``None``) normalizes to ``"general"`` for display. Lives in the
#: pure domain layer so both the HFS services and the GFS ``global_server``
#: can import it without the GFS pulling in ``services.space_service``.
SPACE_CATEGORIES: frozenset[str] = frozenset(
    {
        "general",
        "hobby_crafts",
        "sports_outdoors",
        "gaming",
        "music_arts",
        "food_drink",
        "tech",
        "local",
        "family_parenting",
        "learning",
    }
)


def normalize_category(value: str | None) -> str:
    """Map any stored/received category to a known value (default general)."""
    return value if value in SPACE_CATEGORIES else "general"


# ─── Minimum-age gate (§23.50 / child protection) ─────────────────────────

#: The only ``min_age`` values the schema accepts — every ``min_age`` column
#: (``spaces``, ``public_space_cache``, ``installed_apps``) carries a
#: ``CHECK(min_age IN (0, 13, 16, 18))``.
VALID_MIN_AGES: frozenset[int] = frozenset({0, 13, 16, 18})


def normalize_min_age(value: object) -> int:
    """Map any stored/received min_age to an allowed value (default 0).

    A non-conforming / malicious peer shipping e.g. ``15`` must not reach a
    ``min_age`` CHECK (it would raise and abort the join / discovery tick) —
    anything outside :data:`VALID_MIN_AGES` falls back to ``0`` (no
    restriction, the fail-soft default).
    """
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        return 0
    try:
        coerced = int(value)
    except TypeError, ValueError:
        return 0
    return coerced if coerced in VALID_MIN_AGES else 0


# ─── Space membership roles (§4.2.3) ──────────────────────────────────────


class SpaceRole(StrEnum):
    """Membership role stored in ``space_members.role``.

    Per spec §4.2.3, admin authority is *holding the space private key* —
    not a row in an ACL table. The four roles below are a social-layer
    distinction: ``OWNER`` and ``ADMIN`` are programmatically identical
    at the signing level, ``MEMBER`` is the regular participant, and
    ``SUBSCRIBER`` is the read-only follower of public/global spaces —
    which exist only while the space's ``SpaceFeatures.allow_subscribers``
    opt-in is ON.
    Adding a fifth role, custom per-space roles, or per-user permission
    bitfields would break the federation model — extend
    :class:`SpaceFeatures` with a new feature gate instead.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    SUBSCRIBER = "subscriber"


class RemoteAdminOutcome(StrEnum):
    """Result of a host receiving a forwarded ``SPACE_REMOTE_ADMIN_ACTION``."""

    EXECUTED = "executed"  # delegation ON → ran as owner
    NEEDS_OWNER_APPROVAL = "needs_owner_approval"  # delegation OFF → owner must approve
    DROPPED = "dropped"  # validation failed (unknown space / not hosted here /
    # not an admin / federation not attached)


# ─── Space feature access levels (§4.3) ───────────────────────────────────


class SpaceFeatureAccess(StrEnum):
    """Per-feature permission level for a space.

    * ``OPEN`` — any member may create / edit / delete.
    * ``MODERATED`` — members' submissions enter a pending queue; admins
      approve or reject. Admins bypass the queue.
    * ``ADMIN_ONLY`` — only admins / owner may mutate. Members read-only.
    """

    OPEN = "open"
    MODERATED = "moderated"
    ADMIN_ONLY = "admin_only"


# Default allowed post types for a fresh space. Ordered for a stable wire form.
_ALL_POST_TYPES: tuple[str, ...] = (
    "bazaar",
    "event",
    "file",
    "highlight_share",
    "image",
    "location",
    "poll",
    "schedule",
    "text",
    "transcript",
    "video",
)


@dataclass(slots=True, frozen=True)
class SpaceFeatures:
    """Per-space feature toggles and access levels."""

    # Every per-space feature defaults to ON so a fresh space mirrors
    # the SPA's historical "every tab is visible" behaviour. Admins
    # opt OUT of tabs they don't want. The sole exception is
    # ``location`` — it carries an opt-in privacy contract (§23.8.6),
    # so the admin has to flip it on deliberately.
    calendar: bool = True
    todo: bool = True
    location: bool = False
    #: Privacy tier for the per-space map (§23.8.6). Only meaningful
    #: when ``location`` is True.
    #:
    #: * ``"gps"`` — opted-in members broadcast 4dp GPS to the space.
    #: * ``"zone_only"`` — the originating instance matches each
    #:   member's GPS to a space-defined zone (§23.8.7) and broadcasts
    #:   only the matched zone label. Raw coordinates never leave the
    #:   originating household. Updates outside every zone are silently
    #:   skipped.
    location_mode: Literal["gps", "zone_only"] = "gps"
    stickies: bool = True
    pages: bool = True
    #: Per-space toggle for the gallery tab (§23.119). Defaults to ON so
    #: pre-0008 rows that lack the column still surface as enabled
    #: (matches the migration default and keeps existing spaces working
    #: without admin action).
    gallery: bool = True
    #: Per-space toggle for the Bazaar tab (§23.15). Defaults ON so the
    #: marketplace tab mirrors the always-on Calendar / Gallery tabs;
    #: admins hide it for spaces that aren't a marketplace. Listings can
    #: only be created while this is on.
    bazaar: bool = True

    posts_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN
    pages_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN
    stickies_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN
    calendar_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN
    tasks_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN

    #: Admin opt-in: may STRANGERS follow this space read-only at all?
    #: This — not :class:`JoinMode` — is what makes a public / global space
    #: publicly READABLE. When OFF (the default) the space may still be
    #: LISTED in a directory, but no post is relayed to a GFS, the per-space
    #: content key is never sealed to a subscriber, and a subscribe is
    #: refused on both the household and the GFS side. When ON, the
    #: :class:`SpaceRole.SUBSCRIBER` audience the other two flags describe
    #: can exist. Orthogonal to ``join_mode``, which governs how someone
    #: becomes a MEMBER who can post: ``invite_only`` + subscribers-on is a
    #: broadcast space (invited people write, anyone may read); ``open`` +
    #: subscribers-off is joinable but not publicly readable. Defaults OFF
    #: for the same reason its two siblings do — a space is private until
    #: its owner says otherwise.
    allow_subscribers: bool = False

    #: Subscriber-engagement opt-ins (§23.49).  Subscribers
    #: (``role='subscriber'``) are read-only by default — the
    #: historical contract.  Admins flip these flags when they want
    #: a follower-style audience to be able to leave a comment or a
    #: reaction without being promoted to a full member.  Posting
    #: top-level content stays member-only regardless.  Meaningful only
    #: while ``allow_subscribers`` is ON — without it there is no
    #: subscriber to engage.
    allow_subscriber_comment: bool = False
    allow_subscriber_react: bool = False

    #: Owner opt-in (§ delegated-admin epic). When ON, the space owner has
    #: authorised admins to act on the space's behalf (moderate, invite,
    #: publish) even while the owner is offline. Flipping it on distributes
    #: the space's Ed25519 signing seed to remote admin households via
    #: ``SPACE_ADMIN_KEY_SHARE`` (v_22); flipping it off leaves already-shared
    #: seeds in place (deeper revocation is a later phase). Toggling it is
    #: OWNER-only (a host-local admin can't enact it). Defaults OFF
    #: (least-privilege). Older peers that omit the field default to False.
    delegated_admin_authority: bool = False

    allowed_post_types: tuple[str, ...] = _ALL_POST_TYPES

    # ── Helpers ──────────────────────────────────────────────────────────

    def allows(self, post_type: "PostType | str") -> bool:
        val = post_type.value if hasattr(post_type, "value") else str(post_type)
        return val in self.allowed_post_types

    def access_decision(
        self, feature: str, is_admin: bool
    ) -> Literal["proceed", "queue", "deny"]:
        """Describe what should happen when a member attempts ``feature``.

        Valid ``feature`` values: ``posts``, ``pages``, ``stickies``,
        ``calendar``, ``tasks``.
        """
        access: SpaceFeatureAccess = getattr(
            self, f"{feature}_access", SpaceFeatureAccess.OPEN
        )
        if access is SpaceFeatureAccess.ADMIN_ONLY:
            return "proceed" if is_admin else "deny"
        if access is SpaceFeatureAccess.MODERATED:
            return "proceed" if is_admin else "queue"
        return "proceed"

    def with_allowed_post_types(
        self, types: "set[PostType] | set[str]"
    ) -> "SpaceFeatures":
        if not types:
            raise ValueError("allowed_post_types must contain at least one post type")
        normalised = tuple(
            sorted(t.value if hasattr(t, "value") else str(t) for t in types)
        )
        return copy.replace(self, allowed_post_types=normalised)

    @classmethod
    def from_row(cls, row: dict) -> "SpaceFeatures":
        """Reconstruct from a ``spaces`` table row."""
        allowed = tuple(
            sorted(
                t
                for t, col in (
                    ("text", "allow_post_text"),
                    ("image", "allow_post_image"),
                    ("video", "allow_post_video"),
                    ("transcript", "allow_post_transcript"),
                    ("poll", "allow_post_poll"),
                    ("schedule", "allow_post_schedule"),
                    ("file", "allow_post_file"),
                    ("bazaar", "allow_post_bazaar"),
                    ("event", "allow_post_event"),
                    ("location", "allow_post_location"),
                    ("highlight_share", "allow_post_highlight_share"),
                )
                if row.get(col, 1)
            )
        )
        raw_mode = row.get("location_mode", "gps")
        location_mode: Literal["gps", "zone_only"] = (
            "zone_only" if raw_mode == "zone_only" else "gps"
        )
        return cls(
            calendar=bool(row.get("feature_calendar", 0)),
            todo=bool(row.get("feature_todo", 1)),
            location=bool(row.get("feature_location", 0)),
            location_mode=location_mode,
            stickies=bool(row.get("feature_stickies", 0)),
            pages=bool(row.get("feature_pages", 1)),
            gallery=bool(row.get("feature_gallery", 1)),
            bazaar=bool(row.get("feature_bazaar", 1)),
            posts_access=SpaceFeatureAccess(row.get("posts_access", "open")),
            pages_access=SpaceFeatureAccess(row.get("pages_access", "open")),
            stickies_access=SpaceFeatureAccess(row.get("stickies_access", "open")),
            calendar_access=SpaceFeatureAccess(row.get("calendar_access", "open")),
            tasks_access=SpaceFeatureAccess(row.get("tasks_access", "open")),
            allow_subscribers=bool(row.get("allow_subscribers", 0)),
            allow_subscriber_comment=bool(row.get("allow_subscriber_comment", 0)),
            allow_subscriber_react=bool(row.get("allow_subscriber_react", 0)),
            delegated_admin_authority=bool(row.get("delegated_admin_authority", 0)),
            allowed_post_types=allowed or ("text",),
        )

    def to_columns(self) -> dict:
        return {
            "feature_calendar": int(self.calendar),
            "feature_todo": int(self.todo),
            "feature_location": int(self.location),
            "location_mode": self.location_mode,
            "feature_stickies": int(self.stickies),
            "feature_pages": int(self.pages),
            "feature_gallery": int(self.gallery),
            "feature_bazaar": int(self.bazaar),
            "posts_access": self.posts_access.value,
            "pages_access": self.pages_access.value,
            "stickies_access": self.stickies_access.value,
            "calendar_access": self.calendar_access.value,
            "tasks_access": self.tasks_access.value,
            "allow_subscribers": int(self.allow_subscribers),
            "allow_subscriber_comment": int(self.allow_subscriber_comment),
            "allow_subscriber_react": int(self.allow_subscriber_react),
            "delegated_admin_authority": int(self.delegated_admin_authority),
            "allow_post_text": int("text" in self.allowed_post_types),
            "allow_post_image": int("image" in self.allowed_post_types),
            "allow_post_video": int("video" in self.allowed_post_types),
            "allow_post_transcript": int("transcript" in self.allowed_post_types),
            "allow_post_poll": int("poll" in self.allowed_post_types),
            "allow_post_schedule": int("schedule" in self.allowed_post_types),
            "allow_post_file": int("file" in self.allowed_post_types),
            "allow_post_bazaar": int("bazaar" in self.allowed_post_types),
            "allow_post_event": int("event" in self.allowed_post_types),
            "allow_post_location": int("location" in self.allowed_post_types),
            "allow_post_highlight_share": int(
                "highlight_share" in self.allowed_post_types
            ),
        }

    def to_wire_dict(self) -> dict:
        """Wire form used by SPACE_SYNC_BEGIN."""
        return {
            "calendar": self.calendar,
            "todo": self.todo,
            "location": self.location,
            "location_mode": self.location_mode,
            "stickies": self.stickies,
            "pages": self.pages,
            "gallery": self.gallery,
            "bazaar": self.bazaar,
            "posts_access": self.posts_access.value,
            "pages_access": self.pages_access.value,
            "stickies_access": self.stickies_access.value,
            "calendar_access": self.calendar_access.value,
            "tasks_access": self.tasks_access.value,
            "allow_subscribers": self.allow_subscribers,
            "allow_subscriber_comment": self.allow_subscriber_comment,
            "allow_subscriber_react": self.allow_subscriber_react,
            "delegated_admin_authority": self.delegated_admin_authority,
            "allowed_post_types": list(self.allowed_post_types),
        }

    @classmethod
    def from_wire_dict(
        cls,
        raw: dict,
        *,
        defaults: "SpaceFeatures | None" = None,
    ) -> "SpaceFeatures":
        """Faithful inverse of :meth:`to_wire_dict`.

        The single canonical wire-dict → :class:`SpaceFeatures` parser:
        the route PATCH body, federation receive (remote-space stub
        builder), and the cross-household admin-action host dispatcher
        (``SPACE_REMOTE_ADMIN_ACTION``) all rebuild features through here.
        Each field falls back to *defaults* when absent so a partial /
        older-shaped dict still produces a valid object.
        Unknown access levels fall back to ``OPEN`` rather than raising.

        ``defaults`` is what an ABSENT key means. Pass the space's CURRENT
        features whenever the dict is an EDIT of an existing space (a PATCH
        body, a forwarded ``update_config``) — then "absent" reads as "leave
        it alone", which is what a partial body means. Omitting it falls back
        to the class defaults, i.e. "absent means the factory setting", which
        is right only when there is no prior state to preserve (building a
        brand-new remote stub). Getting this wrong is not cosmetic: a partial
        ``{"bazaar": false}`` PATCH would otherwise reset ``allow_subscribers``
        to its OFF default and silently withdraw the space's public
        readability.
        """
        defaults = defaults if defaults is not None else cls()

        def access(name: str, default: SpaceFeatureAccess) -> SpaceFeatureAccess:
            v = raw.get(name)
            if v is None:
                return default
            try:
                return SpaceFeatureAccess(v)
            except ValueError:
                return default

        location_mode_raw = raw.get("location_mode", defaults.location_mode)
        location_mode: Literal["gps", "zone_only"] = (
            "zone_only" if location_mode_raw == "zone_only" else "gps"
        )
        allowed_raw = raw.get("allowed_post_types")
        allowed = (
            tuple(sorted(str(t) for t in allowed_raw))
            if isinstance(allowed_raw, (list, tuple)) and allowed_raw
            else defaults.allowed_post_types
        )
        return cls(
            calendar=bool(raw.get("calendar", defaults.calendar)),
            todo=bool(raw.get("todo", defaults.todo)),
            location=bool(raw.get("location", defaults.location)),
            location_mode=location_mode,
            stickies=bool(raw.get("stickies", defaults.stickies)),
            pages=bool(raw.get("pages", defaults.pages)),
            gallery=bool(raw.get("gallery", defaults.gallery)),
            bazaar=bool(raw.get("bazaar", defaults.bazaar)),
            posts_access=access("posts_access", defaults.posts_access),
            pages_access=access("pages_access", defaults.pages_access),
            stickies_access=access("stickies_access", defaults.stickies_access),
            calendar_access=access("calendar_access", defaults.calendar_access),
            tasks_access=access("tasks_access", defaults.tasks_access),
            allow_subscribers=bool(
                raw.get("allow_subscribers", defaults.allow_subscribers)
            ),
            allow_subscriber_comment=bool(
                raw.get("allow_subscriber_comment", defaults.allow_subscriber_comment)
            ),
            allow_subscriber_react=bool(
                raw.get("allow_subscriber_react", defaults.allow_subscriber_react)
            ),
            delegated_admin_authority=bool(
                raw.get("delegated_admin_authority", defaults.delegated_admin_authority)
            ),
            allowed_post_types=allowed,
        )


# ─── Household features ───────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class HouseholdFeatures:
    """Feature toggles for the local HA household.

    All features default to ON so a fresh install works immediately. Unlike
    :class:`SpaceFeatures` there is no per-feature access level — everything
    here is always open to all HA users when enabled.
    """

    feed: bool = True
    pages: bool = True
    tasks: bool = True
    stickies: bool = True
    calendar: bool = True
    bazaar: bool = True
    allow_text: bool = True
    allow_image: bool = True
    allow_video: bool = True
    allow_file: bool = True
    allow_poll: bool = True
    allow_schedule: bool = True
    allow_bazaar: bool = True
    household_name: str = "Home"

    def allows_post_type(self, pt: "PostType | str") -> bool:
        val = pt.value if hasattr(pt, "value") else str(pt)
        return {
            "text": self.allow_text,
            "image": self.allow_image,
            "video": self.allow_video,
            "file": self.allow_file,
            "poll": self.allow_poll,
            "schedule": self.allow_schedule,
            "bazaar": self.allow_bazaar,
            "transcript": True,  # transcripts are system-generated
        }.get(val, True)

    def allows_section(self, section: str) -> bool:
        return {
            "feed": self.feed,
            "pages": self.pages,
            "tasks": self.tasks,
            "stickies": self.stickies,
            "calendar": self.calendar,
            "bazaar": self.bazaar,
        }.get(section, True)

    @classmethod
    def from_row(cls, row: dict) -> "HouseholdFeatures":
        return cls(
            feed=bool(row.get("feat_feed", 1)),
            pages=bool(row.get("feat_pages", 1)),
            tasks=bool(row.get("feat_tasks", 1)),
            stickies=bool(row.get("feat_stickies", 1)),
            calendar=bool(row.get("feat_calendar", 1)),
            bazaar=bool(row.get("feat_bazaar", 1)),
            allow_text=bool(row.get("allow_text", 1)),
            allow_image=bool(row.get("allow_image", 1)),
            allow_video=bool(row.get("allow_video", 1)),
            allow_file=bool(row.get("allow_file", 1)),
            allow_poll=bool(row.get("allow_poll", 1)),
            allow_schedule=bool(row.get("allow_schedule", 1)),
            allow_bazaar=bool(row.get("allow_bazaar", 1)),
            household_name=row.get("household_name", "Home"),
        )

    def to_columns(self) -> dict:
        return {
            "feat_feed": int(self.feed),
            "feat_pages": int(self.pages),
            "feat_tasks": int(self.tasks),
            "feat_stickies": int(self.stickies),
            "feat_calendar": int(self.calendar),
            "feat_bazaar": int(self.bazaar),
            "allow_text": int(self.allow_text),
            "allow_image": int(self.allow_image),
            "allow_video": int(self.allow_video),
            "allow_file": int(self.allow_file),
            "allow_poll": int(self.allow_poll),
            "allow_schedule": int(self.allow_schedule),
            "allow_bazaar": int(self.allow_bazaar),
            "household_name": self.household_name,
        }

    def to_wire_dict(self) -> dict:
        return {
            "feed": self.feed,
            "pages": self.pages,
            "tasks": self.tasks,
            "stickies": self.stickies,
            "calendar": self.calendar,
            "bazaar": self.bazaar,
            "allow_text": self.allow_text,
            "allow_image": self.allow_image,
            "allow_video": self.allow_video,
            "allow_file": self.allow_file,
            "allow_poll": self.allow_poll,
            "allow_schedule": self.allow_schedule,
            "allow_bazaar": self.allow_bazaar,
            "household_name": self.household_name,
        }


# ─── Moderation queue ─────────────────────────────────────────────────────


class ModerationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(slots=True, frozen=True)
class SpaceModerationItem:
    """A member submission awaiting admin review in a MODERATED feature.

    ``payload`` carries everything needed to replay the original action on
    approval. After ``rejection_reason`` / ``EXPIRED`` the payload is nulled
    out (via a separate purge job) but the row is retained for audit.
    """

    id: str
    space_id: str
    feature: str
    action: str
    submitted_by: str
    payload: dict
    current_snapshot: str | None
    submitted_at: datetime
    expires_at: datetime
    status: ModerationStatus = ModerationStatus.PENDING
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    rejection_reason: str | None = None


# ─── Signed config events (§4.3) ──────────────────────────────────────────


class SpaceConfigEventType(StrEnum):
    RENAME = "rename"
    FEATURE_CHANGED = "feature_changed"
    ALLOWED_POST_TYPES_CHANGED = "allowed_post_types_changed"
    JOIN_MODE_CHANGED = "join_mode_changed"
    ADMIN_GRANTED = "admin_granted"
    ADMIN_REVOKED = "admin_revoked"
    OWNERSHIP_TRANSFERRED = "ownership_transferred"
    MEMBER_BANNED = "member_banned"
    MEMBER_UNBANNED = "member_unbanned"
    DISSOLVED = "dissolved"
    ARCHIVED = "archived"
    UNARCHIVED = "unarchived"
    PUBLIC_MODE_CHANGED = "public_mode_changed"
    COVER_UPDATED = "cover_updated"
    ICON_UPDATED = "icon_updated"
    ABOUT_UPDATED = "about_updated"


@dataclass(slots=True, frozen=True)
class SpaceConfigEvent:
    """Monotonically-ordered, space-key-signed structural change event.

    Each instance tracks the highest ``sequence`` it has applied per space
    and rejects any event with ``sequence <= last_seen`` (replay) or raises
    :class:`SpaceConfigGapError` when ``sequence > last_seen + 1`` (catch-up
    required).
    """

    space_id: str
    event_type: SpaceConfigEventType
    payload: dict
    issued_by: str
    sequence: int
    issued_at: str
    space_signature: str


# ─── Domain exceptions ────────────────────────────────────────────────────


@dataclass
class SpaceConfigGapError(Exception):
    """Raised when a :class:`SpaceConfigEvent` was received out of order.

    Signals to the service layer that a ``SPACE_CONFIG_CATCH_UP`` fetch is
    required before applying the event.
    """

    space_id: str
    have: int
    need: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"config gap in {self.space_id}: have {self.have}, need {self.need}"


class SpacePermissionError(Exception):
    """Raised when a user attempts a space action they lack authority for.

    ``banned=True`` distinguishes a hard ban from a plain insufficient-role
    outcome; the route layer maps the two to different HTTP responses.
    """

    def __init__(self, message: str, *, banned: bool = False) -> None:
        super().__init__(message)
        self.banned = banned


class PublicSpaceLimitError(Exception):
    """Raised when an instance tries to exceed its cap on public spaces."""


class ModerationAlreadyDecidedError(Exception):
    """Raised when an admin tries to approve/reject a queue item that's
    already been decided (or expired). Route layer maps this to HTTP 409.
    """


# ─── Space entity (§4.3) ──────────────────────────────────────────────────


class SpaceType(StrEnum):
    PRIVATE = "private"
    HOUSEHOLD = "household"
    PUBLIC = "public"
    GLOBAL = "global"


#: The space tiers that relay content to the GFS public-relay path. PRIVATE /
#: HOUSEHOLD spaces are never publicly discoverable, so a post in one must
#: never leave the member households. Shared by the public-relay producers
#: (``space_public_outbound`` + ``space_post_outbound``).
PUBLIC_SPACE_TIERS: frozenset["SpaceType"] = frozenset(
    {SpaceType.PUBLIC, SpaceType.GLOBAL}
)


class JoinMode(StrEnum):
    """How a person becomes a MEMBER (someone who can post) of a space.

    Purely a membership gate: ``invite_only`` needs an invite, ``request``
    needs a member's approval, ``open`` lets anyone in. It says NOTHING about
    whether strangers may READ the space — that is the independent
    ``SpaceFeatures.allow_subscribers`` opt-in, and both combinations are
    meaningful (``invite_only`` + subscribers-on is a broadcast space;
    ``open`` + subscribers-off is joinable but not publicly readable).
    """

    INVITE_ONLY = "invite_only"
    OPEN = "open"
    REQUEST = "request"


def normalize_join_mode(value: object) -> str:
    """Map any stored/received join mode to a known value.

    Fails CLOSED: anything unknown (missing, misspelled, hostile, a non-string
    a remote directory made up) becomes ``"invite_only"`` — the narrowest way
    in. Used wherever a join mode crosses a trust boundary (the GFS publish
    body, a GFS directory listing mirrored onto a local stub) so an
    unparseable value can never widen the membership gate.

    The join mode says nothing about READABILITY — that is the separate
    ``SpaceFeatures.allow_subscribers`` opt-in.
    """
    if isinstance(value, str) and value in tuple(JoinMode):
        return str(value)
    return str(JoinMode.INVITE_ONLY)


@dataclass(slots=True, frozen=True)
class Space:
    """A space (the cross-household container for a group of people)."""

    id: str
    name: str
    owner_instance_id: str
    owner_username: str
    identity_public_key: str
    config_sequence: int
    features: SpaceFeatures
    space_type: SpaceType
    join_mode: JoinMode

    # Optional fields — must come after required ones.
    #: Dedicated monotonic counter for roster gossip (member join / role / ban
    #: tombstone), decoupled from ``config_sequence`` (the config-LWW version).
    #: A roster event advances only this; a real config edit advances only
    #: ``config_sequence``. Backfilled from ``config_sequence`` once on migration
    #: 0036 so member_versions stay monotonic. Default 0 keeps it after the
    #: required fields (dataclass ordering).
    roster_sequence: int = 0
    #: Hybrid Logical Clock (``"<physical_ms>-<counter>"``) advanced once per
    #: local config edit (migration 0037). The config-LWW tie-break key is
    #: ``(config_sequence, config_hlc, config_author_instance)`` — at an equal
    #: sequence the later edit (greater HLC) wins, falling back to the author
    #: tie-break when the HLC ties (a legacy "0-0" row / older sender). See
    #: ``infrastructure/hlc.py`` + ``federation_inbound_service`` config LWW.
    config_hlc: str = "0-0"
    description: str | None = None
    emoji: str | None = None
    retention_days: int | None = None  # None → unlimited
    retention_exempt_types: tuple[str, ...] = field(default_factory=tuple)
    join_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    radius_km: float | None = None
    bot_enabled: bool = False
    dissolved: bool = False
    #: Soft, reversible archive. Distinct from ``dissolved`` (hard-gone):
    #: an archived space stays readable but is read-only and drops out of
    #: active space lists. Federates over SPACE_CONFIG_CHANGED + space_meta.
    archived: bool = False
    #: Why the space is archived. NULL = not-archived or a normal/reversible
    #: admin archive; ``'dissolved'``/``'removed'`` = remote-terminated
    #: (read-only, content kept, not unarchivable).
    archived_reason: str | None = None
    allow_here_mention: bool = False
    # Rich-text "about" block rendered at the top of the space feed
    # via MarkdownView (§23 customization).
    about_markdown: str | None = None
    # Short hex digest of the current cover WebP; bytes in
    # ``space_covers``. None → render a gradient fallback.
    cover_hash: str | None = None
    # Short hex digest of the current icon (avatar) WebP; bytes in
    # ``space_icons``. None → fall back to the space emoji.
    icon_hash: str | None = None
    # IANA timezone name (``"Europe/Berlin"``) that anchors this
    # space's calendar wall clock. Defaults to ``"UTC"`` at the schema
    # level (``spaces.tz NOT NULL DEFAULT 'UTC'``); on space creation
    # the service layer seeds it from the creator's household tz so
    # the common case (everyone in the same household) does the right
    # thing without an extra click. Editable by space admins for the
    # federated-multi-household case where the space anchors to a
    # different city than the home household.
    tz: str = "UTC"
    # §CP.F1 child-protection age gate. Federated in space_meta so a member
    # household enforces the host's gate locally on its own join paths (a
    # protected minor below ``min_age`` can't be seated). 0 → no restriction;
    # CHECK-constrained to {0,13,16,18} at the schema level.
    min_age: int = 0
    #: Discovery category (§23.50) — one of ``SPACE_CATEGORIES`` (above).
    #: ``None`` = unset; normalizes to ``"general"`` on display. Shown only
    #: for public/global tiers. Replaces the legacy ``target_audience`` hint.
    category: str | None = None


@dataclass(slots=True, frozen=True)
class SpaceMember:
    """A single member row in a space.

    ``role`` is one of :class:`SpaceRole`'s values. The dataclass keeps
    it as ``str`` because rows are hydrated directly from SQLite, but
    service-layer code should compare against :class:`SpaceRole` members
    rather than bare string literals. Per §4.2.3 there is no separate
    ACL/permissions field — the role string is the only authorization
    record on the member row.
    """

    space_id: str
    user_id: str
    role: str  # one of SpaceRole values
    joined_at: str
    history_visible_from: str | None = None
    location_share_enabled: bool = False
    space_display_name: str | None = None  # member-self-set alias (§4.1.6)
    # Per-space picture hash (bytes live in
    # ``space_member_profile_pictures``). NULL means inherit household.
    picture_hash: str | None = None


@dataclass(slots=True, frozen=True)
class SpaceZone:
    """A per-space display zone (§23.8.7).

    Each zone is a labelled circle on the space map. The space owns its
    own catalogue — Home Assistant zones never reach a space. Member GPS
    pins are matched to zones client-side; the wire never carries
    "member X is in zone Y" preprocessed labels.

    Fields:

    * ``id`` — opaque identifier (``z_<urlsafe>``).
    * ``space_id`` — owning space.
    * ``name`` — display label, unique within the space.
    * ``latitude``/``longitude`` — centre, 4dp-truncated.
    * ``radius_m`` — display radius (25 m – 50 km).
    * ``color`` — ``"#RRGGBB"`` or ``None`` (client picks from palette).
    * ``created_by`` — ``user_id`` of the admin who first saved the zone.
    """

    id: str
    space_id: str
    name: str
    latitude: float
    longitude: float
    radius_m: int
    created_by: str
    created_at: str
    updated_at: str
    color: str | None = None


@dataclass(slots=True, frozen=True)
class SpacePublicProfile:
    """Public-facing metadata shown on discovery / advertising endpoints.

    Never includes member lists or activity counts beyond the advertised
    public-summary fields in §13.
    """

    space_id: str
    name: str
    description: str | None
    emoji: str | None
    owner_instance_id: str
    member_count: int
    location: tuple[float, float] | None  # (lat, lon), 4dp-truncated
    radius_km: float | None
    join_mode: JoinMode
