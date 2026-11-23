"""The pure "does this space need sync?" rule (:func:`sync_state`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from socialhome.domain.space_sync_status import (
    SpaceSyncOutcome,
    SpaceSyncResult,
    SpaceSyncState,
    SpaceSyncStatus,
    may_be_missing,
    sync_state,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def _o(
    host: str,
    result: SpaceSyncResult | None,
    ok_min: float | None,
    at_min: float | None = None,
):
    return SpaceSyncOutcome(
        instance_id=host,
        result=result,
        ok_at=None if ok_min is None else _ago(ok_min),
        result_at=None if at_min is None else _ago(at_min),
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
            _o("admin", SpaceSyncResult.UNCLEAN, 400, at_min=20),
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
    outcome = SpaceSyncOutcome(
        instance_id="h", result=SpaceSyncResult.OK, ok_at="junk", result_at=None
    )
    assert sync_state([outcome], now=NOW)[0] is SpaceSyncState.NEVER


def test_a_naive_sqlite_time_is_read_as_utc():
    naive = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    outcome = SpaceSyncOutcome(
        instance_id="h", result=SpaceSyncResult.OK, ok_at=naive, result_at=None
    )
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
        "may_be_missing": False,
        "unsent_posts": [],
    }


def test_the_wire_shape_carries_the_warning_and_the_unsent_posts():
    status = SpaceSyncStatus(
        state=SpaceSyncState.BEHIND,
        last_ok_at=None,
        waiting_to_send=1,
        may_be_missing=True,
        unsent_posts=("p1", "p2"),
    )
    wire = status.to_wire()
    assert wire["may_be_missing"] is True
    assert wire["unsent_posts"] == ["p1", "p2"]


def test_parse_result_refuses_unknown_values():
    assert SpaceSyncResult.parse("ok") is SpaceSyncResult.OK
    assert SpaceSyncResult.parse("bogus") is None
    assert SpaceSyncResult.parse(None) is None


def test_an_unclean_first_stream_is_behind_not_never():
    state, last_ok = sync_state([_o("h", SpaceSyncResult.UNCLEAN, None)], now=NOW)
    assert state is SpaceSyncState.BEHIND
    assert last_ok is None


# ── A host that stopped answering (stale last result) ─────────────────────


def test_a_stale_ok_from_a_host_gone_quiet_is_host_offline():
    """The owner was found unreachable; the delegated admin's last answer,
    an ``ok`` 90 minutes ago, is older than two rounds: nobody answers —
    host offline, not up to date."""
    state, last_ok = sync_state(
        [
            _o("owner", SpaceSyncResult.UNREACHABLE, 300, at_min=5),
            _o("admin", SpaceSyncResult.OK, 90, at_min=90),
        ],
        now=NOW,
    )
    assert state is SpaceSyncState.HOST_OFFLINE
    assert last_ok == _ago(90)


def test_a_stale_refusal_or_unclean_answer_is_no_answer_either():
    for result in (SpaceSyncResult.DEFERRED, SpaceSyncResult.UNCLEAN):
        state, _ = sync_state(
            [
                _o("owner", SpaceSyncResult.UNREACHABLE, None, at_min=5),
                _o("admin", result, 400, at_min=70),
            ],
            now=NOW,
        )
        assert state is SpaceSyncState.HOST_OFFLINE, result


def test_a_fresh_answer_keeps_a_space_with_a_silent_host_online():
    state, _ = sync_state(
        [
            _o("owner", SpaceSyncResult.UNREACHABLE, None, at_min=5),
            _o("admin", SpaceSyncResult.DEFERRED, 400, at_min=40),
        ],
        now=NOW,
    )
    assert state is SpaceSyncState.BEHIND


def test_a_stream_arriving_beats_host_offline():
    state, _ = sync_state(
        [_o("owner", SpaceSyncResult.UNREACHABLE, 300, at_min=5)],
        receiving=True,
        now=NOW,
    )
    assert state is SpaceSyncState.CATCHING_UP


def test_an_ok_without_a_result_time_answered_at_its_ok_time():
    """A row written before ``sync_result_at`` was kept reads its ``ok``
    time as the time of its answer."""
    state, _ = sync_state(
        [
            _o("owner", SpaceSyncResult.UNREACHABLE, None, at_min=5),
            _o("admin", SpaceSyncResult.OK, 20),
        ],
        now=NOW,
    )
    assert state is SpaceSyncState.UP_TO_DATE


# ─── "Some posts may be missing" (may_be_missing) ────────────────────────


def _missing(rows, *, proven=False, since=None):
    state, _ = sync_state(rows, now=NOW)
    return may_be_missing(state, rows, proven_missing=proven, since=since)


def _joined(outcome, minutes: float):
    """*outcome* with its seat written *minutes* ago (SQLite's naive UTC)."""
    at = (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    return SpaceSyncOutcome(
        instance_id=outcome.instance_id,
        result=outcome.result,
        ok_at=outcome.ok_at,
        result_at=outcome.result_at,
        joined_at=at,
    )


def test_one_failed_round_after_an_ok_is_not_yet_missing():
    # ok 35 min ago, the round after it unclean (5 min ago): one failure.
    rows = [_o("h", SpaceSyncResult.UNCLEAN, 35, at_min=5)]
    assert sync_state(rows, now=NOW)[0] is SpaceSyncState.BEHIND
    assert _missing(rows) is False


def test_the_second_failed_round_after_an_ok_may_be_missing():
    # ok 65 min ago, failed at 35 and now at 5: two rounds without an ok.
    rows = [_o("h", SpaceSyncResult.UNCLEAN, 65, at_min=5)]
    assert _missing(rows) is True


def test_a_host_refusing_for_two_rounds_may_be_missing():
    rows = [_o("h", SpaceSyncResult.DEFERRED, 70, at_min=10)]
    assert sync_state(rows, now=NOW)[0] is SpaceSyncState.BEHIND
    assert _missing(rows) is True


def test_up_to_date_is_never_missing():
    assert _missing([_o("h", SpaceSyncResult.OK, 10, at_min=10)]) is False


def test_an_away_host_is_never_missing_without_a_proof():
    rows = [_o("h", SpaceSyncResult.UNREACHABLE, 300, at_min=5)]
    assert sync_state(rows, now=NOW)[0] is SpaceSyncState.HOST_OFFLINE
    assert _missing(rows) is False


def test_never_synced_is_not_missing():
    assert _missing([_o("h", None, None)]) is False


def test_a_first_unclean_stream_after_joining_is_no_warning():
    """No ``ok`` ever: a first unclean stream right after joining is the
    space still getting ready, not "posts may be missing"."""
    row = _joined(_o("h", SpaceSyncResult.UNCLEAN, None, at_min=5), 10)
    assert _missing([row]) is False


def test_no_ok_ever_and_two_failed_rounds_since_joining_may_be_missing():
    row = _joined(_o("h", SpaceSyncResult.UNCLEAN, None, at_min=5), 65)
    assert _missing([row]) is True


def test_no_ok_ever_and_no_join_time_is_no_warning():
    assert _missing([_o("h", SpaceSyncResult.UNCLEAN, None, at_min=5)]) is False


def test_one_failed_answer_after_our_own_downtime_is_no_warning():
    """ok 3 h ago, we were down (an HA restart), one unclean answer now:
    one failed round after resuming, not two."""
    rows = [_o("h", SpaceSyncResult.UNCLEAN, 180, at_min=1)]
    assert _missing(rows) is True  # without our start time it would warn
    assert _missing(rows, since=NOW - timedelta(minutes=5)) is False


def test_two_failed_rounds_after_resuming_may_be_missing():
    rows = [_o("h", SpaceSyncResult.UNCLEAN, 180, at_min=1)]
    assert _missing(rows, since=NOW - timedelta(minutes=50)) is True


def test_a_proven_missing_row_is_missing_whatever_the_rounds_say():
    rows = [_o("h", SpaceSyncResult.OK, 10, at_min=10)]
    state = SpaceSyncState.BEHIND  # what a page with match false reads
    assert may_be_missing(state, rows, proven_missing=True) is True


def test_a_running_stream_is_not_missing_even_with_a_proof():
    rows = [_o("h", SpaceSyncResult.UNCLEAN, 300, at_min=5)]
    assert sync_state(rows, receiving=True, now=NOW)[0] is (SpaceSyncState.CATCHING_UP)
    assert (
        may_be_missing(SpaceSyncState.CATCHING_UP, rows, proven_missing=True) is False
    )


def test_the_newest_answer_of_any_acting_host_counts():
    # The owner failed long ago; the delegated admin answered unclean now.
    rows = [
        _o("owner", SpaceSyncResult.UNREACHABLE, 90, at_min=40),
        _o("admin", SpaceSyncResult.UNCLEAN, None, at_min=2),
    ]
    assert sync_state(rows, now=NOW)[0] is SpaceSyncState.BEHIND
    assert _missing(rows) is True
