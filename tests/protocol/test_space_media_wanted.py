"""v_92 — ``SPACE_MEDIA_WANTED`` answered on a real app (provider side).

We are a plain member household of ``SP`` (our person ``u-local``). A
member household asking for a file is answered only for a LIVE item of
that space whose file's content name matches — computed from our unhashed
local copy — and only for files the v_86 gate lets us ship: as a plain
member, our own people's files, never another person's. A household with
no seat in the space gets nothing. An answer is a media outbox row for the
requester (a ``SPACE_MEDIA_BLOB`` push), sent under the asked-for
content name and read from our local copy.
"""

from __future__ import annotations

import hashlib

import pytest

from socialhome.domain.federation import FederationEventType as FET
from socialhome.domain.file_hashes import space_content_media_name
from tests.protocol.test_content_file_hashes import _media, env
from tests.protocol.test_space_content_authorship import (
    LOCAL_USER,
    OTHER,
    SP,
    STRANGER,
    _deliver,
)

pytestmark = pytest.mark.security

__all__ = ["env"]

PIC = b"our person's picture"
NAME = space_content_media_name(SP, hashlib.sha256(PIC).hexdigest(), "pic.webp")


async def _seed(db, tmp_dir, *, author: str, deleted: int = 0) -> None:
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)", (SP, OTHER)
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, image_urls_json,"
        " deleted) VALUES('p-w', ?, ?, 'image', '[\"api/media/pic.webp\"]', ?)",
        (SP, author, deleted),
    )
    _media(tmp_dir).mkdir(parents=True, exist_ok=True)
    (_media(tmp_dir) / "pic.webp").write_bytes(PIC)


async def _queued(db) -> list[tuple[str, str, str]]:
    rows = await db.fetchall(
        "SELECT blob_id, target_instance_id, correlation_id FROM space_media_outbox"
    )
    return [(r["blob_id"], r["target_instance_id"], r["correlation_id"]) for r in rows]


async def _sources(db) -> list[str]:
    rows = await db.fetchall("SELECT bytes_path FROM space_media_outbox")
    return [r["bytes_path"].rsplit("/", 1)[-1] for r in rows]


def _ask(name: str = NAME) -> dict:
    return {"space_id": SP, "files": [{"item": "p-w", "name": name}]}


async def test_a_member_household_gets_our_persons_file(env, tmp_dir):
    app, db = env
    await _seed(db, tmp_dir, author=LOCAL_USER)
    await _deliver(app, FET.SPACE_MEDIA_WANTED, _ask(), sender=OTHER)
    # Sent as the content name the requester asked for, from our own file.
    assert await _queued(db) == [(NAME, OTHER, "p-w")]
    assert await _sources(db) == ["pic.webp"]


async def test_a_household_without_a_seat_gets_nothing(env, tmp_dir):
    app, db = env
    await _seed(db, tmp_dir, author=LOCAL_USER)
    await _deliver(app, FET.SPACE_MEDIA_WANTED, _ask(), sender=STRANGER)
    assert await _queued(db) == []


async def test_a_plain_member_never_ships_another_persons_file(env, tmp_dir):
    app, db = env
    await _seed(db, tmp_dir, author="u-a")
    await _deliver(app, FET.SPACE_MEDIA_WANTED, _ask(), sender=OTHER)
    assert await _queued(db) == []


async def test_a_deleted_item_or_a_name_it_does_not_show_ships_nothing(env, tmp_dir):
    app, db = env
    await _seed(db, tmp_dir, author=LOCAL_USER, deleted=1)
    await _deliver(app, FET.SPACE_MEDIA_WANTED, _ask(), sender=OTHER)
    assert await _queued(db) == []
    await db.enqueue("UPDATE space_posts SET deleted = 0 WHERE id = 'p-w'")
    other = space_content_media_name(SP, "f" * 64, "pic.webp")
    await _deliver(app, FET.SPACE_MEDIA_WANTED, _ask(other), sender=OTHER)
    assert await _queued(db) == []
