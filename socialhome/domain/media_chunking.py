"""How big one media chunk may be for a given household.

A file (a space picture, a DM attachment, a calendar cover) travels as
``SPACE_MEDIA_BLOB`` / ``DM_MEDIA_BLOB`` chunks. Every chunk must fit
every tier that may carry it:

* **WebRTC data channel** — a binary ``fed-media-v1`` frame is limited by
  the channel's negotiated message size: the *smaller* of the two
  households' advertised ``a=max-message-size``. libdatachannel's default
  is 256 KiB; a household from 2026-11-06 on advertises
  :data:`ADVERTISED_MAX_MESSAGE_BYTES`, so two such households negotiate
  1 MiB and an older one keeps 256 KiB. A bigger frame fails with
  "Message size exceeds limit".
* **HTTPS inbox** — the JSON fallback carries the chunk base64'ed in the
  payload, which is then encrypted and base64'ed again (~1.8x), under the
  inbox's 1 MiB body cap (``hardening.DEFAULT_JSON_MAX_BYTES``).
* **Mesh route** — a space member that is not a CONFIRMED direct peer
  gets the JSON chunk sealed inside ``SPACE_ROUTED``: sealed and
  base64'ed once more before the outer envelope encrypts and base64s it
  (~2.4x), under the *first hop's* inbox cap. Those targets get
  :data:`ROUTED_CHUNK_BYTES` (:func:`routed_media_chunk_bytes`).

Within those limits a chunk is as large as possible, so a file takes few
messages: a photo up to :data:`HTTPS_CHUNK_BYTES` is one message, a
100 MB video ~200. The old fixed sizes (a 1 MiB single chunk, 512 KiB
pieces) fit no channel and, for a 0.7–1 MiB file, not the inbox either —
such a file never arrived. Receivers accept up to
:data:`MAX_MEDIA_CHUNKS` chunks per file; a channel too small to stay
under that (or under :data:`MIN_CHANNEL_CHUNK_BYTES`) is skipped for
the HTTPS inbox.

Pure module — no I/O.
"""

from __future__ import annotations

from collections.abc import Iterator

#: The message size this household advertises on its data channels
#: (``RTCConfiguration.max_message_size``). The negotiated limit is the
#: smaller of both sides', so it only takes effect between two households
#: that both advertise it.
ADVERTISED_MAX_MESSAGE_BYTES: int = 1024 * 1024

#: Room for the signed envelope header and the frame around a chunk.
FRAME_OVERHEAD_BYTES: int = 16 * 1024

#: A chunk on the JSON path: base64 in the payload, then encrypted and
#: base64'ed again.
JSON_EXPANSION: float = 16 / 9

#: The largest chunk the HTTPS inbox takes as JSON (≈ 930 KB on the wire
#: of its 1 MiB cap) — also the size between two upgraded households, so
#: a chunk built for the channel still fits the inbox on a fallback.
HTTPS_CHUNK_BYTES: int = 512 * 1024

#: The largest chunk a ``SPACE_ROUTED`` envelope carries under the first
#: hop's 1 MiB inbox: measured ≈ 934 KB on the wire (512 KiB would be
#: ≈ 1.24 MB). Used for every target that is not a CONFIRMED direct peer
#: (:meth:`FederationService.mesh_media_chunk_bytes`). 4096 of them are
#: 1.5 GiB — above every space / DM media max a receiver accepts.
ROUTED_CHUNK_BYTES: int = 384 * 1024

#: Upper bound on ``chunk_count`` for one transfer — a receiver refuses a
#: chunk claiming more (``inbound_media_store``), so a sender never splits
#: a file into more.
MAX_MEDIA_CHUNKS: int = 4096

#: The smallest chunk a data channel is used for. A household whose
#: channel can't carry this (an ``a=max-message-size`` under
#: ``MIN_CHANNEL_CHUNK_BYTES + FRAME_OVERHEAD_BYTES``, ~80 KiB) gets its
#: media over the HTTPS inbox instead: tiny chunks would turn one video
#: into hundreds of thousands of messages (sender memory) and past
#: :data:`MAX_MEDIA_CHUNKS` (the receiver refuses the file).
MIN_CHANNEL_CHUNK_BYTES: int = 64 * 1024


def fits_channel(chunk_len: int, channel_max_message_bytes: int | None) -> bool:
    """Whether a chunk of ``chunk_len`` bytes plus its frame fits one
    message on a channel with that negotiated limit. ``None`` / ``0``
    (unknown) is not a limit we can check, so it fits — the transport
    falls back on a send error."""
    if not channel_max_message_bytes:
        return True
    return chunk_len + FRAME_OVERHEAD_BYTES <= channel_max_message_bytes


def media_chunk_bytes(
    channel_max_message_bytes: int | None, *, file_size_bytes: int
) -> int:
    """The chunk size for one household.

    ``channel_max_message_bytes`` is the negotiated message limit of its
    open media data channel, or ``None`` / ``0`` when there is none (the
    chunks go over the HTTPS inbox). Never more than
    :data:`HTTPS_CHUNK_BYTES`, so a chunk sized for the channel still
    fits the inbox if the channel drops mid-transfer.

    The channel size is used only when it is at least
    :data:`MIN_CHANNEL_CHUNK_BYTES` and big enough to send a
    ``file_size_bytes`` file in at most :data:`MAX_MEDIA_CHUNKS` chunks;
    otherwise the HTTPS size is returned, which does not fit the channel
    (:func:`fits_channel`), so the sender routes those chunks over HTTPS.
    """
    if not channel_max_message_bytes:
        return HTTPS_CHUNK_BYTES
    channel_chunk = min(
        HTTPS_CHUNK_BYTES, channel_max_message_bytes - FRAME_OVERHEAD_BYTES
    )
    floor = max(MIN_CHANNEL_CHUNK_BYTES, -(-file_size_bytes // MAX_MEDIA_CHUNKS))
    if channel_chunk < floor:
        return HTTPS_CHUNK_BYTES
    return channel_chunk


def routed_media_chunk_bytes(*, file_size_bytes: int) -> int:
    """The chunk size for a target reached over the mesh
    (``SPACE_ROUTED``): :data:`ROUTED_CHUNK_BYTES`, grown only when a
    ``file_size_bytes`` file would otherwise take more than
    :data:`MAX_MEDIA_CHUNKS` chunks (above 1.5 GiB — past every media max
    today, so the receiver refuses such a file anyway)."""
    return max(ROUTED_CHUNK_BYTES, -(-file_size_bytes // MAX_MEDIA_CHUNKS))


def chunk_count(size: int, chunk_bytes: int) -> int:
    """How many chunks of ``chunk_bytes`` a ``size``-byte file takes —
    at least one (an empty file is one empty chunk)."""
    return max(1, -(-size // chunk_bytes))


def split_into_chunks(data: bytes, chunk_bytes: int) -> Iterator[tuple[dict, bytes]]:
    """``data`` as ``(sequencing, raw_chunk)`` pairs of at most
    ``chunk_bytes`` each — one pair when it fits (the receiver's
    single-chunk branch). ``sequencing`` is ``chunk_index`` /
    ``chunk_count`` / ``final``; the caller adds its correlation ids.

    Lazy: each chunk is sliced when it is pulled, so a sender holds only
    the chunks it has in flight."""
    total = chunk_count(len(data), chunk_bytes)
    for i in range(total):
        yield (
            {"chunk_index": i, "chunk_count": total, "final": i == total - 1},
            data[i * chunk_bytes : (i + 1) * chunk_bytes],
        )
