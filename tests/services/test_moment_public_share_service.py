"""Per-moment share links: the link carries the moment's item key in
its fragment and opens a sealed share copy on the GFS — minted only by the
author, only for a live public moment, only on the pinned viewer."""

from __future__ import annotations

import base64
import dataclasses
import os
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from socialhome.domain.moment import Moment
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.moment_public_media import blob_id_of, open_share
from socialhome.pinned_viewer import PinnedViewer
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicRegistrationRepo,
    SqliteMomentPublicShareRepo,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.moment_public_share_service import (
    MomentPublicShareService,
    ShareLinkUnavailableError,
)
from socialhome.services.moment_service import MomentNotFoundError

ITEM = os.urandom(32)
VIEWER = PinnedViewer(path="v/0123456789abcdef/", integrity="sha384-" + "A" * 64)
MEDIA = {
    "blob_id": "d" * 64,
    "media_suite": "aesgcm-256-pad",
    "content_type": "image/webp",
}


class _Media:
    def __init__(self, proven: set[str]) -> None:
        self.proven = proven
        self.uploads: list = []
        self.remembered: list = []
        self.ok = True

    async def supported(self, gfs_id: str) -> bool:
        return gfs_id in self.proven

    async def upload(self, gfs_id, prepared, *, author_user_id, expires_at):
        self.uploads.append((gfs_id, dict(prepared.blobs), author_user_id, expires_at))
        return self.ok

    def remember(self, author, moment_id, gfs_id, prepared):
        self.remembered.append((author, moment_id, gfs_id))


@pytest.fixture
async def env(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u-noor','noor','Noor Haddad','active'),"
        " ('u-sam','sam','Sam','active')"
    )
    for gid, url in (("g1", "https://g1.example"), ("g2", "http://evil.example")):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
            " inbox_url, status, paired_at) VALUES(?, ?, 'G', 'ff', ?, 'active',"
            " datetime('now'))",
            (gid, gid, url),
        )
    regs = SqliteMomentPublicRegistrationRepo(db)
    await regs.upsert(user_id="u-noor", gfs_id="g1", default_share=True)
    await regs.upsert(user_id="u-noor", gfs_id="g2", default_share=True)
    moments = SqliteMomentRepo(db)
    media = _Media({"g1", "g2"})
    svc = MomentPublicShareService(
        share_repo=SqliteMomentPublicShareRepo(db),
        moment_repo=moments,
        user_repo=SqliteUserRepo(db),
        registration_repo=regs,
        gfs_repo=SqliteGfsConnectionRepo(db),
        media_outbound=media,  # type: ignore[arg-type]
    )
    svc.attach_identity(key_manager=KeyManager(os.urandom(32)))
    svc.attach_viewer(base_url="https://viewer.social-home.io", viewer=VIEWER)
    return {"svc": svc, "moments": moments, "media": media, "db": db}


def _moment(mid: str = "m-1", **over) -> Moment:
    return dataclasses.replace(
        Moment(
            id=mid,
            author_user_id="u-noor",
            content="Bees swarmed the plum tree",
            media_url="api/media/plum.webp",
            media_type="image",
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="inst-noor",
            created_at="2026-10-21T08:00:00+00:00",
            expires_at="2099-10-22T08:00:00+00:00",
            is_public=True,
        ),
        **over,
    )


async def _published(env, **over) -> Moment:
    m = _moment(**over)
    await env["moments"].save(m)
    await env["svc"].record(m, item_key=ITEM, media=MEDIA)
    return m


def _fragment(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == "viewer.social-home.io"
    assert parts.path == "/v/0123456789abcdef/"
    return {k: v[0] for k, v in parse_qs(parts.fragment).items()}


async def test_the_author_gets_a_link_carrying_the_moments_item_key(env):
    await _published(env)
    url = await env["svc"].link(user_id="u-noor", moment_id="m-1")
    f = _fragment(url)
    assert f["g"] == "https://g1.example"
    assert (f["a"], f["m"]) == ("u-noor", "m-1")
    assert base64.urlsafe_b64decode(f["k"] + "=") == ITEM
    # The share copy went to that GFS once; it opens with the item key.
    [(gfs, blobs, author, expires)] = env["media"].uploads
    assert (gfs, author) == ("g1", "u-noor")
    # A share copy never outlives what a moment can (the GFS refuses more).
    assert expires <= time.time() + 7 * 86_400 + 5
    blob = blobs[f["b"]]
    assert blob_id_of(blob) == f["b"]
    body = open_share(
        blob=blob, item_key=ITEM, author_user_id="u-noor", moment_id="m-1"
    )
    assert body["content"] == "Bees swarmed the plum tree"
    assert body["author_name"] == "Noor Haddad"
    assert body["media"] == MEDIA
    assert "plum.webp" not in str(body)  # the author's local reference never travels
    # Asking again reuses the same copy: no second upload, the same link.
    assert await env["svc"].link(user_id="u-noor", moment_id="m-1") == url
    assert len(env["media"].uploads) == 1
    assert env["media"].remembered == [("u-noor", "m-1", "g1")]


async def test_only_the_author_of_a_live_public_moment_gets_a_link(env):
    await _published(env)
    with pytest.raises(MomentNotFoundError):
        await env["svc"].link(user_id="u-sam", moment_id="m-1")
    with pytest.raises(MomentNotFoundError):
        await env["svc"].link(user_id="u-noor", moment_id="m-unknown")
    await _published(env, mid="m-old", expires_at="2001-01-01T00:00:00+00:00")
    with pytest.raises(MomentNotFoundError):
        await env["svc"].link(user_id="u-noor", moment_id="m-old")
    # A household-only moment never got link material.
    await env["moments"].save(_moment("m-private", is_public=False))
    with pytest.raises(MomentNotFoundError):
        await env["svc"].link(user_id="u-noor", moment_id="m-private")
    assert env["media"].uploads == []


async def test_no_pinned_viewer_means_no_link(env):
    """A GFS-served page would read the key: there is no fallback."""
    await _published(env)
    env["svc"].attach_viewer(base_url="", viewer=VIEWER)
    with pytest.raises(ShareLinkUnavailableError):
        await env["svc"].link(user_id="u-noor", moment_id="m-1")
    env["svc"].attach_viewer(base_url="https://viewer.social-home.io", viewer=None)
    with pytest.raises(ShareLinkUnavailableError):
        await env["svc"].link(user_id="u-noor", moment_id="m-1")


async def test_no_gfs_that_keeps_media_means_no_link(env):
    await _published(env)
    env["media"].proven = set()
    with pytest.raises(ShareLinkUnavailableError):
        await env["svc"].link(user_id="u-noor", moment_id="m-1")


async def test_a_gfs_the_viewer_wouldnt_accept_is_never_named(env):
    """g2 is plain http on a public host — the pinned page refuses it."""
    await _published(env)
    with pytest.raises(ShareLinkUnavailableError):
        await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g2")
    with pytest.raises(ShareLinkUnavailableError):
        await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g-other")


async def test_a_refused_upload_mints_no_link(env):
    await _published(env)
    env["media"].ok = False
    with pytest.raises(ShareLinkUnavailableError):
        await env["svc"].link(user_id="u-noor", moment_id="m-1")


async def test_record_without_identity_keeps_nothing(db):
    svc = MomentPublicShareService(
        share_repo=SqliteMomentPublicShareRepo(db),
        moment_repo=SqliteMomentRepo(db),
        user_repo=SqliteUserRepo(db),
        registration_repo=SqliteMomentPublicRegistrationRepo(db),
        gfs_repo=SqliteGfsConnectionRepo(db),
        media_outbound=_Media(set()),  # type: ignore[arg-type]
    )
    await svc.record(_moment(), item_key=ITEM, media=None)
    assert await SqliteMomentPublicShareRepo(db).get("m-1") is None
