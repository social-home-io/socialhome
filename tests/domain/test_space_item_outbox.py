"""The durable GFS-space item queue's rules (v_97)."""

from socialhome.domain.space_item_outbox import (
    MAX_RETRY_AFTER_S,
    RETRY_BACKOFF_S,
    QueuedSpaceItem,
    UnsentItem,
    backoff_s,
)


def test_backoff_grows_then_repeats_its_last_step_forever():
    assert [backoff_s(n) for n in range(1, len(RETRY_BACKOFF_S) + 3)] == [
        *RETRY_BACKOFF_S,
        RETRY_BACKOFF_S[-1],
        RETRY_BACKOFF_S[-1],
    ]
    assert backoff_s(0) == RETRY_BACKOFF_S[0]


def test_a_longer_retry_after_wins_but_is_capped():
    assert backoff_s(1, retry_after_s=120) == 120
    assert backoff_s(5, retry_after_s=1) == RETRY_BACKOFF_S[-1]
    assert backoff_s(1, retry_after_s=86_400) == MAX_RETRY_AFTER_S


def test_a_row_without_bytes_is_not_retryable():
    row = QueuedSpaceItem(
        item_id="i",
        gfs_id="",
        space_id="sp",
        epoch=0,
        ring_class="durable",
        event_type="t",
        author_user_id="a",
        payload=None,
        next_attempt_at="",
        created_at="",
    )
    assert (row.bytes, row.retryable) == (0, False)


def test_unsent_wire_shape():
    assert UnsentItem("i", "t", "refused", "ts", True, 2).to_wire() == {
        "item_id": "i",
        "event_type": "t",
        "error": "refused",
        "created_at": "ts",
        "retryable": True,
        "count": 2,
    }
