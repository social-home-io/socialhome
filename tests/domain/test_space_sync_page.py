"""``domain/space_sync_page.py`` — the stored page row and what a page adds
to the sync status."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.space_sync_page import (
    StoredSyncPage,
    SyncPagePosition,
    state_with_page,
)
from socialhome.domain.space_sync_status import SpaceSyncState as S


def _page(**over) -> SyncPagePosition:
    fields = {
        "seq": 812,
        "at": "2026-11-21T12:00:00+00:00",
        "signer_role": "owner",
        "applied": None,
        "match": None,
        "differing_buckets": None,
    }
    fields.update(over)
    return SyncPagePosition(**fields)


def test_row_is_frozen_with_an_optional_delta():
    row = StoredSyncPage("sp", "{}", b"\x00" * 8192)
    assert row.delta is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.page_json = "x"  # type: ignore[misc]


def test_no_page_keeps_the_phase_1_state():
    for state in S:
        assert state_with_page(state, None) is state


def test_a_stream_running_stays_catching_up():
    assert state_with_page(S.CATCHING_UP, _page(applied=False)) is S.CATCHING_UP
    assert state_with_page(S.CATCHING_UP, _page(applied=True)) is S.CATCHING_UP


@pytest.mark.parametrize("state", [S.HOST_OFFLINE, S.NEVER, S.BEHIND, S.UP_TO_DATE])
def test_up_to_date_as_of_the_page_even_while_the_host_is_offline(state):
    assert state_with_page(state, _page(applied=True)) is S.UP_TO_DATE
    assert state_with_page(state, _page(match=True)) is S.UP_TO_DATE


def test_a_newer_page_than_we_applied_is_behind_or_catching_up():
    newer = _page(applied=False)
    assert state_with_page(S.HOST_OFFLINE, newer) is S.BEHIND
    assert state_with_page(S.NEVER, newer) is S.BEHIND
    assert state_with_page(S.BEHIND, newer) is S.BEHIND
    assert state_with_page(S.UP_TO_DATE, newer) is S.CATCHING_UP


def test_a_compare_decides_needs_sync_or_up_to_date():
    """Owner decision 2026-11-21: once our rows were compared with the
    newest page, that is the answer — whatever the host's last answer or
    what we applied said."""
    differs = _page(applied=True, match=False, differing_buckets=3)
    for state in (S.HOST_OFFLINE, S.NEVER, S.UP_TO_DATE, S.BEHIND):
        assert state_with_page(state, differs) is S.BEHIND
    matches = _page(applied=False, match=True, differing_buckets=0)
    for state in (S.HOST_OFFLINE, S.NEVER, S.UP_TO_DATE, S.BEHIND):
        assert state_with_page(state, matches) is S.UP_TO_DATE
    assert state_with_page(S.CATCHING_UP, differs) is S.CATCHING_UP
    assert state_with_page(S.HOST_OFFLINE, _page()) is S.HOST_OFFLINE


def test_wire_shape():
    wire = _page(applied=True, match=False, differing_buckets=4).to_wire()
    assert wire == {
        "seq": 812,
        "at": "2026-11-21T12:00:00+00:00",
        "signer_role": "owner",
        "applied": True,
        "match": False,
        "differing_buckets": 4,
        "conflict": False,
    }
