"""``domain/space_sync_page.py`` — the stored page row and what a page adds
to the sync status."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.space_sync_page import (
    PAGE_MATCH_FRESH_S,
    StoredSyncPage,
    SyncPagePosition,
    state_with_page,
)
from socialhome.domain.space_sync_status import SpaceSyncState as S

#: A minute after the test page (``at`` 12:00) was sealed.
NOW = datetime(2026, 11, 21, 12, 1, tzinfo=UTC)


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


def test_row_is_frozen():
    row = StoredSyncPage("sp", "{}", b"\x00" * 8192)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.page_json = "x"  # type: ignore[misc]


def test_no_page_keeps_the_phase_1_state():
    for state in S:
        assert state_with_page(state, None, now=NOW) is state


def test_a_stream_running_stays_catching_up():
    for match in (True, False, None):
        assert (
            state_with_page(S.CATCHING_UP, _page(match=match), now=NOW) is S.CATCHING_UP
        )


@pytest.mark.parametrize("state", [S.HOST_OFFLINE, S.NEVER, S.BEHIND, S.UP_TO_DATE])
def test_nothing_missing_is_up_to_date_even_while_the_host_is_offline(state):
    assert state_with_page(state, _page(match=True, missing=0), now=NOW) is S.UP_TO_DATE


@pytest.mark.parametrize("state", [S.HOST_OFFLINE, S.NEVER, S.BEHIND, S.UP_TO_DATE])
def test_a_proven_missing_row_is_behind(state):
    assert state_with_page(state, _page(match=False, missing=2), now=NOW) is S.BEHIND


@pytest.mark.parametrize("state", list(S))
def test_not_compared_or_unresolved_keeps_the_phase_1_state(state):
    """Review finding 3: no verdict → the answer-based state, never a
    guess of behind or catching up."""
    assert (
        state_with_page(state, _page(match=None, differing_buckets=4), now=NOW) is state
    )


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


@pytest.mark.parametrize("state", [S.NEVER, S.BEHIND, S.UP_TO_DATE])
def test_an_old_match_does_not_mask_the_answers(state):
    """Final review minor 3: a match against a page sealed longer ago than
    two rounds plus the seal interval says nothing about now — while the
    host still answers (``deferred`` / unclean for hours), the answers
    decide."""
    later = NOW + timedelta(seconds=PAGE_MATCH_FRESH_S)
    assert state_with_page(state, _page(match=True, missing=0), now=later) is state
    fresh = later - timedelta(seconds=120)
    assert (
        state_with_page(state, _page(match=True, missing=0), now=fresh) is S.UP_TO_DATE
    )


def test_an_old_match_stands_while_the_host_is_offline():
    """With the host offline no newer page comes from it: the newest page
    is the best word there is, whatever its age (Phase 2's point)."""
    later = NOW + timedelta(days=2)
    page = _page(match=True, missing=0)
    assert state_with_page(S.HOST_OFFLINE, page, now=later) is S.UP_TO_DATE


def test_a_match_stays_fresh_between_two_hourly_pushes():
    """Sync pages push: a member holds a page sealed up to the push interval
    (1 h) + one 30-min tick (the hour-mark push rides it) + the 15-min seal
    lag before the next push replaces it — 105 min. Its match still counts
    then; only past PAGE_MATCH_FRESH_S do the answers decide."""
    page = _page(match=True, missing=0)
    assert state_with_page(S.BEHIND, page, now=NOW + timedelta(minutes=104)) is (
        S.UP_TO_DATE
    )
