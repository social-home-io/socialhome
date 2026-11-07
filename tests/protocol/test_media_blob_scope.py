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
from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.gallery import GalleryItem
from socialhome.services.inbound_media_store import dm_media_name

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
    dm_media_name(SENDER, "m-remote", ".webp"): WEBP + b"remote DM attachment",
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
            (
                "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
                " identity_public_key) VALUES(?,?,?,?,?)"
            ),
            (sid, sid, "the-host", "anna", "00" * 32),
        )
        for sid in (GATED, VICTIM)
    ],
    *[
        (
            (
                "INSERT INTO space_posts(id, space_id, author, type, content,"
                " media_url) VALUES(?,?,?,?,?,?)"
            ),
            (pid, sid, "u-x", "image", "", f"api/media/{fn}"),
        )
        for pid, sid, fn in (
            ("post-a", GATED, "a1.webp"),
            ("post-a-new", GATED, "a-new.webp"),
            ("post-b", VICTIM, "b1.webp"),
        )
    ],
    (
        (
            "INSERT INTO gallery_albums(id, space_id, owner_user_id, name, item_count)"
            " VALUES('album-b', ?, 'u-anna', 'Album', 1)"
        ),
        (VICTIM,),
    ),
    (
        (
            "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
            " thumbnail_filename, width, height) VALUES('gi-b', 'album-b', 'u-anna',"
            " 'photo', 'gb.webp', 'gb-t.webp', 1, 1)"
        ),
        (),
    ),
    *[
        (
            (
                "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
                " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
                " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)"
            ),
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
            (
                "INSERT INTO remote_users(user_id, instance_id, remote_username,"
                " display_name) VALUES(?,?,?,?)"
            ),
            (uid, iid, uid, uid),
        )
        for uid, iid in (("u-rem", SENDER), ("u-other", OTHER))
    ],
    (
        (
            "INSERT INTO conversations(id, type, created_at)"
            " VALUES('conv-1', 'group_dm', datetime('now'))"
        ),
        (),
    ),
    *[
        (
            (
                "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
                " content, type, media_blob_id, media_url, media_sync_status,"
                " created_at) VALUES(?,?,?,?,?,?,?,?, datetime('now'))"
            ),
            (mid, "conv-1", sender, "", "image", blob, url, status),
        )
        for mid, sender, blob, url, status in (
            ("local-dm", "u-anna", None, "api/media/local-dm.webp", None),
            (
                "m-remote",
                "u-rem",
                "m-remote",
                f"api/media/{dm_media_name(SENDER, 'm-remote', '.webp')}",
                None,
            ),
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
                timestamp=datetime.now(UTC).isoformat(),
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
    pytest.param(
        _space("post-unseen", dm_media_name(SENDER, "m-pending", ".webp")),
        id="unseen row, a pending DM's name",
    ),
    pytest.param(_space("post-a", "DM-anything.webp"), id="reserved dm- prefix"),
    pytest.param(_space("post-unseen", "gm-" + "a" * 40 + ".webp"), id="GFS moment"),
    pytest.param(_space("post-unseen", "m-new.preview.webp"), id="DM preview"),
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
    """Control: the post's own file in its own space from its author's
    household, a host's blob that overtook its post, and the DM sender's own
    pending attachment all land."""
    app, db, root = env
    media = root / "media"
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?, ?, 'u-x', 'member')",
        (GATED, SENDER),
    )
    await _send(
        app, FET.SPACE_MEDIA_BLOB, _space("post-a-new", "a-new.webp"), space_id=GATED
    )
    await _send(
        app,
        FET.SPACE_MEDIA_BLOB,
        _space("post-later", "later.webp"),
        from_instance="the-host",
        space_id=GATED,
    )
    await _send(app, FET.DM_MEDIA_BLOB, _dm("m-pending"))
    assert (media / "a-new.webp").read_bytes() == WEBP
    assert (media / "later.webp").read_bytes() == WEBP
    pending = dm_media_name(SENDER, "m-pending", ".webp")
    assert (media / pending).read_bytes() == WEBP
    row = await db.fetchone(
        "SELECT media_url, media_sync_status FROM conversation_messages"
        " WHERE id='m-pending'",
        (),
    )
    assert row["media_url"] == f"api/media/{pending}"
    assert row["media_sync_status"] is None


async def test_a_federated_gallery_items_full_file_and_thumbnail_both_land(env):
    """Regression: once a live ``SPACE_GALLERY_ITEM_CREATED`` lands, the scope
    check has positive evidence — the row — and accepts only the files the
    row references. The item went out as the thumbnail-only projection, so
    the row carried no full ``url`` and the full-size picture was refused
    (and the gallery had nothing to open on zoom). The payload is built with
    the sender's real serializer, not a hand-written dict."""
    app, db, root = env
    await db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name)"
        " VALUES('album-a', ?, 'u-rem', 'Theirs')",
        (GATED,),
    )
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?, ?, 'u-rem', 'member')",
        (GATED, SENDER),
    )
    item = GalleryItem(
        id="gi-a",
        album_id="album-a",
        uploaded_by="u-rem",
        item_type="photo",
        url="api/media/ga-full.webp",
        thumbnail_url="api/media/ga-thumb.webp",
        width=4,
        height=3,
    )
    for handler in app[federation_service_key]._event_registry.handlers_for(
        FET.SPACE_GALLERY_ITEM_CREATED
    ):
        await handler(
            FederationEvent(
                msg_id="m-item",
                event_type=FET.SPACE_GALLERY_ITEM_CREATED,
                from_instance=SENDER,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload={**item.to_federation_dict(), "space_id": GATED},
                space_id=GATED,
            )
        )
    row = await db.fetchone(
        "SELECT filename, thumbnail_filename FROM gallery_items WHERE id='gi-a'", ()
    )
    assert row is not None
    assert (row["filename"], row["thumbnail_filename"]) == (
        "ga-full.webp",
        "ga-thumb.webp",
    )
    for name in ("ga-thumb.webp", "ga-full.webp"):
        await _send(app, FET.SPACE_MEDIA_BLOB, _space("gi-a", name), space_id=GATED)
    media = root / "media"
    assert (media / "ga-thumb.webp").read_bytes() == WEBP
    assert (media / "ga-full.webp").read_bytes() == WEBP


# ── One media namespace: a space blob must never become a DM's file ────

EVIL = WEBP + b"planted by another household"
GOOD = WEBP + b"the sender's own attachment"


def _b64(body: bytes) -> str:
    return base64.b64encode(body).decode("ascii")


async def _dm_message(app, message_id: str) -> None:
    """``SENDER``'s ``u-rem`` opens a 1:1 with local ``anna`` carrying an
    image whose full bytes follow as ``DM_MEDIA_BLOB``."""
    await _send(
        app,
        FET.DM_MESSAGE,
        {
            "conversation_id": f"conv-{message_id}",
            "message_id": message_id,
            "sender_user_id": "u-rem",
            "recipient_user_ids": ["u-anna"],
            "content": "",
            "type": "image",
            "media_blob_id": message_id,
            "mime_type": "image/webp",
        },
    )


async def _dm_file(db, root, message_id: str) -> bytes | None:
    """The bytes the DM row's ``media_url`` serves, or ``None``."""
    row = await db.fetchone(
        "SELECT media_url FROM conversation_messages WHERE id=?", (message_id,)
    )
    assert row is not None
    url = row["media_url"]
    if not url:
        return None
    path = root / "media" / url.removeprefix("api/media/")
    return path.read_bytes() if path.is_file() else None


def _planted_names(message_id: str) -> list[str]:
    """Every name a DM attachment for ``message_id`` from ``SENDER`` could
    be stored under: the legacy ``<message_id><ext>`` and the current
    sender-bound DM name."""
    return [f"{message_id}.webp", dm_media_name(SENDER, message_id, ".webp")]


async def _plant(app, name: str) -> None:
    await _send(
        app,
        FET.SPACE_MEDIA_BLOB,
        {**_space("post-unseen", name), "bytes_b64": _b64(EVIL)},
        from_instance=OTHER,
        space_id=GATED,
    )


def _dm_blob(message_id: str, body: bytes) -> dict:
    return {
        **_dm(message_id),
        "conversation_id": f"conv-{message_id}",
        "bytes_b64": _b64(body),
    }


async def test_space_blob_planted_before_the_dm_is_never_adopted(env):
    """Probe: another household plants the DM's file name through an
    unknown space row, then the DM arrives — the DM must not show it."""
    app, db, root = env
    for name in _planted_names("m-new"):
        await _plant(app, name)
    await _dm_message(app, "m-new")
    assert await _dm_file(db, root, "m-new") != EVIL
    await _send(app, FET.DM_MEDIA_BLOB, _dm_blob("m-new", GOOD))
    assert await _dm_file(db, root, "m-new") == GOOD


async def test_space_blob_planted_while_the_dm_is_pending_is_never_adopted(env):
    """Probe: the DM row waits for its bytes; another household plants the
    name, then the sender's own blob lands — the row gets the sender's
    bytes, never the planted file."""
    app, db, root = env
    for name in _planted_names("m-pending"):
        await _plant(app, name)
    assert await _dm_file(db, root, "m-pending") is None
    await _send(app, FET.DM_MEDIA_BLOB, {**_dm("m-pending"), "bytes_b64": _b64(GOOD)})
    assert await _dm_file(db, root, "m-pending") == GOOD


async def test_another_households_dm_blob_before_the_dm_is_never_adopted(env):
    """Probe: before the DM row exists any household's ``DM_MEDIA_BLOB`` is
    accepted (it may have overtaken its message) — but only the sender's
    own household's bytes may ever become the DM's file."""
    app, db, root = env
    await _send(app, FET.DM_MEDIA_BLOB, _dm_blob("m-new", EVIL), from_instance=OTHER)
    await _dm_message(app, "m-new")
    assert await _dm_file(db, root, "m-new") != EVIL
    await _send(app, FET.DM_MEDIA_BLOB, _dm_blob("m-new", GOOD))
    assert await _dm_file(db, root, "m-new") == GOOD


async def test_dm_blob_overtaking_its_message_is_adopted(env):
    """Control: the sender's own blob that overtook its DM_MESSAGE is the
    DM's file once the message lands."""
    app, db, root = env
    await _send(app, FET.DM_MEDIA_BLOB, _dm_blob("m-new", GOOD))
    await _dm_message(app, "m-new")
    assert await _dm_file(db, root, "m-new") == GOOD


# ── Sender binding: only the row's household (or the host) delivers its file ──
#
# The first-claim attack: a member household M that learned a file name F
# (from A's post, delivered to M) pushes ITS bytes as F to another member
# before A's push lands. Write-once would then keep M's bytes for every row
# naming F — and, since #1309, a host poisoned this way re-seals them for its
# link-joined readers. A ``SPACE_MEDIA_BLOB`` is accepted only from the row's
# author household or a household with content authority (the host above
# all); a blob for a row not seen yet is held per (sender, file) and adopted
# only when the row lands and binds that sender.

HOST = "the-host"
MALLORY = OTHER  # a member household too — just not the author's


async def _seat(db, household: str, user_id: str, role: str = "member") -> None:
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?, ?, ?, ?)",
        (GATED, household, user_id, role),
    )


async def _members(db) -> None:
    """``u-rem`` (on SENDER, the author) and ``u-other`` (on MALLORY) both
    hold member seats in the gated space."""
    await _seat(db, SENDER, "u-rem")
    await _seat(db, MALLORY, "u-other")


async def _post_row(db, post_id: str, filename: str, author: str = "u-rem") -> None:
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, media_url)"
        " VALUES(?,?,?,?,?,?)",
        (post_id, GATED, author, "image", "", f"api/media/{filename}"),
    )


async def _push(app, correlation, filename, body, *, sender):
    await _send(
        app,
        FET.SPACE_MEDIA_BLOB,
        {**_space(correlation, filename), "bytes_b64": _b64(body)},
        from_instance=sender,
        space_id=GATED,
    )


async def _post_arrives(app, post_id: str, filename: str) -> None:
    """``SENDER``'s ``u-rem`` posts a picture: the live SPACE_POST_CREATED."""
    await _send(
        app,
        FET.SPACE_POST_CREATED,
        {
            "id": post_id,
            "space_id": GATED,
            "author": "u-rem",
            "type": "image",
            "content": "",
            "media_url": f"api/media/{filename}",
            "created_at": datetime.now(UTC).isoformat(),
        },
        space_id=GATED,
    )
    assert await app[db_key].fetchone(
        "SELECT id FROM space_posts WHERE id=?", (post_id,)
    ), "the post itself must land for this test to mean anything"


def _file(root, name: str) -> bytes | None:
    path = root / "media" / name
    return path.read_bytes() if path.is_file() else None


async def test_another_member_cannot_first_claim_a_known_posts_file(env):
    app, db, root = env
    await _members(db)
    await _post_row(db, "post-f", "f.webp")
    await _push(app, "post-f", "f.webp", EVIL, sender=MALLORY)
    assert _file(root, "f.webp") is None
    await _push(app, "post-f", "f.webp", GOOD, sender=SENDER)
    assert _file(root, "f.webp") == GOOD


async def test_author_blob_before_its_post_is_adopted_when_the_post_lands(env):
    app, db, root = env
    await _members(db)
    await _push(app, "post-early", "early.webp", GOOD, sender=SENDER)
    assert _file(root, "early.webp") is None  # held, not published yet
    await _post_arrives(app, "post-early", "early.webp")
    assert _file(root, "early.webp") == GOOD


async def test_another_members_blob_before_the_post_is_never_adopted(env):
    app, db, root = env
    await _members(db)
    await _push(app, "post-early", "early.webp", EVIL, sender=MALLORY)
    await _post_arrives(app, "post-early", "early.webp")
    assert _file(root, "early.webp") is None
    assert not list((root / "media" / ".partial").glob("*")), (
        "the refused held copy is dropped once its row decides"
    )
    await _push(app, "post-early", "early.webp", GOOD, sender=SENDER)
    assert _file(root, "early.webp") == GOOD


async def test_both_held_only_the_authors_copy_is_adopted(env):
    app, db, root = env
    await _members(db)
    await _push(app, "post-early", "early.webp", EVIL, sender=MALLORY)
    await _push(app, "post-early", "early.webp", GOOD, sender=SENDER)
    await _post_arrives(app, "post-early", "early.webp")
    assert _file(root, "early.webp") == GOOD


async def test_the_hosts_catch_up_push_is_accepted(env):
    """§25.6: the host ships every member's pictures to a newcomer — for a
    row it already streamed, and for one the blob overtook."""
    app, db, root = env
    await _members(db)
    await _post_row(db, "post-f", "f.webp")
    await _push(app, "post-f", "f.webp", GOOD, sender=HOST)
    await _push(app, "post-unseen-yet", "u.webp", GOOD, sender=HOST)
    assert _file(root, "f.webp") == GOOD
    assert _file(root, "u.webp") == GOOD


async def test_a_mesh_routed_author_push_is_accepted(env):
    """Over the mesh the inner event's ``from_instance`` is the
    authenticated origin (``SpaceRoutedHandler``), i.e. the author."""
    app, db, root = env
    await _members(db)
    await _post_row(db, "post-f", "f.webp")
    handlers = app[federation_service_key]._event_registry.handlers_for(
        FET.SPACE_MEDIA_BLOB
    )
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m-routed",
                event_type=FET.SPACE_MEDIA_BLOB,
                from_instance=SENDER,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload={**_space("post-f", "f.webp"), "bytes_b64": _b64(GOOD)},
                space_id=GATED,
                routed_path=[SENDER, MALLORY, "us"],
                routed_route_id="r-1",
            )
        )
    assert _file(root, "f.webp") == GOOD


async def test_gallery_file_binds_to_the_uploaders_household(env):
    app, db, root = env
    await _members(db)
    await db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name)"
        " VALUES('album-a', ?, 'u-rem', 'Theirs')",
        (GATED,),
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height) VALUES('gi-a', 'album-a', 'u-rem',"
        " 'photo', 'ga.webp', 'ga-t.webp', 1, 1)",
        (),
    )
    await _push(app, "gi-a", "ga.webp", EVIL, sender=MALLORY)
    await _push(app, "gi-a", "ga-t.webp", GOOD, sender=SENDER)
    assert _file(root, "ga.webp") is None
    assert _file(root, "ga-t.webp") == GOOD


async def test_moderation_item_file_binds_to_the_submitters_household(env):
    app, db, root = env
    await _members(db)
    await db.enqueue(
        "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
        " submitted_by, payload_json, submitted_at, expires_at)"
        " VALUES('item-1', ?, 'calendar', 'edit', 'u-rem', ?,"
        " datetime('now'), datetime('now', '+7 days'))",
        (
            GATED,
            (
                '{"entity": "event", "target_id": "ev-1",'
                ' "patch": {"cover_url": "/api/media/mod.webp"}}'
            ),
        ),
    )
    await _push(app, "item-1", "mod.webp", EVIL, sender=MALLORY)
    assert _file(root, "mod.webp") is None
    await _push(app, "item-1", "mod.webp", GOOD, sender=SENDER)
    assert _file(root, "mod.webp") == GOOD


async def test_calendar_cover_comes_from_a_writer_household_only(env):
    """Calendar events are collaborative — any member may change the cover
    (and its household ships it) — so a writer household is accepted; a
    household with no seat in the space is not."""
    app, db, root = env
    await _members(db)
    await db.enqueue(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
        " end_dt, created_by, cover_url) VALUES('ev-1', ?, 'Party',"
        " '2099-01-01T10:00:00+00:00', '2099-01-01T11:00:00+00:00', 'u-rem',"
        " 'api/media/cover.webp')",
        (GATED,),
    )
    await _push(app, "ev-1", "cover.webp", EVIL, sender="peer-stranger")
    assert _file(root, "cover.webp") is None
    await _push(app, "ev-1", "cover.webp", GOOD, sender=MALLORY)
    assert _file(root, "cover.webp") == GOOD


@pytest.mark.parametrize(
    "event_type",
    [
        FET.SPACE_POST_CREATED,
        FET.BAZAAR_LISTING_CREATED,
        FET.SPACE_GALLERY_ITEM_CREATED,
        FET.SPACE_GALLERY_ITEM_UPDATED,
        FET.SPACE_CALENDAR_EVENT_CREATED,
        FET.SPACE_CALENDAR_EVENT_UPDATED,
        FET.SPACE_MODERATION_SUBMITTED,
    ],
)
async def test_held_media_adoption_runs_after_the_row_is_stored(env, event_type):
    """The adopter re-judges held blobs once a row may have landed, so it
    must be the LAST handler of every event that stores a media row."""
    app, _db, _root = env
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert len(handlers) >= 2
    assert getattr(handlers[-1], "__name__", "") == "adopt_held_space_media"
