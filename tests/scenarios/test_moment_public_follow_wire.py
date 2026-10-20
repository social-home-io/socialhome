"""Wire contract: the household's public-moment follow / unfollow bodies are
accepted by the real GFS routes (§Momentum-public).

Regression: the follow body carried no ``instance_id``, which the GFS's
``_rtc_authenticate`` requires on every signed body, so every follow was
refused ``422 missing_fields`` — found by the federation demo. Unit tests
with a stub session never saw it; this one runs the household service
against the GFS app.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, generate_identity_keypair, sign_ed25519
from socialhome.global_server.app_keys import gfs_fed_repo_key, gfs_ws_registry_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.moment_public_inbound import MomentPublicInbound
from socialhome.services.moment_public_service import MomentPublicService


class _ToTestClient:
    """The ``aiohttp.ClientSession`` surface the service uses, routed into
    the GFS ``TestClient`` (absolute URLs → paths)."""

    def __init__(self, tc: TestClient) -> None:
        self._tc = tc

    def post(self, url, *, json=None, **_kw):
        return self._tc.post(url.removeprefix("https://gfs.test"), json=json)


def _sign(kp, body: dict) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return {**body, "signature": b64url_encode(sign_ed25519(kp.private_key, canonical))}


@pytest.fixture
async def gfs(tmp_path):
    app = create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url="http://gfs.test",
            data_dir=str(tmp_path / "gfs"),
            instance_id="gfs-test",
        )
    )
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


async def test_follow_and_unfollow_reach_the_gfs(gfs, db):
    author, follower = generate_identity_keypair(), generate_identity_keypair()
    for iid, kp in (("inst-author", author), ("inst-follower", follower)):
        await gfs._app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key=kp.public_key.hex(),
                status="active",
            )
        )
    r = await gfs.post(
        "/gfs/moments/users/register",
        json=_sign(
            author,
            {
                "user_id": "u-noor",
                "instance_id": "inst-author",
                "username": "noor",
                "display_name": "Noor Haddad",
                "bio": "Beekeeper.",
                "home_instance_pk": author.public_key.hex(),
            },
        ),
    )
    assert r.status == 201

    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u1', 'anna', 'Anna', 'active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g1', 'gfs-test', 'G', 'aa',"
        " 'https://gfs.test', 'active', datetime('now'))"
    )
    follows = SqliteMomentPublicFollowRepo(db)
    svc = MomentPublicService(
        SqliteMomentPublicRegistrationRepo(db),
        follows,
        SqliteUserRepo(db),
        SqliteGfsConnectionRepo(db),
        http_client=_ToTestClient(gfs),  # type: ignore[arg-type]
    )
    svc.attach_identity(
        own_instance_id="inst-follower", signing_key=follower.private_key
    )

    f = await svc.follow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-noor")
    assert f.followed_instance_pk == author.public_key.hex()
    assert f.public_name == "Noor Haddad"
    assert f.followed_bio == "Beekeeper."

    await svc.unfollow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-noor")
    assert await follows.list_for_follower("u1") == []
    registry_follows = await gfs.get("/gfs/moments/users/u-noor")
    assert (await registry_follows.json())["follower_count"] == 0


async def test_a_published_moment_reaches_the_follower_signed_and_named(
    gfs, db, monkeypatch
):
    """Regression: ``_rtc_authenticate`` popped ``signature`` off the body
    and the publish view forwarded that body, so every follower dropped
    every public moment as unsigned — found by the federation demo."""
    author, follower = generate_identity_keypair(), generate_identity_keypair()
    for iid, kp in (("inst-author", author), ("inst-follower", follower)):
        await gfs._app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key=kp.public_key.hex(),
                status="active",
            )
        )
    await gfs.post(
        "/gfs/moments/users/register",
        json=_sign(
            author,
            {
                "user_id": "u-noor",
                "instance_id": "inst-author",
                "username": "noor",
                "display_name": "Noor",
                "home_instance_pk": author.public_key.hex(),
            },
        ),
    )
    await gfs.post(
        "/gfs/moments/users/u-noor/follow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_user_id": "u1",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    frames: list[tuple[str, dict]] = []

    async def _send(_self, instance_id, payload):
        frames.append((instance_id, payload))
        return True

    monkeypatch.setattr(type(gfs._app[gfs_ws_registry_key]), "send", _send)
    envelope = {
        "moment_id": "m-1",
        "author_user_id": "u-noor",
        "author_username": "noor",
        "author_display_name": "Noor Haddad",
        "author_bio": "Beekeeper.",
        "author_picture_digest": None,
        "content": "bees",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "origin_instance_id": "inst-author",
        "instance_id": "inst-author",
        "created_at": "2026-10-20T08:00:00+00:00",
        "expires_at": "2099-10-21T08:00:00+00:00",
    }
    r = await gfs.post("/gfs/moments/publish", json=_sign(author, envelope))
    assert (await r.json()) == {"delivered": 1}
    [(to, frame)] = frames
    assert to == "inst-follower"
    assert frame["payload"]["signature"]

    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u1', 'anna', 'Anna', 'active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g1', 'gfs-test', 'G', 'aa',"
        " 'https://gfs.test', 'active', datetime('now'))"
    )
    follows = SqliteMomentPublicFollowRepo(db)
    await follows.upsert(
        follower_user_id="u1",
        followed_user_id="u-noor",
        gfs_id="g1",
        followed_instance_pk=author.public_key.hex(),
        followed_username="noor",
        followed_display_name="Noor",
    )
    moments = SqliteMomentRepo(db)
    inbound = MomentPublicInbound(
        bus=EventBus(), moment_repo=moments, follow_repo=follows
    )
    await inbound.handle(frame, gfs_id="g1")
    assert await moments.get("m-1") is not None
    got = await follows.get(
        follower_user_id="u1", followed_user_id="u-noor", gfs_id="g1"
    )
    assert got is not None
    assert (got.followed_display_name, got.followed_bio) == (
        "Noor Haddad",
        "Beekeeper.",
    )
