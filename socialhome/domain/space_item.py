"""Member-published space item types (v_49 ``space_item``).

A member household publishes its own items into a space's opaque channel as
the generic ``space_item`` (:mod:`socialhome.domain.gfs_channel`); the
real item type travels only inside the ciphertext, bound in the author
signature. This module names those types, the writer-cert scope each one
needs at the receivers (the GFS can see none of them, so it checks only
``comment``), and the signed time stamp edits and reactions are ordered by.

Two author-signed inner shapes carry them:

* **post-shaped** (:data:`POST_SHAPED_ITEM_TYPES`) — the
  ``space_public_author`` inner the host relay already carries: a ``post``,
  and a ``post_edit``, which is the author's full signed snapshot of the
  edited post plus its signed ``edited_at``;
* **generic** (:data:`GENERIC_ITEM_TYPES`) — the ``space_item_author`` inner
  (its own signature domain and suite) for comments, the post delete and
  reactions.

**Authority kinds** ride the channel as authority items instead
(:data:`SUPPORTED_AUTHORITY_KINDS`): a space-authority-signed removal of a
post or comment (:class:`AuthorityRemoval`) — no author signature, the
authority signature over the envelope its only authorizer — and the
``approved_post`` mark a seed holder sets on an author-signed post it
released from the moderation queue. A member can never send either: a
``space_item`` names only the member types above, an unsigned inner is
only ever a removal, and a seed holder refuses to relay a member's inner
that names an authority kind.

Both relays pad their plaintext to :data:`ITEM_SIZE_BUCKETS`
(:func:`pad_json_object`).

Pure module — no I/O.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeGuard

import orjson

from .errors import CodedError
from .federation import FederationEventType as _E
from .gfs_channel import RING_CLASS_DURABLE, RING_CLASS_EPHEMERAL
from .space import SpaceFeatures
from .user import looks_like_emoji
from .writer_cert import WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE, WriterCert

ITEM_TYPE_POST: str = "post"
#: The author's own edit of their post (full signed snapshot + ``edited_at``).
ITEM_TYPE_POST_EDIT: str = "post_edit"
#: The author's own delete of their post.
ITEM_TYPE_POST_DELETE: str = "post_delete"
ITEM_TYPE_COMMENT: str = "comment"
#: The author's own edit of their comment (full signed snapshot).
ITEM_TYPE_COMMENT_EDIT: str = "comment_edit"
#: The author's own delete of their comment.
ITEM_TYPE_COMMENT_DELETE: str = "comment_delete"
ITEM_TYPE_REACTION_ADD: str = "reaction_add"
ITEM_TYPE_REACTION_REMOVE: str = "reaction_remove"
#: v_60 — the author's own public profile for followers (name, bio, a small
#: picture), so a GFS subscriber with no roster can name them
#: (:mod:`socialhome.domain.space_author_profile`).
ITEM_TYPE_AUTHOR_PROFILE: str = "author_profile"

#: Carried in the post-shaped (``space_public_author``) inner.
POST_SHAPED_ITEM_TYPES: frozenset[str] = frozenset(
    {ITEM_TYPE_POST, ITEM_TYPE_POST_EDIT}
)
#: Carried in the generic (``space_item_author``) inner.
GENERIC_ITEM_TYPES: frozenset[str] = frozenset(
    {
        ITEM_TYPE_POST_DELETE,
        ITEM_TYPE_COMMENT,
        ITEM_TYPE_COMMENT_EDIT,
        ITEM_TYPE_COMMENT_DELETE,
        ITEM_TYPE_REACTION_ADD,
        ITEM_TYPE_REACTION_REMOVE,
        ITEM_TYPE_AUTHOR_PROFILE,
    }
)
SUPPORTED_ITEM_TYPES: frozenset[str] = POST_SHAPED_ITEM_TYPES | GENERIC_ITEM_TYPES

#: The writer-cert scope each type needs at the receivers. Post edits and
#: deletes need ``write`` — the same right as the post itself — so a
#: comment-only household (a follower, or a plain member under a
#: ``MODERATED`` / ``ADMIN_ONLY`` posts level) edits or deletes its post on
#: the host path, never over the relay. Reactions need ``comment`` plus the
#: per-receiver rules in ``space_item_inbound`` (followers ask for more).
_REQUIRED_SCOPE: dict[str, str] = {
    ITEM_TYPE_POST: WRITER_SCOPE_WRITE,
    ITEM_TYPE_POST_EDIT: WRITER_SCOPE_WRITE,
    ITEM_TYPE_POST_DELETE: WRITER_SCOPE_WRITE,
    ITEM_TYPE_COMMENT: WRITER_SCOPE_COMMENT,
    ITEM_TYPE_COMMENT_EDIT: WRITER_SCOPE_COMMENT,
    ITEM_TYPE_COMMENT_DELETE: WRITER_SCOPE_COMMENT,
    ITEM_TYPE_REACTION_ADD: WRITER_SCOPE_COMMENT,
    ITEM_TYPE_REACTION_REMOVE: WRITER_SCOPE_COMMENT,
    # Any member household's own users — the cert's v2 binding names them.
    ITEM_TYPE_AUTHOR_PROFILE: WRITER_SCOPE_COMMENT,
}

#: How far in the future a signed item stamp may lie (seconds). An edit
#: stamped further ahead would win every later edit (last writer wins), so
#: it is refused.
MAX_ITEM_CLOCK_SKEW_S: int = 300

#: Upper bound on a relayed reaction's emoji (characters).
MAX_REACTION_EMOJI_CHARS: int = 32

#: How many reactions one person holds on one space post. Enforced where
#: the reaction is made (a sixth is refused) and wherever one is received
#: (the newest by stamp are kept — the same ones on every household, in any
#: arrival order). It replaces a post-wide cap on distinct emoji, which let
#: one member fill every slot and left each household with whichever
#: arrived first.
MAX_REACTIONS_PER_USER_PER_POST: int = 5


def required_scope(item_type: str) -> str:
    """The scope a cert must grant for ``item_type`` — a v1 item type or
    (v_97) an event-shaped one of :data:`ITEM_SPECS`; :class:`ValueError`
    for a type this build does not carry."""
    spec = ITEM_SPECS.get(item_type)
    if spec is not None:
        return spec.scope
    try:
        return _REQUIRED_SCOPE[item_type]
    except KeyError:
        raise ValueError(f"unsupported space item type {item_type!r}") from None


# ── v_97 event-shaped items: the registry ────────────────────────────────

#: How a re-delivered or out-of-order copy of an item stays harmless at the
#: receiver (the per-type stamp audit, ``docs/protocol/space-items.md``):
#: * ``id`` — an owner-bound / unique id, inserted once (a copy is a no-op);
#: * ``lww`` — a signed stamp or version, the newest wins (``item_merge``);
#: * ``tombstone`` — a removal leaves a tombstone, a late copy never revives;
#: * ``advance`` — a state that only moves forward (a bid, a poll close);
#: * ``latest`` — only the newest value counts (a location pin);
#: * ``arrival`` — applied in arrival order (RSVP / schedule answer live,
#:   corrected by the 30-minute incremental sync — the documented residual
#:   of ``sync.md`` "Not on the rule yet");
#: * ``state`` — a host statement whose repeat converges (roster, config).
IDEMPOTENCY_CLASSES: frozenset[str] = frozenset(
    {"id", "lww", "tombstone", "advance", "latest", "arrival", "state"}
)

#: Sender priority classes, highest first (``space-items.md`` › budgets).
BUDGET_AUTHORITY: str = "authority"
BUDGET_CONTENT: str = "content"
BUDGET_CHAT: str = "chat"
BUDGET_LOCATION: str = "location"
BUDGET_CLASSES: tuple[str, ...] = (
    BUDGET_AUTHORITY,
    BUDGET_CONTENT,
    BUDGET_CHAT,
    BUDGET_LOCATION,
)


@dataclass(slots=True, frozen=True)
class ItemSpec:
    """One v_97 event-shaped space item type (:data:`ITEM_SPECS`).

    ``event_type`` is the federation event the item replaces — its value
    is the item's ``item_type`` and its payload the item's ``body``, so the
    receiver hands it to that event's own handler. ``scope`` is the writer
    cert scope a person item needs; ``reader_as`` the v1 type a follower's
    ``reader`` cert is judged as (:func:`reader_cert_permits`; ``None`` —
    a reader cert never carries it). ``authority`` marks a host statement
    (an authority ``space_event`` inner, never author-signed)."""

    event_type: _E
    scope: str = WRITER_SCOPE_WRITE
    ring_class: str = RING_CLASS_DURABLE
    authority: bool = False
    idempotency: str = "id"
    budget: str = BUDGET_CONTENT
    reader_as: str | None = None


def _person(
    event_type: _E,
    idempotency: str,
    *,
    scope: str = WRITER_SCOPE_WRITE,
    budget: str = BUDGET_CONTENT,
    ring_class: str = RING_CLASS_DURABLE,
    reader_as: str | None = None,
) -> ItemSpec:
    return ItemSpec(
        event_type=event_type,
        scope=scope,
        ring_class=ring_class,
        idempotency=idempotency,
        budget=budget,
        reader_as=reader_as,
    )


def _authority(event_type: _E, idempotency: str = "state") -> ItemSpec:
    return ItemSpec(
        event_type=event_type,
        authority=True,
        idempotency=idempotency,
        budget=BUDGET_AUTHORITY,
    )


_C = WRITER_SCOPE_COMMENT

#: Every v_97 space item type, by ``item_type`` (the event type's value).
#: Person actions broadcast to a whole space, and the host statements of
#: ``space-items.md``. Targeted secrets (rekeys, key shares, grants, roster
#: snapshots, invites and joins, moderation submissions, reports, DMs,
#: bazaar threads and offers, app sessions, sync, media blobs) are never
#: items. The v1 shapes above stay on the channel for the types they carry
#: until v_98.
ITEM_SPECS: dict[str, ItemSpec] = {
    spec.event_type.value: spec
    for spec in (
        # ── Feed ──
        _person(_E.SPACE_POST_CREATED, "id"),
        _person(_E.SPACE_POST_UPDATED, "lww"),
        _person(_E.SPACE_POST_DELETED, "tombstone"),
        _person(
            _E.SPACE_POST_REACTION,
            "lww",
            scope=_C,
            reader_as=ITEM_TYPE_REACTION_ADD,
        ),
        _person(_E.SPACE_COMMENT_CREATED, "id", scope=_C, reader_as=ITEM_TYPE_COMMENT),
        _person(
            _E.SPACE_COMMENT_UPDATED,
            "lww",
            scope=_C,
            reader_as=ITEM_TYPE_COMMENT_EDIT,
        ),
        _person(
            _E.SPACE_COMMENT_DELETED,
            "tombstone",
            scope=_C,
            reader_as=ITEM_TYPE_COMMENT_DELETE,
        ),
        # ── Pages, tasks, polls, stickies ──
        _person(_E.SPACE_PAGE_CREATED, "id"),
        _person(_E.SPACE_PAGE_UPDATED, "lww"),
        _person(_E.SPACE_PAGE_DELETED, "tombstone"),
        _person(_E.SPACE_TASK_CREATED, "id"),
        _person(_E.SPACE_TASK_UPDATED, "lww"),
        _person(_E.SPACE_TASK_DELETED, "tombstone"),
        _person(_E.SPACE_TASK_LIST_CREATED, "id"),
        _person(_E.SPACE_TASK_LIST_UPDATED, "lww"),
        _person(_E.SPACE_TASK_LIST_DELETED, "tombstone"),
        _person(_E.SPACE_POLL_CREATED, "id"),
        _person(_E.SPACE_POLL_VOTE_CAST, "lww"),
        _person(_E.SPACE_POLL_CLOSED, "advance"),
        _person(_E.SPACE_STICKY_CREATED, "id"),
        _person(_E.SPACE_STICKY_UPDATED, "lww"),
        _person(_E.SPACE_STICKY_DELETED, "tombstone"),
        # ── Calendar, schedules ──
        _person(_E.SPACE_CALENDAR_EVENT_CREATED, "id"),
        _person(_E.SPACE_CALENDAR_EVENT_UPDATED, "lww"),
        _person(_E.SPACE_CALENDAR_EVENT_DELETED, "tombstone"),
        _person(_E.SPACE_RSVP_UPDATED, "arrival"),
        _person(_E.SPACE_RSVP_DELETED, "arrival"),
        _person(_E.SPACE_SCHEDULE_CREATED, "id"),
        _person(_E.SPACE_SCHEDULE_RESPONSE_UPDATED, "arrival"),
        _person(_E.SPACE_SCHEDULE_FINALIZED, "advance"),
        # ── Gallery ──
        _person(_E.SPACE_GALLERY_ALBUM_CREATED, "id"),
        _person(_E.SPACE_GALLERY_ALBUM_UPDATED, "lww"),
        _person(_E.SPACE_GALLERY_ALBUM_DELETED, "tombstone"),
        _person(_E.SPACE_GALLERY_ITEM_CREATED, "id"),
        _person(_E.SPACE_GALLERY_ITEM_UPDATED, "lww"),
        _person(_E.SPACE_GALLERY_ITEM_DELETED, "tombstone"),
        # ── Bazaar (listings and bids; threads and offers stay targeted) ──
        _person(_E.BAZAAR_LISTING_CREATED, "id"),
        _person(_E.BAZAAR_LISTING_UPDATED, "lww"),
        _person(_E.BAZAAR_LISTING_FAQ_UPDATED, "lww"),
        _person(_E.BAZAAR_BID_PLACED, "advance"),
        _person(_E.BAZAAR_OFFER_ACCEPTED, "advance"),
        # ── Apps, zones, timetables ──
        _person(_E.SPACE_APP_TABLE, "lww"),
        _person(_E.SPACE_ZONE_UPSERTED, "lww"),
        _person(_E.SPACE_ZONE_DELETED, "tombstone"),
        _person(_E.SPACE_TIMETABLE_UPSERTED, "lww"),
        _person(_E.SPACE_TIMETABLE_DELETED, "tombstone"),
        # ── Chat and location ──
        _person(_E.SPACE_CHAT_MESSAGE_CREATED, "id", budget=BUDGET_CHAT),
        _person(_E.SPACE_CHAT_MESSAGE_UPDATED, "lww", budget=BUDGET_CHAT),
        _person(_E.SPACE_CHAT_MESSAGE_DELETED, "tombstone", budget=BUDGET_CHAT),
        _person(_E.SPACE_CHAT_REACTION, "lww", budget=BUDGET_CHAT),
        _person(
            _E.SPACE_LOCATION_UPDATED,
            "latest",
            budget=BUDGET_LOCATION,
            ring_class=RING_CLASS_EPHEMERAL,
        ),
        # ── Host statements (authority ``space_event`` inners) ──
        _authority(_E.SPACE_MEMBER_JOINED),
        _authority(_E.SPACE_MEMBER_LEFT),
        _authority(_E.SPACE_MEMBER_BANNED),
        _authority(_E.SPACE_MEMBER_UNBANNED),
        _authority(_E.SPACE_MEMBER_ROLE_CHANGED),
        _authority(_E.SPACE_CONFIG_CHANGED, "lww"),
        _authority(_E.SPACE_AGE_GATE_UPDATED, "lww"),
        _authority(_E.SPACE_DISSOLVED, "advance"),
        _authority(_E.SPACE_MODERATION_DECIDED, "advance"),
        _authority(_E.SPACE_ADMIN_PROPOSAL_UPDATED, "advance"),
    )
}

#: The person-action item types (author-signed v2 inners).
PERSON_ITEM_TYPES: frozenset[str] = frozenset(
    t for t, spec in ITEM_SPECS.items() if not spec.authority
)
#: The host-statement item types (authority ``space_event`` inners).
AUTHORITY_ITEM_TYPES: frozenset[str] = frozenset(
    t for t, spec in ITEM_SPECS.items() if spec.authority
)


def item_spec(item_type: object) -> ItemSpec | None:
    """The :class:`ItemSpec` of a v_97 event-shaped ``item_type``, or
    ``None`` (a v1 type, an unknown one, or not a string)."""
    if not isinstance(item_type, str):
        return None
    return ITEM_SPECS.get(item_type)


#: v_96 — what a follower's ``reader`` cert may carry, by the follower switch
#: that must be on in the RECEIVER's pinned space config.
_READER_COMMENT_TYPES: frozenset[str] = frozenset(
    {ITEM_TYPE_COMMENT, ITEM_TYPE_COMMENT_EDIT}
)
_READER_REACTION_TYPES: frozenset[str] = frozenset(
    {ITEM_TYPE_REACTION_ADD, ITEM_TYPE_REACTION_REMOVE}
)


def reader_cert_permits(
    cert: WriterCert, item_type: str, features: SpaceFeatures
) -> bool:
    """Whether ``cert`` may authorize an ``item_type`` item under the
    receiver's pinned ``features`` (v_96), on top of its scope.

    A non-reader cert → ``True`` (its scope decides). A follower's
    ``reader`` cert carries comments (and their edits) only while
    ``allow_subscriber_comment`` is on, reactions only while
    ``allow_subscriber_react`` is on, and the author's delete of their own
    comment always (owner decision F6, 2026-11-23: the receiver decides
    that one on authorship — the stored comment's author — not on a
    switch); anything else — posts and their edits / deletes, the v_60
    ``author_profile`` (a follower is named by the roster and never
    publishes one), any type this build does not know — is refused. Fail
    closed: the flags are read from the receiver's own copy, never from the
    item."""
    if not cert.reader:
        return True
    spec = item_spec(item_type)
    if spec is not None:
        # v_97 — an event-shaped item is judged as the v1 type it maps to;
        # a type with none is never a follower's to send.
        if spec.reader_as is None:
            return False
        item_type = spec.reader_as
    if item_type == ITEM_TYPE_COMMENT_DELETE:
        return True
    if item_type in _READER_COMMENT_TYPES:
        return bool(features.allow_subscriber_comment)
    if item_type in _READER_REACTION_TYPES:
        return bool(features.allow_subscriber_react)
    return False


def item_stamp(raw: object, *, now: datetime | None = None) -> datetime | None:
    """Parse a signed item stamp (``ts`` / ``edited_at``): a tz-aware ISO
    8601 string no more than :data:`MAX_ITEM_CLOCK_SKEW_S` ahead of ``now``.
    ``None`` for anything else."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    limit = (now or datetime.now(UTC)) + timedelta(seconds=MAX_ITEM_CLOCK_SKEW_S)
    if parsed > limit:
        return None
    return parsed


def stored_stamp(raw: object, *, now: datetime | None = None) -> datetime | None:
    """A stamp a §25.6 record copies from the host's rows — the live
    :func:`item_stamp` rule (no more than :data:`MAX_ITEM_CLOCK_SKEW_S`
    ahead of ``now``), except that a naive stamp is UTC, the shape every
    stored one has (:func:`stamp_to_db`, SQLite ``datetime('now')``)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return item_stamp(parsed.isoformat(), now=now)


def reaction_emoji_ok(emoji: object, *, legacy: bool = False) -> TypeGuard[str]:
    """The reaction emoji rule: a non-empty string of at most
    :data:`MAX_REACTION_EMOJI_CHARS` characters, already NFC and trimmed —
    and an emoji, not free text, by the sanity check a status emoji gets
    (:func:`~socialhome.domain.user.looks_like_emoji`).

    ``legacy`` leaves the emoji check out: before v_93 a reaction could be
    any short text, so a REMOVAL (and a catch-up copy of an old reaction)
    is held to the length rule only — an old one can always be taken back.
    """
    return (
        isinstance(emoji, str)
        and bool(emoji)
        and len(emoji) <= MAX_REACTION_EMOJI_CHARS
        and unicodedata.normalize("NFC", emoji.strip()) == emoji
        and (legacy or looks_like_emoji(emoji))
    )


class TooManyReactionsError(CodedError, ValueError):
    """A person's reaction past :data:`MAX_REACTIONS_PER_USER_PER_POST` on
    one space post, refused where it is made."""

    status = 422
    code = "REACTION_LIMIT"
    detail = "Too many reactions: you can leave up to five on a post."


def stamp_to_db(stamp: datetime) -> str:
    """A stamp in the naive-UTC shape of SQLite's ``datetime('now')``, with
    microseconds — so the ``edited_at`` column keeps one shape and compares
    as a string against rows the local edit paths stamped. A naive stamp is
    UTC (every stored one is): a row read back and saved again keeps its
    time. Every writer of a space post's or comment's ``edited_at`` writes
    this shape: through here, or SQLite's own ``datetime('now')`` (a local
    edit, whole seconds)."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (
        stamp.astimezone(UTC)
        .replace(tzinfo=None)
        .isoformat(sep=" ", timespec="microseconds")
    )


class StaleItemStamp(Exception):
    """A stamped write is not newer than the one already applied for the
    same row (or reaction) — a late duplicate, never applied."""


# ── Size padding ─────────────────────────────────────────────────────────

#: Plaintext sizes a relayed item is padded up to before encryption, so the
#: ciphertext length tells a connection server (or anyone on the wire) only
#: the bucket — a reaction, a comment, a removal notice and a short post all
#: look alike. The largest stays well under the GFS payload cap once
#: encrypted and base64'd.
#: v_97 adds the 2 KiB bucket (a chat message or a pin padded to 4 KiB
#: doubled the GFS egress of the busiest items; owner sign-off 4).
ITEM_SIZE_BUCKETS: tuple[int, ...] = (1024, 2048, 4096, 16384, 65536, 131072)

#: The padding field. A JSON key, so the padding sits INSIDE the AEAD
#: (authenticated with the item) and a receiver from before padding — which
#: reads the keys it knows and ignores the rest — still parses a padded item.
#: Never covered by an author signature (those sign a fixed field list).
PAD_FIELD: str = "_pad"


def pad_json_object(
    body: dict,
    *,
    buckets: tuple[int, ...] = ITEM_SIZE_BUCKETS,
) -> bytes:
    """``body`` as JSON, padded with ASCII ``0`` in :data:`PAD_FIELD` to
    exactly the smallest of ``buckets`` (ascending; default
    :data:`ITEM_SIZE_BUCKETS`) that fits. A pad the body already carries is
    replaced. A body larger than the largest bucket is left with an empty
    pad (its size is then its own — callers with a hard cap refuse it).
    ``body`` is not mutated."""
    out = {k: v for k, v in body.items() if k != PAD_FIELD}
    out[PAD_FIELD] = ""
    base = orjson.dumps(out)
    bucket = next((b for b in buckets if b >= len(base)), None)
    if bucket is None:
        return base
    out[PAD_FIELD] = "0" * (bucket - len(base))
    return orjson.dumps(out)


# ── Authority-only notices (host relay, ``space_post_public``) ───────────

#: The inner key naming an authority-only notice. Only ever read on an inner
#: WITHOUT an author signature; a seed holder refuses to relay a member's
#: author-signed inner that carries it.
AUTHORITY_KIND_FIELD: str = "authority_kind"
#: A seed holder's removal of a post or comment (a moderator's, an admin's or
#: the author's own delete, applied on the host path).
AUTHORITY_KIND_REMOVAL: str = "removal"
#: A seed holder's mark on an AUTHOR-SIGNED post inner it relays after
#: releasing it from the moderation queue: the author's household may hold
#: only a ``comment``-scope writer cert there, and the mark (outside the author
#: signature, under the authority signature) is what lets a follower accept
#: that. A seed holder never relays a member's inner that already carries it.
AUTHORITY_KIND_APPROVED_POST: str = "approved_post"
SUPPORTED_AUTHORITY_KINDS: frozenset[str] = frozenset(
    {AUTHORITY_KIND_REMOVAL, AUTHORITY_KIND_APPROVED_POST}
)

#: v_96 — the ``item_type`` of an authority-only item on a space's opaque
#: channel: ``{"item_type": "authority", "inner": <inner + authority
#: signature>}`` inside the channel ciphertext. The channel frame has no
#: authority signature of its own (the GFS authorizes it on the channel
#: writer key, which every writer holds), so the space authority signs the
#: inner itself (``authority_sig`` / ``authority_sig_suite``, event type
#: ``space_channel_authority``). Never on the space-keyed relay.
CHANNEL_AUTHORITY_ITEM_TYPE: str = "authority"

#: v_96 — the mark an author's household sets in the ``public_relay`` hint
#: of its federated post (outside the author signature, inside the encrypted
#: payload) when it puts the post on the space's channel itself: the host
#: then does not put it there again. Absent → the host does.
CHANNEL_SENT_FIELD: str = "channel_sent"

REMOVAL_TARGET_POST: str = "post"
REMOVAL_TARGET_COMMENT: str = "comment"
REMOVAL_TARGETS: frozenset[str] = frozenset(
    {REMOVAL_TARGET_POST, REMOVAL_TARGET_COMMENT}
)

#: Upper bound on an id in a removal notice (post / comment ids are uuid4
#: hex or owner-bound ids — both far shorter).
_MAX_REMOVAL_ID: int = 128


def _removal_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_REMOVAL_ID:
        raise ValueError(f"removal notice: bad {what}")
    return value


@dataclass(slots=True, frozen=True)
class AuthorityRemoval:
    """A space-authority removal notice for one post or comment.

    Carries exactly what a follower needs to apply it — never who removed
    it or why. ``post_id`` is the post itself for a post removal and the
    comment's post for a comment removal. ``author_user_id`` is the item's
    author (empty when the seed holder does not know it): a follower already
    shows it with the item, and needs it to leave a tombstone for an item it
    does not hold yet — only under an id owner-bound to that author in THIS
    space, so a notice can never pre-empt another space's row.
    """

    space_id: str
    target: str
    item_id: str
    post_id: str
    author_user_id: str = ""

    def __post_init__(self) -> None:
        _removal_id(self.space_id, "space_id")
        _removal_id(self.item_id, "item_id")
        _removal_id(self.post_id, "post_id")
        if self.author_user_id:
            _removal_id(self.author_user_id, "author_user_id")
        if self.target not in REMOVAL_TARGETS:
            raise ValueError(f"removal notice: bad target {self.target!r}")
        if self.target == REMOVAL_TARGET_POST and self.post_id != self.item_id:
            raise ValueError("removal notice: a post removal names its own post")

    def to_inner(self) -> dict:
        inner = {
            AUTHORITY_KIND_FIELD: AUTHORITY_KIND_REMOVAL,
            "space_id": self.space_id,
            "target": self.target,
            "item_id": self.item_id,
            "post_id": self.post_id,
        }
        if self.author_user_id:
            inner["author_user_id"] = self.author_user_id
        return inner

    @classmethod
    def from_inner(cls, inner: dict) -> AuthorityRemoval:
        """Parse a decrypted inner; :class:`ValueError` when it is not a
        well-formed removal notice."""
        if inner.get(AUTHORITY_KIND_FIELD) != AUTHORITY_KIND_REMOVAL:
            raise ValueError("not a removal notice")
        target = inner.get("target")
        author = inner.get("author_user_id")
        if author is not None and not isinstance(author, str):
            raise ValueError("removal notice: bad author_user_id")
        return cls(
            space_id=_removal_id(inner.get("space_id"), "space_id"),
            target=str(target) if isinstance(target, str) else "",
            item_id=_removal_id(inner.get("item_id"), "item_id"),
            post_id=_removal_id(inner.get("post_id"), "post_id"),
            author_user_id=author or "",
        )
