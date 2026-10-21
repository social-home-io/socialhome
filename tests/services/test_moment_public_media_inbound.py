"""Follower side of encrypted moment media (v_63): fetch the blob from the
GFS, check its address, open it, keep the file — or store the moment
without it."""

from __future__ import annotations

import asyncio
import os

import pytest

from socialhome.moment_public_media import blob_id_of, seal_media
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services import moment_public_media_inbound as mmi
from socialhome.services.moment_public_media_inbound import (
    MediaJob,
    MomentPublicMediaInbound,
)

ITEM = os.urandom(32)
PICTURE = b"RIFF" + os.urandom(3000)


def _blob(data: bytes = PICTURE, *, role: str = "media", moment: str = "m1") -> bytes:
    return seal_media(
        item_key=ITEM, author_user_id="u-noor", moment_id=moment, role=role, data=data
    )


class _Resp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.content = self

    async def read(self, n: int = -1) -> bytes:
        """Like a stream: up to *n* bytes per call, then ``b""``."""
        out = self._body if n < 0 else self._body[:n]
        self._body = self._body[len(out) :]
        return out

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.gets: list[str] = []

    def get(self, url, **_kw):
        self.gets.append(url)
        bid = url.rsplit("/", 1)[-1]
        if bid in self.blobs:
            return _Resp(200, self.blobs[bid])
        return _Resp(404, b"")


@pytest.fixture
async def env(db, tmp_path):
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g1', 'g1', 'G', 'ff',"
        " 'https://g1.example', 'active', datetime('now'))"
    )
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    svc = MomentPublicMediaInbound(
        gfs_repo=SqliteGfsConnectionRepo(db), media_dir=media_dir
    )
    return {"svc": svc, "dir": media_dir}


def _media(blob: bytes, *, content_type: str = "image/webp", **extra) -> dict:
    return {
        "blob_id": blob_id_of(blob),
        "media_suite": "aesgcm-256-pad",
        "content_type": content_type,
        **extra,
    }


async def _fetch(svc, media: dict, *, moment: str = "m1"):
    return await svc.fetch(
        gfs_id="g1",
        author_user_id="u-noor",
        moment_id=moment,
        item_key=ITEM,
        media=media,
    )


async def test_a_picture_is_fetched_opened_and_kept(env):
    blob = _blob()
    session = _Session({blob_id_of(blob): blob})
    env["svc"].attach_session(session)
    url, kind = await _fetch(env["svc"], _media(blob))
    name = f"gm-{blob_id_of(blob)[:40]}.webp"
    assert (url, kind) == (f"api/media/{name}", "image")
    assert (env["dir"] / name).read_bytes() == PICTURE
    assert session.gets == [f"https://g1.example/gfs/moments/media/{blob_id_of(blob)}"]


async def test_a_video_brings_its_poster_under_the_same_name(env):
    video = _blob(b"\x1aE\xdf\xa3" + b"v" * 4000)
    poster = _blob(b"RIFFposter", role="poster")
    env["svc"].attach_session(
        _Session({blob_id_of(video): video, blob_id_of(poster): poster})
    )
    url, kind = await _fetch(
        env["svc"],
        _media(video, content_type="video/webm", poster_blob_id=blob_id_of(poster)),
    )
    stem = f"gm-{blob_id_of(video)[:40]}"
    assert (url, kind) == (f"api/media/{stem}.webm", "video")
    # The poster path the household derives for a ``.webm`` (same stem).
    assert (env["dir"] / f"{stem}.webp").read_bytes() == b"RIFFposter"


@pytest.mark.parametrize(
    "patch",
    [
        {"content_type": "text/html"},
        {"media_suite": "aesgcm-256"},
        {"blob_id": "not-hex"},
    ],
)
async def test_unknown_shapes_fetch_nothing(env, patch):
    blob = _blob()
    session = _Session({blob_id_of(blob): blob})
    env["svc"].attach_session(session)
    assert await _fetch(env["svc"], {**_media(blob), **patch}) == (None, None)
    assert session.gets == []


async def test_a_blob_that_doesnt_match_its_address_is_dropped(env):
    blob = _blob()
    env["svc"].attach_session(_Session({blob_id_of(blob): _blob(b"other")}))
    assert await _fetch(env["svc"], _media(blob)) == (None, None)
    assert list(env["dir"].iterdir()) == []


async def test_a_blob_for_another_moment_doesnt_open(env):
    blob = _blob(moment="m-other")
    env["svc"].attach_session(_Session({blob_id_of(blob): blob}))
    assert await _fetch(env["svc"], _media(blob)) == (None, None)


async def test_a_missing_blob_or_no_session_gives_no_media(env):
    blob = _blob()
    assert await _fetch(env["svc"], _media(blob)) == (None, None)  # no session
    env["svc"].attach_session(_Session({}))
    assert await _fetch(env["svc"], _media(blob)) == (None, None)


async def test_an_unknown_connection_fetches_nothing(env):
    blob = _blob()
    session = _Session({blob_id_of(blob): blob})
    env["svc"].attach_session(session)
    got = await env["svc"].fetch(
        gfs_id="nope",
        author_user_id="u-noor",
        moment_id="m1",
        item_key=ITEM,
        media=_media(blob),
    )
    assert got == (None, None) and session.gets == []


async def test_a_file_already_kept_is_not_rewritten(env):
    blob = _blob()
    env["svc"].attach_session(_Session({blob_id_of(blob): blob}))
    name = f"gm-{blob_id_of(blob)[:40]}.webp"
    (env["dir"] / name).write_bytes(PICTURE)
    url, _ = await _fetch(env["svc"], _media(blob))
    assert url == f"api/media/{name}"


async def test_jobs_run_in_the_background_and_call_back(env, monkeypatch):
    blob = _blob()
    env["svc"].attach_session(_Session({blob_id_of(blob): blob}))
    done: list = []

    async def _then(url, kind):
        done.append((url, kind))

    await env["svc"].start()
    await env["svc"].start()  # idempotent
    assert env["svc"].submit(
        MediaJob(
            gfs_id="g1",
            author_user_id="u-noor",
            moment_id="m1",
            item_key=ITEM,
            media=_media(blob),
            then=_then,
        )
    )
    for _ in range(100):
        if done:
            break
        await asyncio.sleep(0.01)
    assert done and done[0][1] == "image"
    await env["svc"].stop()


async def test_a_full_queue_refuses_more(env, monkeypatch):
    monkeypatch.setattr(mmi, "MAX_QUEUED_JOBS", 1)
    svc = MomentPublicMediaInbound(gfs_repo=env["svc"]._gfs_repo, media_dir=env["dir"])

    async def _then(url, kind):
        return None

    job = MediaJob(
        gfs_id="g1",
        author_user_id="u",
        moment_id="m",
        item_key=ITEM,
        media={},
        then=_then,
    )
    assert svc.submit(job)
    assert not svc.submit(job)


async def test_a_body_larger_than_any_blob_is_cut_off(env, monkeypatch):
    monkeypatch.setattr(mmi, "MAX_BLOB_BYTES", 100)
    blob = _blob()
    env["svc"].attach_session(_Session({blob_id_of(blob): blob}))
    assert await _fetch(env["svc"], _media(blob)) == (None, None)


async def test_forget_removes_only_files_it_kept(env):
    (env["dir"] / "gm-abc.webm").write_bytes(b"v")
    (env["dir"] / "gm-abc.webp").write_bytes(b"p")
    (env["dir"] / "mine.webp").write_bytes(b"m")
    await env["svc"].forget("api/media/gm-abc.webm")
    await env["svc"].forget("api/media/mine.webp")
    await env["svc"].forget(None)
    assert sorted(p.name for p in env["dir"].iterdir()) == ["mine.webp"]
