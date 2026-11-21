"""The pure "does this space need sync?" rule (:func:`sync_state`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from socialhome.domain.space_sync_status import (
    SpaceSyncOutcome,
    SpaceSyncResult,
    SpaceSyncState,
    SpaceSyncStatus,
    sync_state,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def _o(host: str, result: SpaceSyncResult | None, ok_min: float | None):
    return SpaceSyncOutcome(
        instance_id=host,
        result=result,
        ok_at=None if ok_min is None else _ago(ok_min),
    )


def test_an_ok_answer_within_the_hour_is_up_to_date():
    state, last_ok = sync_state([_o("h", SpaceSyncResult.OK, 20)], now=NOW)
    assert state is SpaceSyncState.UP_TO_DATE
    assert last_ok == _ago(20)


def test_an_ok_answer_stays_up_to_date_up_to_two_hours():
    state, _ = sync_state([_o("h", SpaceSyncResult.OK, 100)], now=NOW)
    assert state is SpaceSyncState.UP_TO_DATE


def test_no_ok_for_over_two_hours_while_the_host_answers_is_behind():
    state, last_ok = sync_state([_o("h", SpaceSyncResult.OK, 130)], now=NOW)
    assert state is SpaceSyncState.BEHIND
    assert last_ok == _ago(130)


def test_an_unclean_last_result_is_behind_even_after_a_recent_ok():
    state, _ = sync_state([_o("h", SpaceSyncResult.UNCLEAN, 10)], now=NOW)
    assert state is SpaceSyncState.BEHIND


def test_a_busy_host_after_a_recent_ok_is_still_up_to_date():
    state, _ = sync_state([_o("h", SpaceSyncResult.DEFERRED, 10)], now=NOW)
    assert state is SpaceSyncState.UP_TO_DATE


def test_a_busy_host_with_an_older_ok_is_behind():
    state, _ = sync_state([_o("h", SpaceSyncResult.DEFERRED, 90)], now=NOW)
    assert state is SpaceSyncState.BEHIND


def test_a_running_stream_is_catching_up_unless_already_up_to_date():
    state, _ = sync_state(
        [_o("h", SpaceSyncResult.UNCLEAN, 10)], receiving=True, now=NOW
    )
    assert state is SpaceSyncState.CATCHING_UP
    state, _ = sync_state([_o("h", None, None)], receiving=True, now=NOW)
    assert state is SpaceSyncState.CATCHING_UP
    state, _ = sync_state([_o("h", SpaceSyncResult.OK, 10)], receiving=True, now=NOW)
    assert state is SpaceSyncState.UP_TO_DATE


def test_no_acting_host_reachable_is_host_offline_with_the_last_ok():
    state, last_ok = sync_state(
        [_o("owner", SpaceSyncResult.UNREACHABLE, 300), _o("admin", None, None)],
        now=NOW,
    )
    assert state is SpaceSyncState.HOST_OFFLINE
    assert last_ok == _ago(300)


def test_a_delegated_admin_answering_keeps_the_space_up_to_date():
    state, _ = sync_state(
        [
            _o("owner", SpaceSyncResult.UNREACHABLE, 300),
            _o("admin", SpaceSyncResult.OK, 5),
        ],
        now=NOW,
    )
    assert state is SpaceSyncState.UP_TO_DATE


def test_one_host_answering_not_ok_while_the_other_is_silent_is_behind():
    state, last_ok = sync_state(
        [
            _o("owner", SpaceSyncResult.UNREACHABLE, None),
            _o("admin", SpaceSyncResult.UNCLEAN, 400),
        ],
        now=NOW,
    )
    assert state is SpaceSyncState.BEHIND
    assert last_ok == _ago(400)


def test_never_answered_is_never():
    state, last_ok = sync_state([_o("h", None, None)], now=NOW)
    assert state is SpaceSyncState.NEVER
    assert last_ok is None
    assert sync_state([], now=NOW)[0] is SpaceSyncState.NEVER


def test_answered_but_never_ok_is_never():
    state, _ = sync_state([_o("h", SpaceSyncResult.DEFERRED, None)], now=NOW)
    assert state is SpaceSyncState.NEVER


def test_an_unparseable_ok_time_counts_as_none():
    outcome = SpaceSyncOutcome(instance_id="h", result=SpaceSyncResult.OK, ok_at="junk")
    assert sync_state([outcome], now=NOW)[0] is SpaceSyncState.NEVER


def test_a_naive_sqlite_time_is_read_as_utc():
    naive = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    outcome = SpaceSyncOutcome(instance_id="h", result=SpaceSyncResult.OK, ok_at=naive)
    assert sync_state([outcome], now=NOW)[0] is SpaceSyncState.UP_TO_DATE


def test_the_wire_shape():
    status = SpaceSyncStatus(
        state=SpaceSyncState.BEHIND,
        last_ok_at="2026-11-21T10:00:00+00:00",
        waiting_to_send=3,
    )
    assert status.to_wire() == {
        "state": "behind",
        "last_ok_at": "2026-11-21T10:00:00+00:00",
        "waiting_to_send": 3,
    }


def test_parse_result_refuses_unknown_values():
    assert SpaceSyncResult.parse("ok") is SpaceSyncResult.OK
    assert SpaceSyncResult.parse("bogus") is None
    assert SpaceSyncResult.parse(None) is None


def test_an_unclean_first_stream_is_behind_not_never():
    state, last_ok = sync_state([_o("h", SpaceSyncResult.UNCLEAN, None)], now=NOW)
    assert state is SpaceSyncState.BEHIND
    assert last_ok is None
