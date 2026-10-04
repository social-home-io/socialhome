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

Pure module — no I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    limit = (now or datetime.now(timezone.utc)) + timedelta(
        seconds=MAX_ITEM_CLOCK_SKEW_S
    )
    if parsed > limit:
        return None
    return parsed


def stamp_to_db(stamp: datetime) -> str:
    """A stamp in the naive-UTC shape of SQLite's ``datetime('now')``, with
    microseconds — so the ``edited_at`` column keeps one shape and compares
    as a string against rows the local edit paths stamped."""
    return (
        stamp.astimezone(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(sep=" ", timespec="microseconds")
    )
