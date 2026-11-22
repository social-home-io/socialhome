"""The sync page a household holds for a space (migration 0151), and what it
adds to the space's sync status.

``docs/protocol/sync-pages.md``. :class:`StoredSyncPage` is the row as
stored: the signed header as canonical JSON and its 8 KiB bucket vector.
Parsing and verification live in :mod:`socialhome.sync_page`.

:class:`SyncPagePosition` is what the newest page says about this
household's copy, and :func:`state_with_page` folds it into the Phase-1
state (:mod:`.space_sync_status`). Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .space_sync_status import SpaceSyncState, parse_utc

#: A match counts as "up to date" for this long after its page was sealed:
#: the hourly push interval + one 30-min tick (a page that waited for its
#: hour mark goes out on the next tick) + the 15-min seal lag, plus 15 min
#: slack — 2 h, so an active space's members, pushed a newer page at most
#: every hour and a half, keep their match between pushes (final review
#: minor 3; widened from 75 min for the push). Past it, while the host still
#: answers, the answers decide — an old match must not mask a host
#: answering ``deferred`` / unclean for hours. While the host is offline no
#: newer page comes from it, so the newest page stands whatever its age.
PAGE_MATCH_FRESH_S: float = 60 * 60 + 30 * 60 + 15 * 60 + 15 * 60


@dataclass(slots=True, frozen=True)
class StoredSyncPage:
    """One ``space_sync_pages`` row."""

    space_id: str
    page_json: str
    vector: bytes


@dataclass(slots=True, frozen=True)
class SyncPagePosition:
    """Where this household stands against the newest page it holds.

    ``seq`` / ``at`` — the page's number (the signer's change counter) and
    when it was sealed (UTC ISO 8601). ``signer_role`` — ``owner`` or
    ``admin`` (a seed-holding delegated admin; a label). ``match`` — nothing
    the page commits to is missing here (``False`` only when a bucket
    listing PROVED a row missing; ``None`` while not compared, a listing is
    pending, or a bucket stayed unresolved). ``missing`` — the rows proven
    missing (``0`` with a match). ``differing_buckets`` — how many buckets
    differ from the page at all (being ahead included). ``conflict`` — the
    signer equivocated (two pages that cannot both be honest were seen)."""

    seq: int
    at: str
    signer_role: str
    match: bool | None
    missing: int | None
    differing_buckets: int | None
    conflict: bool = False

    def to_wire(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "signer_role": self.signer_role,
            "match": self.match,
            "missing": self.missing,
            "differing_buckets": self.differing_buckets,
            "conflict": self.conflict,
        }


def state_with_page(
    state: SpaceSyncState, page: SyncPagePosition | None, *, now: datetime
) -> SpaceSyncState:
    """The Phase-1 *state* refined by the newest signed *page*.

    * A stream running stays **catching up**.
    * ``match: true`` — nothing the page commits to is missing: **up to
      date**, also while the host is offline (any member may have relayed
      the page); otherwise only while the page is at most
      :data:`PAGE_MATCH_FRESH_S` old at *now*.
    * ``match: false`` — a listing proved a row missing: **behind**.
    * Otherwise (not compared yet, pending, unresolved, an old match): the
      Phase-1 state.
    """
    if page is None or state is SpaceSyncState.CATCHING_UP:
        return state
    if page.match is True:
        at = parse_utc(page.at)
        fresh = at is not None and now - at <= timedelta(seconds=PAGE_MATCH_FRESH_S)
        if state is SpaceSyncState.HOST_OFFLINE or fresh:
            return SpaceSyncState.UP_TO_DATE
        return state
    if page.match is False:
        return SpaceSyncState.BEHIND
    return state


__all__ = [
    "PAGE_MATCH_FRESH_S",
    "StoredSyncPage",
    "SyncPagePosition",
    "state_with_page",
]
