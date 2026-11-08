"""v_85 — file hashes in space content, end to end on a real app.

A hashed file is stored content-addressed (``sc-`` = H(space, h)) and only
bytes that hash to ``h`` ever land under that name. Who vouches for the
hash: the author household's envelope (live), the host's §25.6 stream
(which may hash content from before v_85 from its own copy) — never
another provider's stream.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, space_sync_receiver_key
from socialhome.domain.federation import FederationEventType as FET
from socialhome.domain.file_hashes import space_content_media_name
from socialhome.services.inbound_media_store import space_direct_media_name
from tests.protocol.test_space_content_authorship import (
    _SEED,
    AUTHOR,
    HOST,
    OTHER,
    SP,
    _config,
    _deliver,
)

pytestmark = pytest.mark.security


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    """The authorship suite's household: we are a member of ``SP``, hosted
    by ``HOST``; ``AUTHOR`` and ``OTHER`` seat members."""
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    return app, db


PIC = b"the author's picture"
H = hashlib.sha256(PIC).hexdigest()


def _files(name: str, h: str = H) -> dict:
    return {"files": {name: {"h": h}}, "files_suite": "sha256"}


async def _image_urls(db, post_id: str) -> str | None:
    row = await db.fetchone(
        "SELECT image_urls_json FROM space_posts WHERE id=?", (post_id,)
    )
    return None if row is None else row["image_urls_json"]


def _media(tmp_dir: pathlib.Path) -> pathlib.Path:
    return tmp_dir / "media"


def _post(post_id: str, author: str = "u-a", **extra) -> dict:
    return {
        "id": post_id,
        "author": author,
        "type": "image",
        "content": "",
        "image_urls": ["api/media/pic.webp"],
        **extra,
    }


async def test_a_live_hashed_post_and_its_bytes_land_content_addressed(env, tmp_dir):
    app, db = env
    await _deliver(
        app, FET.SPACE_POST_CREATED, _post("p-h", **_files("pic.webp")), sender=AUTHOR
    )
    sc = space_content_media_name(SP, H, "pic.webp")
    assert sc in (await _image_urls(db, "p-h") or "")
    await _deliver(
        app,
        FET.SPACE_MEDIA_BLOB,
        {
            "correlation_id": "p-h",
            "filename": "pic.webp",
            "bytes_b64": base64.b64encode(PIC).decode(),
            "h": H,
            "h_suite": "sha256",
        },
        sender=AUTHOR,
    )
    assert (_media(tmp_dir) / sc).read_bytes() == PIC


async def test_tampered_bytes_never_land(env, tmp_dir):
    app, _db = env
    await _deliver(
        app, FET.SPACE_POST_CREATED, _post("p-t", **_files("pic.webp")), sender=AUTHOR
    )
    # Another member pushes other bytes under the post's hash.
    await _deliver(
        app,
        FET.SPACE_MEDIA_BLOB,
        {
            "correlation_id": "p-t",
            "filename": "pic.webp",
            "bytes_b64": base64.b64encode(b"planted").decode(),
            "h": H,
            "h_suite": "sha256",
        },
        sender=OTHER,
    )
    assert not (_media(tmp_dir) / space_content_media_name(SP, H, "pic.webp")).exists()


async def test_a_raw_content_name_cannot_be_written(env, tmp_dir):
    app, _db = env
    sc = space_content_media_name(SP, H, "pic.webp")
    await _deliver(
        app,
        FET.SPACE_MEDIA_BLOB,
        {
            "correlation_id": "p-unknown",
            "filename": sc,
            "bytes_b64": base64.b64encode(b"planted").decode(),
        },
        sender=OTHER,
    )
    assert not (_media(tmp_dir) / sc).exists()
    assert not (_media(tmp_dir) / space_direct_media_name(SP, OTHER, sc)).exists()


async def test_the_hosts_catch_up_vouches_for_hashes(env):
    app, db = env
    await app[space_sync_receiver_key]._dispatch(
        "posts", SP, [_post("p-host", **_files("pic.webp"))], provider=HOST
    )
    assert space_content_media_name(SP, H, "pic.webp") in (
        await _image_urls(db, "p-host") or ""
    )


async def test_a_non_host_providers_hashes_are_ignored(env):
    """A member household streaming its own post may not vouch for hashes
    (decision B: only the host / a delegated admin): legacy names."""
    app, db = env
    await app[space_sync_receiver_key]._dispatch(
        "posts", SP, [_post("p-member", **_files("pic.webp"))], provider=AUTHOR
    )
    urls = await _image_urls(db, "p-member") or ""
    assert "sc-" not in urls
    assert space_direct_media_name(SP, AUTHOR, "pic.webp") in urls
