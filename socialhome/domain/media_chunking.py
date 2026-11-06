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

Within those limits a chunk is as large as possible, so a file takes few
messages: a photo up to :data:`HTTPS_CHUNK_BYTES` is one message, a
100 MB video ~200. The old fixed sizes (a 1 MiB single chunk, 512 KiB
pieces) fit no channel and, for a 0.7–1 MiB file, not the inbox either —
such a file never arrived. Receivers accept up to 4096 chunks per file
(``inbound_media_store.MAX_MEDIA_CHUNKS``).

Pure module — no I/O.
"""

from __future__ import annotations

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

#: A chunk to a household reached ONLY through the GFS relay (an
#: invite-link seat): its JSON envelope (~1.8x) is padded and sealed into
#: the relay's top size bucket, 191 KiB of plaintext. Measured with the
#: real encoder: 104 KiB raw is the largest that fits, 108 KiB is not —
#: 100 KiB keeps a margin for longer ids (pinned by
#: ``tests/federation/test_federation_service.py``). Every relay chunk
#: costs that top bucket on the GFS, ~1.9x the raw bytes.
RELAY_CHUNK_BYTES: int = 100 * 1024

#: The largest chunk the HTTPS inbox takes as JSON (≈ 930 KB on the wire
#: of its 1 MiB cap) — also the size between two upgraded households, so
#: a chunk built for the channel still fits the inbox on a fallback.
HTTPS_CHUNK_BYTES: int = 512 * 1024


def media_chunk_bytes(
    channel_max_message_bytes: int | None, *, relay_only: bool = False
) -> int:
    """The chunk size for one household.

    ``channel_max_message_bytes`` is the negotiated message limit of its
    open media data channel, or ``None`` / ``0`` when there is none (the
    chunks go over the HTTPS inbox). Never more than
    :data:`HTTPS_CHUNK_BYTES`, so a chunk sized for the channel still
    fits the inbox if the channel drops mid-transfer. ``relay_only``: a
    household reached only through the GFS relay — :data:`RELAY_CHUNK_BYTES`.
    """
    if relay_only:
        return RELAY_CHUNK_BYTES
    if not channel_max_message_bytes:
        return HTTPS_CHUNK_BYTES
    return max(
        1,
        min(HTTPS_CHUNK_BYTES, channel_max_message_bytes - FRAME_OVERHEAD_BYTES),
    )


def split_into_chunks(data: bytes, chunk_bytes: int) -> list[tuple[dict, bytes]]:
    """``data`` as ``(sequencing, raw_chunk)`` pairs of at most
    ``chunk_bytes`` each — one pair when it fits (the receiver's
    single-chunk branch). ``sequencing`` is ``chunk_index`` /
    ``chunk_count`` / ``final``; the caller adds its correlation ids."""
    size = len(data)
    total = max(1, -(-size // chunk_bytes))
    return [
        (
            {"chunk_index": i, "chunk_count": total, "final": i == total - 1},
            data[i * chunk_bytes : (i + 1) * chunk_bytes],
        )
        for i in range(total)
    ]
