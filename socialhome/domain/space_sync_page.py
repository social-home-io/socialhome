"""The sync page a household holds for a space (migration 0151), and what it
adds to the space's sync status.

``docs/protocol/sync-pages.md``. :class:`StoredSyncPage` is the row as
stored: the signed header as canonical JSON, its 8 KiB bucket vector and —
on the signer only — the delta since its previous page. Parsing and
verification live in :mod:`socialhome.sync_page`.

:class:`SyncPagePosition` is what the newest page says about this
household's copy, and :func:`state_with_page` folds it into the Phase-1
state (:mod:`.space_sync_status`). Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from .space_sync_status import SpaceSyncState


@dataclass(slots=True, frozen=True)
class StoredSyncPage:
    """One ``space_sync_pages`` row."""

    space_id: str
    page_json: str
    vector: bytes
    delta: bytes | None = None


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
    state: SpaceSyncState, page: SyncPagePosition | None
) -> SpaceSyncState:
    """The Phase-1 *state* refined by the newest signed *page*.

    * A stream running stays **catching up**.
    * ``match: true`` — nothing the page commits to is missing: **up to
      date**, also while the host is offline (any member may have relayed
      the page).
    * ``match: false`` — a listing proved a row missing: **behind**.
    * Otherwise (not compared yet, pending, unresolved): the Phase-1 state.
    """
    if page is None or state is SpaceSyncState.CATCHING_UP:
        return state
    if page.match is True:
        return SpaceSyncState.UP_TO_DATE
    if page.match is False:
        return SpaceSyncState.BEHIND
    return state


__all__ = ["StoredSyncPage", "SyncPagePosition", "state_with_page"]
