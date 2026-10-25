"""Per-moment share links: the link carries the moment's item key in
its fragment and opens a sealed share copy on the GFS — minted only by the
author, only for a live public moment, only on the pinned viewer."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
from datetime import UTC, datetime
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
from socialhome.services.moment_public_media_outbound import PreparedMedia
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


RESEALED = {
    "blob_id": "e" * 64,
    "media_suite": "aesgcm-256-pad",
    "content_type": "image/webp",
}


class _Media:
    def __init__(self, proven: set[str]) -> None:
        self.proven = proven
        self.uploads: list = []
        self.remembered: list = []
        self.prepared: list = []
        self.ok = True
        #: What :meth:`prepare` hands back: a sealed file, ``None`` (nothing
        #: this build sends) or ``"pending"`` (a video still transcoding).
        self.next: object = "file"

    async def supported(self, gfs_id: str) -> bool:
        return gfs_id in self.proven

    async def prepare(self, moment, *, item_key):
        self.prepared.append((moment.id, item_key))
        if self.next is None:
            return None
        if self.next == "pending":
            return PreparedMedia(ready=False)
        return PreparedMedia(body=dict(RESEALED), blobs={"e" * 64: b"file"})

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
    for gid, url in (
        ("g1", "https://g1.example"),
        ("g2", "http://evil.example"),
        ("g3", "https://g3.example"),
    ):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
            " inbox_url, status, paired_at) VALUES(?, ?, 'G', 'ff', ?, 'active',"
            " datetime('now'))",
            (gid, gid, url),
        )
    regs = SqliteMomentPublicRegistrationRepo(db)
    await regs.upsert(user_id="u-noor", gfs_id="g1", default_share=True)
    await regs.upsert(user_id="u-noor", gfs_id="g2", default_share=True)
    await regs.upsert(user_id="u-noor", gfs_id="g3", default_share=True)
    moments = SqliteMomentRepo(db)
    media = _Media({"g1", "g2", "g3"})
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
    # At publish the file went to g1 only (g3 was registered later, or
    # refused it then).
    await env["svc"].record(m, item_key=ITEM, media_by_gfs={"g1": MEDIA})
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
    # v_66: the share copy is asked for the moment's own life — each GFS is
    # then asked for as much of it as it keeps (``keep_until``).
    assert expires == int(datetime(2099, 10, 22, 8, tzinfo=UTC).timestamp())
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
    # g1 already held the file: nothing was sealed again.
    assert env["media"].prepared == []


async def test_a_gfs_without_the_moments_file_gets_it_with_the_link(env):
    """A link names a GFS — it must hold the moment's file, or the viewer
    would read "no longer available" for a live moment."""
    await _published(env)
    url = await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g3")
    f = _fragment(url)
    assert f["g"] == "https://g3.example"
    # The file is sealed again under the same item key and uploaded to g3
    # first; then the share copy, naming that blob.
    assert env["media"].prepared == [("m-1", ITEM)]
    [(gfs_a, file_blobs, *_), (gfs_b, share_blobs, *_)] = env["media"].uploads
    assert (gfs_a, list(file_blobs)) == ("g3", ["e" * 64])
    assert gfs_b == "g3"
    body = open_share(
        blob=share_blobs[f["b"]],
        item_key=ITEM,
        author_user_id="u-noor",
        moment_id="m-1",
    )
    assert body["media"] == RESEALED
    assert ("u-noor", "m-1", "g3") in env["media"].remembered
    # Kept: a later share copy for g3 names the same blob, sealed nothing new.
    share = await env["svc"]._shares.get("m-1")
    assert json.loads(share.media_by_gfs) == {"g1": MEDIA, "g3": RESEALED}
    await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g1")
    await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g3")
    assert len(env["media"].prepared) == 1


async def test_a_file_still_processing_mints_no_link_yet(env):
    await _published(env)
    env["media"].next = "pending"
    with pytest.raises(ShareLinkUnavailableError):
        await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g3")
    assert env["media"].uploads == []


async def test_a_file_that_cant_be_sent_leaves_the_share_copy_without_it(env):
    """Like a follower, a link's viewer gets the moment without a file this
    build doesn't send (missing, too large) — never a broken reference."""
    await _published(env)
    env["media"].next = None
    url = await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g3")
    [(_, blobs, *_)] = env["media"].uploads
    body = open_share(
        blob=blobs[_fragment(url)["b"]],
        item_key=ITEM,
        author_user_id="u-noor",
        moment_id="m-1",
    )
    assert body["media"] is None and body["media_type"] is None


async def test_a_gfs_refusing_the_file_mints_no_link(env):
    await _published(env)
    env["media"].ok = False
    with pytest.raises(ShareLinkUnavailableError):
        await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g3")
    share = await env["svc"]._shares.get("m-1")
    assert json.loads(share.media_by_gfs) == {"g1": MEDIA}


async def test_a_moment_without_media_never_seals_a_file(env):
    await _published(env, media_url=None, media_type=None)
    await env["svc"].link(user_id="u-noor", moment_id="m-1", gfs_id="g3")
    assert env["media"].prepared == []


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


async def test_a_public_reply_under_a_household_moment_gets_no_link(env):
    """A public reply whose root has since lost ``is_public`` keeps its own
    flag — the link is refused all the same (fail closed): a fresh share
    copy would put it on the GFS again. The DB refuses storing such a
    reply (migration 0123), so the root loses ``is_public`` afterwards."""
    await env["moments"].save(_moment("m-root"))
    await _published(env, mid="m-reply", parent_moment_id="m-root")
    await env["moments"].save(_moment("m-root", is_public=False))
    with pytest.raises(MomentNotFoundError):
        await env["svc"].link(user_id="u-noor", moment_id="m-reply")
    assert env["media"].uploads == []


async def test_a_public_reply_under_a_public_moment_gets_a_link(env):
    await env["moments"].save(_moment("m-root"))
    await _published(env, mid="m-reply", parent_moment_id="m-root")
    url = await env["svc"].link(user_id="u-noor", moment_id="m-reply")
    assert _fragment(url)["m"] == "m-reply"


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
    await svc.record(_moment(), item_key=ITEM, media_by_gfs={})
    assert await SqliteMomentPublicShareRepo(db).get("m-1") is None
