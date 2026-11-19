"""``stream_health``: the per-stream tally of a §25.6 sync on the receiving
side, unit-tested on its own (the chunk path drives it in
``test_receiver.py``)."""

from __future__ import annotations

from socialhome.federation.sync.space.stream_health import (
    HELD_BACK_LIMIT,
    MAX_TRACKED_STREAMS,
    PERSIST_FAILURE_LIMIT,
    HeldBack,
    HeldBackStreaks,
    StreamHealth,
    StreamVerdict,
)


def test_held_back_counts_and_fails():
    held = HeldBack()
    assert held.count == 0 and held.failed is False
    held.add()
    held.add(2)
    held.fail()
    assert held.count == 3 and held.failed is True


def test_every_numbered_chunk_stored_is_clean():
    health = StreamHealth()
    health.applied("s", 0)
    health.applied("s", 1)
    assert health.finish("s", 2) is StreamVerdict.CLEAN


def test_a_duplicate_index_does_not_fill_a_lost_chunk():
    health = StreamHealth()
    health.applied("s", 0)
    health.applied("s", 0)
    assert health.finish("s", 2) is StreamVerdict.UNCLEAN


def test_a_stream_without_a_count_is_unclean():
    health = StreamHealth()
    health.applied("s")
    assert health.finish("s", None) is StreamVerdict.UNCLEAN
    assert health.finish("t", True) is StreamVerdict.UNCLEAN


def test_unnumbered_and_opaque_chunks_count():
    health = StreamHealth()
    health.applied("s")
    health.opaque("s")
    assert health.finish("s", 2) is StreamVerdict.CLEAN


def test_held_persist_failed_failed_and_pending_verdicts():
    health = StreamHealth()
    health.held("held", 0)
    assert health.finish("held", 1) is StreamVerdict.HELD_BACK
    health.persist_failed("pf", 0)
    assert health.finish("pf", 1) is StreamVerdict.PERSIST_FAILED
    health.applied("bad", 0)
    health.failed("bad")
    assert health.finish("bad", 1) is StreamVerdict.UNCLEAN
    health.applied("wait", 0)
    health.stashed("wait")
    assert health.finish("wait", 1) is StreamVerdict.UNCLEAN


def test_a_resolved_stash_no_longer_blocks():
    health = StreamHealth()
    health.stashed("s")
    health.resolved("s")
    health.applied("s", 0)
    assert health.finish("s", 1) is StreamVerdict.CLEAN


def test_a_finished_stream_opens_no_new_entry():
    health = StreamHealth()
    health.applied("s", 0)
    health.finish("s", 1)
    assert health.begun("s")
    health.applied("s", 1)
    assert health.finish("s", 1) is StreamVerdict.UNCLEAN


def test_archive_changed_compares_with_the_first_chunk():
    health = StreamHealth()
    assert health.archive_changed("s", archived=True) is False
    health.begin("s", archived=False)
    health.begin("s", archived=True)  # only the first chunk's flag counts
    assert health.archive_changed("s", archived=False) is False
    assert health.archive_changed("s", archived=True) is True


def test_tracked_streams_are_bounded():
    health = StreamHealth()
    for i in range(MAX_TRACKED_STREAMS + 5):
        health.applied(f"s-{i}", 0)
    assert not health.begun("s-0")
    assert health.begun(f"s-{MAX_TRACKED_STREAMS + 4}")


def test_clean_is_clean_and_unclean_is_unclean():
    streaks = HeldBackStreaks()
    assert streaks.clean(StreamVerdict.CLEAN, "sp", "p") is True
    assert streaks.clean(StreamVerdict.UNCLEAN, "sp", "p") is False


def test_held_back_runs_are_bounded(caplog):
    streaks = HeldBackStreaks()
    verdicts = [
        streaks.clean(StreamVerdict.HELD_BACK, "sp", "p")
        for _ in range(HELD_BACK_LIMIT)
    ]
    assert verdicts == [False] * (HELD_BACK_LIMIT - 1) + [True]
    assert "held records back that never landed" in caplog.text


def test_persist_failure_runs_are_bounded_and_broken_by_another_verdict():
    streaks = HeldBackStreaks()
    for _ in range(PERSIST_FAILURE_LIMIT - 1):
        assert streaks.clean(StreamVerdict.PERSIST_FAILED, "sp", "p") is False
    # Another verdict breaks the run: it starts over.
    streaks.clean(StreamVerdict.HELD_BACK, "sp", "p")
    assert streaks.clean(StreamVerdict.PERSIST_FAILED, "sp", "p") is False
