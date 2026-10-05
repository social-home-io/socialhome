"""Receiver-side ``DM_MEDIA_BLOB`` handling — coverage for the
preview-now-sync-later flow's inbound half.

Exercises the chunk-buffer-and-concat path, the back-compat
single-chunk shape, the blob-before-message reordering guard,
and the MIME-magic-byte sniff that flags suspicious payloads
without dropping the file.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.user import User
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


pytestmark = pytest.mark.asyncio


_WEBP_HEADER = b"\x52\x49\x46\x46\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def _event(event_type, payload):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance="peer-a",
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


async def _seed_remote_user(db, user_id: str, instance_id: str) -> None:
    await db.enqueue(
        "INSERT OR IGNORE INTO remote_instances"
        "(id, display_name, remote_identity_pk, key_self_to_remote,"
        " key_remote_to_self, remote_inbox_url, local_inbox_id, status,"
        " source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            "00" * 32,
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, user_id, user_id),
    )


@pytest.fixture
async def inbound_with_media(db, bus, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=media_dir,
    )
    # The message's sender lives on ``peer-a`` — the household the
    # blob envelopes come from.
    await _seed_remote_user(db, "user-remote", "peer-a")
    # Seed a conversation + message row that the blob handler can
    # update.
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    await db.enqueue(
        """
        INSERT INTO conversation_messages(
            id, conversation_id, sender_user_id, content, type,
            media_blob_id, media_sync_status, created_at
        ) VALUES(?,?,?,?,?,?,?, datetime('now'))
        """,
        ("m-1", "conv-1", "user-remote", "", "image", "m-1", "pending"),
    )
    return svc, media_dir, db


# ── _receive_media_preview branches ───────────────────────────────────


async def test_receive_media_preview_non_media_keeps_a_local_ref(inbound_with_media):
    """A message without a blob id keeps a ``media_url`` that has the
    local upload shape (normalised to ``api/media/<name>``)."""
    svc, _media_dir, _db = inbound_with_media
    url, status = await svc._receive_media_preview(
        payload={"media_url": "/api/media/abc123.webp?exp=1&sig=x"},
        message_id="m-1",
        msg_type="text",
    )
    assert url == "api/media/abc123.webp"
    assert status is None


@pytest.mark.parametrize("msg_type", ["text", "file", "image", "video"])
@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(document.domain)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "https://elsewhere.example/foo.jpg",
        "//elsewhere.example/foo.jpg",
        "api/media/../../etc/passwd",
        "api/media/.hidden",
        "api/media/",
        "media/abc.webp",
        42,
    ],
)
async def test_receive_media_preview_drops_a_non_local_media_url(
    inbound_with_media, caplog, msg_type, bad
):
    """A peer-supplied ``media_url`` that is not a local media reference
    (a ``javascript:`` URL for a DM file chip's ``href``, a remote
    tracker, a path escape) is stored as ``None`` and logged at WARNING."""
    svc, _media_dir, _db = inbound_with_media
    with caplog.at_level(logging.WARNING):
        url, status = await svc._receive_media_preview(
            payload={"media_url": bad},
            message_id="m-x",
            msg_type=msg_type,
        )
    assert url is None
    assert status is None
    assert "non-local media_url" in caplog.text


async def test_receive_media_preview_absent_media_url_is_quiet(
    inbound_with_media, caplog
):
    """A plain text message carries no ``media_url`` — no warning."""
    svc, _media_dir, _db = inbound_with_media
    with caplog.at_level(logging.WARNING):
        url, status = await svc._receive_media_preview(
            payload={"content": "hi"}, message_id="m-t", msg_type="text"
        )
    assert (url, status) == (None, None)
    assert "media_url" not in caplog.text


async def test_receive_media_preview_pre_arrived_full_file(inbound_with_media):
    """If the blob landed before the message, the full file is
    adopted directly — no preview write, no pending state."""
    svc, media_dir, _db = inbound_with_media
    full = media_dir / "m-2.webp"
    full.write_bytes(_WEBP_HEADER)
    url, status = await svc._receive_media_preview(
        payload={
            "media_blob_id": "m-2",
            "mime_type": "image/webp",
            # preview shouldn't be touched because full file exists
            "preview_bytes_b64": "ignored",
        },
        message_id="m-2",
        msg_type="image",
    )
    assert url == "api/media/m-2.webp"
    assert status is None
    # Preview file was dropped (if it existed from a stale attempt).
    assert not (media_dir / "m-2.preview.webp").exists()


async def test_receive_media_preview_writes_preview(inbound_with_media):
    """A normal v_3 media DM with embedded preview lands as a
    ``<msg_id>.preview.webp`` and the row stays ``pending``."""
    svc, media_dir, _db = inbound_with_media
    url, status = await svc._receive_media_preview(
        payload={
            "media_blob_id": "m-3",
            "mime_type": "image/webp",
            "preview_bytes_b64": base64.b64encode(_WEBP_HEADER).decode(),
        },
        message_id="m-3",
        msg_type="image",
    )
    assert url == "api/media/m-3.preview.webp"
    assert status == "pending"
    assert (media_dir / "m-3.preview.webp").is_file()


async def test_receive_media_preview_without_preview_field(inbound_with_media):
    """v_3 media without an inline preview (video / file before
    poster-extraction landed, or a sender that built none) sets
    pending state and ``media_url=None`` — the SPA shows a
    placeholder glyph until the blob arrives."""
    svc, _media_dir, _db = inbound_with_media
    url, status = await svc._receive_media_preview(
        payload={
            "media_blob_id": "m-4",
            "mime_type": "video/webm",
        },
        message_id="m-4",
        msg_type="video",
    )
    assert url is None
    assert status == "pending"


async def test_receive_media_preview_no_media_dir_returns_pending(db, bus):
    """Service without ``media_dir`` can't write the preview — it
    returns ``pending`` with no URL so the SPA still shows the
    placeholder."""
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=None,
    )
    url, status = await svc._receive_media_preview(
        payload={
            "media_blob_id": "m-5",
            "mime_type": "image/webp",
            "preview_bytes_b64": "x",
        },
        message_id="m-5",
        msg_type="image",
    )
    assert url is None
    assert status == "pending"


# ── Single-chunk (back-compat) blob ────────────────────────────────────


async def test_dm_media_blob_single_chunk_writes_and_swaps(inbound_with_media):
    """Legacy single-payload shape (no chunk fields) writes the
    file straight to ``<msg_id>.<ext>`` and clears the row's
    ``media_sync_status``.
    """
    svc, media_dir, db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "bytes_b64": base64.b64encode(_WEBP_HEADER).decode("ascii"),
            },
        )
    )
    dest = media_dir / "m-1.webp"
    assert dest.is_file()
    assert dest.read_bytes() == _WEBP_HEADER

    row = await db.fetchone(
        "SELECT media_url, media_sync_status FROM conversation_messages WHERE id=?",
        ("m-1",),
    )
    assert row is not None
    assert row["media_url"] == "api/media/m-1.webp"
    assert row["media_sync_status"] is None  # cleared


async def test_dm_media_blob_missing_fields_drops(inbound_with_media):
    """Payloads without the required fields are dropped (no crash)."""
    svc, _media_dir, _db = inbound_with_media
    # No bytes_b64 — fall through.
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {"media_blob_id": "m-1", "message_id": "m-1"},
        )
    )
    # No raise = pass.


async def test_dm_media_blob_skips_when_media_dir_unwired(db, bus):
    """A service constructed without media_dir tolerates the event."""
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=None,
    )
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-2",
                "message_id": "m-2",
                "bytes_b64": base64.b64encode(b"x").decode("ascii"),
            },
        )
    )
    # No raise = pass.


# ── Chunked payload ─────────────────────────────────────────────────────


async def test_dm_media_blob_chunked_writes_parts_then_concats(
    inbound_with_media,
):
    """Three-chunk payload: each ``part<idx>`` lands, final
    triggers concat, parts are cleaned up, row updates."""
    svc, media_dir, db = inbound_with_media
    full = b"chunk0_" + b"chunk1_" + b"chunk2_"
    parts = [b"chunk0_", b"chunk1_", b"chunk2_"]
    for i, p in enumerate(parts):
        await svc._on_dm_media_blob(
            _event(
                FederationEventType.DM_MEDIA_BLOB,
                {
                    "media_blob_id": "m-1",
                    "message_id": "m-1",
                    "conversation_id": "conv-1",
                    "mime_type": "image/webp",
                    "chunk_index": i,
                    "chunk_count": 3,
                    "final": i == 2,
                    "bytes_b64": base64.b64encode(p).decode("ascii"),
                },
            )
        )
        if i < 2:
            # Pre-final chunks land as part files, no concat yet.
            assert (media_dir / f"m-1.part{i:05d}").is_file()
            assert not (media_dir / "m-1.webp").is_file()
    # Final chunk → concat, part files removed.
    dest = media_dir / "m-1.webp"
    assert dest.is_file()
    # Bytes don't match a real WebP so the sniff flags failed,
    # but the file still lands. The content is the concat though.
    assert dest.read_bytes() == full
    for i in range(3):
        assert not (media_dir / f"m-1.part{i:05d}").exists()


async def test_dm_media_blob_malformed_chunk_meta_defaults_to_single(
    inbound_with_media,
):
    """Garbage ``chunk_index`` / ``chunk_count`` values fall back
    to single-chunk handling so a sender on a different language
    runtime can't poison the receiver."""
    svc, media_dir, db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "chunk_index": "not-a-number",
                "chunk_count": None,
                "bytes_b64": base64.b64encode(_WEBP_HEADER).decode("ascii"),
            },
        )
    )
    # Garbage chunk fields → defaults to single chunk → file lands
    # at the final destination.
    assert (media_dir / "m-1.webp").is_file()


async def test_dm_media_blob_unknown_mime_uses_bin_ext(inbound_with_media):
    """An unknown ``mime_type`` writes the file with the ``.bin``
    fallback extension."""
    svc, media_dir, _db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "application/x-weird",
                "bytes_b64": base64.b64encode(b"weird bytes").decode("ascii"),
            },
        )
    )
    assert (media_dir / "m-1.bin").is_file()


async def test_dm_media_blob_malformed_b64_drops(inbound_with_media):
    """Bad base64 → log + drop, no crash."""
    svc, media_dir, _db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "bytes_b64": "!@#$%^&*not-valid-b64",
            },
        )
    )
    # No file created.
    assert not (media_dir / "m-1.webp").exists()


async def test_dm_media_blob_chunked_missing_chunk_bails(inbound_with_media):
    """Final chunk arriving without an earlier part → log + bail.

    The receiver doesn't write a partial concat; the sender's
    outbox retry resends the missing chunk and finalisation runs
    again.
    """
    svc, media_dir, _db = inbound_with_media
    # Skip chunk 0, deliver final (chunk 1 of 2).
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "chunk_index": 1,
                "chunk_count": 2,
                "final": True,
                "bytes_b64": base64.b64encode(b"tail").decode("ascii"),
            },
        )
    )
    # Final wasn't able to finalise (chunk 0 missing) — the
    # ``m-1.webp`` file should NOT exist yet, the part for index
    # 1 should be there.
    assert (media_dir / "m-1.part00001").is_file()
    assert not (media_dir / "m-1.webp").is_file()


# ── MIME sniff ──────────────────────────────────────────────────────────


async def test_dm_media_blob_mime_sniff_flags_failed(inbound_with_media):
    """A claimed ``image/webp`` whose bytes don't match WebP
    magic still stores the file but flips
    ``media_sync_status='failed'`` so the receiver bubble surfaces
    the warning."""
    svc, media_dir, db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "bytes_b64": base64.b64encode(b"NOT-A-WEBP-BLOB").decode(),
            },
        )
    )
    dest = media_dir / "m-1.webp"
    assert dest.is_file()
    row = await db.fetchone(
        "SELECT media_sync_status FROM conversation_messages WHERE id=?",
        ("m-1",),
    )
    assert row is not None
    assert row["media_sync_status"] == "failed"


def test_bytes_match_mime_webp_and_webm():
    assert _bytes_match_mime(_WEBP_HEADER, "image/webp") is True
    assert _bytes_match_mime(b"\x1a\x45\xdf\xa3...", "video/webm") is True
    assert _bytes_match_mime(b"random", "image/webp") is False
    assert _bytes_match_mime(b"random", "image/jpeg") is False  # no entry → flag
    # Files / unknown text MIMEs pass through (``None``).
    assert _bytes_match_mime(b"%PDF", "application/pdf") is None
    assert _bytes_match_mime(b"hello", "text/plain") is None


def test_mime_to_ext_known_and_unknown():
    assert _mime_to_ext("image/webp") == ".webp"
    assert _mime_to_ext("application/pdf") == ".pdf"
    assert _mime_to_ext("text/x-unknown") == ".bin"


# ── Scope + write-once ──────────────────────────────────────────────────


def _blob_event(payload, *, from_instance="peer-a", body=_WEBP_HEADER):
    return FederationEvent(
        msg_id="msg-blob",
        event_type=FederationEventType.DM_MEDIA_BLOB,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={
            "mime_type": "image/webp",
            "bytes_b64": base64.b64encode(body).decode("ascii"),
            **payload,
        },
    )


async def _row(db, message_id):
    return await db.fetchone(
        "SELECT media_url, media_sync_status FROM conversation_messages WHERE id=?",
        (message_id,),
    )


async def test_dm_media_blob_from_another_household_is_refused(
    inbound_with_media, caplog
):
    """The message is known and was sent from ``peer-a``; ``peer-b`` (say a
    household in the same group DM) can't supply its bytes."""
    svc, media_dir, db = inbound_with_media
    with caplog.at_level("WARNING"):
        await svc._on_dm_media_blob(
            _blob_event(
                {"media_blob_id": "m-1", "message_id": "m-1"},
                from_instance="peer-b",
            )
        )
    assert not (media_dir / "m-1.webp").exists()
    row = await _row(db, "m-1")
    assert row["media_sync_status"] == "pending"
    assert "refusing" in caplog.text


async def test_dm_media_blob_for_local_members_message_is_refused(inbound_with_media):
    svc, media_dir, db = inbound_with_media
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    await db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
        " content, type, media_blob_id, media_url, created_at)"
        " VALUES(?,?,?,?,?,?,?, datetime('now'))",
        ("m-local", "conv-1", "u-anna", "", "image", "m-local", "api/media/x.webp"),
    )
    await svc._on_dm_media_blob(
        _blob_event({"media_blob_id": "m-local", "message_id": "m-local"})
    )
    assert not (media_dir / "m-local.webp").exists()
    assert (await _row(db, "m-local"))["media_url"] == "api/media/x.webp"


async def test_dm_media_blob_with_mismatched_blob_id_is_refused(inbound_with_media):
    svc, media_dir, db = inbound_with_media
    await svc._on_dm_media_blob(
        _blob_event({"media_blob_id": "other-blob", "message_id": "m-1"})
    )
    assert not (media_dir / "m-1.webp").exists()
    assert (await _row(db, "m-1"))["media_sync_status"] == "pending"


@pytest.mark.parametrize("bad", ["../escape", "sub/m", ".hidden", "nul\x00"])
async def test_dm_media_blob_unsafe_message_id_is_refused(inbound_with_media, bad):
    svc, media_dir, _db = inbound_with_media
    await svc._on_dm_media_blob(_blob_event({"media_blob_id": bad, "message_id": bad}))
    assert list(media_dir.iterdir()) == []
    assert not (media_dir.parent / "escape.webp").exists()


async def test_dm_media_blob_never_overwrites_existing_file(inbound_with_media):
    """Write-once: the sender's own re-delivery re-adopts the file already
    there instead of replacing it."""
    svc, media_dir, db = inbound_with_media
    (media_dir / "m-1.webp").write_bytes(_WEBP_HEADER + b"ORIGINAL")
    await svc._on_dm_media_blob(
        _blob_event({"media_blob_id": "m-1", "message_id": "m-1"})
    )
    assert (media_dir / "m-1.webp").read_bytes() == _WEBP_HEADER + b"ORIGINAL"
    row = await _row(db, "m-1")
    assert row["media_url"] == "api/media/m-1.webp"
    assert row["media_sync_status"] is None


async def test_dm_media_blob_chunked_never_overwrites_existing_file(
    inbound_with_media,
):
    svc, media_dir, _db = inbound_with_media
    (media_dir / "m-1.webp").write_bytes(b"ORIGINAL")
    for i, part in enumerate((b"A", b"B")):
        await svc._on_dm_media_blob(
            _blob_event(
                {
                    "media_blob_id": "m-1",
                    "message_id": "m-1",
                    "chunk_index": i,
                    "chunk_count": 2,
                    "final": i == 1,
                },
                body=part,
            )
        )
    assert (media_dir / "m-1.webp").read_bytes() == b"ORIGINAL"
    assert not list(media_dir.glob("m-1.part*"))


async def test_dm_media_blob_before_message_is_written_once(inbound_with_media):
    """The blob can overtake its DM_MESSAGE; it lands (write-once) and a
    second copy under the same name doesn't replace it."""
    svc, media_dir, _db = inbound_with_media
    await svc._on_dm_media_blob(
        _blob_event({"media_blob_id": "m-new", "message_id": "m-new"})
    )
    assert (media_dir / "m-new.webp").read_bytes() == _WEBP_HEADER
    await svc._on_dm_media_blob(
        _blob_event(
            {"media_blob_id": "m-new", "message_id": "m-new"},
            from_instance="peer-b",
            body=b"SECOND",
        )
    )
    assert (media_dir / "m-new.webp").read_bytes() == _WEBP_HEADER


@pytest.mark.parametrize(("index", "count"), [(-1, 2), (2, 2), (0, 10**9)])
async def test_dm_media_blob_out_of_range_chunk_meta_is_refused(
    inbound_with_media, index, count
):
    svc, media_dir, _db = inbound_with_media
    await svc._on_dm_media_blob(
        _blob_event(
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "chunk_index": index,
                "chunk_count": count,
            }
        )
    )
    assert list(media_dir.iterdir()) == []


# ── F6: peer-supplied message ids never become paths outside media ────


def _unsafe_ids(media_dir):
    """``../`` and absolute message ids — each would name a file
    outside ``media_dir`` if joined as-is."""
    return ["../escape", str(media_dir.parent / "abs-escape"), "sub/escape"]


def _outside_files(media_dir):
    root = media_dir.parent
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and media_dir not in p.parents
    )


async def test_receive_media_preview_refuses_unsafe_message_id(inbound_with_media):
    """The helper itself never writes (or deletes) outside ``media_dir``,
    whatever id it is handed — defence in depth behind the early
    ``DM_MESSAGE`` check."""
    svc, media_dir, _db = inbound_with_media
    for bad in _unsafe_ids(media_dir):
        url, status = await svc._receive_media_preview(
            payload={
                "media_blob_id": "blob",
                "mime_type": "image/webp",
                "preview_bytes_b64": base64.b64encode(_WEBP_HEADER).decode(),
            },
            message_id=bad,
            msg_type="image",
        )
        assert (url, status) == (None, None), bad
    assert _outside_files(media_dir) == []
    assert list(media_dir.iterdir()) == []


async def test_receive_media_preview_unsafe_id_never_deletes_outside(
    inbound_with_media,
):
    """The reorder guard removes ``<id>.preview.webp`` when ``<id>.<ext>``
    exists — with a ``../`` id that would delete a file outside media."""
    svc, media_dir, _db = inbound_with_media
    victim = media_dir.parent / "escape.preview.webp"
    victim.write_bytes(b"keep")
    (media_dir.parent / "escape.webp").write_bytes(b"x")
    url, status = await svc._receive_media_preview(
        payload={"media_blob_id": "b", "mime_type": "image/webp"},
        message_id="../escape",
        msg_type="image",
    )
    assert (url, status) == (None, None)
    assert victim.read_bytes() == b"keep"


def _dm_message_event(message_id: str) -> FederationEvent:
    return _event(
        FederationEventType.DM_MESSAGE,
        {
            "conversation_id": "conv-new",
            "message_id": message_id,
            "sender_user_id": "user-remote",
            "sender_display_name": "Remote",
            "type": "image",
            "content": "",
            "media_blob_id": message_id,
            "mime_type": "image/webp",
            "preview_bytes_b64": base64.b64encode(_WEBP_HEADER).decode(),
            "recipient_user_ids": ["uid-carol"],
        },
    )


async def test_dm_message_with_safe_id_writes_its_preview(inbound_with_media):
    """Control for the refusal below: the same envelope with a normal id
    is accepted and its preview lands inside ``media_dir``."""
    svc, media_dir, db = inbound_with_media
    await SqliteUserRepo(db).save(
        User(user_id="uid-carol", username="carol", display_name="Carol")
    )
    await svc._on_dm_message(_dm_message_event("m-safe"))
    assert (media_dir / "m-safe.preview.webp").is_file()
    msgs = await SqliteConversationRepo(db).list_messages("conv-new", limit=10)
    assert [m.id for m in msgs] == ["m-safe"]


async def test_dm_message_with_unsafe_id_is_refused(inbound_with_media, caplog):
    """F6: a ``DM_MESSAGE`` whose ``message_id`` is ``../x`` or an absolute
    path is dropped before anything is stored or written, with a WARNING
    that does not echo the raw id."""
    svc, media_dir, db = inbound_with_media
    await SqliteUserRepo(db).save(
        User(user_id="uid-carol", username="carol", display_name="Carol")
    )
    for bad in _unsafe_ids(media_dir):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await svc._on_dm_message(_dm_message_event(bad))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, bad
        assert all(bad not in r.getMessage() for r in warnings), bad
    assert _outside_files(media_dir) == []
    assert list(media_dir.iterdir()) == []
    assert await SqliteConversationRepo(db).list_messages("conv-new", limit=10) == []
