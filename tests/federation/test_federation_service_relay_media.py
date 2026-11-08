"""v_87 — media chunks to a household seated from an invite link travel as
binary frames through the GFS relay; the receiver runs the inbox's §24.11
media tail on them (parity with the HTTPS inbox and the DataChannel)."""

from __future__ import annotations

import dataclasses
import os

import orjson
import pytest

from socialhome.crypto import generate_x25519_keypair
from socialhome.domain.federation import (
    DELIVERY_ERROR_RELAY_THROTTLED,
    FederationEventType,
    InstanceSource,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.media_chunking import (
    HTTPS_CHUNK_BYTES,
    RELAY_JSON_CHUNK_BYTES,
)
from socialhome.federation import gfs_relay_media as grm
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.gfs_relay_transport import (
    RELAY_MAX_ENVELOPE_BYTES,
    RELAY_STATUS_THROTTLED,
    build_relay_plaintext,
)
from socialhome.federation.keywrap_seal import UnsupportedKemSuite
from socialhome.federation.media_envelope import (
    build_media_envelope,
    encode_media_body,
)
from socialhome.services.gfs_relay_inbound import GfsRelayInbound
from tests.federation.test_federation_service import (
    _CaptureMediaTransport,
    _paired,
)

GFS = "https://gfs.example"


class _CaptureRelayTransport(_CaptureMediaTransport):
    """No media channel; records the relay media leg."""

    def __init__(self) -> None:
        super().__init__()
        self.media_ready = False
        self.relay_ready = True
        self.relay_media: list[tuple[dict, bytes]] = []
        self.relay_result: tuple[bool, int | None] = (True, None)

    async def relay_media_ready(self, instance) -> bool:
        return self.relay_ready

    async def send_media_relay(self, *, instance, header_dict, payload_bytes):
        self.relay_media.append((header_dict, payload_bytes))
        return self.relay_result


def _relay_paired(*, peer_version: int = FederationCapability.MIN_FOR_RELAY_MEDIA):
    """A and B seated from an invite link on both sides (``space_session``),
    reached through a GFS; B owns a key-wrap keypair."""
    p = _paired(peer_proto_version=peer_version)
    p.kw = generate_x25519_keypair()
    p.repo_a._instances[p.b_id] = dataclasses.replace(
        p.repo_a._instances[p.b_id],
        source=InstanceSource.SPACE_SESSION,
        remote_inbox_url="",
        relay_via=GFS,
        remote_keywrap_pk=p.kw.public_key.hex(),
    )
    p.repo_b._instances[p.a_id] = dataclasses.replace(
        p.repo_b._instances[p.a_id],
        source=InstanceSource.SPACE_SESSION,
        remote_inbox_url="",
        relay_via=GFS,
    )
    p.cap = _CaptureRelayTransport()
    p.svc_a.attach_transport(p.cap)
    p.inbound = GfsRelayInbound(
        federation=p.svc_b,
        keywrap_private_key=p.kw.private_key,
        invite_coordinator=None,  # type: ignore[arg-type]
        gfs_connection_repo=None,  # type: ignore[arg-type]
    )
    return p


async def _send(p, raw: bytes):
    return await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.SPACE_MEDIA_BLOB,
        payload={"filename": "img.webp", "transfer_id": "t1"},
        raw_chunk=raw,
        space_id="sp1",
        mesh_fallback=True,
    )


def _relay_frame(p, header: dict, sealed: bytes, *, to: str | None = None) -> dict:
    body = grm.seal_relay_media(
        to_instance=to or p.b_id,
        media_body=encode_media_body(header, sealed),
        recipient_keywrap_pub=p.kw.public_key,
    )
    return {"type": "envelope_media", "body": body}


# ── Sender selection ─────────────────────────────────────────────────────


async def test_relay_seat_at_v87_gets_one_binary_relay_frame():
    p = _relay_paired()
    raw = os.urandom(HTTPS_CHUNK_BYTES)
    result = await _send(p, raw)
    assert result.ok and result.via == "gfs_relay"
    assert p.cap.json_envelopes == []
    [(header, sealed)] = p.cap.relay_media
    assert len(sealed) == 12 + len(raw) + 16  # raw AEAD, no base64
    # Sealed for the relay, a full chunk fits the GFS's 528 KiB cap.
    frame = _relay_frame(p, header, sealed)
    assert len(frame["body"]) <= grm.RELAY_MEDIA_MAX_BODY_BYTES


async def test_relay_seat_chunk_size_follows_the_relay_capability():
    p = _relay_paired()
    size = 5 * 1024 * 1024
    assert await p.svc_a.mesh_media_chunk_bytes(p.b_id, file_size_bytes=size) == (
        HTTPS_CHUNK_BYTES
    )
    p.cap.relay_ready = False  # an older GFS (no ``envelope_media``)
    assert await p.svc_a.mesh_media_chunk_bytes(p.b_id, file_size_bytes=size) == (
        RELAY_JSON_CHUNK_BYTES
    )


@pytest.mark.parametrize("gfs_ready", [True, False])
async def test_older_recipient_or_gfs_falls_back_to_json_chunks(gfs_ready):
    p = _relay_paired(
        peer_version=FederationCapability.MIN_FOR_RELAY_MEDIA - (1 if gfs_ready else 0)
    )
    p.cap.relay_ready = gfs_ready
    size = 300 * 1024
    chunk = await p.svc_a.mesh_media_chunk_bytes(p.b_id, file_size_bytes=size)
    assert chunk == RELAY_JSON_CHUNK_BYTES
    result = await _send(p, os.urandom(chunk))
    assert result.ok
    assert p.cap.relay_media == []
    [envelope] = p.cap.json_envelopes
    # The JSON chunk fits the JSON relay's top size bucket.
    assert len(build_relay_plaintext(envelope)) <= RELAY_MAX_ENVELOPE_BYTES


async def test_a_throttled_relay_frame_is_waitable_and_never_goes_json():
    p = _relay_paired()
    p.cap.relay_result = (False, RELAY_STATUS_THROTTLED)
    result = await _send(p, b"x" * 1000)
    assert not result.ok
    assert result.error == DELIVERY_ERROR_RELAY_THROTTLED
    assert p.cap.json_envelopes == []


async def test_a_paired_direct_peer_never_uses_the_relay_leg():
    p = _relay_paired()
    p.repo_a._instances[p.b_id] = dataclasses.replace(
        p.repo_a._instances[p.b_id], source=InstanceSource.MANUAL
    )
    await _send(p, b"x" * 1000)
    assert p.cap.relay_media == []


# ── Receive: §24.11 parity ───────────────────────────────────────────────


async def test_relay_frame_round_trips_through_the_media_pipeline():
    p = _relay_paired()
    raw = os.urandom(300 * 1024)
    await _send(p, raw)
    [(header, sealed)] = p.cap.relay_media
    res = await p.inbound.handle_frame(_relay_frame(p, header, sealed))
    assert res == {"status": "ok"}
    [event] = p.received
    assert event.event_type is FederationEventType.SPACE_MEDIA_BLOB
    assert event.media_bytes == raw
    assert event.payload["filename"] == "img.webp"


def _frame(p, *, raw=b"chunk", event_type=FederationEventType.SPACE_MEDIA_BLOB):
    return build_media_envelope(
        encoder=FederationEncoder(p.a_kp.private_key),
        session_key=p.session,
        from_instance=p.a_id,
        to_instance=p.b_id,
        sig_suite="ed25519",
        event_type=event_type,
        payload={"filename": "f.webp", "transfer_id": "t1"},
        raw_chunk=raw,
        space_id="sp1",
    )


async def test_replayed_relay_frame_is_refused():
    p = _relay_paired()
    header, sealed = _frame(p)
    await p.inbound.handle_frame(_relay_frame(p, header, sealed))
    with pytest.raises(ValueError, match="Replay detected"):
        await p.inbound.handle_frame(_relay_frame(p, header, sealed))
    assert len(p.received) == 1


async def test_tampered_relay_frame_is_refused():
    p = _relay_paired()
    header, sealed = _frame(p)
    frame = _relay_frame(p, header, sealed)
    body = bytearray(frame["body"])
    body[-1] ^= 1
    with pytest.raises(ValueError, match="does not open"):
        await p.inbound.handle_frame({**frame, "body": bytes(body)})
    assert p.received == []


async def test_swapped_chunk_inside_a_valid_seal_is_refused():
    p = _relay_paired()
    header, _ = _frame(p, raw=b"committed")
    other = p.svc_b._encoder.encrypt_bytes(b"swapped", p.session)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        await p.inbound.handle_frame(_relay_frame(p, header, other))
    assert p.received == []


async def test_relay_frame_for_another_household_is_refused():
    p = _relay_paired()
    header, sealed = _frame(p)
    with pytest.raises(ValueError, match="another household"):
        await p.inbound.handle_frame(_relay_frame(p, header, sealed, to="c" * 32))


async def test_unknown_kem_suite_is_refused():
    p = _relay_paired()
    header, sealed = _frame(p)
    frame = _relay_frame(p, header, sealed)
    body = bytearray(frame["body"])
    body[37] = 0x7F  # the kem-suite byte after magic, version, recipient
    with pytest.raises(UnsupportedKemSuite):
        await p.inbound.handle_frame({**frame, "body": bytes(body)})


async def test_a_non_media_event_in_a_relay_frame_is_refused():
    p = _relay_paired()
    header, sealed = _frame(p, event_type=FederationEventType.SPACE_POST_CREATED)
    with pytest.raises(ValueError):
        await p.inbound.handle_frame(_relay_frame(p, header, sealed))
    assert p.received == []


async def test_a_paired_peer_without_the_relay_opt_in_is_refused():
    """The relay opt-in gate holds for media frames too."""
    p = _relay_paired()
    p.repo_b._instances[p.a_id] = dataclasses.replace(
        p.repo_b._instances[p.a_id], source=InstanceSource.MANUAL, gfs_relay=False
    )
    header, sealed = _frame(p)
    with pytest.raises(ValueError, match="GFS relay is not enabled"):
        await p.inbound.handle_frame(_relay_frame(p, header, sealed))


async def test_a_sender_header_without_from_instance_is_refused():
    p = _relay_paired()
    header, sealed = _frame(p)
    header = {k: v for k, v in header.items() if k != "from_instance"}
    with pytest.raises(ValueError, match="names no sender"):
        await p.svc_b.handle_inbound_media_relay(encode_media_body(header, sealed))


async def test_a_malformed_media_body_is_refused():
    p = _relay_paired()
    with pytest.raises(ValueError, match="Invalid media frame"):
        await p.svc_b.handle_inbound_media_relay(b"\x00SHM\x01junk")


async def test_relay_frame_header_carries_no_plaintext_for_the_gfs():
    """What the GFS holds names the recipient only — the signed header's
    sender, event type and space are inside the seal."""
    p = _relay_paired()
    header, sealed = _frame(p)
    body = _relay_frame(p, header, sealed)["body"]
    for leak in (p.a_id, "space_media_blob", "sp1"):
        assert leak.encode() not in body
    assert orjson.dumps(header)[:20] not in body
