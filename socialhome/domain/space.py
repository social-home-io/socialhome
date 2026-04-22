"""Space and household feature domain types (§4.3 / §13).

Defines:

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
    "file",
    "image",
    "poll",
    "schedule",
    "text",
    "transcript",
    "video",
)


@dataclass(slots=True, frozen=True)
class SpaceFeatures:
    """Per-space feature toggles and access levels."""

    calendar: bool = False
    todo: bool = True
    location: bool = False
    location_mode: str = "off"  # "off" | "zone_only" | "gps"
    stickies: bool = False
    pages: bool = True

    posts_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN
    pages_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN
    stickies_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN
    calendar_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN
    tasks_access: SpaceFeatureAccess = SpaceFeatureAccess.OPEN

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
                )
                if row.get(col, 1)
            )
        )
        return cls(
            calendar=bool(row.get("feature_calendar", 0)),
            todo=bool(row.get("feature_todo", 1)),
            location=bool(row.get("feature_location", 0)),
            location_mode=row.get("location_mode", "off"),
            stickies=bool(row.get("feature_stickies", 0)),
            pages=bool(row.get("feature_pages", 1)),
            posts_access=SpaceFeatureAccess(row.get("posts_access", "open")),
            pages_access=SpaceFeatureAccess(row.get("pages_access", "open")),
            stickies_access=SpaceFeatureAccess(row.get("stickies_access", "open")),
            calendar_access=SpaceFeatureAccess(row.get("calendar_access", "open")),
            tasks_access=SpaceFeatureAccess(row.get("tasks_access", "open")),
            allowed_post_types=allowed or ("text",),
        )

    def to_columns(self) -> dict:
        return {
            "feature_calendar": int(self.calendar),
            "feature_todo": int(self.todo),
            "feature_location": int(self.location),
            "feature_stickies": int(self.stickies),
            "feature_pages": int(self.pages),
            "location_mode": self.location_mode,
            "posts_access": self.posts_access.value,
            "pages_access": self.pages_access.value,
            "stickies_access": self.stickies_access.value,
            "calendar_access": self.calendar_access.value,
            "tasks_access": self.tasks_access.value,
            "allow_post_text": int("text" in self.allowed_post_types),
            "allow_post_image": int("image" in self.allowed_post_types),
            "allow_post_video": int("video" in self.allowed_post_types),
            "allow_post_transcript": int("transcript" in self.allowed_post_types),
            "allow_post_poll": int("poll" in self.allowed_post_types),
            "allow_post_schedule": int("schedule" in self.allowed_post_types),
            "allow_post_file": int("file" in self.allowed_post_types),
            "allow_post_bazaar": int("bazaar" in self.allowed_post_types),
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
            "posts_access": self.posts_access.value,
            "pages_access": self.pages_access.value,
            "stickies_access": self.stickies_access.value,
            "calendar_access": self.calendar_access.value,
            "tasks_access": self.tasks_access.value,
            "allowed_post_types": list(self.allowed_post_types),
        }


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
    PUBLIC_MODE_CHANGED = "public_mode_changed"
    COVER_UPDATED = "cover_updated"
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


class JoinMode(StrEnum):
    INVITE_ONLY = "invite_only"
    OPEN = "open"
    LINK = "link"
    REQUEST = "request"


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
    allow_here_mention: bool = False
    # Rich-text "about" block rendered at the top of the space feed
    # via MarkdownView (§23 customization).
    about_markdown: str | None = None
    # Short hex digest of the current cover WebP; bytes in
    # ``space_covers``. None → render a gradient fallback.
    cover_hash: str | None = None


@dataclass(slots=True, frozen=True)
class SpaceMember:
    """A single member row in a space."""

    space_id: str
    user_id: str
    role: str  # "owner" | "admin" | "member"
    joined_at: str
    history_visible_from: str | None = None
    location_share_enabled: bool = False
    space_display_name: str | None = None  # member-self-set alias (§4.1.6)
    # Per-space picture hash (bytes live in
    # ``space_member_profile_pictures``). NULL means inherit household.
    picture_hash: str | None = None


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
