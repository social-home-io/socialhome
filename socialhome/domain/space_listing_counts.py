"""A space's listing counts — members and followers, declared by its host.

Owner decision (2026-11-23): the member and follower counts are part of the
space metadata a household shares, the same way on both paths:

* to a **GFS**, inside the owner-signed listing publish body (only to a
  server that proved the signed ``listing_counts`` capability — an older one
  folds only the fields it knows into the bytes it verifies, so the fields
  would break the signature);
* to a **paired household**, in the ``SPACE_DIRECTORY_SYNC`` snapshot.

``member_count`` is ``space_repo.MEMBER_COUNT_SQL`` (local seats and live
remote seats, never a follower); ``follower_count`` is the live follower
seats, local plus remote non-tombstoned (``FOLLOWER_COUNT_SQL``). A follower
reached only through a GFS is not a roster seat yet, so it is not counted.

A receiver that gets no value (an older household, an older GFS) shows NO
number — never ``0``. Every receiver re-validates with
:func:`parse_listing_count`.

Pure: no I/O. Shared by the household and the GFS.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MAX_LISTING_COUNT",
    "SpaceListingCounts",
    "parse_listing_count",
]

#: The largest count a receiver accepts. Far above any real space; a value
#: past it is a bug or a lie, and is shown as no number.
MAX_LISTING_COUNT = 10_000_000


@dataclass(slots=True, frozen=True)
class SpaceListingCounts:
    """How many members and followers a space has, as its host counts them."""

    member_count: int
    follower_count: int


def parse_listing_count(value: object) -> int | None:
    """A count from the wire, or ``None`` when absent or not a sane count.

    Only a real ``int`` (never a ``bool``, a float or a numeric string) in
    ``0..MAX_LISTING_COUNT`` is accepted.
    """
    if type(value) is not int:
        return None
    if value < 0 or value > MAX_LISTING_COUNT:
        return None
    return value
