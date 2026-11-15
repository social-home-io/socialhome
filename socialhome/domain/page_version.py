"""Space-page version identity (§4.4.4.1, v_48).

Space pages are **host-sequenced**: the space owner's household numbers
every canonical version (``Page.seq``); member households mirror it. A
version hash is never used for ordering — only to recognise a version:
validating a proposal's base, spotting duplicates, matching an
acknowledgement to the draft it answers.

* :func:`version_hash` — ``"sha256:" + hex`` over the canonical JSON of the
  stored ``title`` + ``content`` + ``cover_image_url``. The ``sha256:``
  prefix is the suite tag: a receiver rejects any other prefix
  (:func:`is_version_hash`), never guesses.
* :class:`PageConflictSide` — one version the host could not merge, kept
  beside the canonical body until somebody resolves it.
* :func:`merge_scalar` — three-way rule for a one-line field (title, cover);
  two different changes are a conflict.

Pure functions, no I/O.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from ..canonical_json import canonical_utf8_str
from .relay_media_frame import COUNTER_MAX, WIDEST_ID, WIDEST_STAMP

if TYPE_CHECKING:
    from .page import Page

#: The suite tag of a version hash — the only one this build speaks.
VERSION_HASH_SUITE = "sha256"

#: A well-formed version hash.
VERSION_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(slots=True, frozen=True)
class PageConflictSide:
    """One version of an open conflict: an edit the host could not merge
    into the canonical body, kept beside it (one per user)."""

    #: The side's version hash — also its id (``side_id`` on the wire).
    hash: str
    title: str
    content: str
    #: Who authored this version.
    by: str
    #: When the host recorded it — its storage key, the same everywhere.
    at: str
    cover_image_url: str | None = None
    #: The canonical ``seq`` the side's author edited from.
    base_seq: int = 0


@dataclass(slots=True, frozen=True)
class DraftBase:
    """The canonical version a member household's local draft was made
    from (sent as the proposal's ``base_seq`` / ``base_hash``), plus the
    sides a pending resolution settles."""

    title: str
    content: str
    seq: int
    by: str
    cover_image_url: str | None = None
    resolves: tuple[str, ...] = ()
    #: Hash of the draft as last SENT to the host (``None``: never sent).
    #: Only a sent, resolve-free draft may be settled by a host version's
    #: content — an unsent one always goes to the host.
    sent: str | None = None


def version_hash(title: str, content: str, cover_image_url: str | None = None) -> str:
    """The version id of a page body: ``sha256:`` + hex digest of the
    canonical JSON ``{"content": …, "cover": …, "title": …}``."""
    blob = canonical_utf8_str(
        {
            "content": content or "",
            "cover": cover_image_url or "",
            "title": title or "",
        }
    )
    return f"{VERSION_HASH_SUITE}:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def side_to_wire(side: PageConflictSide) -> dict[str, Any]:
    """One open conflict side as it travels (``conflict`` entries)."""
    return {
        "side_id": side.hash,
        "title": side.title,
        "content": side.content,
        "cover": side.cover_image_url,
        "by": side.by,
        "base_seq": side.base_seq,
        "at": side.at,
    }


#: A version hash's length on the wire, without hashing: what
#: :func:`page_record` puts in place of the hash when it only measures.
_MEASURE_HASH = f"{VERSION_HASH_SUITE}:" + "0" * 64

#: A page's ids and stamps — they differ between the household that saves
#: it, the host that sequences it and every receiver — and its counter.
_PAGE_IDS = (
    "id",
    "space_id",
    "created_by",
    "last_editor_user_id",
    "locked_by",
    "delete_requested_by",
    "delete_approved_by",
)
_PAGE_STAMPS = (
    "created_at",
    "updated_at",
    "last_edited_at",
    "locked_at",
    "lock_expires_at",
    "delete_requested_at",
    "delete_approved_at",
)


def page_record(
    page: Page, sides: Sequence[PageConflictSide], *, measure: bool = False
) -> dict[str, Any]:
    """A space page as it travels: its §25.6 ``pages`` record — the row
    (not this household's draft marker), its version hash and every open
    conflict side. What the exporter streams.

    *measure*: the record a page's size is measured on — the same, with
    every id, stamp and counter at its widest (and the fixed-length hash
    not computed), so the household that saves a page, the host that
    sequences it and every receiver measure the same number for the same
    words. Receivers bound those fields to that width
    (``domain/relay_media_frame.py``), so what travels is never larger."""
    record = asdict(page)
    record.pop("pending_base_seq", None)
    if measure:
        record.update(dict.fromkeys(_PAGE_IDS, WIDEST_ID))
        record.update(dict.fromkeys(_PAGE_STAMPS, WIDEST_STAMP))
        record["seq"] = COUNTER_MAX
        record["version_hash"] = _MEASURE_HASH
        record["conflict"] = [
            {
                **side_to_wire(s),
                "side_id": _MEASURE_HASH,
                "by": WIDEST_ID,
                "at": WIDEST_STAMP,
                "base_seq": COUNTER_MAX,
            }
            for s in sides
        ]
        return record
    record["version_hash"] = version_hash(
        page.title, page.content, page.cover_image_url
    )
    record["conflict"] = [side_to_wire(s) for s in sides]
    return record


def is_version_hash(value: object) -> bool:
    """Is ``value`` a well-formed version hash of a supported suite?"""
    return isinstance(value, str) and VERSION_HASH_RE.fullmatch(value) is not None


class ScalarConflict:
    """Marker: both sides changed a one-line field differently."""


SCALAR_CONFLICT = ScalarConflict()


def merge_scalar(
    base: str | None, mine: str | None, theirs: str | None
) -> str | None | ScalarConflict:
    """Three-way merge of a one-line field (title, cover): an unchanged
    side takes the other's change; two different changes are
    :data:`SCALAR_CONFLICT` — the version becomes a conflict side."""
    if mine == theirs:
        return mine
    if mine == base:
        return theirs
    if theirs == base:
        return mine
    return SCALAR_CONFLICT
