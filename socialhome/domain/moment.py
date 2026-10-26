"""Domain types for the Momentum pillar (§Momentum).

A *moment* is a one-shot household-broadcast post: ≤ 1 000 chars text
plus an optional image or short (≤ 15 s) video. Moments fan out to
every paired peer and their peers, up to 3 hops. They live 24 h by
default; if a local viewer has explicitly followed the author (directly or
through a GFS) the moment surfaces in their inbox until its ``expires_at``
— the author's choice, 1–30 days, 7 by default (the same row, just a
longer visible window — retention is per-viewer at the list-query layer).

Replies are themselves moments and carry a ``parent_moment_id``. The
reply inherits the parent's audience implicitly via the federation
relay: the parent already reached every relevant peer mesh, so the
reply re-uses the same fan-out as a top-level moment from the same
author. Threading stays flat (replies-to-replies attach to the same
root parent_moment_id).
"""

from __future__ import annotations

import enum
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .emoji import is_single_emoji
from .errors import CodedError

#: Hard caps. Composer + service both enforce.
MOMENT_MAX_CONTENT_LEN: int = 1_000
MOMENT_MAX_VIDEO_MS: int = 15_000

#: Federation relay: cap at 3 hops. Origin sends with hop=1; each
#: receiver bumps and re-fans up to ``MAX_HOPS - 1``.
MOMENT_MAX_HOPS: int = 3

#: How long people who follow an author keep the author's moments when the
#: author chose nothing (``moments.follower_retention_days``, decision 6d).
#: Everyone else sees a moment for 24 hours.
MOMENT_DEFAULT_RETENTION_DAYS: int = 7

#: Shortest an author may choose.
MOMENT_MIN_RETENTION_DAYS: int = 1

#: Longest any moment lives on a household that did not write it
#: (``expires_at ≤ created_at + 30 d``, v_65): the most an author may choose,
#: the cap a receiver clamps a received ``expires_at`` to, and the lifetime
#: of a delete tombstone for a moment this household never stored — no
#: genuine create can outlive it.
MOMENT_MAX_LIFETIME_DAYS: int = 30

#: A relaying household passes a create on only while the moment is younger
#: than this (by its signed ``occurred_at``, v_65). Mesh copies travel in
#: seconds and an outbox retries for well under a day; anything older is a
#: replay, and with moments that live a month a replay must not re-flood
#: the mesh. The household still stores it (subject to the clamp).
MOMENT_RELAY_MAX_AGE_HOURS: int = 48

#: Household default for how much moment media each person keeps in their
#: archive (decision 6a, owner sign-off 6, 2026-10-25). A moment written
#: here outlives its ``expires_at`` on this household only; past the
#: cap its oldest pictures and videos go first (the text stays). 2 GiB is
#: about a year of a heavy poster (5 photos a day ≈ 1.8 GB) and 10 GiB for
#: a household of five — sized for a 32 GB Home Assistant box. ``0`` means
#: no limit.
MOMENT_ARCHIVE_DEFAULT_CAP_BYTES: int = 2 * 1024**3

#: Most archived files the retention tick drops per run (bounded work; the
#: next hourly tick continues).
MOMENT_ARCHIVE_MAX_DROPS_PER_TICK: int = 200


def _parse_utc(value: object) -> datetime | None:
    """An ISO 8601 time as aware UTC (naive = UTC), or ``None``."""
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


def clamp_moment_expiry(created_at: str, expires_at: str) -> str:
    """``expires_at`` as a receiver keeps it: never later than
    ``created_at`` + :data:`MOMENT_MAX_LIFETIME_DAYS` (v_65).

    ``expires_at`` is under the author's origin signature, so a receiver
    can't trust a relay to have checked it — but nothing stops an author's
    own household from signing a year. The clamp holds every household to
    the month a person can choose. An in-range value is returned
    unchanged (byte for byte); an unparseable ``created_at`` leaves the
    value to the caller's own expiry checks.
    """
    created = _parse_utc(created_at)
    expires = _parse_utc(expires_at)
    if created is None or expires is None:
        return expires_at
    latest = created + timedelta(days=MOMENT_MAX_LIFETIME_DAYS)
    return latest.isoformat() if expires > latest else expires_at


def moment_too_old_to_relay(
    occurred_at: object, *, now: datetime | None = None
) -> bool:
    """``True`` when a create's signed ``occurred_at`` is more than
    :data:`MOMENT_RELAY_MAX_AGE_HOURS` ago (or missing / unparseable) — a
    relaying household then keeps it but does not pass it on."""
    created = _parse_utc(occurred_at) if occurred_at else None
    if created is None:
        return True
    now = now or datetime.now(UTC)
    return created < now - timedelta(hours=MOMENT_RELAY_MAX_AGE_HOURS)


class MomentTombstone(enum.Enum):
    """What recording a delete changed on this household."""

    #: A live moment was wiped down to a tombstone.
    REMOVED = "removed"
    #: The moment was never held here; a content-free tombstone now is.
    RECORDED = "recorded"
    #: A tombstone was already held — nothing changed (a replayed delete).
    ALREADY = "already"

    @property
    def changed(self) -> bool:
        """Whether this household learnt something — only then is the
        delete worth relaying on (v_65)."""
        return self is not MomentTombstone.ALREADY


@dataclass(slots=True, frozen=True)
class Moment:
    """A single broadcast post.

    ``author_user_id`` is plain text (no FK) so remote authors land in
    the shared ``moments`` table — same convention as
    ``conversation_messages.sender_user_id`` for DMs.

    ``parent_moment_id`` is ``None`` for top-level moments.
    """

    id: str
    author_user_id: str
    content: str
    media_url: str | None
    media_type: str | None  # 'image' | 'video' | None
    duration_ms: int | None
    parent_moment_id: str | None
    origin_instance_id: str
    created_at: str
    expires_at: str
    #: Hop the row landed on this instance. ``1`` for the origin
    #: author; ``2``/``3`` for relayed arrivals. Compared against
    #: each viewer's :data:`MomentPreferences.max_hops` to gate
    #: visibility (relay decisions stay bounded by
    #: :data:`MOMENT_MAX_HOPS`, the global wire cap).
    hop_count: int = 1
    #: Author chose to fan out via at least one GFS (§Momentum-public).
    is_public: bool = False
    #: How this row landed locally — ``self``/``household``/``gfs``.
    #: GFS-received rows must NOT be relayed to paired peers (the
    #: no-redistribute rule); the federation outbound checks this.
    received_via: str = "self"
    #: When ``received_via='gfs'``, points at the GFS connection that
    #: relayed the moment. NULL otherwise.
    received_via_gfs_id: str | None = None
    #: Set when the moment was deleted: the row stays as a content-free
    #: tombstone until ``expires_at`` so a replayed or late-arriving create
    #: for the same id is refused. Ordinary reads never return tombstones.
    deleted_at: str | None = None


@dataclass(slots=True, frozen=True)
class MomentArchiveUsage:
    """How much moment media one person keeps here, against the cap.

    ``cap_bytes == 0`` means the household set no limit."""

    used_bytes: int
    cap_bytes: int


@dataclass(slots=True, frozen=True)
class SavedMoment:
    """One person's save of a moment (decision 6b): *user_id* keeps
    *moment* past its expiry until its author deletes it. ``saved_at`` is
    UTC ISO 8601 — the Saved list pages on it, newest first."""

    moment: Moment
    user_id: str
    saved_at: str


class MomentOwnSaveError(CodedError):
    """A person tried to save their own moment — it already stays in their
    archive (decision 6a)."""

    status = 422
    code = "OWN_MOMENT"
    detail = "Your own moments already stay in your archive."


class MomentSaveUnavailableError(CodedError):
    """Save is offered only where the author's delete can still reach the
    copy: a household that keeps deletes queued (v_66+) or a GFS that keeps
    delete certificates (``moment_delete_log``)."""

    status = 422
    code = "SAVE_UNAVAILABLE"
    detail = (
        "This moment can't be saved: its author's household or server "
        "can't reach a saved copy with a later delete."
    )


@dataclass(slots=True, frozen=True)
class MomentReaction:
    """One reactor's per-moment emoji. UPSERT keyed on (moment_id, reactor)."""

    moment_id: str
    reactor_user_id: str
    emoji: str
    reacted_at: str


#: Longest reaction a moment takes, in UTF-8 bytes — room for any one emoji
#: (a ZWJ family or a subdivision flag is ~25 bytes), never a sentence.
MOMENT_REACTION_MAX_EMOJI_BYTES: int = 32


def is_valid_moment_reaction(emoji: object) -> bool:
    """Whether *emoji* is a reaction a moment takes: exactly one emoji
    (:func:`~socialhome.domain.emoji.is_single_emoji`) of at most
    :data:`MOMENT_REACTION_MAX_EMOJI_BYTES` bytes. Enforced on the route, in
    the service and on every inbound path."""
    return (
        isinstance(emoji, str)
        and len(emoji.encode("utf-8")) <= MOMENT_REACTION_MAX_EMOJI_BYTES
        and is_single_emoji(emoji)
    )


class MomentReactionInvalidError(CodedError):
    """A reaction that is not exactly one emoji of ≤ 32 bytes."""

    status = 422
    code = "INVALID_REACTION"
    detail = "A reaction is one emoji."


#: Where a reaction to a moment reaches (the API's ``reactions_reach``).
#: ``author``: the author's household gets it — the author is here, on a
#: paired household, or (v_67) further away along the moment's return
#: tokens. ``household``: it stays in this household only.
REACTION_REACH_AUTHOR: str = "author"
REACTION_REACH_HOUSEHOLD: str = "household"


@dataclass(slots=True, frozen=True)
class MomentReactionRoute:
    """How a reaction gets back to a moment's origin household (v_67).

    Kept on the moment row (``moments.reaction_route``) of a moment that
    arrived through relays, so a reaction days later still finds its way:
    ``keywrap_pk`` is the origin's static X25519 key-wrap key (hex, bound to
    its identity key when the create was received), ``via`` the neighbour
    the moment came from, ``tokens`` the relays' return tokens in hop order
    (one per relay — ``hop_count − 1``) and ``token_suite`` their suite.
    Only a complete route is ever stored.
    """

    keywrap_pk: str
    via: str
    tokens: tuple[str, ...]
    token_suite: str


@dataclass(slots=True, frozen=True)
class MomentGfsReactionTarget:
    """How a reaction to a public moment that came through a GFS gets to its
    author's household (v_68): posted to ``gfs_id`` — the GFS the moment
    arrived on, which proved ``moment_reactions`` — sealed to ``keywrap_pk``,
    the author household's static key-wrap key (hex) from its signed
    envelope, under ``keywrap_suite``, the KEM suite it was named under (one
    this build supports — today ``x25519``)."""

    gfs_id: str
    keywrap_pk: str
    keywrap_suite: str


#: v_69 — most emoji a reaction-totals update names (the most used first;
#: the rest still count in ``total``).
MOMENT_COUNTS_MAX_EMOJIS: int = 8

#: v_69 — highest ``total`` (and per-emoji count) a totals update may carry:
#: far above any household's reach, low enough to stay a small JSON integer.
MOMENT_COUNTS_MAX_TOTAL: int = 1_000_000


@dataclass(slots=True, frozen=True)
class MomentReactionSummary:
    """A moment's reaction totals as its author's household last sent them
    (v_69, ``moments.reaction_summary``): per-emoji counts (at most
    :data:`MOMENT_COUNTS_MAX_EMOJIS`, the most used first), the total over
    every emoji, and ``rev`` — the author's send time in Unix milliseconds,
    which only ever grows. Totals only — never who reacted."""

    rev: int
    counts: tuple[tuple[str, int], ...]
    total: int


def summarize_reactions(
    tally: dict[str, int], *, max_emojis: int = MOMENT_COUNTS_MAX_EMOJIS
) -> tuple[dict[str, int], int]:
    """``(counts, total)`` of a moment's ``{emoji: n}`` *tally*: the
    *max_emojis* most used emoji (ties by emoji, so the same tally always
    gives the same bytes) and the total over all of them."""
    ranked = sorted(
        ((e, n) for e, n in tally.items() if n > 0), key=lambda p: (-p[1], p[0])
    )
    return dict(ranked[:max_emojis]), sum(n for _, n in ranked)


def reaction_counts_valid(counts: object, total: object) -> bool:
    """Whether *counts* / *total* are a well-formed totals update: at most
    :data:`MOMENT_COUNTS_MAX_EMOJIS` entries, each one emoji
    (:func:`is_valid_moment_reaction`) with a count of 1 … *total*, the
    counts summing to at most *total*, and *total* a whole number in 0 …
    :data:`MOMENT_COUNTS_MAX_TOTAL`."""
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not 0 <= total <= MOMENT_COUNTS_MAX_TOTAL
    ):
        return False
    if not isinstance(counts, dict) or len(counts) > MOMENT_COUNTS_MAX_EMOJIS:
        return False
    seen = 0
    for emoji, n in counts.items():
        if not is_valid_moment_reaction(emoji):
            return False
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= total:
            return False
        seen += n
    return seen <= total


@dataclass(slots=True, frozen=True)
class MomentReactionTotals:
    """One moment's reactions as the API shows them to one viewer (v_69):
    per-emoji ``counts`` and their ``total``; ``totals_rev`` when they are
    the author household's totals (``None``: the reactions held here); the
    viewer's own emoji."""

    counts: dict[str, int]
    total: int
    totals_rev: int | None
    my_reaction: str | None


def merged_reaction_view(
    *,
    summary: MomentReactionSummary | None,
    local: dict[str, int],
    mine: tuple[str, int] | None,
) -> tuple[dict[str, int], int, int | None]:
    """What the API shows for one moment: ``(counts, total, totals_rev)``.

    Without a *summary* (the author's household sent none, or reactions here
    don't reach it): the reactions held here (*local*). With one: its totals,
    plus the viewer's own reaction (*mine* = ``(emoji, reacted_at_ms)``) when
    it was made after the summary was sent — it can't be in it yet.
    ``totals_rev`` is the summary's ``rev`` (``None`` for local counts)."""
    if summary is None:
        return dict(local), sum(local.values()), None
    counts = dict(summary.counts)
    total = summary.total
    if mine is not None and mine[1] > summary.rev:
        counts[mine[0]] = counts.get(mine[0], 0) + 1
        total += 1
    return counts, total, summary.rev


#: Cap on how many tags survive per moment. Twitter is generous on
#: tags-in-a-tweet but most quality timelines stay below 5; clamp here
#: so a tag-spam moment doesn't inflate the trending list.
MOMENT_MAX_HASHTAGS_PER_POST: int = 10

#: Per-tag length cap (code points after the leading ``#``). A longer
#: run is not a tag at all — dropped, never cut to a different tag.
MOMENT_MAX_HASHTAG_LEN: int = 32


def _is_tag_char(ch: str) -> bool:
    """A tag is a run of letters, marks, numbers and ``_`` in any script
    (Unicode categories L*, M*, N*). Marks count so a decomposed ``ü`` or
    a Devanagari vowel sign doesn't end the tag. The SPA's
    ``features/momentum/hashtags.tsx`` uses the same class
    (``[\\p{L}\\p{M}\\p{N}_]``)."""
    return ch == "_" or unicodedata.category(ch)[0] in "LMN"


def normalize_hashtag(tag: str) -> str:
    """The stored form of a tag: NFC, lowercased, without a leading
    ``#``. The SPA does the same (``normalize('NFC').toLowerCase()``), so
    a chip's ``?tag=`` matches the stored row."""
    return unicodedata.normalize("NFC", tag.strip().lstrip("#")).lower()


def extract_hashtags(content: str) -> list[str]:
    """Return the unique normalised hashtags found in ``content``.

    * A tag is ``#`` + letters / marks / numbers / ``_`` in any script
      (``#Kürbisfest``, ``#Fußball``, ``#東京``), ended by anything
      else (space, punctuation, emoji). Stored NFC + lowercased
      (:func:`normalize_hashtag`).
    * A ``#`` inside a word (``issue#42``) starts no tag; ``##foo``
      counts as ``foo``.
    * A run longer than :data:`MOMENT_MAX_HASHTAG_LEN` is dropped.
    * Order is preserved by *first* occurrence; capped at
      :data:`MOMENT_MAX_HASHTAGS_PER_POST`.
    """
    if not content:
        return []
    text = unicodedata.normalize("NFC", content)
    seen: set[str] = set()
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "#" or (i > 0 and _is_tag_char(text[i - 1])):
            i += 1
            continue
        j = i + 1
        while j < n and _is_tag_char(text[j]):
            j += 1
        raw = text[i + 1 : j]
        i = j
        if not raw or len(raw) > MOMENT_MAX_HASHTAG_LEN:
            continue
        tag = raw.lower()
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= MOMENT_MAX_HASHTAGS_PER_POST:
            break
    return out
