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
        "match": None,
        "missing": None,
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
    for match in (True, False, None):
        assert state_with_page(S.CATCHING_UP, _page(match=match)) is S.CATCHING_UP


@pytest.mark.parametrize("state", [S.HOST_OFFLINE, S.NEVER, S.BEHIND, S.UP_TO_DATE])
def test_nothing_missing_is_up_to_date_even_while_the_host_is_offline(state):
    assert state_with_page(state, _page(match=True, missing=0)) is S.UP_TO_DATE


@pytest.mark.parametrize("state", [S.HOST_OFFLINE, S.NEVER, S.BEHIND, S.UP_TO_DATE])
def test_a_proven_missing_row_is_behind(state):
    assert state_with_page(state, _page(match=False, missing=2)) is S.BEHIND


@pytest.mark.parametrize("state", list(S))
def test_not_compared_or_unresolved_keeps_the_phase_1_state(state):
    """Review finding 3: no verdict → the answer-based state, never a
    guess of behind or catching up."""
    assert state_with_page(state, _page(match=None, differing_buckets=4)) is state


def test_wire_shape():
    wire = _page(match=False, missing=1, differing_buckets=4).to_wire()
    assert wire == {
        "seq": 812,
        "at": "2026-11-21T12:00:00+00:00",
        "signer_role": "owner",
        "match": False,
        "missing": 1,
        "differing_buckets": 4,
        "conflict": False,
    }
