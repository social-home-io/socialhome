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
    ``admin`` (a seed-holding delegated admin). ``applied`` — whether a
    clean stream from the signer at or past the page's ``seq`` was applied
    here (``None``: we do not sync from that signer, so it cannot say).
    ``match`` / ``differing_buckets`` — the last root check of our own rows
    against the page (``None``: not checked yet for this page).
    ``conflict`` — the signer equivocated (two pages that cannot both be
    honest were seen)."""

    seq: int
    at: str
    signer_role: str
    applied: bool | None
    match: bool | None
    differing_buckets: int | None
    conflict: bool = False

    def to_wire(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "signer_role": self.signer_role,
            "applied": self.applied,
            "match": self.match,
            "differing_buckets": self.differing_buckets,
            "conflict": self.conflict,
        }


def state_with_page(
    state: SpaceSyncState, page: SyncPagePosition | None
) -> SpaceSyncState:
    """The Phase-1 *state* refined by the newest signed *page*.

    * A stream running stays **catching up**.
    * We applied the signer's stream at or past the page, or our rows match
      it: **up to date** "as of page N" — also while the host is offline
      (the page may have been relayed by any member).
    * A newer page exists than what we applied from its signer: **catching
      up** while the host answers, **behind** while it is silent.
    * We do not sync from the signer: a checked mismatch makes a silent
      host's space **behind** (never "never" — we know what we lack).
    """
    if page is None or state is SpaceSyncState.CATCHING_UP:
        return state
    if page.applied or page.match:
        return SpaceSyncState.UP_TO_DATE
    if page.applied is False:
        if state is SpaceSyncState.UP_TO_DATE:
            return SpaceSyncState.CATCHING_UP
        return SpaceSyncState.BEHIND
    if page.match is False and state is not SpaceSyncState.UP_TO_DATE:
        return SpaceSyncState.BEHIND
    return state


__all__ = ["StoredSyncPage", "SyncPagePosition", "state_with_page"]
