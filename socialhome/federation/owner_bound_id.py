"""Row ids that commit to their creator (§24.11 authorship, v_34 / v_36).

A space row created on one household reaches the others as a federated
create naming a fresh random id. With a *plain* random id the first valid
claim of that id wins on each receiver, so a member household that has
seen the id could announce it first for its own user and have the
creator's real announcement refused as a clash.

An **owner-bound id** makes such a claim self-evidently invalid: the id
carries a random nonce and a commitment over ``(kind, space_id,
owner_user_id, nonce)``. A receiver recomputes the commitment from the
payload it was handed and refuses a mismatch. The owner's household needs
no separate field: a ``user_id`` is derived from its home instance's key
(§4.1.3), and :class:`~socialhome.federation.space_authorship.SpaceAuthorship`
already binds that user to the household that signed the envelope (or to
the space host relaying a remote member's row). So the id stays valid when
the host relays it, and no other household can claim it for anyone else.

Wire shape — 32 lowercase hex characters, laid out as an RFC 9562 UUIDv8
so it never collides with the uuid4 hex every earlier id used::

    id[0:16]   nonce — 60 random bits; id[12] is the UUID version nibble "8"
    id[16]     suite nibble, in the UUID variant position (8 / 9 / a / b)
    id[17:32]  commitment — the first 60 bits of SHA-256 over
               "socialhome/owner-bound-id/v1" | kind | space_id
               | owner_user_id | nonce   (NUL-separated)

The suite nibble names the commitment algorithm (the ``*_suite`` rule for
crypto wire shapes): ``"8"`` is SHA-256. A receiver that meets an unknown
suite nibble refuses the id — there is no default fallback.

Nothing is stored beside the id and no payload field is added: the id is
self-verifying, so every path that re-sends the row (live fan-out, the
§25.6 sync, the ``SPACE_SYNC_RESUME`` replay, a host relay) carries the
proof for free, and a peer that predates the binding stores it as the
opaque string it always was.

v_34 bound space gallery albums; v_36 binds every other federated row
with an owner — space posts (and so the bazaar / poll / schedule rows
hung off a wrapper post) and comments, gallery items, calendar events,
tasks, pages, stickies — and moments. A moment belongs to no space, so
its kind is **unscoped** (:data:`UNSCOPED_KINDS`): the space component is
the empty string and the commitment covers only the kind, the author and
the nonce — the author's ``user_id`` already derives from its home
household's key, which is all a moment's receivers need to verify.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import secrets

log = logging.getLogger(__name__)

#: The only commitment suite today: SHA-256, truncated to 60 bits.
OWNER_BOUND_ID_SUITE_SHA256: str = "sha256"
SUPPORTED_OWNER_BOUND_ID_SUITES: frozenset[str] = frozenset(
    {OWNER_BOUND_ID_SUITE_SHA256}
)

#: Row kinds, domain-separating the commitment per table.
GALLERY_ALBUM_KIND: str = "gallery-album"
GALLERY_ITEM_KIND: str = "gallery-item"
SPACE_POST_KIND: str = "space-post"
SPACE_COMMENT_KIND: str = "space-comment"
SPACE_CALENDAR_EVENT_KIND: str = "space-calendar-event"
SPACE_TASK_KIND: str = "space-task"
SPACE_PAGE_KIND: str = "space-page"
SPACE_STICKY_KIND: str = "space-sticky"
MOMENT_KIND: str = "moment"

#: Kinds whose rows belong to no space: minted and checked with
#: ``space_id=""``. Every other kind needs a space.
UNSCOPED_KINDS: frozenset[str] = frozenset({MOMENT_KIND})

_DOMAIN = b"socialhome/owner-bound-id/v1"
_HEX = frozenset("0123456789abcdef")
_VERSION_POS = 12  # UUID version nibble
_VERSION = "8"  # UUIDv8 — "custom"; every earlier id is a uuid4 ("4")
_SUITE_POS = 16  # UUID variant nibble — its top bits are always ``10``
_VARIANT_NIBBLES = frozenset("89ab")
_SUITE_BY_NIBBLE: dict[str, str] = {"8": OWNER_BOUND_ID_SUITE_SHA256}
_NIBBLE_BY_SUITE: dict[str, str] = {v: k for k, v in _SUITE_BY_NIBBLE.items()}


class UnsupportedOwnerBoundIdSuite(ValueError):
    """An owner-bound id names a commitment suite this build does not know."""


class OwnerBinding(enum.Enum):
    """The outcome of :func:`check_owner_bound_id`."""

    #: Not an owner-bound id at all — minted before the binding existed.
    LEGACY = "legacy"
    #: Owner-bound and the commitment matches the claimed creator.
    VALID = "valid"
    #: Owner-bound, but for another owner, space or kind — or an unknown suite.
    MISMATCH = "mismatch"


def is_owner_bound(row_id: str) -> bool:
    """``row_id`` has the owner-bound shape (whatever its suite)."""
    return (
        len(row_id) == 32
        and _HEX.issuperset(row_id)
        and row_id[_VERSION_POS] == _VERSION
        and row_id[_SUITE_POS] in _VARIANT_NIBBLES
    )


def suite_of(row_id: str) -> str:
    """The commitment suite an owner-bound id names.

    Raises :class:`UnsupportedOwnerBoundIdSuite` for a suite nibble this
    build does not know, and :class:`ValueError` for a non-bound id.
    """
    if not is_owner_bound(row_id):
        raise ValueError(f"{row_id!r} is not an owner-bound id")
    suite = _SUITE_BY_NIBBLE.get(row_id[_SUITE_POS])
    if suite is None:
        raise UnsupportedOwnerBoundIdSuite(
            f"owner-bound id suite nibble {row_id[_SUITE_POS]!r}"
        )
    return suite


def _commitment(kind: str, space_id: str, owner_user_id: str, nonce: str) -> str:
    material = b"\x00".join(
        (
            _DOMAIN,
            kind.encode("utf-8"),
            space_id.encode("utf-8"),
            owner_user_id.encode("utf-8"),
            nonce.encode("ascii"),
        )
    )
    return hashlib.sha256(material).hexdigest()[:15]


def _space_fits(kind: str, space_id: str) -> bool:
    """A space-scoped kind names a space; an unscoped one names none."""
    return (space_id == "") if kind in UNSCOPED_KINDS else bool(space_id)


def mint_owner_bound_id(
    kind: str,
    *,
    space_id: str,
    owner_user_id: str,
    suite: str = OWNER_BOUND_ID_SUITE_SHA256,
) -> str:
    """A fresh id for a ``kind`` row of ``space_id`` created by ``owner_user_id``."""
    if suite not in SUPPORTED_OWNER_BOUND_ID_SUITES:
        raise UnsupportedOwnerBoundIdSuite(suite)
    if not kind or not owner_user_id or not _space_fits(kind, space_id):
        raise ValueError(
            "kind and owner_user_id are required, and a space_id exactly "
            "when the kind is space-scoped"
        )
    raw = secrets.token_hex(8)
    nonce = raw[:_VERSION_POS] + _VERSION + raw[_VERSION_POS + 1 :]
    tag = _commitment(kind, space_id, owner_user_id, nonce)
    return nonce + _NIBBLE_BY_SUITE[suite] + tag


def check_owner_bound_id(
    kind: str,
    row_id: str,
    *,
    space_id: str,
    owner_user_id: str | None,
) -> OwnerBinding:
    """Does ``row_id`` commit to ``owner_user_id`` creating it in ``space_id``?

    A legacy id (any other shape) is :attr:`OwnerBinding.LEGACY` — the
    caller keeps its pre-binding rules for it. An owner-bound id with no
    owner, an unknown suite, or a commitment over anything else is
    :attr:`OwnerBinding.MISMATCH`.
    """
    if not is_owner_bound(row_id):
        return OwnerBinding.LEGACY
    try:
        suite_of(row_id)
    except UnsupportedOwnerBoundIdSuite:
        return OwnerBinding.MISMATCH
    if not owner_user_id or not _space_fits(kind, space_id):
        return OwnerBinding.MISMATCH
    expected = _commitment(kind, space_id, owner_user_id, row_id[:_SUITE_POS])
    if secrets.compare_digest(expected, row_id[_SUITE_POS + 1 :]):
        return OwnerBinding.VALID
    return OwnerBinding.MISMATCH


def owner_bound_id_refused(
    kind: str,
    row_id: str,
    *,
    space_id: str,
    owner_user_id: str | None,
    context: str,
) -> bool:
    """``True`` — with a WARNING naming ``context`` — when ``row_id`` is
    owner-bound but not to ``owner_user_id`` (in ``space_id``).

    The one call every receiver path makes before filing a new row: a
    mismatch (or an unknown suite) is a claim on somebody else's id and is
    refused; a legacy id returns ``False`` so the caller keeps its
    pre-binding rules.
    """
    binding = check_owner_bound_id(
        kind, row_id, space_id=space_id, owner_user_id=owner_user_id
    )
    if binding is not OwnerBinding.MISMATCH:
        return False
    log.warning(
        "%s: %s id %s is not bound to %r%s — refusing the write",
        context,
        kind,
        row_id,
        owner_user_id,
        f" in space {space_id}" if space_id else "",
    )
    return True
