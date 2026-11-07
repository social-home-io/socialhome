"""Who may deliver a space file — the ``SPACE_MEDIA_BLOB`` sender binding.

A space file is addressed by name, and a received file is write-once: the
first complete copy of a name is the one every row naming it shows (and,
through the GFS media cache, the one this household re-seals for its
link-joined readers). So the receiver binds the authenticated sender
(``from_instance`` — the origin household, also after a ``SPACE_ROUTED``
unwrap) to the row the blob is correlated with:

* the row's **own household** — its author / uploader / seller / submitter
  holds a live seat on the sender (the roster mirror, never a payload
  field);
* a household with **content authority** in the space — the host (owner, or
  the delegated admin household) and admin / moderator households: the host
  re-ships every member's files on the §25.6 catch-up, an approver household
  ships the files of a submission it released;
* for a **collaborative** row (a calendar event, which any member may edit,
  cover included) any writer household.

A blob for a row this household has not seen yet is **held** per (sender,
file) and adopted only once the row arrives and binds that sender — except
from the host, which may name any row of its space and is accepted at once.

Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MediaBlobVerdict(StrEnum):
    """What the receiver does with a ``SPACE_MEDIA_BLOB``."""

    #: Publish under its name (write-once).
    ACCEPT = "accept"
    #: The row is not here yet: keep the bytes aside, keyed by sender.
    HOLD = "hold"
    #: Out of scope, or a sender the row does not bind.
    REFUSE = "refuse"


@dataclass(slots=True, frozen=True)
class SpaceMediaOwner:
    """A known row's answer to "may this file land, and from whom".

    ``in_scope`` — the row lives in the blob's space and references the file.
    ``author_user_id`` — the person whose household delivers the row's files
    (empty: only content authority may). ``collaborative`` — any writer
    household may (a calendar event's cover).
    """

    in_scope: bool
    author_user_id: str = ""
    collaborative: bool = False


@dataclass(slots=True, frozen=True)
class HeldSpaceBlob:
    """A complete space file held until its row arrives.

    ``key`` names its folder under ``media_dir/.partial``; ``sender`` is the
    household whose chunks assembled it (authenticated, never a payload
    field); ``space_id`` / ``correlation_id`` / ``filename`` are what it was
    sent for.
    """

    key: str
    sender: str
    space_id: str
    correlation_id: str
    filename: str

    def to_dict(self) -> dict[str, str]:
        return {
            "sender": self.sender,
            "space_id": self.space_id,
            "correlation_id": self.correlation_id,
            "filename": self.filename,
        }

    @classmethod
    def from_dict(cls, key: str, data: object) -> HeldSpaceBlob | None:
        """Parse a stored record; ``None`` when any field is missing."""
        if not isinstance(data, dict):
            return None
        fields = [data.get(k) for k in ("sender", "space_id", "correlation_id")]
        filename = data.get("filename")
        if not all(isinstance(v, str) and v for v in (*fields, filename)):
            return None
        sender, space_id, correlation_id = fields
        return cls(
            key=key,
            sender=str(sender),
            space_id=str(space_id),
            correlation_id=str(correlation_id),
            filename=str(filename),
        )
