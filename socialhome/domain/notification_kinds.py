"""What a person hears about, and how (notifications redesign, D4 / D10).

People never see notification *types*; Settings → Notifications offers
seven **kinds** (:class:`NotifKind`), each with one choice
(:class:`NotifDelivery`): Push (a row and a push), In app (a row, no push)
or Off (nothing). :func:`notif_kind` files every type under its kind.

Stored in ``users.preferences_json`` (no table):

* ``notif_kinds`` — ``{kind: delivery}``, only the kinds someone changed;
  a missing or invalid entry reads as the kind's default
  (:data:`KIND_DEFAULTS`).
* ``notif_muted_posts`` — ``{object_key: last activity (UTC ISO)}``: posts
  muted from a row's "…" menu (D10). A mute lapses after
  :data:`MUTED_POST_QUIET_DAYS` days without activity on the post and at
  most :data:`MUTED_POSTS_MAX` are kept.

Pure — :class:`NotifSettings` parses an already-decoded blob.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class NotifKind(StrEnum):
    """The seven kinds Settings → Notifications offers (D4)."""

    MESSAGES = "messages"  # Messages and calls
    REPLIES = "replies"  # Replies and mentions
    NEEDS_YOU = "needs_you"
    PLANS = "plans"  # Plans and tasks
    ABOUT_YOU = "about_you"
    REACTIONS = "reactions"  # Reactions and follows
    ACTIVITY = "activity"  # Activity in your spaces


class NotifDelivery(StrEnum):
    """One kind's choice: a row and a push, a row only, or nothing."""

    PUSH = "push"
    IN_APP = "in_app"
    OFF = "off"


_ALL = (NotifDelivery.PUSH, NotifDelivery.IN_APP, NotifDelivery.OFF)

#: The choices each kind offers. Chat messages never make a row (D3), so
#: Messages and calls is Push or Off; Needs you can't be Off — those are
#: requests only you can unblock.
KIND_CHOICES: dict[NotifKind, tuple[NotifDelivery, ...]] = {
    NotifKind.MESSAGES: (NotifDelivery.PUSH, NotifDelivery.OFF),
    NotifKind.REPLIES: _ALL,
    NotifKind.NEEDS_YOU: (NotifDelivery.PUSH, NotifDelivery.IN_APP),
    NotifKind.PLANS: _ALL,
    NotifKind.ABOUT_YOU: _ALL,
    NotifKind.REACTIONS: _ALL,
    NotifKind.ACTIVITY: _ALL,
}

#: What someone who never chose hears: reactions (D13) and activity in
#: their spaces (D5, D6) are nice to see, never worth a buzz.
KIND_DEFAULTS: dict[NotifKind, NotifDelivery] = {
    NotifKind.MESSAGES: NotifDelivery.PUSH,
    NotifKind.REPLIES: NotifDelivery.PUSH,
    NotifKind.NEEDS_YOU: NotifDelivery.PUSH,
    NotifKind.PLANS: NotifDelivery.PUSH,
    NotifKind.ABOUT_YOU: NotifDelivery.PUSH,
    NotifKind.REACTIONS: NotifDelivery.IN_APP,
    NotifKind.ACTIVITY: NotifDelivery.IN_APP,
}

#: Every notification type, by kind. A type the service creates must be
#: here (a test walks the service), so a new type decides where it goes.
_KIND_OF_TYPE: dict[str, NotifKind] = {
    # Messages and calls — chat messages push only (D3).
    "dm_message": NotifKind.MESSAGES,
    "call_incoming": NotifKind.MESSAGES,
    "call_missed": NotifKind.MESSAGES,
    # Replies and mentions — things said *to* you.
    "post_replied": NotifKind.REPLIES,
    "comment_replied": NotifKind.REPLIES,
    "space_mention": NotifKind.REPLIES,
    "space_here": NotifKind.REPLIES,
    "dm_mention": NotifKind.REPLIES,
    "moment_replied": NotifKind.REPLIES,
    "chat_added": NotifKind.REPLIES,
    # Needs you — requests only you can unblock.
    "space_join_requested": NotifKind.NEEDS_YOU,
    "moderation_pending": NotifKind.NEEDS_YOU,
    "space_report": NotifKind.NEEDS_YOU,
    "moderation_unavailable": NotifKind.NEEDS_YOU,
    # Retired: an old row still files under Needs you (it resolves).
    "dm_contact_requested": NotifKind.NEEDS_YOU,
    "app_challenge": NotifKind.NEEDS_YOU,
    # "Ben wants to continue your Chess game" — an old game picked up again.
    "app_challenge_continue": NotifKind.NEEDS_YOU,
    # A member asked for an app ("Ask an admin", D4).
    "app_request": NotifKind.NEEDS_YOU,
    # A newer version of an installed app waits for an admin's tap (D7).
    "app_update": NotifKind.NEEDS_YOU,
    "space_invite": NotifKind.NEEDS_YOU,
    "pairing_request": NotifKind.NEEDS_YOU,
    # Plans and tasks.
    "task_assigned": NotifKind.PLANS,
    "task_deadline": NotifKind.PLANS,
    "task_completed": NotifKind.PLANS,
    "calendar_event_created": NotifKind.PLANS,  # on your own calendar
    "calendar_event_updated": NotifKind.PLANS,
    "calendar_event_cancelled": NotifKind.PLANS,
    "calendar_reminder": NotifKind.PLANS,
    "poll_result": NotifKind.PLANS,
    "poll_scheduled": NotifKind.PLANS,
    # About you — answers and decisions about your requests and posts.
    "space_join_approved": NotifKind.ABOUT_YOU,
    "space_join_denied": NotifKind.ABOUT_YOU,
    "join_answer": NotifKind.ABOUT_YOU,
    "space_remote_invite_accepted": NotifKind.ABOUT_YOU,
    "space_remote_invite_declined": NotifKind.ABOUT_YOU,
    # The person you invited to a game said no (D11).
    "app_challenge_declined": NotifKind.ABOUT_YOU,
    "moderation_decided": NotifKind.ABOUT_YOU,
    "post_moderated": NotifKind.ABOUT_YOU,
    "space_ownership_transferred": NotifKind.ABOUT_YOU,
    "space_dissolved": NotifKind.ABOUT_YOU,
    "space_admins_reconfirm": NotifKind.ABOUT_YOU,
    # A space you're in now offers location sharing — yours to opt into.
    "space_location_enabled": NotifKind.ABOUT_YOU,
    "report_answer": NotifKind.ABOUT_YOU,
    "bazaar_bid_placed": NotifKind.ABOUT_YOU,
    "bazaar_offer_accepted": NotifKind.ABOUT_YOU,
    "bazaar_offer_rejected": NotifKind.ABOUT_YOU,
    "bazaar_listing_sold": NotifKind.ABOUT_YOU,
    "bazaar_listing_expired": NotifKind.ABOUT_YOU,
    # Reactions and follows.
    "post_reacted": NotifKind.REACTIONS,
    "moment_reacted": NotifKind.REACTIONS,
    "highlight_reacted": NotifKind.REACTIONS,
    "user_followed": NotifKind.REACTIONS,
    # Activity in your spaces (and the household feed).
    "post_created": NotifKind.ACTIVITY,
    "comment_added": NotifKind.ACTIVITY,
    "space_post_created": NotifKind.ACTIVITY,
    "space_comment_added": NotifKind.ACTIVITY,
    "space_member_joined": NotifKind.ACTIVITY,
    # A new app in the household (D13: "Anna added Chess"). Household
    # activity like a new feed post — nothing asked of you, nothing about
    # you — so it is In app by default: a row, never a buzz.
    "app_added": NotifKind.ACTIVITY,
}

#: Every type :func:`notif_kind` knows (for the service's walk test).
KNOWN_TYPES: frozenset[str] = frozenset(_KIND_OF_TYPE)


def notif_kind(ntype: str, space_id: str | None = None) -> NotifKind | None:
    """The kind *ntype* falls under; ``None`` for a type nobody filed.

    One type splits on where it happened: an event someone added to *your*
    calendar is a plan; a new event in a space's calendar is activity."""
    if ntype == "calendar_event_created" and space_id:
        return NotifKind.ACTIVITY
    return _KIND_OF_TYPE.get(ntype)


def kind_of(ntype: str, space_id: str | None = None) -> str | None:
    """:func:`notif_kind` as its wire value (``None`` when unfiled)."""
    kind = notif_kind(ntype, space_id)
    return kind.value if kind is not None else None


#: Posts muted at once, at most — the quietest go first past it.
MUTED_POSTS_MAX: int = 200
#: A post mute lapses after this many days without activity on the post.
MUTED_POST_QUIET_DAYS: int = 90
#: Activity on a muted post moves its stamp at most once in this long, so
#: a busy thread costs one preferences write a day, not one per comment.
MUTED_POST_REFRESH: timedelta = timedelta(days=1)

_POST_KEY = re.compile(r"^(post|space_post):[A-Za-z0-9_.\-]{1,128}$")


def mutable_post_key(key: object) -> bool:
    """Whether *key* names a post a person may mute (``post:<id>`` or
    ``space_post:<id>``)."""
    return isinstance(key, str) and _POST_KEY.match(key) is not None


def _stamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


@dataclass(slots=True, frozen=True)
class NotifSettings:
    """One person's kind choices and muted posts, parsed defensively: a
    malformed blob never turns something off or on by accident."""

    chosen: Mapping[NotifKind, NotifDelivery] = field(default_factory=dict)
    muted_posts: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, blob: object) -> NotifSettings:
        if not isinstance(blob, Mapping):
            return cls()
        chosen: dict[NotifKind, NotifDelivery] = {}
        raw_kinds = blob.get("notif_kinds")
        if isinstance(raw_kinds, Mapping):
            for raw_kind, raw_choice in raw_kinds.items():
                try:
                    kind = NotifKind(raw_kind)
                    choice = NotifDelivery(raw_choice)
                except ValueError:
                    continue
                if choice in KIND_CHOICES[kind]:
                    chosen[kind] = choice
        muted: dict[str, str] = {}
        raw_posts = blob.get("notif_muted_posts")
        if isinstance(raw_posts, Mapping):
            for key, stamp in raw_posts.items():
                if mutable_post_key(key) and _stamp(stamp) is not None:
                    muted[key] = stamp
        return cls(chosen=chosen, muted_posts=muted)

    def delivery(self, kind: NotifKind) -> NotifDelivery:
        """The choice for *kind*: theirs, else the default."""
        return self.chosen.get(kind, KIND_DEFAULTS[kind])

    def _active(self, stamp: str, now: datetime) -> bool:
        when = _stamp(stamp)
        return when is not None and now - when < timedelta(days=MUTED_POST_QUIET_DAYS)

    def post_muted(self, key: str | None, *, now: datetime) -> bool:
        """Whether the post *key* is muted right now."""
        stamp = self.muted_posts.get(key) if key else None
        return stamp is not None and self._active(stamp, now)

    def needs_refresh(self, key: str, *, now: datetime) -> bool:
        """Whether activity on muted post *key* should move its stamp."""
        when = _stamp(self.muted_posts.get(key))
        return when is not None and now - when >= MUTED_POST_REFRESH

    def active_muted_posts(self, *, now: datetime) -> dict[str, str]:
        """The muted posts that have not lapsed."""
        return {k: v for k, v in self.muted_posts.items() if self._active(v, now)}

    def with_muted_post(self, key: str, *, now: datetime) -> dict[str, str]:
        """The muted posts with *key* added (stamped *now*): lapsed ones
        dropped, and past :data:`MUTED_POSTS_MAX` the quietest go."""
        posts = self.active_muted_posts(now=now)
        posts[key] = now.isoformat()
        if len(posts) > MUTED_POSTS_MAX:
            keep = sorted(
                posts.items(),
                key=lambda kv: _stamp(kv[1]) or now,
                reverse=True,
            )[:MUTED_POSTS_MAX]
            posts = dict(keep)
        return posts


@dataclass(slots=True, frozen=True)
class SpaceNotifPref:
    """One person's stored choice for one space (``space_notif_prefs``).

    ``level`` is what they picked; with ``muted_until`` set (0130) the space
    reads as "Nothing" until then and ``level`` — the level they had before
    — applies again afterwards (D9)."""

    level: str
    muted_until: str | None = None

    def muted_now(self, now: datetime) -> bool:
        """Whether a timed "Nothing" is still running at *now*."""
        until = _stamp(self.muted_until)
        return until is not None and until > now

    def effective(self, now: datetime) -> str:
        """The level that applies at *now*: ``muted`` during a timed mute,
        else the stored one."""
        return "muted" if self.muted_now(now) else self.level


@dataclass(slots=True, frozen=True)
class NotifChange:
    """One line of "Muted and changed" (D4): a space, a chat or a post the
    person set differently from its default, or muted.

    * ``target`` — ``space`` / ``chat`` / ``post``; ``id`` — the space id,
      conversation id or post ``object_key``.
    * ``level`` — what applies now (``all`` / ``mentions`` / ``muted``);
      ``default_level`` — what applies without a choice.
    * ``muted_until`` — when a mute ends (UTC ISO; the far-future
      ``MUTED_FOREVER`` sentinel for "until I turn it back on"), ``None``
      when not muted; ``then_level`` — the level that comes back after it.
    * ``name`` — the space or chat name (``None``: the client names it, e.g.
      the household chat, a 1:1 by its person); ``place`` — the kind of
      place (``household`` / ``private`` / ``public`` / ``global`` for a
      space; ``dm`` / ``group`` / ``household`` / ``space`` for a chat;
      ``post`` / ``space_post`` for a post).
    * ``space_id`` / ``space_name`` — where a space chat or a space post
      lives; ``snippet`` — a post's first words (the person's own app, never
      a push).
    """

    target: str
    id: str
    level: str
    default_level: str
    place: str
    name: str | None = None
    muted_until: str | None = None
    then_level: str | None = None
    space_id: str | None = None
    space_name: str | None = None
    snippet: str | None = None
