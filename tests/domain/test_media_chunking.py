"""How big one media chunk may be for a given household (§ media sizing).

Every chunk must fit every tier that may carry it: a binary frame on the
WebRTC data channel (the channel's negotiated message limit), and the
JSON fallback over the HTTPS inbox (1 MiB body cap, the chunk inflated
~1.8x by two base64 passes). Within those, a chunk is as large as
possible — few messages per file.
"""

from collections.abc import Iterator

import pytest

from socialhome.domain.media_chunking import (
    ADVERTISED_MAX_MESSAGE_BYTES,
    FRAME_OVERHEAD_BYTES,
    HTTPS_CHUNK_BYTES,
    JSON_EXPANSION,
    MAX_MEDIA_CHUNKS,
    MIN_CHANNEL_CHUNK_BYTES,
    fits_channel,
    media_chunk_bytes,
    split_into_chunks,
)
from socialhome.hardening import DEFAULT_JSON_MAX_BYTES
from socialhome.services import inbound_media_store

#: libdatachannel's default message limit — an older household's.
DEFAULT_RTC_LIMIT = 256 * 1024


def test_no_channel_uses_the_https_size_which_fits_the_inbox():
    assert media_chunk_bytes(None, file_size_bytes=0) == HTTPS_CHUNK_BYTES
    assert (
        HTTPS_CHUNK_BYTES * JSON_EXPANSION + FRAME_OVERHEAD_BYTES
        <= DEFAULT_JSON_MAX_BYTES
    )


def test_two_upgraded_households_send_the_https_size_over_the_channel():
    """Both advertise 1 MiB: a chunk as big as the inbox allows also fits
    the channel, so a fallback never needs a different size."""
    size = media_chunk_bytes(ADVERTISED_MAX_MESSAGE_BYTES, file_size_bytes=0)
    assert size == HTTPS_CHUNK_BYTES
    assert size + FRAME_OVERHEAD_BYTES <= ADVERTISED_MAX_MESSAGE_BYTES


def test_an_older_household_gets_chunks_its_channel_takes():
    """A 256 KiB channel — the limit a 680 KB picture sent whole broke
    ("Message size exceeds limit")."""
    size = media_chunk_bytes(DEFAULT_RTC_LIMIT, file_size_bytes=0)
    assert size + FRAME_OVERHEAD_BYTES <= DEFAULT_RTC_LIMIT
    # Not needlessly small: within one frame overhead of the limit.
    assert size >= DEFAULT_RTC_LIMIT - 2 * FRAME_OVERHEAD_BYTES


@pytest.mark.parametrize(
    "limit",
    [
        1,
        1024,
        FRAME_OVERHEAD_BYTES,
        16384,
        MIN_CHANNEL_CHUNK_BYTES + FRAME_OVERHEAD_BYTES - 1,
    ],
)
def test_a_tiny_channel_limit_falls_back_to_the_https_size(limit):
    """Regression: ``a=max-message-size:1`` gave 1-byte chunks — a
    200 MiB file became 200 million chunks (the sender ran out of memory)
    and more than the receiver's chunk cap (the file never arrived). A
    channel that can't carry :data:`MIN_CHANNEL_CHUNK_BYTES` is not used
    for media: the chunks are sized for the HTTPS inbox and go over it."""
    size = media_chunk_bytes(limit, file_size_bytes=0)
    assert size == HTTPS_CHUNK_BYTES
    assert not fits_channel(size, limit)  # → the sender takes HTTPS


def test_the_floor_itself_is_still_carried_by_the_channel():
    limit = MIN_CHANNEL_CHUNK_BYTES + FRAME_OVERHEAD_BYTES
    assert media_chunk_bytes(limit, file_size_bytes=0) == MIN_CHANNEL_CHUNK_BYTES
    assert fits_channel(MIN_CHANNEL_CHUNK_BYTES, limit)


def test_unknown_limit_uses_the_https_size():
    assert media_chunk_bytes(0, file_size_bytes=0) == HTTPS_CHUNK_BYTES


def test_a_200_mib_file_at_a_64_kib_limit_stays_under_the_chunk_cap():
    file_size = 200 * 1024 * 1024
    size = media_chunk_bytes(64 * 1024, file_size_bytes=file_size)
    assert -(-file_size // size) <= MAX_MEDIA_CHUNKS


def test_a_file_too_big_for_the_channel_floor_goes_over_https():
    """A channel just over the floor still can't carry 300 MiB in
    4096 chunks — the chunk would have to exceed the channel, so the file
    goes over HTTPS in inbox-sized chunks."""
    limit = MIN_CHANNEL_CHUNK_BYTES + FRAME_OVERHEAD_BYTES
    file_size = 300 * 1024 * 1024
    size = media_chunk_bytes(limit, file_size_bytes=file_size)
    assert size == HTTPS_CHUNK_BYTES
    assert not fits_channel(size, limit)
    assert -(-file_size // size) <= MAX_MEDIA_CHUNKS


@pytest.mark.parametrize(
    "limit", [None, 1, 16384, 65536, 81920, 100_000, 262144, 1024 * 1024]
)
@pytest.mark.parametrize(
    "file_size", [0, 1, 700_000, 100 * 1024 * 1024, 200 * 1024 * 1024, 1 << 30]
)
def test_no_channel_limit_or_file_size_breaks_the_chunk_cap(limit, file_size):
    size = media_chunk_bytes(limit, file_size_bytes=file_size)
    assert size >= MIN_CHANNEL_CHUNK_BYTES
    assert max(1, -(-file_size // size)) <= MAX_MEDIA_CHUNKS


def test_the_receiver_cap_is_the_domain_constant():
    assert inbound_media_store.MAX_MEDIA_CHUNKS == MAX_MEDIA_CHUNKS


def test_split_into_chunks_is_lazy():
    """The sender never holds every chunk at once."""
    chunks = split_into_chunks(b"x" * 10_000_000, 1)
    assert isinstance(chunks, Iterator)
    seq, raw = next(chunks)
    assert seq == {"chunk_index": 0, "chunk_count": 10_000_000, "final": False}
    assert raw == b"x"


def test_a_700_kb_photo_is_two_messages_between_upgraded_households():
    size = media_chunk_bytes(ADVERTISED_MAX_MESSAGE_BYTES, file_size_bytes=0)
    assert -(-700_000 // size) == 2


def test_split_into_chunks_keeps_every_byte_in_order():
    data = bytes(range(256)) * 10  # 2560 bytes
    pieces = list(split_into_chunks(data, 1000))
    assert [len(p[1]) for p in pieces] == [1000, 1000, 560]
    assert [p[0] for p in pieces] == [
        {"chunk_index": 0, "chunk_count": 3, "final": False},
        {"chunk_index": 1, "chunk_count": 3, "final": False},
        {"chunk_index": 2, "chunk_count": 3, "final": True},
    ]
    assert b"".join(p[1] for p in pieces) == data


def test_a_file_that_fits_is_one_chunk():
    assert list(split_into_chunks(b"abc", 1000)) == [
        ({"chunk_index": 0, "chunk_count": 1, "final": True}, b"abc")
    ]
    # An empty file is one empty chunk (the receiver's single-chunk branch).
    assert list(split_into_chunks(b"", 1000)) == [
        ({"chunk_index": 0, "chunk_count": 1, "final": True}, b"")
    ]


def test_the_file_size_is_a_required_keyword():
    """No silent 0: a caller that forgets the file size would skip the
    chunk-cap floor."""
    with pytest.raises(TypeError):
        media_chunk_bytes(ADVERTISED_MAX_MESSAGE_BYTES)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        media_chunk_bytes(ADVERTISED_MAX_MESSAGE_BYTES, 0)  # type: ignore[misc]
