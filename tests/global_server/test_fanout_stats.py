"""Tests for :mod:`socialhome.global_server.fanout_stats`."""

from __future__ import annotations

import pytest

from socialhome.domain.space_item import ITEM_SIZE_BUCKETS
from socialhome.global_server.fanout_stats import (
    LARGER_CLASS,
    MAX_KINDS,
    OTHER_KIND,
    SIZE_CLASSES,
    FanoutStats,
    frame_kind,
    size_class,
)


def test_size_classes_follow_the_padding_buckets() -> None:
    assert [name for name, _ in SIZE_CLASSES] == [
        f"{b // 1024}k" for b in ITEM_SIZE_BUCKETS
    ]
    ceilings = [c for _, c in SIZE_CLASSES]
    assert ceilings == sorted(ceilings)


@pytest.mark.parametrize(
    ("frame_bytes", "expected"),
    [
        (300, "1k"),
        (1700, "1k"),  # a 1 KiB item: ~1.4 KiB base64 + routing fields
        (5823, "4k"),  # today's 300-character public post
        (22207, "16k"),  # a long CJK post under the old encoding
        (10**7, LARGER_CLASS),
    ],
)
def test_size_class(frame_bytes: int, expected: str) -> None:
    assert size_class(frame_bytes) == expected


def test_frame_kind_names_type_and_event() -> None:
    assert frame_kind({"type": "relay", "event_type": "space_item"}) == (
        "relay:space_item"
    )
    assert frame_kind({"type": "envelope"}) == "envelope"
    assert frame_kind({"event_type": 7}) == "untyped"
    assert len(frame_kind({"type": "x" * 500})) == 64


def test_record_and_snapshot() -> None:
    stats = FanoutStats()
    stats.record("relay:space_item", 5823)
    stats.record("relay:space_item", 5823)
    stats.record("envelope", 4000)
    snap = stats.snapshot()
    assert snap["total"] == {"frames": 3, "bytes": 15646}
    assert snap["by_kind"] == {
        "envelope": {"frames": 1, "bytes": 4000},
        "relay:space_item": {"frames": 2, "bytes": 11646},
    }
    assert snap["by_size"] == {"4k": {"frames": 3, "bytes": 15646}}


def test_kinds_are_bounded() -> None:
    """A publisher inventing event types cannot grow the table without
    bound: past :data:`MAX_KINDS` every new kind counts as ``other``."""
    stats = FanoutStats()
    for i in range(MAX_KINDS + 10):
        stats.record(f"relay:e{i}", 100)
    by_kind = stats.snapshot()["by_kind"]
    assert len(by_kind) == MAX_KINDS + 1
    assert by_kind[OTHER_KIND]["frames"] == 10
    # A kind already counted keeps its own row.
    stats.record("relay:e0", 100)
    assert stats.snapshot()["by_kind"]["relay:e0"]["frames"] == 2
