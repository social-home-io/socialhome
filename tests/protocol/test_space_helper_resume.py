"""v_92 — a trusted helper serves a member's resume while the host is
offline, on a real app (provider side).

We are a member household of ``SP`` whose person (``u-local``) holds a
MODERATOR seat — a trusted helper — and not its host. A member household
whose host stayed silent asks us for ``SPACE_SYNC_RESUME``. Rights must not
grow: we replay only OUR people's rows (every receiver takes other people's
rows only from the household acting as host) and only THEIR files — never
another person's file, hashed or not (those come on view,
``SPACE_MEDIA_WANTED``). A ``since`` older than 30 days is clamped.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.app_keys import federation_service_key
from socialhome.domain.federation import FederationEventType as FET
from socialhome.domain.file_hashes import space_content_media_name
from tests.protocol.test_content_file_hashes import _media, env
from tests.protocol.test_space_content_authorship import (
    LOCAL_USER,
    OTHER,
    SP,
    _deliver,
)

pytestmark = pytest.mark.security

__all__ = ["env"]

THEIRS = b"another person's picture"
SC = space_content_media_name(SP, hashlib.sha256(THEIRS).hexdigest(), "t.webp")


def _ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


async def _helper_household(app, db, tmp_dir, monkeypatch) -> list[dict]:
    """Seat us as a moderator, OTHER as a v_92 member household, seed one
    post of ours and two of u-a's (one hashed), and record what we send."""
    await db.enqueue(
        "UPDATE space_members SET role='moderator' WHERE space_id=? AND user_id=?",
        (SP, LOCAL_USER),
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id, proto_version)"
        " VALUES(?,?,92)",
        (SP, OTHER),
    )
    for pid, author, url, days in (
        ("p-ours", LOCAL_USER, "api/media/ours.webp", 1),
        ("p-theirs", "u-a", f"api/media/{SC}", 1),
        ("p-theirs-raw", "u-a", "api/media/raw.webp", 1),
        ("p-ours-old", LOCAL_USER, "api/media/old.webp", 40),
    ):
        await db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, media_url,"
            " created_at) VALUES(?, ?, ?, 'image', ?, ?)",
            (pid, SP, author, url, _ago(days)),
        )
    media = _media(tmp_dir)
    media.mkdir(parents=True, exist_ok=True)
    (media / "ours.webp").write_bytes(b"our picture")
    (media / SC).write_bytes(THEIRS)
    (media / "raw.webp").write_bytes(b"unhashed")
    (media / "old.webp").write_bytes(b"old")

    sent: list[dict] = []

    async def _send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        sent.append({"to": to_instance_id, "type": event_type, "payload": payload})

    monkeypatch.setattr(type(app[federation_service_key]), "send_event", _send_event)
    return sent


async def _queued(db) -> set[tuple[str, str]]:
    rows = await db.fetchall(
        "SELECT blob_id, target_instance_id FROM space_media_outbox"
    )
    return {(r["blob_id"], r["target_instance_id"]) for r in rows}


async def test_a_helper_replays_its_own_rows_and_their_files_only(
    env, tmp_dir, monkeypatch
):
    app, db = env
    sent = await _helper_household(app, db, tmp_dir, monkeypatch)
    await _deliver(
        app,
        FET.SPACE_SYNC_RESUME,
        # Asks for a year: clamped to 30 days (p-ours-old stays out).
        {"space_id": SP, "since": _ago(365).replace(" ", "T") + "+00:00"},
        sender=OTHER,
    )
    posts = [s["payload"] for s in sent if s["type"] is FET.SPACE_POST_CREATED]
    assert {p["id"] for p in posts} >= {"p-ours"}
    assert {p["author"] for p in posts} == {LOCAL_USER}
    assert "p-ours-old" not in {p["id"] for p in posts}
    assert all(s["to"] == OTHER for s in sent)
    queued = await _queued(db)
    assert ("ours.webp", OTHER) in queued
    assert (SC, OTHER) not in queued
    assert ("raw.webp", OTHER) not in queued
    assert ("old.webp", OTHER) not in queued
