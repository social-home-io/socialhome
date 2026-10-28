"""Notification domain type (§17.2).

One notification-centre entry per user. Body is redacted for
privacy-sensitive content (DMs, location, UGC) per §25.3.

A row is about one thing (:class:`NotificationSubject`): its
``object_key`` (``<kind>:<id>``, kinds in :class:`NotificationObject`) and
the space it lives in. New activity on that thing for the same user and
type re-opens the row instead of adding one (D7) — :func:`regroup` keeps
the counts and the people in :class:`NotificationParams` — and
:data:`GROUP_QUIET_DAYS` quiet days start a fresh row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .space import SpaceRole, SpaceType

#: Rows that ask someone to act (a "Needs you" item): a join request, a
#: post waiting for review, a report. Unread, they survive both the
#: per-user cap and the age purge — dropping one would lose the only
#: pointer to work still waiting. Once handled (by anyone) they read
#: as read for every recipient and age out like any other row.
ACTION_ITEM_TYPES: frozenset[str] = frozenset(
    {"space_join_requested", "moderation_pending", "space_report"}
)

#: A row re-opens on new activity for this long after its last one; after
#: that many quiet days the next event starts a fresh row (D7), so the
#: page's day headers stay honest.
GROUP_QUIET_DAYS: int = 7

#: People a row remembers (newest first) — enough for "Ben, Anna and 3
#: others" and to tell a returning person from a new one; bounded so a busy
#: post can't grow the row. Past it, :attr:`NotificationParams.actor_count`
#: still counts each newcomer.
MAX_ACTORS: int = 20


class SpaceNotifLevel(StrEnum):
    """How much one person hears from one space (``space_notif_prefs.level``).

    * ``ALL`` ("Everything") — replies and mentions push; every other new
      post and comment is an in-app row, no push.
    * ``MENTIONS`` ("Replies and mentions") — only a comment on your post,
      a reply to your comment, an @-mention of you or an ``@here`` (members
      only); a comment on a post you commented on is an in-app row.
    * ``MUTED`` ("Nothing") — nothing, mentioned or not.
    """

    ALL = "all"
    MENTIONS = "mentions"
    MUTED = "muted"


#: Space types whose members hear "Everything" unless they chose otherwise
#: (D5): the household's own spaces and private ones. Strangers' activity in
#: public and global spaces is the noise, so those default to "Replies and
#: mentions".
_EVERYTHING_BY_DEFAULT: frozenset[SpaceType] = frozenset(
    {SpaceType.HOUSEHOLD, SpaceType.PRIVATE}
)


def default_space_notif_level(
    space_type: SpaceType | str, role: SpaceRole | str
) -> SpaceNotifLevel:
    """The level of someone who never chose one for this space (D5).

    "Everything" in a household or private space; "Replies and mentions" in
    a public or global one, and in any space they only follow (a
    subscriber seat)."""
    if role == SpaceRole.SUBSCRIBER:
        return SpaceNotifLevel.MENTIONS
    try:
        kind = SpaceType(space_type)
    except ValueError:
        return SpaceNotifLevel.MENTIONS
    if kind in _EVERYTHING_BY_DEFAULT:
        return SpaceNotifLevel.ALL
    return SpaceNotifLevel.MENTIONS


class NotificationObject(StrEnum):
    """The kinds of thing a notification is about — the ``<kind>`` of an
    ``object_key``. Two rows share a group only with the same key."""

    #: A household feed post (its comments group on it).
    POST = "post"
    #: A space post (its comments and mentions group on it).
    SPACE_POST = "space_post"
    #: The household feed (:data:`HOUSEHOLD_FEED_ID`) — its new posts group
    #: here, one row for a burst (D5).
    FEED = "feed"
    #: A space's feed (``<space_id>``) — its new posts group here (D5).
    SPACE_FEED = "space_feed"
    #: A space comment — a mention in it (deleting the comment drops it).
    SPACE_COMMENT = "space_comment"
    TASK = "task"
    #: A calendar event (household or space).
    EVENT = "event"
    #: A conversation (1:1, group, household or space chat).
    CONVERSATION = "dm"
    #: A Bazaar listing (its post id).
    LISTING = "listing"
    MOMENT = "moment"
    HIGHLIGHT = "highlight"
    #: A space itself: its review queue, roster, settings, end.
    SPACE = "space"
    #: A household's seat in a space (``<space>/<instance>``).
    SPACE_HOUSEHOLD = "space_household"
    #: One item that waited in a space's review queue.
    MODERATION_ITEM = "moderation"
    #: Your followers (``<user_id>``) — new follows group here.
    FOLLOWERS = "followers"
    #: Someone asking to message you (``<requester user_id>``).
    CONTACT = "contact"
    #: An installed Social Home App (``<app_id>``).
    APP = "app"


#: The id of the household feed's ``feed:<id>`` key — there is one.
HOUSEHOLD_FEED_ID: str = "household"


def object_key(kind: NotificationObject, object_id: str) -> str:
    """``<kind>:<id>`` — what :meth:`NotificationSubject.of` stores."""
    return f"{kind.value}:{object_id}"


@dataclass(slots=True, frozen=True)
class NotificationSubject:
    """What a notification is about: the thing and the space it lives in.

    ``space_name`` is a snapshot for the client to render (the live name
    wins when the client has it)."""

    object_key: str
    space_id: str | None = None
    space_name: str | None = None

    @classmethod
    def of(
        cls,
        kind: NotificationObject,
        object_id: str,
        *,
        space_id: str | None = None,
        space_name: str | None = None,
    ) -> NotificationSubject:
        return cls(object_key(kind, object_id), space_id, space_name)


@dataclass(slots=True, frozen=True)
class NotificationActor:
    """Someone who caused activity on a row (``name`` as held then)."""

    id: str
    name: str | None = None


@dataclass(slots=True, frozen=True)
class NotificationParams:
    """What a row's client renders from (stored as JSON in ``params``).

    * ``actors`` — who acted since the row was last read, newest first,
      distinct, at most :data:`MAX_ACTORS`.
    * ``actor_count`` — how many distinct people that is (it keeps counting
      past the list's bound).
    * ``new`` — events since the row was last read; ``total`` — events
      since the row started.
    * ``space_name`` — the space's name when it was written.
    """

    actors: tuple[NotificationActor, ...] = ()
    actor_count: int = 0
    new: int = 1
    total: int = 1
    space_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "actors": [{"id": a.id, "name": a.name} for a in self.actors],
            "actor_count": self.actor_count,
            "new": self.new,
            "total": self.total,
        }
        if self.space_name is not None:
            out["space_name"] = self.space_name
        return out

    @classmethod
    def from_dict(cls, raw: object) -> NotificationParams | None:
        """The params a stored dict holds; ``None`` for none. Unknown keys
        are ignored and bad values fall back, so an older or newer row
        never breaks a list."""
        if not isinstance(raw, Mapping):
            return None
        actors = tuple(
            NotificationActor(
                id=a["id"],
                name=a["name"] if isinstance(a.get("name"), str) else None,
            )
            for a in raw.get("actors") or ()
            if isinstance(a, Mapping) and isinstance(a.get("id"), str)
        )
        space_name = raw.get("space_name")
        return cls(
            actors=actors,
            actor_count=_count(raw.get("actor_count"), len(actors)),
            new=_count(raw.get("new"), 1),
            total=_count(raw.get("total"), 1),
            space_name=space_name if isinstance(space_name, str) else None,
        )


def _count(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def first_params(
    actor: NotificationActor | None, *, space_name: str | None = None
) -> NotificationParams:
    """A fresh row's params: one event, by *actor* (``None`` for a notice
    nobody caused)."""
    actors = (actor,) if actor is not None else ()
    return NotificationParams(
        actors=actors, actor_count=len(actors), new=1, total=1, space_name=space_name
    )


def regroup(
    old: NotificationParams | None,
    actor: NotificationActor | None,
    *,
    reopen: bool,
    space_name: str | None = None,
) -> NotificationParams:
    """*old*'s params after one more event by *actor*.

    ``reopen`` (the row had been read): only the new event counts as new
    and only its actor is named; ``total`` keeps growing. Otherwise the
    event joins the unread ones: ``new`` grows and *actor* moves to the
    front (counted once). A row from before 0129 (*old* ``None``) counts
    as one earlier event by nobody known."""
    if old is None:
        old = NotificationParams()
    space_name = space_name or old.space_name
    if reopen:
        fresh = first_params(actor, space_name=space_name)
        return NotificationParams(
            actors=fresh.actors,
            actor_count=fresh.actor_count,
            new=1,
            total=old.total + 1,
            space_name=space_name,
        )
    actors = old.actors
    count = old.actor_count
    if actor is not None:
        if not any(a.id == actor.id for a in actors):
            count += 1
        actors = (actor, *(a for a in actors if a.id != actor.id))[:MAX_ACTORS]
    return NotificationParams(
        actors=actors,
        actor_count=count,
        new=old.new + 1,
        total=old.total + 1,
        space_name=space_name,
    )


@dataclass(slots=True, frozen=True)
class Notification:
    """One notification centre entry."""

    id: str
    user_id: str
    type: str  # free-form category tag, e.g. "mention"
    title: str
    created_at: str

    body: str | None = None  # redacted for DM / location / UGC
    link_url: str | None = None
    read_at: str | None = None
    #: What the row is about (``<kind>:<id>``) — ``None`` on a row from
    #: before 0129.
    object_key: str | None = None
    #: The space the thing lives in.
    space_id: str | None = None
    #: Who acted, the counts, the space name — ``None`` before 0129.
    params: NotificationParams | None = None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
