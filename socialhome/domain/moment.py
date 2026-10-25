"""Domain types for the Momentum pillar (§Momentum).

A *moment* is a one-shot household-broadcast post: ≤ 1 000 chars text
plus an optional image or short (≤ 15 s) video. Moments fan out to
every paired peer and their peers, up to 3 hops. They live 24 h by
default; if a local viewer has explicitly followed the author the
moment surfaces in their inbox up to 7 d (the same row, just a longer
visible window — retention is per-viewer at the list-query layer).

Replies are themselves moments and carry a ``parent_moment_id``. The
reply inherits the parent's audience implicitly via the federation
relay: the parent already reached every relevant peer mesh, so the
reply re-uses the same fan-out as a top-level moment from the same
author. Threading stays flat (replies-to-replies attach to the same
root parent_moment_id).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

#: Hard caps. Composer + service both enforce.
MOMENT_MAX_CONTENT_LEN: int = 1_000
MOMENT_MAX_VIDEO_MS: int = 15_000

#: Federation relay: cap at 3 hops. Origin sends with hop=1; each
#: receiver bumps and re-fans up to ``MAX_HOPS - 1``.
MOMENT_MAX_HOPS: int = 3

#: Absolute on-disk lifetime of a moment (``expires_at = created_at + 7 d``).
#: Also the lifetime of a delete tombstone for a moment this household never
#: stored — no genuine create can outlive it.
MOMENT_RETENTION_DAYS: int = 7

#: Household default for how much moment media each person keeps in their
#: archive (decision 6a, owner sign-off 6, 2026-10-25). A moment written
#: here outlives ``MOMENT_RETENTION_DAYS`` on this household only; past the
#: cap its oldest pictures and videos go first (the text stays). 2 GiB is
#: about a year of a heavy poster (5 photos a day ≈ 1.8 GB) and 10 GiB for
#: a household of five — sized for a 32 GB Home Assistant box. ``0`` means
#: no limit.
MOMENT_ARCHIVE_DEFAULT_CAP_BYTES: int = 2 * 1024**3

#: Most archived files the retention tick drops per run (bounded work; the
#: next hourly tick continues).
MOMENT_ARCHIVE_MAX_DROPS_PER_TICK: int = 200


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
class MomentReaction:
    """One reactor's per-moment emoji. UPSERT keyed on (moment_id, reactor)."""

    moment_id: str
    reactor_user_id: str
    emoji: str
    reacted_at: str


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
