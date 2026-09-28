"""Receiver-side handling of voice-note media.

Two surfaces:

1. ``_BLOB_MAGIC`` extended with ``audio/ogg`` — covers MIME-sniff
   acceptance for valid OGG blobs, rejection of a blob masquerading
   as ``audio/ogg`` with bogus magic, and the file extension picked
   for the on-disk filename.
2. ``_on_dm_message`` distinguishes the first-arrival case
   (publishes ``DmMessageCreated``) from the in-place patch case
   (publishes ``DmMessageUpdated`` — exercised by sending the same
   ``message_id`` twice with updated content).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import DmMessageCreated, DmMessageUpdated
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
    _bytes_match_mime,
    _mime_to_ext,
)


def _event(event_type: FederationEventType, payload: dict) -> FederationEvent:
    return FederationEvent(
        msg_id=f"msg-{event_type.value}",
        event_type=event_type,
        from_instance="peer-a",
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


# ── MIME sniff ─────────────────────────────────────────────────────────


def test_audio_ogg_mime_sniff_accepts_oggs_magic():
    """A real OGG/Opus blob's leading ``OggS`` passes the sniff."""
    blob = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00" + b"\x00" * 80
    assert _bytes_match_mime(blob, "audio/ogg") is True


def test_audio_ogg_mime_sniff_rejects_wrong_magic():
    """Bytes claiming ``audio/ogg`` but not starting with ``OggS`` lose."""
    fake = b"NotOgg" + b"\x00" * 32
    assert _bytes_match_mime(fake, "audio/ogg") is False


def test_audio_unrelated_format_under_audio_prefix_rejected():
    """``audio/mp3`` (no registered signature) is suspicious."""
    assert _bytes_match_mime(b"ID3\x04\x00" + b"\x00" * 32, "audio/mpeg") is False


def test_audio_ogg_filename_extension_is_dot_ogg():
    """``audio/ogg`` lands as ``.ogg`` on disk."""
    assert _mime_to_ext("audio/ogg") == ".ogg"


def test_audio_mp4_mime_sniff_accepts_ftyp_magic():
    """A Safari-style MP4/AAC blob's ``ftyp`` box passes the sniff."""
    fake = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
    assert _bytes_match_mime(fake, "audio/mp4") is True


def test_audio_mp4_filename_extension_is_dot_m4a():
    """``audio/mp4`` lands as ``.m4a`` on disk (the audio-only flavor)."""
    assert _mime_to_ext("audio/mp4") == ".m4a"


def test_audio_webm_mime_sniff_accepts_ebml_magic():
    """A Chromium-style WebM/Opus blob's EBML magic passes the sniff."""
    fake = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
    assert _bytes_match_mime(fake, "audio/webm") is True


# ── _on_dm_message dispatch ────────────────────────────────────────────


@pytest.fixture
async def inbound_svc(db, bus, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    # A local recipient and the sender ``u-remote`` of ``peer-a``.
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("local", "u-local", "Local"),
    )
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES('peer-a', 'peer-a', ?, 'k1',"
        " 'k2', 'https://peer-a/wh', 'wh-peer-a', 'confirmed', 'manual')",
        ("00" * 32,),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-remote', 'peer-a', 'remote', 'Remote')",
        (),
    )
    return FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=media_dir,
    )


@pytest.mark.asyncio
async def test_inbound_audio_first_arrival_publishes_created(inbound_svc, db, bus):
    """The first ``DM_MESSAGE`` for an audio note publishes ``DmMessageCreated``."""
    received: list = []
    bus.subscribe(DmMessageCreated, received.append)
    bus.subscribe(DmMessageUpdated, received.append)

    payload = {
        "conversation_id": "conv-audio",
        "message_id": "m-aud-1",
        "sender_user_id": "u-remote",
        "sender_display_name": "Remote Sender",
        "type": "audio",
        "content": "",
        "media_url": "api/media/voice.ogg",
        "mime_type": "audio/ogg",
        "file_name": "voice.ogg",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "recipient_user_ids": ["u-local"],
    }
    await inbound_svc._on_dm_message(_event(FederationEventType.DM_MESSAGE, payload))

    created = [e for e in received if isinstance(e, DmMessageCreated)]
    updated = [e for e in received if isinstance(e, DmMessageUpdated)]
    assert len(created) == 1
    assert created[0].message_type == "audio"
    assert created[0].content == ""
    assert not updated


@pytest.mark.asyncio
async def test_inbound_audio_repeat_with_transcript_publishes_updated(
    inbound_svc, db, bus
):
    """Receiving the same ``message_id`` again with new content fires
    ``DmMessageUpdated``, not a second create."""
    received: list = []
    bus.subscribe(DmMessageCreated, received.append)
    bus.subscribe(DmMessageUpdated, received.append)

    base_payload = {
        "conversation_id": "conv-audio2",
        "message_id": "m-aud-2",
        "sender_user_id": "u-remote",
        "sender_display_name": "Remote Sender",
        "type": "audio",
        "media_url": "api/media/voice2.ogg",
        "mime_type": "audio/ogg",
        "file_name": "voice2.ogg",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "recipient_user_ids": ["u-local"],
    }
    # First leg — no transcript yet.
    await inbound_svc._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {**base_payload, "content": ""},
        )
    )
    # Second leg — sender's STT landed; same id, new content + edited_at.
    edited_at = datetime.now(timezone.utc).isoformat()
    await inbound_svc._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {**base_payload, "content": "hello world", "edited_at": edited_at},
        )
    )

    created = [e for e in received if isinstance(e, DmMessageCreated)]
    updated = [e for e in received if isinstance(e, DmMessageUpdated)]
    assert len(created) == 1
    assert len(updated) == 1
    assert updated[0].content == "hello world"


@pytest.mark.asyncio
async def test_inbound_audio_missing_required_fields_is_logged_not_raised(
    inbound_svc, db, bus
):
    """Malformed ``DM_MESSAGE`` payloads short-circuit cleanly."""
    received: list = []
    bus.subscribe(DmMessageCreated, received.append)
    # ``conversation_id`` is required; without it the handler logs + returns.
    await inbound_svc._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {"message_id": "x", "sender_user_id": "y", "type": "audio"},
        )
    )
    assert received == []
