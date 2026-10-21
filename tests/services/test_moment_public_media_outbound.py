"""Author side of encrypted moment media (v_63): seal each file once,
upload the same blob to every GFS that proved it, withdraw it on delete."""

from __future__ import annotations

import json
import os

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.moment import Moment
from socialhome.moment_public_media import (
    MAX_MEDIA_BYTES,
    blob_id_of,
    open_media,
)
from socialhome.moment_public_seal import verify_wire
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.moment_public_media_outbound import (
    MomentPublicMediaOutbound,
)

KP = generate_identity_keypair()
ITEM = os.urandom(32)


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.puts: list[tuple[str, bytes, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def put(self, url, *, data=None, headers=None, **_kw):
        self.puts.append((url, data, headers or {}))
        return _Resp(self.status)

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _Resp(200)


class _Gfs:
    def __init__(self, proven: set[str]) -> None:
        self.proven = proven

    async def encrypted_moment_media_supported(self, gfs_id: str) -> bool:
        return gfs_id in self.proven


@pytest.fixture
async def env(db, tmp_path):
    for gid in ("g1", "g2"):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
            " inbox_url, status, paired_at) VALUES(?, ?, 'G', 'ff', ?, 'active',"
            " datetime('now'))",
            (gid, gid, f"https://{gid}.example"),
        )
    media = tmp_path / "media"
    media.mkdir()
    svc = MomentPublicMediaOutbound(
        gfs_repo=SqliteGfsConnectionRepo(db),
        gfs_service=_Gfs({"g1"}),
        media_dir=media,
    )
    session = _Session()
    svc.attach_session(session)
    svc.attach_identity(own_instance_id="inst-a", signing_key=KP.private_key)
    return {"svc": svc, "dir": media, "session": session}


def _moment(url: str | None, media_type: str | None = "image") -> Moment:
    return Moment(
        id="m1",
        author_user_id="u-noor",
        content="bees",
        media_url=url,
        media_type=media_type,
        duration_ms=1000 if media_type == "video" else None,
        parent_moment_id=None,
        origin_instance_id="inst-a",
        created_at="2026-10-20T08:00:00+00:00",
        expires_at="2026-10-21T08:00:00+00:00",
        is_public=True,
    )


async def test_a_picture_is_sealed_once_and_uploaded_where_proven(env):
    (env["dir"] / "abc.webp").write_bytes(b"RIFF" + b"p" * 2000)
    prepared = await env["svc"].prepare(_moment("api/media/abc.webp"), item_key=ITEM)
    assert prepared is not None and prepared.ready
    media = prepared.body
    assert media["content_type"] == "image/webp"
    assert media["media_suite"] == "aesgcm-256-pad"
    blob = prepared.blobs[media["blob_id"]]
    assert blob_id_of(blob) == media["blob_id"]
    assert b"pppp" not in blob
    assert open_media(
        blob=blob, item_key=ITEM, author_user_id="u-noor", moment_id="m1", role="media"
    ).startswith(b"RIFF")
    assert await env["svc"].upload(
        "g1", prepared, author_user_id="u-noor", expires_at=2_000_000_000
    )
    assert not await env["svc"].upload(
        "g2", prepared, author_user_id="u-noor", expires_at=2_000_000_000
    )
    [(url, data, headers)] = env["session"].puts
    assert url == f"https://g1.example/gfs/moments/media/{media['blob_id']}"
    assert data == blob
    # Header-signed by the author's household.
    signed = {
        "instance_id": "inst-a",
        "blob_id": media["blob_id"],
        "author_user_id": "u-noor",
        "expires_at": int(headers["X-SH-Expires"]),
        "ts": int(headers["X-SH-Timestamp"]),
        "signature": headers["X-SH-Signature"],
    }
    assert verify_wire(signed, KP.public_key.hex())
    assert headers["X-SH-Author"] == "u-noor" and headers["X-SH-Instance"] == "inst-a"


async def test_a_video_brings_its_poster(env):
    (env["dir"] / "v1.webm").write_bytes(b"\x1aE\xdf\xa3" + b"v" * 3000)
    (env["dir"] / "v1.webp").write_bytes(b"RIFFposter")
    prepared = await env["svc"].prepare(
        _moment("api/media/v1.webm", "video"), item_key=ITEM
    )
    assert prepared is not None
    poster = prepared.blobs[prepared.body["poster_blob_id"]]
    assert (
        open_media(
            blob=poster,
            item_key=ITEM,
            author_user_id="u-noor",
            moment_id="m1",
            role="poster",
        )
        == b"RIFFposter"
    )
    await env["svc"].upload(
        "g1", prepared, author_user_id="u-noor", expires_at=2_000_000_000
    )
    assert len(env["session"].puts) == 2


async def test_a_video_still_processing_is_not_ready(env):
    prepared = await env["svc"].prepare(
        _moment("api/media/later.webm", "video"), item_key=ITEM
    )
    assert prepared is not None and not prepared.ready


@pytest.mark.parametrize("name", ["x.txt", "noext", "../etc.webp"])
async def test_files_we_dont_send(env, name):
    assert await env["svc"].prepare(_moment(f"api/media/{name}"), item_key=ITEM) is None


async def test_no_media_or_a_missing_picture_or_too_big(env):
    assert await env["svc"].prepare(_moment(None, None), item_key=ITEM) is None
    assert (
        await env["svc"].prepare(_moment("api/media/gone.webp"), item_key=ITEM) is None
    )
    big = env["dir"] / "big.webp"
    with big.open("wb") as fh:
        fh.truncate(MAX_MEDIA_BYTES + 1)
    assert (
        await env["svc"].prepare(_moment("api/media/big.webp"), item_key=ITEM) is None
    )


async def test_a_refused_upload_reports_false(env):
    (env["dir"] / "abc.webp").write_bytes(b"RIFF")
    env["session"].status = 429
    prepared = await env["svc"].prepare(_moment("api/media/abc.webp"), item_key=ITEM)
    assert not await env["svc"].upload(
        "g1", prepared, author_user_id="u-noor", expires_at=2_000_000_000
    )


async def test_withdraw_deletes_what_was_uploaded(env):
    (env["dir"] / "abc.webp").write_bytes(b"RIFF")
    prepared = await env["svc"].prepare(_moment("api/media/abc.webp"), item_key=ITEM)
    await env["svc"].upload(
        "g1", prepared, author_user_id="u-noor", expires_at=2_000_000_000
    )
    env["svc"].remember("u-noor", "m1", "g1", prepared)
    assert await env["svc"].withdraw("u-noor", "m1") == 1
    [(url, body)] = env["session"].posts
    bid = prepared.body["blob_id"]
    assert url == f"https://g1.example/gfs/moments/media/{bid}/delete"
    assert verify_wire(body, KP.public_key.hex())
    assert json.dumps(body).count(bid) == 1
    assert await env["svc"].withdraw("u-noor", "m1") == 0
