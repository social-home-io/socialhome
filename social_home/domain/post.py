"""Post-related domain types (§5.2).

Covers the universe of things that can show up in a household feed or a space
feed: text / image / video / transcript posts, polls, schedule polls, file
attachments and bazaar (marketplace) listings.

All types are immutable. Mutations return new dataclass instances.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from enum import StrEnum


# ─── Core enums ───────────────────────────────────────────────────────────


class PostType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    TRANSCRIPT = "transcript"
    POLL = "poll"
    SCHEDULE = "schedule"
    FILE = "file"
    BAZAAR = "bazaar"


class CommentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"


class Availability(StrEnum):
    YES = "yes"
    NO = "no"
    MAYBE = "maybe"


class BazaarMode(StrEnum):
    FIXED = "fixed"
    OFFER = "offer"
    BID_FROM = "bid_from"
    NEGOTIABLE = "negotiable"
    AUCTION = "auction"


class BazaarStatus(StrEnum):
    ACTIVE = "active"
    SOLD = "sold"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# ─── Reactions ────────────────────────────────────────────────────────────

#: Mapping of reaction emoji → frozenset of user_ids that reacted with it.
type Reactions = dict[str, frozenset[str]]

#: Max distinct reaction emoji per post. Prevents storage abuse (§5.2).
MAX_DISTINCT_REACTIONS_PER_POST: int = 20


# ─── File attachments ─────────────────────────────────────────────────────

#: Max file-attachment size for a PostType.FILE.
FILE_MAX_BYTES: int = 10 * 1024 * 1024

#: Allowed MIME types for file attachments. Anything else is rejected.
ALLOWED_FILE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "text/plain",
        "text/csv",
    }
)

#: Map MIME type → filename extension used when serving downloads.
FILE_EXTENSION_MAP: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "text/plain": ".txt",
    "text/csv": ".csv",
}


@dataclass(slots=True, frozen=True)
class FileMeta:
    """Metadata for a ``PostType.FILE`` attachment."""

    url: str
    mime_type: str
    original_name: str
    size_bytes: int


# ─── Bazaar (marketplace) ─────────────────────────────────────────────────

BAZAAR_MAX_IMAGES: int = 5
BAZAAR_MAX_DURATION_DAYS: int = 7

#: ISO 4217 currencies → number of fractional digits.
BAZAAR_CURRENCIES: dict[str, int] = {
    "AUD": 2,
    "BGN": 2,
    "BRL": 2,
    "CAD": 2,
    "CHF": 2,
    "CNY": 2,
    "CZK": 2,
    "DKK": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "HUF": 2,
    "IDR": 2,
    "ILS": 2,
    "INR": 2,
    "ISK": 0,
    "JPY": 0,
    "KRW": 0,
    "MXN": 2,
    "MYR": 2,
    "NOK": 2,
    "NZD": 2,
    "PHP": 2,
    "PLN": 2,
    "RON": 2,
    "SEK": 2,
    "SGD": 2,
    "THB": 2,
    "TRY": 2,
    "USD": 2,
    "ZAR": 2,
}


@dataclass(slots=True, frozen=True)
class BazaarListing:
    """The listing payload embedded in a ``PostType.BAZAAR`` post.

    Monetary amounts are integers in the currency's smallest unit (cents for
    EUR / USD, yen for JPY, etc.) — never floats.
    """

    post_id: str
    seller_user_id: str
    mode: BazaarMode
    title: str
    end_time: str
    currency: str
    status: BazaarStatus
    created_at: str

    description: str | None = None
    image_urls: tuple[str, ...] = ()
    price: int | None = None
    start_price: int | None = None
    step_price: int | None = None
    winner_user_id: str | None = None
    winning_price: int | None = None
    sold_at: str | None = None


@dataclass(slots=True, frozen=True)
class BazaarBid:
    """One bid (AUCTION) or offer (OFFER) placed on a :class:`BazaarListing`.

    OFFER-mode state machine::

        pending → accepted  (seller accepted this offer)
                → rejected  (seller rejected, or another offer was accepted)
                → withdrawn (bidder withdrew before the seller acted)

    For AUCTION mode the accepted / rejected / rejection_reason fields are
    unused; the winner is the highest bid at ``end_time``.
    """

    id: str
    listing_post_id: str
    bidder_user_id: str
    amount: int
    created_at: str

    message: str | None = None
    accepted: bool = False
    rejected: bool = False
    rejection_reason: str | None = None
    withdrawn: bool = False


# ─── Polls ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PollOption:
    id: str
    text: str
    vote_count: int = 0


@dataclass(slots=True, frozen=True)
class PollVote:
    option_id: str
    voter_user_id: str
    voted_at: datetime


@dataclass(slots=True, frozen=True)
class Poll:
    """A one-shot reply poll embedded in a post.

    State is expressed as an immutable snapshot. Mutations return a new
    :class:`Poll`.
    """

    id: str
    question: str
    options: tuple[PollOption, ...]
    votes: tuple[PollVote, ...] = ()
    closes_at: datetime | None = None
    closed: bool = False

    def cast_vote(self, voter_user_id: str, option_id: str) -> "Poll":
        """Return a new :class:`Poll` with this voter's choice replaced."""
        if self.closed:
            raise ValueError("Cannot cast a vote on a closed poll")
        if not any(o.id == option_id for o in self.options):
            raise ValueError(f"Unknown poll option {option_id!r}")
        filtered = tuple(v for v in self.votes if v.voter_user_id != voter_user_id)
        new_vote = PollVote(
            option_id=option_id,
            voter_user_id=voter_user_id,
            voted_at=datetime.now(timezone.utc),
        )
        return copy.replace(self, votes=(*filtered, new_vote))

    def retract_vote(self, voter_user_id: str) -> "Poll":
        return copy.replace(
            self,
            votes=tuple(v for v in self.votes if v.voter_user_id != voter_user_id),
        )

    def close(self) -> "Poll":
        return copy.replace(self, closed=True)

    def vote_count(self, option_id: str) -> int:
        return sum(1 for v in self.votes if v.option_id == option_id)


@dataclass(slots=True, frozen=True)
class PollData:
    """Wire / storage format for a poll embedded in a post."""

    question: str
    options: tuple[PollOption, ...]
    allow_multiple: bool = False
    closed: bool = False
    closes_at: datetime | None = None
    total_votes: int = 0
    user_vote: tuple[int, ...] | None = None


# ─── Schedule polls (Doodle-style) ────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class ScheduleSlot:
    id: str
    slot_date: date
    position: int
    start_time: time | None = None
    end_time: time | None = None

    def label(self) -> str:
        """Human-readable label, e.g. ``'Sat 12 Apr · 19:00–21:00'``."""
        d = self.slot_date.strftime("%a %d %b").replace(" 0", " ")
        if self.start_time is None:
            return d
        t = self.start_time.strftime("%H:%M")
        if self.end_time is None:
            return f"{d} · {t}"
        return f"{d} · {t}–{self.end_time.strftime('%H:%M')}"


@dataclass(slots=True, frozen=True)
class ScheduleSlotCreate:
    """Input schema for one slot when creating a schedule poll."""

    slot_date: date
    start_time: time | None = None
    end_time: time | None = None


@dataclass(slots=True, frozen=True)
class ScheduleResponse:
    slot_id: str
    user_id: str
    availability: Availability
    responded_at: datetime


@dataclass(slots=True, frozen=True)
class SchedulePoll:
    """A Doodle-style date/time availability poll."""

    id: str
    title: str
    slots: tuple[ScheduleSlot, ...]
    responses: tuple[ScheduleResponse, ...] = ()
    deadline: datetime | None = None
    finalized_slot_id: str | None = None
    closed: bool = False

    def with_response(
        self, user_id: str, slot_id: str, availability: Availability
    ) -> "SchedulePoll":
        """Return a new poll with this user's availability for ``slot_id`` set."""
        if self.closed:
            raise ValueError("Cannot respond to a finalized or closed schedule poll")
        if not any(s.id == slot_id for s in self.slots):
            raise ValueError(f"Slot {slot_id!r} not found in this schedule poll")
        filtered = tuple(
            r
            for r in self.responses
            if not (r.user_id == user_id and r.slot_id == slot_id)
        )
        new_response = ScheduleResponse(
            slot_id=slot_id,
            user_id=user_id,
            availability=availability,
            responded_at=datetime.now(timezone.utc),
        )
        return copy.replace(self, responses=(*filtered, new_response))

    def retract_response(self, user_id: str, slot_id: str) -> "SchedulePoll":
        return copy.replace(
            self,
            responses=tuple(
                r
                for r in self.responses
                if not (r.user_id == user_id and r.slot_id == slot_id)
            ),
        )

    def finalize(self, slot_id: str) -> "SchedulePoll":
        if not any(s.id == slot_id for s in self.slots):
            raise ValueError(f"Slot {slot_id!r} not found in this schedule poll")
        return copy.replace(self, finalized_slot_id=slot_id, closed=True)

    def response_summary(self) -> dict[str, dict[Availability, int]]:
        """``{slot_id: {yes: n, no: m, maybe: k}}`` for each slot with replies."""
        summary: dict[str, dict[Availability, int]] = {}
        for r in self.responses:
            counts = summary.setdefault(r.slot_id, {a: 0 for a in Availability})
            counts[r.availability] += 1
        return summary

    def responses_for_user(self, user_id: str) -> dict[str, Availability]:
        return {
            r.slot_id: r.availability for r in self.responses if r.user_id == user_id
        }


# ─── Post + Comment ───────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Post:
    """A post — in the household feed or in a space feed.

    Mutations return new :class:`Post` instances so this can be shared freely.
    """

    id: str
    author: str  # user_id
    type: PostType
    created_at: datetime

    content: str | None = None
    media_url: str | None = None
    reactions: Reactions = field(default_factory=dict)
    comment_count: int = 0
    poll: Poll | None = None
    schedule: SchedulePoll | None = None
    pinned: bool = False
    deleted: bool = False
    edited_at: datetime | None = None
    no_link_preview: bool = False
    moderated: bool = False
    file_meta: FileMeta | None = None

    def with_reaction(self, emoji: str, user_id: str) -> "Post":
        current = self.reactions.get(emoji, frozenset())
        return copy.replace(
            self,
            reactions={**self.reactions, emoji: current | {user_id}},
        )

    def without_reaction(self, emoji: str, user_id: str) -> "Post":
        current = self.reactions.get(emoji, frozenset()) - {user_id}
        if not current:
            return copy.replace(
                self,
                reactions={k: v for k, v in self.reactions.items() if k != emoji},
            )
        return copy.replace(
            self,
            reactions={**self.reactions, emoji: current},
        )

    def increment_comments(self) -> "Post":
        return copy.replace(self, comment_count=self.comment_count + 1)

    def decrement_comments(self) -> "Post":
        return copy.replace(self, comment_count=max(0, self.comment_count - 1))

    def soft_delete(self) -> "Post":
        """Retain the post node (reactions, comments, pin) but clear content."""
        return copy.replace(self, content=None, media_url=None, deleted=True)

    def edit(self, new_content: str, now: datetime | None = None) -> "Post":
        return copy.replace(
            self,
            content=new_content,
            edited_at=now or datetime.now(timezone.utc),
        )


#: Household-scope post. Structurally identical to a space post.
FeedPost = Post
#: Space-scope post. Structurally identical to a household post.
SpacePost = Post


@dataclass(slots=True, frozen=True)
class Comment:
    """A comment on a :class:`Post`. Threaded via ``parent_id`` pointers."""

    id: str
    post_id: str
    author: str  # user_id
    type: CommentType
    created_at: datetime

    parent_id: str | None = None
    content: str | None = None
    media_url: str | None = None
    deleted: bool = False
    edited_at: datetime | None = None
    children: tuple["Comment", ...] = ()

    def soft_delete(self) -> "Comment":
        """Clear content but keep the node in the tree (children remain)."""
        return copy.replace(self, content=None, media_url=None, deleted=True)
