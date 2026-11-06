"""How big one media chunk may be for a given household (§ media sizing).

Every chunk must fit every tier that may carry it: a binary frame on the
WebRTC data channel (the channel's negotiated message limit), and the
JSON fallback over the HTTPS inbox (1 MiB body cap, the chunk inflated
~1.8x by two base64 passes). Within those, a chunk is as large as
possible — few messages per file.
"""

from socialhome.domain.media_chunking import (
    ADVERTISED_MAX_MESSAGE_BYTES,
    FRAME_OVERHEAD_BYTES,
    HTTPS_CHUNK_BYTES,
    JSON_EXPANSION,
    RELAY_CHUNK_BYTES,
    media_chunk_bytes,
    split_into_chunks,
)
from socialhome.hardening import DEFAULT_JSON_MAX_BYTES

#: libdatachannel's default message limit — an older household's.
DEFAULT_RTC_LIMIT = 256 * 1024


def test_no_channel_uses_the_https_size_which_fits_the_inbox():
    assert media_chunk_bytes(None) == HTTPS_CHUNK_BYTES
    assert (
        HTTPS_CHUNK_BYTES * JSON_EXPANSION + FRAME_OVERHEAD_BYTES
        <= DEFAULT_JSON_MAX_BYTES
    )


def test_two_upgraded_households_send_the_https_size_over_the_channel():
    """Both advertise 1 MiB: a chunk as big as the inbox allows also fits
    the channel, so a fallback never needs a different size."""
    size = media_chunk_bytes(ADVERTISED_MAX_MESSAGE_BYTES)
    assert size == HTTPS_CHUNK_BYTES
    assert size + FRAME_OVERHEAD_BYTES <= ADVERTISED_MAX_MESSAGE_BYTES


def test_an_older_household_gets_chunks_its_channel_takes():
    """A 256 KiB channel — the limit a 680 KB picture sent whole broke
    ("Message size exceeds limit")."""
    size = media_chunk_bytes(DEFAULT_RTC_LIMIT)
    assert size + FRAME_OVERHEAD_BYTES <= DEFAULT_RTC_LIMIT
    # Not needlessly small: within one frame overhead of the limit.
    assert size >= DEFAULT_RTC_LIMIT - 2 * FRAME_OVERHEAD_BYTES


def test_a_tiny_or_broken_limit_still_yields_a_usable_chunk():
    assert media_chunk_bytes(0) == HTTPS_CHUNK_BYTES  # unknown → inbox size
    assert 0 < media_chunk_bytes(FRAME_OVERHEAD_BYTES + 1024) <= 1024
    assert media_chunk_bytes(FRAME_OVERHEAD_BYTES) >= 1


def test_a_700_kb_photo_is_two_messages_between_upgraded_households():
    size = media_chunk_bytes(ADVERTISED_MAX_MESSAGE_BYTES)
    assert -(-700_000 // size) == 2


def test_split_into_chunks_keeps_every_byte_in_order():
    data = bytes(range(256)) * 10  # 2560 bytes
    pieces = split_into_chunks(data, 1000)
    assert [len(p[1]) for p in pieces] == [1000, 1000, 560]
    assert [p[0] for p in pieces] == [
        {"chunk_index": 0, "chunk_count": 3, "final": False},
        {"chunk_index": 1, "chunk_count": 3, "final": False},
        {"chunk_index": 2, "chunk_count": 3, "final": True},
    ]
    assert b"".join(p[1] for p in pieces) == data


def test_a_file_that_fits_is_one_chunk():
    assert split_into_chunks(b"abc", 1000) == [
        ({"chunk_index": 0, "chunk_count": 1, "final": True}, b"abc")
    ]
    # An empty file is one empty chunk (the receiver's single-chunk branch).
    assert split_into_chunks(b"", 1000) == [
        ({"chunk_index": 0, "chunk_count": 1, "final": True}, b"")
    ]


def test_a_relay_only_household_gets_relay_sized_chunks_whatever_else_is_known():
    assert media_chunk_bytes(None, relay_only=True) == RELAY_CHUNK_BYTES
    assert (
        media_chunk_bytes(ADVERTISED_MAX_MESSAGE_BYTES, relay_only=True)
        == RELAY_CHUNK_BYTES
    )
    assert RELAY_CHUNK_BYTES < HTTPS_CHUNK_BYTES
