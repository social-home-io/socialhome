"""Tests for the receiver-side STT fallback scheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import DmMessageUpdated
from socialhome.domain.user import RemoteUser
from socialhome.infrastructure.audio_transcript_scheduler import (
    AudioTranscriptScheduler,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.user_service import UserService


pytestmark = pytest.mark.asyncio


class _FakeTranscription:
    """Deterministic stand-in for the audio transcription service."""

    def __init__(self, result: str | None) -> None:
        self.result = result
        self.calls: list[int] = []

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: str = "en",
    ) -> str | None:
        self.calls.append(len(audio_bytes))
        return self.result


@pytest.fixture
async def stack(tmp_dir):
    """DB + service stack + a 1:1 DM between a LOCAL user and a REMOTE
    sender so the scheduler's "skip own-household messages" filter has
    a real remote row to find."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key, "
        "identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    events: list = []
    bus.subscribe(DmMessageUpdated, events.append)

    user_repo = SqliteUserRepo(db)
    conv_repo = SqliteConversationRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    media_dir = tmp_dir / "media"
    media_dir.mkdir()

    # One local user, one remote user.
    local = await user_svc.provision(username="anna", display_name="Anna")
    # Seed a remote instance + remote user manually. The columns
    # mirror the production ``remote_instances`` shape — the
    # scheduler only cares about the user FK so most fields are
    # filler.
    await db.enqueue(
        "INSERT INTO remote_instances("
        "id, display_name, remote_identity_pk, key_self_to_remote, "
        "key_remote_to_self, remote_inbox_url, local_inbox_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "inst-bob",
            "Bob's House",
            "ff" * 32,
            "self-to-remote",
            "remote-to-self",
            "https://bob.example/inbox",
            "inbox-bob-1",
        ),
    )
    remote = RemoteUser(
        user_id="rem-bob",
        instance_id="inst-bob",
        remote_username="bob",
        display_name="Bob",
        public_key="ff" * 32,
        public_key_version=1,
    )
    await user_repo.upsert_remote(remote)

    # A conversation with both members.
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?,datetime('now'))",
        ("conv-1", "dm"),
    )
    await db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username, joined_at) "
        "VALUES(?,?,datetime('now'))",
        ("conv-1", "anna"),
    )

    class S:
        pass

    s = S()
    s.db = db
    s.bus = bus
    s.events = events
    s.conv_repo = conv_repo
    s.user_repo = user_repo
    s.media_dir = media_dir
    s.local = local
    s.remote = remote
    yield s
    await db.shutdown()


async def _insert_audio_message(
    db: AsyncDatabase,
    *,
    msg_id: str,
    sender_user_id: str,
    content: str = "",
    media_url: str = "api/media/audio.ogg",
    created_at: str | None = None,
) -> None:
    """Insert a row directly so we control ``created_at`` precisely."""
    ts = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id, "
        "content, type, media_url, deleted, created_at) "
        "VALUES(?,?,?,?,?,?,0,?)",
        (msg_id, "conv-1", sender_user_id, content, "audio", media_url, ts),
    )


async def test_sweep_finds_remote_audio_and_patches(stack):
    """Pending audio from a remote sender is transcribed + patched."""
    blob_name = "voice.ogg"
    blob_path = stack.media_dir / blob_name
    blob_path.write_bytes(b"OggS" + b"\x00" * 64)
    await _insert_audio_message(
        stack.db,
        msg_id="m-remote-1",
        sender_user_id=stack.remote.user_id,
        media_url=f"api/media/{blob_name}",
    )

    transcribe = _FakeTranscription("from-receiver")
    sched = AudioTranscriptScheduler(
        conversation_repo=stack.conv_repo,
        user_repo=stack.user_repo,
        transcribe=transcribe,
        bus=stack.bus,
        media_dir=stack.media_dir,
    )
    patched = await sched._sweep_once()
    assert patched == 1
    # Row updated.
    msg = await stack.conv_repo.get_message("m-remote-1")
    assert msg is not None and msg.content == "from-receiver"
    # Event fired.
    assert any(
        isinstance(e, DmMessageUpdated) and e.content == "from-receiver"
        for e in stack.events
    )


async def test_sweep_skips_own_household_messages(stack):
    """The scheduler ignores rows whose sender is a LOCAL user."""
    blob_path = stack.media_dir / "voice2.ogg"
    blob_path.write_bytes(b"OggS" + b"\x00" * 64)
    await _insert_audio_message(
        stack.db,
        msg_id="m-local-1",
        sender_user_id=stack.local.user_id,
        media_url="api/media/voice2.ogg",
    )

    transcribe = _FakeTranscription("from-receiver")
    sched = AudioTranscriptScheduler(
        conversation_repo=stack.conv_repo,
        user_repo=stack.user_repo,
        transcribe=transcribe,
        bus=stack.bus,
        media_dir=stack.media_dir,
    )
    assert await sched._sweep_once() == 0
    assert transcribe.calls == []


async def test_sweep_skips_messages_older_than_one_hour(stack):
    """Rows past the window are silently skipped."""
    blob_path = stack.media_dir / "voice3.ogg"
    blob_path.write_bytes(b"OggS" + b"\x00" * 64)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await _insert_audio_message(
        stack.db,
        msg_id="m-old-1",
        sender_user_id=stack.remote.user_id,
        media_url="api/media/voice3.ogg",
        created_at=old_ts,
    )

    transcribe = _FakeTranscription("ignored")
    sched = AudioTranscriptScheduler(
        conversation_repo=stack.conv_repo,
        user_repo=stack.user_repo,
        transcribe=transcribe,
        bus=stack.bus,
        media_dir=stack.media_dir,
    )
    assert await sched._sweep_once() == 0
    assert transcribe.calls == []


async def test_sweep_skips_messages_with_content_already_set(stack):
    """A row whose transcript landed earlier is not re-processed."""
    blob_path = stack.media_dir / "voice4.ogg"
    blob_path.write_bytes(b"OggS" + b"\x00" * 64)
    await _insert_audio_message(
        stack.db,
        msg_id="m-already-1",
        sender_user_id=stack.remote.user_id,
        media_url="api/media/voice4.ogg",
        content="already transcribed",
    )

    transcribe = _FakeTranscription("ignored")
    sched = AudioTranscriptScheduler(
        conversation_repo=stack.conv_repo,
        user_repo=stack.user_repo,
        transcribe=transcribe,
        bus=stack.bus,
        media_dir=stack.media_dir,
    )
    assert await sched._sweep_once() == 0


async def test_sweep_skips_when_blob_missing(stack):
    """A row whose blob hasn't synced yet is left for the next pass."""
    await _insert_audio_message(
        stack.db,
        msg_id="m-noblob-1",
        sender_user_id=stack.remote.user_id,
        media_url="api/media/missing.ogg",
    )

    transcribe = _FakeTranscription("never")
    sched = AudioTranscriptScheduler(
        conversation_repo=stack.conv_repo,
        user_repo=stack.user_repo,
        transcribe=transcribe,
        bus=stack.bus,
        media_dir=stack.media_dir,
    )
    patched = await sched._sweep_once()
    assert patched == 0
    assert transcribe.calls == []


async def test_sweep_none_when_transcription_returns_none(stack):
    """Fail-silent: transcription returning ``None`` leaves the row alone."""
    blob_path = stack.media_dir / "voice6.ogg"
    blob_path.write_bytes(b"OggS" + b"\x00" * 64)
    await _insert_audio_message(
        stack.db,
        msg_id="m-fail-1",
        sender_user_id=stack.remote.user_id,
        media_url="api/media/voice6.ogg",
    )

    transcribe = _FakeTranscription(None)
    sched = AudioTranscriptScheduler(
        conversation_repo=stack.conv_repo,
        user_repo=stack.user_repo,
        transcribe=transcribe,
        bus=stack.bus,
        media_dir=stack.media_dir,
    )
    assert await sched._sweep_once() == 0
    # The transcribe call happened — proves the scheduler tried.
    assert len(transcribe.calls) == 1
    # Row is still empty.
    msg = await stack.conv_repo.get_message("m-fail-1")
    assert msg is not None and msg.content == ""


async def test_start_stop_lifecycle(stack):
    """``start()`` is idempotent and ``stop()`` joins the loop."""
    transcribe = _FakeTranscription(None)
    sched = AudioTranscriptScheduler(
        conversation_repo=stack.conv_repo,
        user_repo=stack.user_repo,
        transcribe=transcribe,
        bus=stack.bus,
        media_dir=stack.media_dir,
        interval_seconds=10.0,  # well outside the test window
    )
    await sched.start()
    await sched.start()  # idempotent — second call is a no-op
    await asyncio.sleep(0)
    assert sched._task is not None and not sched._task.done()
    await sched.stop()
    assert sched._task is None


class _GuardianBlocks:
    """§CP.F2 stand-in: ``pairs`` are guardian-blocked both ways."""

    def __init__(self, *pairs: tuple[str, str]) -> None:
        self.pairs = {frozenset(p) for p in pairs}

    def register_gate(self, _gate) -> None:
        pass

    async def guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        return frozenset(
            other
            for pair in self.pairs
            if user_id in pair
            for other in pair - {user_id}
        )


async def test_late_transcript_never_reaches_a_guardian_blocked_member(stack):
    """A blocked sender's voice-note transcript is not pushed to the
    protected member; everyone else still gets it."""
    carl = await UserService(
        stack.user_repo, stack.bus, own_instance_public_key=b"\x00" * 32
    ).provision(username="carl", display_name="Carl")
    await stack.db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username, joined_at) "
        "VALUES('conv-1','carl',datetime('now'))"
    )
    (stack.media_dir / "v.ogg").write_bytes(b"OggS" + b"\x00" * 64)
    await _insert_audio_message(
        stack.db,
        msg_id="m-blocked",
        sender_user_id=stack.remote.user_id,
        media_url="api/media/v.ogg",
    )
    sched = AudioTranscriptScheduler(
        conversation_repo=stack.conv_repo,
        user_repo=stack.user_repo,
        transcribe=_FakeTranscription("hi"),
        bus=stack.bus,
        media_dir=stack.media_dir,
    )
    sched.attach_child_protection(
        _GuardianBlocks((stack.local.user_id, stack.remote.user_id))
    )
    assert await sched._sweep_once() == 1
    (event,) = [e for e in stack.events if isinstance(e, DmMessageUpdated)]
    assert event.recipient_user_ids == (carl.user_id,)
