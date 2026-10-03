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
import json
import re
from dataclasses import dataclass

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


def version_hash(title: str, content: str, cover_image_url: str | None = None) -> str:
    """The version id of a page body: ``sha256:`` + hex digest of the
    canonical JSON ``{"content": …, "cover": …, "title": …}``."""
    blob = json.dumps(
        {
            "content": content or "",
            "cover": cover_image_url or "",
            "title": title or "",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"{VERSION_HASH_SUITE}:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
