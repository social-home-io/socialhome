"""Tests for ``socialhome.federation.sync.space.exporters.timetables``."""

from __future__ import annotations

import os
from datetime import datetime, time, timezone

import orjson
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import b64url_encode
from socialhome.domain.timetable import (
    MAX_ENTRIES,
    MAX_NAME,
    MAX_WIRE_BYTES,
    Timetable,
    TimetableEntry,
    TimetableValidationError,
    from_wire_dict,
    to_wire_dict,
    validate,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.gfs_relay_transport import (
    RELAY_MAX_ENVELOPE_BYTES,
    RELAY_SIZE_BUCKETS,
    build_relay_plaintext,
)
from socialhome.federation.sync.space.exporter import ChunkBuilder, serialise_chunk
from socialhome.federation.sync.space.exporters import TimetablesExporter


async def test_timetables_exporter_streams_live_wire_dicts():
    """Space timetables ride the chunked sync as their domain wire dict —
    the same shape as the live SPACE_TIMETABLE_UPSERTED event."""
    at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    tt = Timetable(id="tt-1", name="5b", created_by="u-a", created_at=at, updated_at=at)

    class _Repo:
        async def list_by_space(self, space_id):
            assert space_id == "sp-1"
            return [tt]

    exporter = TimetablesExporter(_Repo())
    assert exporter.resource == "timetables"
    [rec] = await exporter.list_records("sp-1")
    assert from_wire_dict(rec) == tt


# ─── Transport fit (M7) ──────────────────────────────────────────────────

#: libdatachannel's SCTP max message size between two libdatachannel peers:
#: each side advertises ``DEFAULT_LOCAL_MAX_MESSAGE_SIZE`` (256 KiB,
#: ``src/impl/internals.hpp``) in its SDP and socialhome never overrides
#: ``RTCConfiguration.max_message_size``; ``DataChannel.send`` refuses a
#: larger message outright, which would kill the sync stream.
_DATACHANNEL_MAX_MESSAGE_BYTES = 256 * 1024
#: An Ed25519 + ML-DSA-65 signature pair, base64 — the largest set a chunk
#: or an envelope can carry.
_SIGS = {"ed25519": "x" * 88, "mldsa65": "y" * 4412}


class _SealLikeTheSpaceKey:
    """``SpaceContentEncryption.encrypt_chunk``'s exact wire shape."""

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        nonce = os.urandom(12)
        ct = AESGCM(os.urandom(32)).encrypt(nonce, plaintext, b"aad")
        return 7, b64url_encode(nonce) + ":" + b64url_encode(ct)


class _OneRecord:
    resource = "timetables"

    def __init__(self, record):
        self._record = record

    async def list_records(self, space_id):
        return [self._record]


def _max_size_record() -> dict:
    """The largest space timetable ``validate`` admits, as its sync record."""
    days = tuple(range(7))
    entries = [
        TimetableEntry(
            id=f"e{wd}-{i}",
            weekday=wd,
            start=time(i // 2, (i % 2) * 30),
            end=time(i // 2, (i % 2) * 30 + 29),
            title="ü" * 60,
            room="ü" * 30,
            teacher="ü" * 60,
            note="ü" * 200,
            label="ü" * 8,
        )
        for wd in days
        for i in range(24)
    ][:MAX_ENTRIES]
    at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    while True:
        tt = Timetable(
            id="a" * 32,
            name="n" * MAX_NAME,
            created_by="u" * 32,
            updated_by="u" * 32,
            created_at=at,
            updated_at=at,
            days=days,
            entries=tuple(entries),
        )
        try:
            validate(tt)
        except TimetableValidationError:
            entries.pop()
            continue
        wire = to_wire_dict(tt)
        assert MAX_WIRE_BYTES - 2048 < len(orjson.dumps(wire)) <= MAX_WIRE_BYTES
        return wire


async def test_a_max_size_timetable_sync_chunk_fits_every_transport():
    """One max-size record is one chunk far above the 8 KiB budget (the
    builder never splits a record). It must still fit the sync DataChannel,
    and — sealed a second time as a ``SPACE_SYNC_CHUNK`` event for the HTTPS
    path — the connection-server relay's envelope cap, which is the only
    transport a link-joined member household has."""
    enc = FederationEncoder(os.urandom(32))
    builder = ChunkBuilder(enc, _SealLikeTheSpaceKey())
    [chunk] = [
        c
        async for c in builder.build_chunks(
            exporter=_OneRecord(_max_size_record()),
            space_id="s" * 32,
            sync_id="y" * 36,
            sig_suite="ed25519",
        )
    ]
    chunk["signatures"] = _SIGS
    raw = serialise_chunk(chunk)
    assert len(raw) < _DATACHANNEL_MAX_MESSAGE_BYTES
    # The provider's HTTPS leg: {sync_id, chunk} sealed per peer.
    payload = orjson.dumps({"sync_id": "y" * 36, "chunk": raw.decode()}).decode()
    envelope = {
        "msg_id": "0" * 36,
        "event_type": "space_sync_chunk",
        "from_instance": "i" * 64,
        "to_instance": "j" * 64,
        "timestamp": "2026-06-01T10:00:00+00:00",
        "encrypted_payload": enc.encrypt_payload(payload, os.urandom(32)),
        "space_id": "s" * 32,
        "proto_version": 1,
        "sig_suite": "ed25519+mldsa65",
        "signatures": _SIGS,
    }
    assert len(orjson.dumps(envelope)) < RELAY_MAX_ENVELOPE_BYTES
    # And it pads into a relay size bucket, i.e. a seal every receiver opens.
    assert len(build_relay_plaintext(envelope)) in RELAY_SIZE_BUCKETS
