"""Member-published space item types (v_49 ``space_item``).

A member household publishes its own items over the connection server as
the generic ``space_item`` (:mod:`socialhome.domain.gfs_member_publish`); the
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

**Authority kinds** ride the host's ``space_post_public`` relay instead
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

import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import orjson

from .writer_cert import WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE

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


def required_scope(item_type: str) -> str:
    """The scope a cert must grant for ``item_type``; :class:`ValueError`
    for a type this build does not carry."""
    try:
        return _REQUIRED_SCOPE[item_type]
    except KeyError:
        raise ValueError(f"unsupported space item type {item_type!r}") from None


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


def stamp_to_db(stamp: datetime) -> str:
    """A stamp in the naive-UTC shape of SQLite's ``datetime('now')``, with
    microseconds — so the ``edited_at`` column keeps one shape and compares
    as a string against rows the local edit paths stamped."""
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
ITEM_SIZE_BUCKETS: tuple[int, ...] = (1024, 4096, 16384, 65536, 131072)

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


# ── Payload suites (how a relayed item's plaintext is encoded) ───────────

#: The item plaintext is the padded JSON of :func:`pad_json_object`. A wire
#: shape with no ``payload_suite`` field is this suite.
PAYLOAD_SUITE_JSON: str = "json"
#: The compact JSON, zlib-compressed (level 9), then padded with zero BYTES
#: to the smallest :data:`ITEM_SIZE_BUCKETS` size that fits — all of it
#: inside the AEAD, so the ciphertext length still names only the bucket.
#: A 300-character post shrinks from the 4 KiB bucket to 1 KiB.
PAYLOAD_SUITE_ZLIB: str = "zlib1"
SUPPORTED_PAYLOAD_SUITES: frozenset[str] = frozenset(
    {PAYLOAD_SUITE_JSON, PAYLOAD_SUITE_ZLIB}
)
#: The wire field naming the suite (routing-visible, like
#: ``authority_sig_suite``; the GFS gates compressed items on it).
PAYLOAD_SUITE_FIELD: str = "payload_suite"

#: Most a compressed item may inflate to. A sender compresses only JSON at
#: most this long (larger items keep :data:`PAYLOAD_SUITE_JSON`), so a
#: receiver refuses any stream that would inflate further — a
#: decompression bomb costs it at most this much memory.
MAX_INFLATED_ITEM_BYTES: int = ITEM_SIZE_BUCKETS[-1]

_ZLIB_LEVEL: int = 9


class UnsupportedPayloadSuite(ValueError):
    """A relayed item names a payload suite this build does not read."""


#: The ``error`` a GFS answers (HTTP 409) when it refuses a compressed item
#: because a follower of the space does not read it. The publisher sends the
#: same item as :data:`PAYLOAD_SUITE_JSON` and asks again later.
PAYLOAD_SUITE_UNSUPPORTED_ERROR: str = "payload_suite_unsupported"


class PayloadSuiteRefused(Exception):
    """The GFS refuses a compressed item: some seat of the space lacks the
    ``payload_zlib1`` label (see :data:`PAYLOAD_SUITE_UNSUPPORTED_ERROR`)."""


def payload_suite_of(raw: object) -> str:
    """The suite a wire shape names — :data:`PAYLOAD_SUITE_JSON` when the
    field is absent (first-revision shapes). Raises
    :class:`UnsupportedPayloadSuite` for any other unknown value; never
    falls back."""
    if raw is None:
        return PAYLOAD_SUITE_JSON
    if not isinstance(raw, str) or raw not in SUPPORTED_PAYLOAD_SUITES:
        raise UnsupportedPayloadSuite(str(raw)[:32])
    return raw


def zlib_item_plaintext(
    body: dict, *, buckets: tuple[int, ...] = ITEM_SIZE_BUCKETS
) -> bytes | None:
    """``body`` under :data:`PAYLOAD_SUITE_ZLIB`: compact orjson, zlib,
    zero-padded to a bucket. ``None`` when the suite cannot carry it (its
    JSON is over :data:`MAX_INFLATED_ITEM_BYTES`, or the stream over the
    largest bucket) — the caller sends :data:`PAYLOAD_SUITE_JSON`."""
    raw = orjson.dumps({k: v for k, v in body.items() if k != PAD_FIELD})
    if len(raw) > MAX_INFLATED_ITEM_BYTES:
        return None
    data = zlib.compress(raw, _ZLIB_LEVEL)
    bucket = next((b for b in buckets if b >= len(data)), None)
    if bucket is None:
        return None
    return data + b"\0" * (bucket - len(data))


def seal_item_plaintext(
    body: dict,
    *,
    suite: str = PAYLOAD_SUITE_JSON,
    buckets: tuple[int, ...] = ITEM_SIZE_BUCKETS,
) -> bytes | None:
    """The plaintext a relayed item is encrypted as under ``suite``.
    :data:`PAYLOAD_SUITE_JSON` is :func:`pad_json_object`;
    :data:`PAYLOAD_SUITE_ZLIB` is :func:`zlib_item_plaintext` (``None`` when
    the suite cannot carry the item). Raises
    :class:`UnsupportedPayloadSuite` for any other suite."""
    if suite == PAYLOAD_SUITE_JSON:
        return pad_json_object(body, buckets=buckets)
    if suite == PAYLOAD_SUITE_ZLIB:
        return zlib_item_plaintext(body, buckets=buckets)
    raise UnsupportedPayloadSuite(suite)


def open_item_plaintext(plaintext: bytes, suite: str) -> bytes:
    """The JSON bytes of a decrypted item sealed under ``suite``.

    :data:`PAYLOAD_SUITE_ZLIB` inflates at most
    :data:`MAX_INFLATED_ITEM_BYTES` (a stream that would inflate further is
    refused, never buffered), requires the stream to end, and allows only
    zero padding after it. Raises :class:`ValueError` on any of that and
    :class:`UnsupportedPayloadSuite` for an unknown suite."""
    if suite == PAYLOAD_SUITE_JSON:
        return plaintext
    if suite != PAYLOAD_SUITE_ZLIB:
        raise UnsupportedPayloadSuite(suite)
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(plaintext, MAX_INFLATED_ITEM_BYTES)
    except zlib.error as exc:
        raise ValueError(f"corrupt zlib item: {exc}") from exc
    if inflater.unconsumed_tail:
        raise ValueError("zlib item inflates past the cap")
    if not inflater.eof:
        raise ValueError("zlib item stream does not end")
    if inflater.unused_data.strip(b"\0"):
        raise ValueError("zlib item has data after its stream")
    return out


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
