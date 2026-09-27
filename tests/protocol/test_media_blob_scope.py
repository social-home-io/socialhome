"""Release-blocker protocol tests: a media blob only lands what it was sent for.

Marked ``@pytest.mark.security``.

``SPACE_MEDIA_BLOB`` and ``DM_MEDIA_BLOB`` write peer-supplied bytes under
``media_dir`` by a peer-supplied name. The rules these tests encode,
against the real application registry, SQLite and media directory:

* **Write-once** — a file that already exists is never replaced, whoever
  sends the blob and whatever row it claims to be for.
* **Space scope** — a space blob gated for space A whose post / gallery
  item / listing is known here must belong to space A and reference the
  filename.
* **DM scope** — a DM blob for a message that is known here must come from
  the household the message was sent from, under the blob id the message
  announced; never for a local member's message.
* **Names** — a name that is not a single safe path component writes
  nothing, anywhere.

The whole data directory (media and everything else) plus the message
rows are snapshotted before and after each attack and must be identical.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType

pytestmark = pytest.mark.security

FET = FederationEventType

GATED = "sp-a"
VICTIM = "sp-b"
SENDER = "peer-a"  # member of sp-a; sender of DM m-remote
OTHER = "peer-b"  # another paired household (e.g. in the same group DM)
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16

#: Files already on disk before any blob arrives.
EXISTING = {
    "b1.webp": b"space B picture",
    "a1.webp": b"space A picture",
    "local-dm.webp": b"local member's DM attachment",
    "m-remote.webp": WEBP + b"remote DM attachment",
}


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "media-scope.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


_SEED = [
    (
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    ),
    *[
        (
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?,?,?,?,?)",
            (sid, sid, "the-host", "anna", "00" * 32),
        )
        for sid in (GATED, VICTIM)
    ],
    *[
        (
            "INSERT INTO space_posts(id, space_id, author, type, content,"
            " media_url) VALUES(?,?,?,?,?,?)",
            (pid, sid, "u-x", "image", "", f"api/media/{fn}"),
        )
        for pid, sid, fn in (
            ("post-a", GATED, "a1.webp"),
            ("post-a-new", GATED, "a-new.webp"),
            ("post-b", VICTIM, "b1.webp"),
        )
    ],
    (
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name, item_count)"
        " VALUES('album-b', ?, 'u-anna', 'Album', 1)",
        (VICTIM,),
    ),
    (
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height) VALUES('gi-b', 'album-b', 'u-anna',"
        " 'photo', 'gb.webp', 'gb-t.webp', 1, 1)",
        (),
    ),
    *[
        (
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                iid,
                iid,
                "00" * 32,
                "k1",
                "k2",
                f"https://{iid}/wh",
                f"wh-{iid}",
                "confirmed",
                "manual",
            ),
        )
        for iid in (SENDER, OTHER)
    ],
    *[
        (
            "INSERT INTO remote_users(user_id, instance_id, remote_username,"
            " display_name) VALUES(?,?,?,?)",
            (uid, iid, uid, uid),
        )
        for uid, iid in (("u-rem", SENDER), ("u-other", OTHER))
    ],
    (
        "INSERT INTO conversations(id, type, created_at)"
        " VALUES('conv-1', 'group_dm', datetime('now'))",
        (),
    ),
    *[
        (
            "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
            " content, type, media_blob_id, media_url, media_sync_status,"
            " created_at) VALUES(?,?,?,?,?,?,?,?, datetime('now'))",
            (mid, "conv-1", sender, "", "image", blob, url, status),
        )
        for mid, sender, blob, url, status in (
            ("local-dm", "u-anna", None, "api/media/local-dm.webp", None),
            ("m-remote", "u-rem", "m-remote", "api/media/m-remote.webp", None),
            ("m-pending", "u-rem", "m-pending", None, "pending"),
        )
    ],
]


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    media = tmp_dir / "media"
    media.mkdir(exist_ok=True)
    for name, body in EXISTING.items():
        (media / name).write_bytes(body)
    return app, db, tmp_dir


def _tree(root: pathlib.Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith("media-scope.db")
    }


async def _snapshot(db, root) -> tuple:
    rows = await db.fetchall(
        "SELECT id, media_url, media_sync_status FROM conversation_messages"
        " ORDER BY id",
        (),
    )
    return _tree(root), [tuple(r) for r in rows]


async def _send(app, event_type, payload, *, from_instance=SENDER, space_id=None):
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={
                    "bytes_b64": base64.b64encode(WEBP).decode("ascii"),
                    **payload,
                },
                space_id=space_id,
            )
        )


def _space(correlation: str, filename: str, **extra) -> dict:
    return {
        "post_id": correlation,
        "correlation_id": correlation,
        "filename": filename,
        **extra,
    }


_SPACE_ATTACKS = [
    pytest.param(_space("post-b", "b1.webp"), id="another space's post"),
    pytest.param(_space("gi-b", "gb.webp"), id="another space's gallery item"),
    pytest.param(_space("post-a", "b1.webp"), id="own post, another space's file"),
    pytest.param(_space("post-unseen", "b1.webp"), id="unseen row, existing file"),
    pytest.param(_space("post-unseen", "local-dm.webp"), id="unseen row, DM file"),
    pytest.param(_space("post-a", "a1.webp"), id="own post, file already present"),
    pytest.param(
        _space("post-a", "a1.webp", transfer_id="t", chunk_index=0, chunk_count=1),
        id="chunked, file already present",
    ),
    pytest.param(_space("post-a", "../escape.webp"), id="path traversal"),
    pytest.param(_space("post-a", ".partial"), id="dot name"),
    pytest.param(_space("post-b", "b1.webp", space_id=VICTIM), id="payload space"),
]


@pytest.mark.parametrize("payload", _SPACE_ATTACKS)
async def test_space_media_blob_outside_its_scope_writes_nothing(env, payload):
    app, db, root = env
    before = await _snapshot(db, root)
    await _send(app, FET.SPACE_MEDIA_BLOB, dict(payload), space_id=GATED)
    assert await _snapshot(db, root) == before


def _dm(message_id: str, blob_id: str | None = None) -> dict:
    return {
        "message_id": message_id,
        "media_blob_id": blob_id or message_id,
        "conversation_id": "conv-1",
        "mime_type": "image/webp",
    }


_DM_ATTACKS = [
    pytest.param(_dm("m-pending"), OTHER, id="another household's message"),
    pytest.param(_dm("local-dm"), SENDER, id="a local member's message"),
    pytest.param(_dm("m-pending", "other-blob"), SENDER, id="wrong blob id"),
    pytest.param(_dm("m-remote"), SENDER, id="file already present"),
    pytest.param(_dm("m-remote"), OTHER, id="file present, other household"),
    pytest.param(_dm("../escape"), SENDER, id="path traversal"),
]


@pytest.mark.parametrize(("payload", "sender"), _DM_ATTACKS)
async def test_dm_media_blob_outside_its_scope_writes_nothing(env, payload, sender):
    app, db, root = env
    before = await _snapshot(db, root)
    await _send(app, FET.DM_MEDIA_BLOB, dict(payload), from_instance=sender)
    assert await _snapshot(db, root) == before


async def test_legitimate_blobs_still_land(env):
    """Control: the post's own file in its own space, a blob that overtook
    its post, and the DM sender's own pending attachment all land."""
    app, db, root = env
    media = root / "media"
    await _send(
        app, FET.SPACE_MEDIA_BLOB, _space("post-a-new", "a-new.webp"), space_id=GATED
    )
    await _send(
        app, FET.SPACE_MEDIA_BLOB, _space("post-later", "later.webp"), space_id=GATED
    )
    await _send(app, FET.DM_MEDIA_BLOB, _dm("m-pending"))
    assert (media / "a-new.webp").read_bytes() == WEBP
    assert (media / "later.webp").read_bytes() == WEBP
    assert (media / "m-pending.webp").read_bytes() == WEBP
    row = await db.fetchone(
        "SELECT media_url, media_sync_status FROM conversation_messages"
        " WHERE id='m-pending'",
        (),
    )
    assert row["media_url"] == "api/media/m-pending.webp"
    assert row["media_sync_status"] is None
