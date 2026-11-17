"""v_92 — a trusted helper serves a member's resume while the host is
offline, on a real app (provider side).

We are a member household of ``SP`` whose person (``u-local``) holds a
MODERATOR seat — a trusted helper — and not its host. A member household
whose host stayed silent asks us for ``SPACE_SYNC_RESUME``. Rights must not
grow: we replay only OUR people's rows (every receiver takes other people's
rows only from the household acting as host) and only THEIR files — never
another person's file, hashed or not (those come on view,
``SPACE_MEDIA_WANTED``). A ``since`` older than 30 days is clamped.

Receiver side: a helper's replay of a post is the live ``SPACE_POST_CREATED``
— a member that already holds the post keeps its pictures, one that missed
it gets them under the content names every other path gives them.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.app_keys import federation_service_key
from socialhome.domain.federation import FederationEventType as FET
from socialhome.domain.file_hashes import space_content_media_name
from socialhome.domain.post import Post, PostType
from socialhome.federation.sync.space.resume import SpaceSyncResumeProvider
from socialhome.services.media_file_hasher import MediaFileHasher
from tests.protocol.test_content_file_hashes import _image_urls, _media, env
from tests.protocol.test_space_content_authorship import (
    AUTHOR,
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


# ── Receiver side: what a helper's replay leaves on the member ────────────

PIC = b"the helper's own picture"
PIC_H = hashlib.sha256(PIC).hexdigest()
#: The author's household names its own picture by its content (v_85).
PIC_SC = space_content_media_name(SP, PIC_H, "pic.webp")


class _Mesh:
    """The helper's federation service: records what the replay sends."""

    own_instance_id = AUTHOR

    def __init__(self) -> None:
        self.sent: list[tuple[FET, dict]] = []

    async def peer_supports(self, instance_id, *, min_version):
        return True

    async def space_member_supports(self, instance_id, *, min_version):
        return True

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append((event_type, payload))


class _HelperPosts:
    def __init__(self, posts: list[Post]) -> None:
        self._posts = posts

    async def list_since(self, space_id, since, *, limit=500):
        return list(self._posts)

    async def list_comments_since(self, space_id, since, *, limit=500):
        return []


class _HelperSpaces:
    async def list_member_instances(self, space_id):
        return ["us"]

    async def get(self, space_id):
        return None


async def _helper_replay(helper_media) -> list[tuple[FET, dict]]:
    """What the AUTHOR household — a trusted helper — replays of its own
    picture post ``p-kept`` on a ``SPACE_SYNC_RESUME`` (the real provider)."""
    helper_media.mkdir(parents=True, exist_ok=True)
    (helper_media / PIC_SC).write_bytes(PIC)
    mesh = _Mesh()
    provider = SpaceSyncResumeProvider(
        federation_service=mesh,
        space_repo=_HelperSpaces(),
        space_post_repo=_HelperPosts(
            [
                Post(
                    id="p-kept",
                    author="u-a",
                    type=PostType.IMAGE,
                    created_at=datetime.now(UTC) - timedelta(hours=1),
                    content="posted while the host was away",
                    image_urls=(f"api/media/{PIC_SC}",),
                )
            ]
        ),
        media_dir=helper_media,
        file_hasher=MediaFileHasher(helper_media),
    )
    assert (
        await provider.replay_space_to(space_id=SP, instance_id="us", since=_ago(2))
        == 1
    )
    return mesh.sent


async def _blob(app, post_id: str) -> None:
    await _deliver(
        app,
        FET.SPACE_MEDIA_BLOB,
        {
            "correlation_id": post_id,
            "filename": PIC_SC,
            "bytes_b64": base64.b64encode(PIC).decode(),
            "h": PIC_H,
            "h_suite": "sha256",
        },
        sender=AUTHOR,
    )


@pytest.mark.parametrize("held", [True, False], ids=["held", "missed"])
async def test_a_helpers_replay_leaves_a_post_its_pictures(env, tmp_dir, held):
    """The GFS demo chain: e held d's host-offline post with its picture,
    asked the helper d for a resume — and the replayed ``SPACE_POST_CREATED``
    (an upsert of a held post) carried no ``image_urls``: the picture was
    gone from e, and d's bytes were refused ("not a file of the row as held
    here"). Held or missed, the member ends up with the picture under its
    content name, and the helper's bytes land."""
    app, db = env
    if held:
        await _deliver(
            app,
            FET.SPACE_POST_CREATED,
            {
                "id": "p-kept",
                "author": "u-a",
                "type": "image",
                "content": "posted while the host was away",
                "image_urls": [f"api/media/{PIC_SC}"],
                "files": {PIC_SC: {"h": PIC_H}},
                "files_suite": "sha256",
            },
            sender=AUTHOR,
        )
        assert PIC_SC in (await _image_urls(db, "p-kept") or "")
    for event_type, payload in await _helper_replay(tmp_dir / "helper-media"):
        await _deliver(app, event_type, payload, sender=AUTHOR)
    assert PIC_SC in (await _image_urls(db, "p-kept") or "")
    await _blob(app, "p-kept")
    assert (_media(tmp_dir) / PIC_SC).read_bytes() == PIC
