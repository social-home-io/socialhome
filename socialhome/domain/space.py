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
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, TYPE_CHECKING
from urllib.parse import urlsplit

from .errors import CodedError
from .presence import truncate_coord

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

    Per spec §4.2.3, signing authority is *holding the space private key*
    — not a row in an ACL table. The roles below are a social-layer
    distinction, ordered ``OWNER > ADMIN > MODERATOR > MEMBER >
    SUBSCRIBER``:

    * ``OWNER`` / ``ADMIN`` hold **settings authority** — config,
      features, access levels, members and roles, invites, bans, keys,
      archive, zones, timetables, bots, themes, ``@here``.
      (:data:`SETTINGS_AUTHORITY_ROLES`)
    * ``MODERATOR`` holds **content authority** only — approve / reject
      the moderation queue, edit / delete other people's content,
      bypass ``MODERATED`` for their own. No settings power at all.
      (:data:`CONTENT_AUTHORITY_ROLES`)
    * ``MEMBER`` is the regular participant. (:data:`WRITER_ROLES`)
    * ``SUBSCRIBER`` is the read-only follower of public/global spaces,
      which exists only while ``SpaceFeatures.allow_subscribers`` is ON.

    The role set is closed and ordered — one role per seat, never a
    per-user permission bitfield or custom per-space roles. A new
    *capability* on existing roles extends :class:`SpaceFeatures` with a
    feature gate; a new *role* (as ``MODERATOR`` was, federation v_41) is
    a protocol change: a migration widening both role CHECKs, a
    ``proto_version`` bump, and a degraded wire form for older peers.
    Compare against the authority sets below, never against an ad-hoc
    tuple, so a role added later lands in exactly one place.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MODERATOR = "moderator"
    MEMBER = "member"
    SUBSCRIBER = "subscriber"


#: Settings authority — the space's structure and people (§4.2.3). Every
#: settings guard (``_require_admin_or_owner`` and friends) reads this.
SETTINGS_AUTHORITY_ROLES: frozenset[SpaceRole] = frozenset(
    {SpaceRole.OWNER, SpaceRole.ADMIN}
)

#: Content authority — act on other people's content: the moderation
#: queue, edits / deletes of others' posts and comments, single gallery
#: item deletes, RSVP approvals. Settings authority implies it. (A whole
#: gallery album stays settings authority.)
CONTENT_AUTHORITY_ROLES: frozenset[SpaceRole] = frozenset(
    {SpaceRole.OWNER, SpaceRole.ADMIN, SpaceRole.MODERATOR}
)

#: Seats that write space content — everyone but a read-only subscriber.
WRITER_ROLES: frozenset[SpaceRole] = frozenset(
    {SpaceRole.OWNER, SpaceRole.ADMIN, SpaceRole.MODERATOR, SpaceRole.MEMBER}
)


def role_change_allowed(
    actor_role: object, target_current_role: object, new_role: object
) -> bool:
    """May a seat holding ``actor_role`` move a target from
    ``target_current_role`` to ``new_role``?

    * The **owner** may set ``admin`` / ``moderator`` / ``member`` on any
      non-owner seat.
    * An **admin** may move a target only between ``member`` and
      ``moderator`` — never touch another admin, never make one.
    * Nobody else changes roles. Nobody assigns ``owner`` (that is
      ``transfer_ownership``) and nobody demotes the owner.

    Unknown values (a future role, junk from a request body) answer
    ``False``. Plain strings and :class:`SpaceRole` members compare equal.
    """
    try:
        actor = SpaceRole(str(actor_role))
        current = SpaceRole(str(target_current_role))
        new = SpaceRole(str(new_role))
    except ValueError:
        return False
    if current is SpaceRole.OWNER or new is SpaceRole.OWNER:
        return False
    if actor is SpaceRole.OWNER:
        return new in (SpaceRole.ADMIN, SpaceRole.MODERATOR, SpaceRole.MEMBER)
    if actor is SpaceRole.ADMIN:
        movable = (SpaceRole.MODERATOR, SpaceRole.MEMBER)
        return current in movable and new in movable
    return False


#: Roles a **remote** household's seat may carry — the
#: ``space_remote_members.role`` CHECK (migrations 0009 + 0054 + 0065), in
#: code. ``subscriber`` is a household that redeemed a Follower invite link
#: (v_30); ``moderator`` is a content-authority seat (v_41); ``owner`` is
#: absent because ownership is a local-only privilege with no remote row
#: shape, and the host legitimately ships its own ``space_members.role`` —
#: ``owner`` included — on the roster wire.
MIRRORABLE_REMOTE_ROLES: frozenset[str] = frozenset(
    {
        SpaceRole.MEMBER.value,
        SpaceRole.ADMIN.value,
        SpaceRole.MODERATOR.value,
        SpaceRole.SUBSCRIBER.value,
    }
)


def mirrorable_remote_role(raw: object) -> str:
    """Coerce a wire-supplied role to one a remote seat may hold.

    The role is advisory on the wire; the mutation carrying it is not. A
    value this household's ``space_remote_members.role`` CHECK rejects —
    an ``owner``, or a role from a future version — raises IntegrityError
    out of the apply and takes the WHOLE event down with it, tombstone
    included, and the version guard then refuses the retry at the same
    ``member_version``. So an unknown role becomes the least-privileged
    real seat and the mutation survives: losing "which seat" is
    recoverable from the next snapshot, losing "they left" is not.

    Note the direction of the coercion — DOWN to ``member``, never up.
    A missing role on a first-revision payload means an old sender that
    knew only ``member``/``admin``, which is the same answer.
    """
    role = str(raw or SpaceRole.MEMBER.value)
    return role if role in MIRRORABLE_REMOTE_ROLES else SpaceRole.MEMBER.value


def owner_seat_from_roster(roster: object, host_instance_id: str) -> str | None:
    """The owner's ``user_id`` in a host's invite roster (``space_meta``).

    The host ships its own members with their real ``space_members.role``,
    ``owner`` included; only an entry ON the host's (authenticated) instance
    can name the owner. ``None`` when the roster names none.
    """
    if not isinstance(roster, list) or not host_instance_id:
        return None
    for entry in roster:
        if (
            isinstance(entry, dict)
            and entry.get("role") == SpaceRole.OWNER.value
            and entry.get("instance_id") == host_instance_id
            and entry.get("user_id")
        ):
            return str(entry["user_id"])
    return None


#: Privilege order of the roles a remote seat may hold (low → high).
_REMOTE_ROLE_RANK: dict[str, int] = {
    SpaceRole.SUBSCRIBER.value: 0,
    SpaceRole.MEMBER.value: 1,
    SpaceRole.MODERATOR.value: 2,
    SpaceRole.ADMIN.value: 3,
}


def cap_remote_role(role: str, ceiling: str) -> str:
    """``role``, but never more privileged than ``ceiling``.

    The household that HOSTS a space applies this to inbound roster gossip
    (v_44): promotion is the owner's own ``set_remote_member_role``, so a
    gossiped role may lower a seat but never raise it — otherwise a
    demoted seed holder could gossip itself back to admin and be handed
    the new signing seed. Both arguments are mirrorable roles (unknown →
    ``member``, see :func:`mirrorable_remote_role`).
    """
    role = mirrorable_remote_role(role)
    ceiling = mirrorable_remote_role(ceiling)
    return role if _REMOTE_ROLE_RANK[role] <= _REMOTE_ROLE_RANK[ceiling] else ceiling


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
    * ``MODERATED`` — members' submissions enter a pending queue; content
      authority (owner / admin / moderator) approves or rejects, and
      bypasses the queue for its own submissions.
    * ``ADMIN_ONLY`` — only admins / owner may mutate. Members and
      moderators read-only.
    """

    OPEN = "open"
    MODERATED = "moderated"
    ADMIN_ONLY = "admin_only"


#: The features carrying a ``*_access`` level, in wire order. A post of any
#: type is ``posts``; task lists ride ``tasks``.
ACCESS_FEATURES: tuple[str, ...] = ("posts", "pages", "stickies", "calendar", "tasks")


class ContentAction(StrEnum):
    """What a write does to a feature's content (§4.3 access levels).

    * ``CREATE`` — a new post / page / task or list / sticky / event.
    * ``EDIT`` — any change to an existing item's fields, a task's status
      or column move included.
    * ``DELETE`` — delete, archive, a task list's delete.
    * ``LAYOUT`` — arrangement only: a task's reorder within its column,
      a sticky's position move. ``ADMIN_ONLY`` denies it like any write;
      under ``MODERATED`` it never queues.
    """

    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    LAYOUT = "layout"


class AccessDecision(StrEnum):
    """The answer of :meth:`SpaceFeatures.access_decision`."""

    PROCEED = "proceed"
    QUEUE = "queue"
    DENY = "deny"


def restricted_access_changes(
    before: "SpaceFeatures", after: "SpaceFeatures"
) -> tuple[str, ...]:
    """The :data:`ACCESS_FEATURES` an edit from ``before`` to ``after`` sets
    to a level other than ``OPEN`` that they did not already have, sorted.

    Relaxing a feature back to ``OPEN`` is never listed: an older peer that
    cannot enforce a level loses nothing when the level goes away.
    """
    return tuple(
        sorted(
            f
            for f in ACCESS_FEATURES
            if after.access_level(f) is not SpaceFeatureAccess.OPEN
            and after.access_level(f) is not before.access_level(f)
        )
    )


# Default allowed post types for a fresh space. Ordered for a stable wire form.
#: Invite link types (migration 0079), chosen by the issuer at mint time.
#: ``gfs``: the code carries the issuer's key-wrap key, so a household that
#: never met the issuer redeems it through the connection-server relay
#: (§D2b) — allowed on a private space only while its owner has
#: ``SpaceFeatures.private_gfs`` ON. ``internal``: paired / mesh households
#: only — the code carries no key-wrap key and the issuer refuses a redeem of
#: it that arrives over the relay, so it never touches a connection server.
#: ``gfs_legacy``: never minted — a live link of a private space that
#: migration 0079 left OFF (every pre-0079 link was relay-redeemable). It
#: stays redeemable over the relay until used up or expired, and the first
#: household that joins through one turns the space's ``private_gfs`` ON.
INVITE_VIA_GFS = "gfs"
INVITE_VIA_INTERNAL = "internal"
INVITE_VIA_GFS_LEGACY = "gfs_legacy"
#: The types a new link may be minted with.
INVITE_VIAS: frozenset[str] = frozenset({INVITE_VIA_GFS, INVITE_VIA_INTERNAL})


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


#: Values ``spaces.retention_exempt_json`` may hold — every post type. The
#: retention sweep filters ``space_posts.type NOT IN (...)``, so anything
#: else (the pre-#733 UI's ``pages`` / ``gallery`` / ``tasks``) could never
#: match a row and would be a silently-ignored exemption.
RETENTION_EXEMPTABLE_TYPES: frozenset[str] = frozenset(_ALL_POST_TYPES)


def normalize_retention_exempt_types(
    value: object,
    *,
    strict: bool = False,
) -> tuple[str, ...]:
    """Normalise a ``retention_exempt_types`` list to sorted, de-duplicated
    post-type values.

    ``strict=True`` (a local admin's edit) raises :class:`ValueError` for a
    non-list or a value outside :data:`RETENTION_EXEMPTABLE_TYPES`, so the
    API answers 422. ``strict=False`` (a stored row, a remote admin's
    forwarded edit) drops anything unknown instead — a newer peer's post
    type must not fail the rest of the config edit. Blank entries are
    ignored either way; ``None`` means "no exemptions".
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        if strict:
            raise ValueError("retention_exempt_types must be a list of post types")
        return ()
    kept: set[str] = set()
    for raw in value:
        item = raw.strip() if isinstance(raw, str) else None
        if not item:
            if strict and not isinstance(raw, str):
                raise ValueError(f"invalid retention exempt type {raw!r}")
            continue
        if item not in RETENTION_EXEMPTABLE_TYPES:
            if strict:
                raise ValueError(f"unknown retention exempt type {item!r}")
            continue
        kept.add(item)
    return tuple(sorted(kept))


def _publish_mode(
    raw: object, default: Literal["trusted", "strict"]
) -> Literal["trusted", "strict"]:
    """A ``gfs_publish_mode`` value: ``"strict"`` / ``"trusted"`` as given,
    ``default`` for anything else (absent, or an unknown value from a newer
    peer — never silently ``strict``, never an error)."""
    if raw == "strict":
        return "strict"
    if raw == "trusted":
        return "trusted"
    return default


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
    #: Per-space toggle for shared timetables (a class *Stundenplan*
    #: posted by the space admins). Defaults OFF — unlike the other tabs —
    #: because it only makes sense for a school / class space; migration
    #: 0063 added the column with ``DEFAULT 0``.
    timetable: bool = False
    #: Per-space chat (a system group conversation of the space's writers,
    #: federated as the v_55 ``SPACE_CHAT_*`` events). Defaults ON — owner
    #: decision: every space, existing (migration 0083 ``DEFAULT 1``) and
    #: new, has one; space admins turn it off. Off hides the chat, refuses
    #: writes and drops inbound chat events; the messages are kept. Older
    #: peers that omit the field read ON.
    chat: bool = True

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
    #: ``SPACE_ADMIN_KEY_SHARE`` (v_22); flipping it off rotates the space
    #: authority key so every shared seed stops verifying (v_44, see
    #: ``SpaceAuthorityRotationService``). Toggling it is
    #: OWNER-only (a host-local admin can't enact it). Defaults OFF
    #: (least-privilege). Older peers that omit the field default to False.
    delegated_admin_authority: bool = False

    #: Owner choice (v_50): how member households publish over a connection
    #: server (GFS). ``"trusted"`` (the default): the publish is IDENTIFIED —
    #: the server learns which household posted, never the content.
    #: ``"strict"``: the publish is ANONYMOUS, signed with the epoch's writer
    #: group key; the server learns only that some publisher of the space
    #: posted, and refuses identified publishes into the space. OWNER-only
    #: (like ``allow_subscribers``): pinned on host inbound and only taken
    #: from the owner household on every other household. Older peers that
    #: omit the field read ``"trusted"``.
    gfs_publish_mode: Literal["trusted", "strict"] = "trusted"

    #: Owner choice for a PRIVATE space: may it use a connection server
    #: (GFS) at all? OFF (the default for a new private space): the space
    #: never touches a GFS — no invite link redeemable through the relay, no
    #: opaque channel, no subscriptions. ON: invite links of type ``"gfs"``
    #: are allowed (a stranger joins through the relay, no pairing needed),
    #: the owner registers the space's opaque channel on its connection
    #: servers, and EVERY member household connected to one of them —
    #: paired and mesh-only members included — takes a seat on it, so
    #: members reach each other while the host is offline. The server then
    #: learns those member households, never the space id, name, key or
    #: content. Turning it OFF is refused while link-joined members remain.
    #: OWNER-only (like ``gfs_publish_mode``): pinned on host inbound and
    #: only taken from the owner household everywhere else. Meaningless on a
    #: public / global space. Older peers that omit the field read OFF.
    private_gfs: bool = False

    allowed_post_types: tuple[str, ...] = _ALL_POST_TYPES

    # ── Helpers ──────────────────────────────────────────────────────────

    def allows(self, post_type: "PostType | str") -> bool:
        val = post_type.value if hasattr(post_type, "value") else str(post_type)
        return val in self.allowed_post_types

    def access_level(self, feature: str) -> SpaceFeatureAccess:
        """The access level of one of :data:`ACCESS_FEATURES`.

        :class:`ValueError` for any other name — a typo must not read as
        ``OPEN``.
        """
        if feature not in ACCESS_FEATURES:
            raise ValueError(f"unknown access-gated feature {feature!r}")
        level: SpaceFeatureAccess = getattr(self, f"{feature}_access")
        return level

    def access_decision(
        self,
        feature: str,
        *,
        role: "SpaceRole | str | None",
        action: ContentAction,
        owns_target: bool,
    ) -> AccessDecision:
        """What happens when a seat holding ``role`` attempts ``action``
        on ``feature`` (§4.3 feature access levels).

        * ``OPEN`` — :attr:`AccessDecision.PROCEED`.
        * ``ADMIN_ONLY`` — PROCEED for settings authority (owner / admin),
          DENY for everyone else, moderators included, for every action
          (``LAYOUT`` too).
        * ``MODERATED`` — content authority (owner / admin / moderator)
          PROCEEDs; a member's ``CREATE`` QUEUEs; a member's ``EDIT`` /
          ``DELETE`` PROCEEDs on their own item and QUEUEs on someone
          else's; ``LAYOUT`` PROCEEDs.

        ``role`` may be a :class:`SpaceRole` or the raw
        ``space_members.role`` string. No seat (``None``), a subscriber or
        an unknown value fails closed (DENY) on a restricted feature — the
        writer gates refuse those earlier, this is the backstop — except a
        read-only seat's edit / delete of its own item under ``MODERATED``.
        """
        level = self.access_level(feature)
        if level is SpaceFeatureAccess.OPEN:
            return AccessDecision.PROCEED
        try:
            seat = SpaceRole(str(role)) if role is not None else None
        except ValueError:
            seat = None
        if seat is None or seat not in WRITER_ROLES:
            # A read-only seat (a demoted author) keeps an edit / delete of
            # its OWN item under MODERATED — today's rule; never more.
            if (
                seat is not None
                and level is SpaceFeatureAccess.MODERATED
                and owns_target
                and action in (ContentAction.EDIT, ContentAction.DELETE)
            ):
                return AccessDecision.PROCEED
            return AccessDecision.DENY
        if level is SpaceFeatureAccess.ADMIN_ONLY:
            if seat in SETTINGS_AUTHORITY_ROLES:
                return AccessDecision.PROCEED
            return AccessDecision.DENY
        # MODERATED
        if seat in CONTENT_AUTHORITY_ROLES:
            return AccessDecision.PROCEED
        if action is ContentAction.CREATE:
            return AccessDecision.QUEUE
        if action is ContentAction.LAYOUT or owns_target:
            return AccessDecision.PROCEED
        return AccessDecision.QUEUE

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
            timetable=bool(row.get("feature_timetable", 0)),
            chat=bool(row.get("feature_chat", 1)),
            posts_access=SpaceFeatureAccess(row.get("posts_access", "open")),
            pages_access=SpaceFeatureAccess(row.get("pages_access", "open")),
            stickies_access=SpaceFeatureAccess(row.get("stickies_access", "open")),
            calendar_access=SpaceFeatureAccess(row.get("calendar_access", "open")),
            tasks_access=SpaceFeatureAccess(row.get("tasks_access", "open")),
            allow_subscribers=bool(row.get("allow_subscribers", 0)),
            allow_subscriber_comment=bool(row.get("allow_subscriber_comment", 0)),
            allow_subscriber_react=bool(row.get("allow_subscriber_react", 0)),
            delegated_admin_authority=bool(row.get("delegated_admin_authority", 0)),
            gfs_publish_mode=_publish_mode(row.get("gfs_publish_mode"), "trusted"),
            private_gfs=bool(row.get("private_gfs", 0)),
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
            "feature_timetable": int(self.timetable),
            "feature_chat": int(self.chat),
            "posts_access": self.posts_access.value,
            "pages_access": self.pages_access.value,
            "stickies_access": self.stickies_access.value,
            "calendar_access": self.calendar_access.value,
            "tasks_access": self.tasks_access.value,
            "allow_subscribers": int(self.allow_subscribers),
            "allow_subscriber_comment": int(self.allow_subscriber_comment),
            "allow_subscriber_react": int(self.allow_subscriber_react),
            "delegated_admin_authority": int(self.delegated_admin_authority),
            "gfs_publish_mode": self.gfs_publish_mode,
            "private_gfs": int(self.private_gfs),
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
            "timetable": self.timetable,
            "chat": self.chat,
            "posts_access": self.posts_access.value,
            "pages_access": self.pages_access.value,
            "stickies_access": self.stickies_access.value,
            "calendar_access": self.calendar_access.value,
            "tasks_access": self.tasks_access.value,
            "allow_subscribers": self.allow_subscribers,
            "allow_subscriber_comment": self.allow_subscriber_comment,
            "allow_subscriber_react": self.allow_subscriber_react,
            "delegated_admin_authority": self.delegated_admin_authority,
            "gfs_publish_mode": self.gfs_publish_mode,
            "private_gfs": self.private_gfs,
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
            timetable=bool(raw.get("timetable", defaults.timetable)),
            chat=bool(raw.get("chat", defaults.chat)),
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
            gfs_publish_mode=_publish_mode(
                raw.get("gfs_publish_mode"), defaults.gfs_publish_mode
            ),
            private_gfs=bool(raw.get("private_gfs", defaults.private_gfs)),
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


#: The key of the approval block inside a sealed ``SPACE_*`` content payload
#: (v_43): ``{"item_id": …, "approved_by": …}``. Present only on content an
#: approver household released from the moderation queue.
MODERATION_BLOCK_KEY = "moderation"

#: Bounds on the block's fields (an item id is a uuid4 hex; a user id is a
#: derived hex id — both far shorter).
_MAX_ITEM_ID = 64
_MAX_USER_ID = 128


@dataclass(slots=True, frozen=True)
class ModerationApproval:
    """Who released a queue item: the approval block content carries (v_43).

    Not a signature — the receiver re-derives every claim from what it
    already holds (the sender's seats, its own queue row): see
    ``SpaceAuthorship.may_author_approved``.
    """

    item_id: str
    approved_by: str

    def to_wire(self) -> dict[str, str]:
        return {"item_id": self.item_id, "approved_by": self.approved_by}

    @classmethod
    def from_wire(cls, raw: object) -> "ModerationApproval | None":
        """The block of a payload, ``None`` when absent or malformed."""
        if not isinstance(raw, dict):
            return None
        item_id = raw.get("item_id")
        approved_by = raw.get("approved_by")
        if not isinstance(item_id, str) or not isinstance(approved_by, str):
            return None
        if not item_id or not approved_by:
            return None
        if len(item_id) > _MAX_ITEM_ID or len(approved_by) > _MAX_USER_ID:
            return None
        return cls(item_id=item_id, approved_by=approved_by)


# ─── Signed config events (§4.3) ──────────────────────────────────────────


class SpaceConfigEventType(StrEnum):
    RENAME = "rename"
    FEATURE_CHANGED = "feature_changed"
    ALLOWED_POST_TYPES_CHANGED = "allowed_post_types_changed"
    JOIN_MODE_CHANGED = "join_mode_changed"
    ADMIN_GRANTED = "admin_granted"
    ADMIN_REVOKED = "admin_revoked"
    #: A role change that neither grants nor revokes admin (member ↔
    #: moderator, v_41). Local bus / realtime only — like the two above it
    #: never federates as ``SPACE_CONFIG_CHANGED``.
    ROLE_CHANGED = "role_changed"
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


def role_change_event_type(
    old_role: object, new_role: object
) -> "SpaceConfigEventType":
    """The bus event naming a role change: admin granted / revoked when the
    admin seat moves, else :attr:`SpaceConfigEventType.ROLE_CHANGED`."""
    if str(new_role) == SpaceRole.ADMIN.value:
        return SpaceConfigEventType.ADMIN_GRANTED
    if str(old_role) == SpaceRole.ADMIN.value:
        return SpaceConfigEventType.ADMIN_REVOKED
    return SpaceConfigEventType.ROLE_CHANGED


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


class AlreadyMemberError(CodedError, ValueError):
    """The caller asked to join a space they already belong to."""

    status = 422
    code = "ALREADY_MEMBER"
    detail = "already a member"


class UserAlreadyMemberError(CodedError, SpacePermissionError):
    """An admin invited someone who already belongs to the space."""

    status = 403
    code = "USER_ALREADY_MEMBER"
    detail = "user is already a member"


class BannedFromSpaceError(CodedError, SpacePermissionError):
    """The caller is banned from the space they tried to join. The detail
    names nobody — never the raw user id."""

    status = 403
    code = "BANNED"
    detail = "banned from this space"

    def __init__(self) -> None:
        super().__init__()
        self.banned = True


class UserBannedError(CodedError, SpacePermissionError):
    """An admin tried to add or invite someone banned from the space."""

    status = 403
    code = "USER_BANNED"
    detail = "user is banned from this space"

    def __init__(self) -> None:
        super().__init__()
        self.banned = True


class InviteOnlyError(CodedError, SpacePermissionError):
    """A join request to a space that only takes invited members."""

    status = 403
    code = "INVITE_ONLY"
    detail = "space is invite-only"


class SubscribeNotAllowedError(CodedError, SpacePermissionError):
    """A follow (subscribe) of a space that doesn't take followers."""

    status = 403
    code = "SUBSCRIBE_NOT_ALLOWED"
    detail = "this space does not allow subscribers"


class SubscriberReadOnlyError(CodedError, SpacePermissionError):
    """A subscriber (follower) tried to post, comment or react where
    followers may only read. ``params.action`` is the attempted action."""

    status = 403
    code = "SUBSCRIBER_READ_ONLY"

    def __init__(self, action: str) -> None:
        super().__init__(
            f"subscribers can only read — joining as a member is required to {action}",
            params={"action": action},
        )


class SpaceArchivedError(CodedError, SpacePermissionError):
    """A write to an archived (read-only) space."""

    status = 403
    code = "SPACE_ARCHIVED"
    detail = "space is archived (read-only) — unarchive it to make changes"


class HostNotPairedError(CodedError, SpacePermissionError):
    """The space's host household isn't a confirmed connection."""

    status = 403
    code = "NOT_PAIRED"
    detail = "host household is not a CONFIRMED peer — pair first"


class AgeRestrictedError(CodedError, SpacePermissionError):
    """§CP.F1: a protected account below the space's minimum age."""

    status = 403
    code = "AGE_RESTRICTED"

    def __init__(self, min_age: int) -> None:
        super().__init__(
            f"This space is restricted to users aged {min_age}+.",
            params={"min_age": min_age},
        )


class InviteExpiredError(CodedError, KeyError):
    """An invite token that is unknown, expired or used up."""

    status = 404
    code = "INVITE_EXPIRED"
    detail = "invite token invalid, expired, or exhausted"

    def __str__(self) -> str:
        # ``KeyError.__str__`` would quote the detail.
        return self.detail


class HouseholdUpgradeRequiredError(SpacePermissionError):
    """A role the target's home household cannot hold yet (v_41
    ``moderator`` on a v_40 peer). The API answers 403 with the stable code
    ``HOUSEHOLD_UPGRADE_REQUIRED`` so the SPA can show translated copy."""


class AccessAdminOnlyError(SpacePermissionError):
    """A write to a feature whose access level is ``ADMIN_ONLY`` by a seat
    without settings authority. The API answers 403 with the stable code
    ``ACCESS_ADMIN_ONLY`` (plus the ``feature``) so the SPA can show
    translated copy."""

    def __init__(self, feature: str) -> None:
        super().__init__(f"only space admins can change {feature} here")
        self.feature = feature


class PeersTooOldError(Exception):
    """An access-level change some member households cannot enforce yet
    (below v_42). The API answers 409 ``PEERS_TOO_OLD`` with the households
    so the admin can upgrade them first or apply anyway (``force``)."""

    def __init__(self, households: "list[dict[str, object]]") -> None:
        super().__init__("some member households cannot enforce access levels yet")
        self.households = households


class PrivateGfsLinkMembersError(Exception):
    """The owner tried to turn a private space's connection-server option
    (``SpaceFeatures.private_gfs``) OFF while households that joined through
    an invite link are still members — they reach the host only over the
    connection server, so turning it off would strand them. The API answers
    409 ``PRIVATE_GFS_LINK_MEMBERS`` naming them; the owner removes them
    first."""

    def __init__(self, households: "list[dict[str, object]]") -> None:
        super().__init__(
            "households that joined through an invite link are still members"
        )
        self.households = households


class PrivateGfsOffError(Exception):
    """A connection-server (``gfs``) invite link was asked for on a private
    space whose owner has not turned ``SpaceFeatures.private_gfs`` on. The
    API answers 409 ``PRIVATE_GFS_OFF``."""


class PublicSpaceLimitError(Exception):
    """Raised when an instance tries to exceed its cap on public spaces."""


class ModerationAlreadyDecidedError(Exception):
    """Raised when an admin tries to approve/reject a queue item that's
    already been decided (or expired). Route layer maps this to HTTP 409.
    """


class ContentQueuedForReview(Exception):
    """Not an error: a write under ``MODERATED`` went to the space's
    moderation queue instead of the content table (§4.3).

    Raised (never returned) by every content service whose gate answered
    QUEUE, so no caller can mistake the queued submission for the persisted
    object — the write simply did not happen yet. The API answers 202
    ``{queued: true, item_id, feature, action}`` (``BaseView._queued``).
    """

    def __init__(self, item: "SpaceModerationItem") -> None:
        super().__init__(f"{item.feature} {item.action} queued for review")
        self.item = item


class HostTooOldError(Exception):
    """The space's host is too old for what a member household asked of it.
    409 ``HOST_TOO_OLD`` — nothing is stored or sent.

    ``feature`` names what needs the newer host, so the SPA can say why:

    * ``"moderation"`` — a submission for review needs the host to hold
      moderation items for other households (v_43).
    * ``"role_change"`` — a role change made on a member household is
      forwarded to the host, which must apply ``set_member_role`` (v_47).
    * ``"invite_link"`` — an invite link minted on a member household is
      forwarded to the host, which must mint it and answer (v_52).
    """

    def __init__(self, host_instance_id: str, *, feature: str = "moderation") -> None:
        super().__init__(
            "the space's host household must be updated before members of "
            "other households can submit for review"
            if feature == "moderation"
            else "the space's home household needs an update before its "
            "invite links can be managed from another household"
            if feature == "invite_link"
            else "the space's host household must be updated before roles "
            "can be changed from another household"
        )
        self.host_instance_id = host_instance_id
        self.feature = feature


class HostUnreachableError(Exception):
    """A forward to the space's host went nowhere — no route, an unknown or
    unconfirmed host, or (``reason="unknown_host"``) a stub with no recorded
    host at all. 503 ``HOST_UNREACHABLE``: nothing was queued, so the SPA
    must not say "sent"."""

    def __init__(self, host_instance_id: str, *, reason: str = "unreachable") -> None:
        super().__init__(
            "this space's host household is not known here"
            if reason == "unknown_host"
            else "couldn't reach the space's host household"
        )
        self.host_instance_id = host_instance_id
        self.reason = reason


class ModerationQueueFullError(Exception):
    """Too many pending items for one submitter or one space (429)."""


class ModerationPayloadTooLargeError(ValueError):
    """A submission whose serialised payload exceeds the queue cap (413)."""


class ModerationStaleError(Exception):
    """The target changed since the item was submitted (a page edit based
    on an older version). 409 ``STALE``; approving with ``force`` applies
    anyway (latest wins)."""

    def __init__(self, *, current: dict, proposed: dict, base: dict) -> None:
        super().__init__("the item changed since this was submitted")
        self.current = current
        self.proposed = proposed
        self.base = base


class ModerationTargetGoneError(Exception):
    """An edit of an item that no longer exists — the queue item expires
    (410 ``TARGET_GONE``)."""


class ModerationExpiredError(ModerationTargetGoneError):
    """Approving an item past its review window: it expires instead
    (410 ``EXPIRED``)."""


class ModerationInProgressError(Exception):
    """Another approve (or resume) of the same item is still running here:
    409 ``IN_PROGRESS`` instead of a second, racing apply."""


class ModerationUnavailableError(Exception):
    """The feature was switched off or the space archived since the item
    was submitted: approve answers 409 ``FEATURE_UNAVAILABLE`` (reject still
    works)."""


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
    #: Rotation counter of ``identity_public_key`` (migration 0066, v_44).
    #: 0 = the creation-time key; each owner-certified rotation bumps it. A
    #: receiver adopts a new pin only from a cert whose epoch is HIGHER than
    #: this, so a replayed older cert can never restore a revoked key. Never
    #: federated as a plain field and never written by ``save`` — only by a
    #: verified cert (``adopt_authority_key``) or the owner's own rotation.
    authority_key_epoch: int = 0
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


#: §23.8.7: a zone name is a short map label — longer than this is not
#: a label, and from a peer it's a payload-size abuse.
MAX_ZONE_NAME_LENGTH = 64

#: Unicode categories a zone name may not contain: C0/C1 controls
#: (``Cc`` — NUL, ESC, newline, tab, DEL, NEL…) and the line / paragraph
#: separators (``Zl`` / ``Zp``). Format characters (``Cf``) stay allowed
#: so ZWJ emoji sequences keep working.
_ZONE_NAME_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})

#: ``#RRGGBB`` only — the colour reaches inline styles and SVG attributes
#: on every member's map, so nothing else may get through.
_ZONE_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def validate_zone_name(name: object) -> str:
    """Return the stripped zone name, or raise :class:`ValueError`.

    Applied to every zone write — the local API, ``SPACE_ZONE_UPSERTED``
    and the space-sync ``space_zones`` resource — so a peer can't store
    what a local admin couldn't. The error never echoes the name.
    """
    if not isinstance(name, str):
        raise ValueError("zone name must be a string")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("zone name must not be empty")
    if len(cleaned) > MAX_ZONE_NAME_LENGTH:
        raise ValueError(
            f"zone name must be {MAX_ZONE_NAME_LENGTH} characters or fewer",
        )
    if any(
        unicodedata.category(ch) in _ZONE_NAME_FORBIDDEN_CATEGORIES for ch in cleaned
    ):
        raise ValueError("zone name must not contain control characters")
    return cleaned


def validate_zone_color(color: object) -> str | None:
    """Return the lower-cased ``#RRGGBB`` colour (``None`` passes through),
    or raise :class:`ValueError`."""
    if color is None:
        return None
    if not isinstance(color, str) or not _ZONE_COLOR_RE.fullmatch(color):
        raise ValueError("color must be a #RRGGBB hex string or None")
    return color.lower()


#: §23.8.7: 25 m floor (just above the 4-dp ~11 m precision); 50 km
#: ceiling (a "city-wide" zone is the largest meaningful display bucket
#: on a per-space map).
MIN_ZONE_RADIUS_M: int = 25
MAX_ZONE_RADIUS_M: int = 50_000


def validate_zone_coord(value: object, *, name: str, limit: float) -> float:
    """Return a zone centre coordinate rounded to 4 dp, or raise
    :class:`ValueError`.

    ``limit`` is 90 for latitude, 180 for longitude. NaN, ±inf, booleans
    and out-of-range values are refused; the 4-dp truncation is the
    CLAUDE.md GPS rule. Applied to the local API, ``SPACE_ZONE_UPSERTED``
    and the ``space_zones`` sync resource alike.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        coerced = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(coerced) or not -limit <= coerced <= limit:
        raise ValueError(f"{name} out of range")
    # ``+ 0.0`` folds a rounded ``-0.0`` into ``0.0``.
    return truncate_coord(coerced) + 0.0  # type: ignore[operator]


def validate_zone_radius(value: object) -> int:
    """Return the zone radius in whole metres, or raise :class:`ValueError`
    when it is not a finite number between :data:`MIN_ZONE_RADIUS_M` and
    :data:`MAX_ZONE_RADIUS_M`."""
    if isinstance(value, bool):
        raise ValueError("radius_m must be an integer")
    try:
        coerced = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("radius_m must be an integer") from exc
    if not (MIN_ZONE_RADIUS_M <= coerced <= MAX_ZONE_RADIUS_M):
        raise ValueError(
            f"radius_m must be between {MIN_ZONE_RADIUS_M} and {MAX_ZONE_RADIUS_M}",
        )
    return coerced


#: Longest quick-link URL a space admin may store.
MAX_SPACE_LINK_URL_LENGTH: int = 2048

_LINK_SCHEMES: frozenset[str] = frozenset({"http", "https"})


class InvalidSpaceLinkError(CodedError, ValueError):
    """A space quick link was refused (422 ``INVALID_LINK``). Each raise
    site passes a fixed English detail naming the rule, never the
    submitted value; the SPA shows its own translated line."""

    status = 422
    code = "INVALID_LINK"
    detail = "A link needs a name and an http(s) web address."


def validate_space_link_url(url: object) -> str:
    """Return the stripped quick-link URL, or raise
    :class:`InvalidSpaceLinkError`.

    Quick links render as ``<a href>`` for every member, so only an
    absolute ``http(s)://host/…`` URL is stored — never ``javascript:``,
    ``data:``, an app-relative path or embedded credentials. Whitespace and
    control characters anywhere are refused (a browser drops tabs inside a
    scheme, so ``java\tscript:`` must not slip past the scheme check).
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidSpaceLinkError("url must not be empty")
    url = url.strip()
    if len(url) > MAX_SPACE_LINK_URL_LENGTH:
        raise InvalidSpaceLinkError(
            f"url must be at most {MAX_SPACE_LINK_URL_LENGTH} characters"
        )
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise InvalidSpaceLinkError("url must not contain spaces or control characters")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise InvalidSpaceLinkError("url must be an http(s) web address") from None
    if parts.scheme.lower() not in _LINK_SCHEMES or not parts.hostname:
        raise InvalidSpaceLinkError("url must be an http(s) web address")
    if parts.username is not None or parts.password is not None:
        raise InvalidSpaceLinkError("url must not contain a user name or password")
    return url


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
