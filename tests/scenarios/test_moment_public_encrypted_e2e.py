"""End to end: a public moment reaches a GFS follower encrypted (v_62).

Two households (separate databases) and the real GFS app. The author
household seals its moment content key to the follower when the GFS names
it (``follow_changed``), and publishes the moment encrypted; the follower
opens it. A follower that missed the key asks for it (``moment_key_request``)
and opens the moment it held. Everything the GFS was sent — every HTTP body
and every frame it pushed — carries no moment text, id or author name.
"""

from __future__ import annotations

import json
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import MomentCreated
from socialhome.domain.moment import Moment
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.global_server.ws_registry import GfsWebSocketRegistry
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicKeyRepo,
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import moment_public_key_outbound
from socialhome.services.moment_public_inbound import MomentPublicInbound
from socialhome.services.moment_public_key_inbound import MomentPublicKeyInbound
from socialhome.services.moment_public_key_outbound import (
    ROTATE_AT_MOST_EVERY_S,
    MomentPublicKeyOutbound,
)
from socialhome.services.moment_public_outbound import MomentPublicOutbound
from socialhome.services.moment_public_service import MomentPublicService

#: Words that must never reach the GFS: the moment's text, its media name
#: and the author's display name (the directory carries the handle only).
SECRET_TEXT = "Bees swarmed the plum tree at dawn"
MOMENT_ID = "m-plum-tree-7f3a"
SECRET_NAME = "Noor Al-Haddad"


class _Household:
    """One household's identity: Ed25519 + its published key-wrap key."""

    def __init__(self) -> None:
        self.ident = generate_identity_keypair()
        self.wrap = generate_x25519_keypair()
        self.instance_id = derive_instance_id(self.ident.public_key)

    def client_instance(self) -> ClientInstance:
        return ClientInstance(
            instance_id=self.instance_id,
            display_name=self.instance_id,
            public_key=self.ident.public_key.hex(),
            status="active",
            keywrap_public_key=self.wrap.public_key.hex(),
            kem_suite="x25519",
            keywrap_sig=b64url_encode(
                sign_ed25519(self.ident.private_key, self.wrap.public_key)
            ),
        )


class _Wire:
    """The household's HTTP session, routed into the GFS app — and a record
    of every body the GFS was sent."""

    def __init__(self, tc: TestClient, seen: list) -> None:
        self._tc = tc
        self._seen = seen

    def post(self, url, *, json=None, **_kw):
        self._seen.append(json)
        return self._tc.post(url.removeprefix("https://gfs.test"), json=json)


class _Proven:
    async def encrypted_moments_supported(self, gfs_id: str) -> bool:
        return True


async def _db(path) -> AsyncDatabase:
    db = AsyncDatabase(path, batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g1', 'gfs-test', 'G', 'aa',"
        " 'https://gfs.test', 'active', datetime('now'))"
    )
    return db


@pytest.fixture
async def world(tmp_path, monkeypatch):
    gfs_app = create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url="http://gfs.test",
            data_dir=str(tmp_path / "gfs"),
            instance_id="gfs-test",
        )
    )
    pushed: list[tuple[str, dict]] = []

    async def _send(_self, instance_id, frame):
        pushed.append((instance_id, frame))
        return True

    monkeypatch.setattr(GfsWebSocketRegistry, "send", _send)
    a_db = await _db(tmp_path / "author.db")
    f_db = await _db(tmp_path / "follower.db")
    async with TestClient(TestServer(gfs_app)) as gfs:
        author, follower = _Household(), _Household()
        for h in (author, follower):
            await gfs_app[gfs_fed_repo_key].upsert_instance(h.client_instance())
        sent: list = []
        wire = _Wire(gfs, sent)

        # ── The author household ─────────────────────────────────────
        await a_db.enqueue(
            "INSERT INTO users(user_id, username, display_name, state)"
            " VALUES('u-noor', 'noor', ?, 'active')",
            (SECRET_NAME,),
        )
        a_bus = EventBus()
        a_moments = SqliteMomentRepo(a_db)
        a_regs = SqliteMomentPublicRegistrationRepo(a_db)
        await a_regs.upsert(user_id="u-noor", gfs_id="g1", default_share=True)
        r = await gfs.post(
            "/gfs/moments/users/register",
            json=_signed(
                author,
                {
                    "user_id": "u-noor",
                    "instance_id": author.instance_id,
                    "username": "noor",
                    "display_name": "noor",
                    "home_instance_pk": author.ident.public_key.hex(),
                },
            ),
        )
        assert r.status == 201
        keys_out = MomentPublicKeyOutbound(
            key_repo=SqliteMomentPublicKeyRepo(a_db),
            registration_repo=a_regs,
            user_repo=SqliteUserRepo(a_db),
            gfs_repo=SqliteGfsConnectionRepo(a_db),
            gfs_service=_Proven(),  # type: ignore[arg-type]
        )
        keys_out.attach_session(wire)  # type: ignore[arg-type]
        keys_out.attach_identity(
            own_instance_id=author.instance_id,
            signing_key=author.ident.private_key,
            key_manager=KeyManager(os.urandom(32)),
        )
        outbound = MomentPublicOutbound(
            bus=a_bus,
            moment_repo=a_moments,
            registration_repo=a_regs,
            user_repo=SqliteUserRepo(a_db),
            gfs_repo=SqliteGfsConnectionRepo(a_db),
            key_outbound=keys_out,
            gfs_service=_Proven(),  # type: ignore[arg-type]
        )
        outbound.attach_session(wire)  # type: ignore[arg-type]
        outbound.attach_identity(
            own_instance_id=author.instance_id, signing_key=author.ident.private_key
        )
        outbound.wire()

        # ── The follower household ───────────────────────────────────
        await f_db.enqueue(
            "INSERT INTO users(user_id, username, display_name, state)"
            " VALUES('u-anna', 'anna', 'Anna', 'active')"
        )
        f_follows = SqliteMomentPublicFollowRepo(f_db)
        f_moments = SqliteMomentRepo(f_db)
        keys_in = MomentPublicKeyInbound(
            key_repo=SqliteMomentPublicKeyRepo(f_db),
            follow_repo=f_follows,
            user_repo=SqliteUserRepo(f_db),
            gfs_repo=SqliteGfsConnectionRepo(f_db),
        )
        keys_in.attach_session(wire)  # type: ignore[arg-type]
        keys_in.attach_identity(
            own_instance_id=follower.instance_id,
            signing_key=follower.ident.private_key,
            key_manager=KeyManager(os.urandom(32)),
            keywrap_private_key=follower.wrap.private_key,
        )
        inbound = MomentPublicInbound(
            bus=EventBus(),
            moment_repo=f_moments,
            follow_repo=f_follows,
            key_inbound=keys_in,
        )
        follow_svc = MomentPublicService(
            SqliteMomentPublicRegistrationRepo(f_db),
            f_follows,
            SqliteUserRepo(f_db),
            SqliteGfsConnectionRepo(f_db),
            http_client=wire,  # type: ignore[arg-type]
        )
        follow_svc.attach_identity(
            own_instance_id=follower.instance_id,
            signing_key=follower.ident.private_key,
        )
        follow_svc.attach_key_inbound(keys_in)

        async def pump(*, drop: set[str] = frozenset()) -> list[str]:
            """Hand every frame the GFS pushed to its household, until quiet.
            Frame types in *drop* are lost (the household was offline)."""
            delivered: list[str] = []
            while pushed:
                to, frame = pushed.pop(0)
                delivered.append(frame["type"])
                if frame["type"] in drop:
                    continue
                if to == author.instance_id:
                    await keys_out.handle(frame, gfs_id="g1")
                    await keys_out.flush()
                elif to == follower.instance_id:
                    await inbound.handle(frame, gfs_id="g1")
            return delivered

        yield {
            "gfs": gfs,
            "pushed": pushed,
            "sent": sent,
            "pump": pump,
            "bus": a_bus,
            "author_moments": a_moments,
            "follower_moments": f_moments,
            "follow": follow_svc,
            "keys_in": keys_in,
            "follower_keys": SqliteMomentPublicKeyRepo(f_db),
            "author": author,
            "follower": follower,
            "inbound": inbound,
            "keys_out": keys_out,
            "gfs_dir": tmp_path / "gfs",
        }
    await a_db.shutdown()
    await f_db.shutdown()


def _signed(h: _Household, body: dict) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return {
        **body,
        "signature": b64url_encode(sign_ed25519(h.ident.private_key, canonical)),
    }


async def _post_moment(w, moment_id: str = MOMENT_ID) -> None:
    moment = Moment(
        id=moment_id,
        author_user_id="u-noor",
        content=SECRET_TEXT,
        media_url="api/media/plum.webp",
        media_type="image",
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="",
        created_at="2026-10-20T08:00:00+00:00",
        expires_at="2099-10-21T08:00:00+00:00",
        is_public=True,
    )
    await w["author_moments"].save(moment)
    await w["bus"].publish(
        MomentCreated(
            moment_id=moment.id,
            author_user_id="u-noor",
            content=SECRET_TEXT,
            media_url=moment.media_url,
            media_type=moment.media_type,
            duration_ms=None,
            parent_moment_id=None,
            parent_author_user_id=None,
            origin_instance_id="",
            expires_at=moment.expires_at,
        )
    )


def _assert_gfs_saw_no_content(w) -> None:
    for blob in (json.dumps(w["sent"]), json.dumps(w["pushed"])):
        # The moment id is a routing field (bound into the AEAD); its
        # content — text, media, the author's name — never shows.
        for secret in (SECRET_TEXT, SECRET_NAME, "plum.webp"):
            assert secret not in blob, secret


async def test_a_follower_opens_the_moment_the_gfs_could_not_read(world):
    w = world
    await w["follow"].follow(
        follower_user_id="u-anna", gfs_id="g1", followed_user_id="u-noor"
    )
    # The GFS names the new follower to the author; the author seals its key
    # back, to the follower alone.
    assert await w["pump"]() == ["follow_changed", "moment_key"]
    await _post_moment(w)
    assert await w["pump"]() == ["incoming_public_moment"]
    got = await w["follower_moments"].get(MOMENT_ID)
    assert got is not None
    assert got.content == SECRET_TEXT
    assert got.media_url == "api/media/plum.webp"
    assert got.received_via == "gfs"
    _assert_gfs_saw_no_content(w)


async def test_a_follower_that_missed_the_key_asks_and_then_opens_it(world):
    w = world
    await w["follow"].follow(
        follower_user_id="u-anna", gfs_id="g1", followed_user_id="u-noor"
    )
    # The author household was offline: the follow notice is lost.
    assert await w["pump"](drop={"follow_changed"}) == ["follow_changed"]
    await _post_moment(w)
    # The moment arrives without its key: held, and the key asked for.
    assert await w["pump"]() == [
        "incoming_public_moment",
        "moment_key_request",
        "moment_key",
    ]
    got = await w["follower_moments"].get(MOMENT_ID)
    assert got is not None and got.content == SECRET_TEXT
    _assert_gfs_saw_no_content(w)


async def test_after_unfollowing_no_key_reaches_the_household(world):
    """The GFS routes keys only to a household that follows: a household
    that left gets no new key, even when it asks."""
    w = world
    await w["follow"].follow(
        follower_user_id="u-anna", gfs_id="g1", followed_user_id="u-noor"
    )
    await w["pump"]()
    assert await w["follower_keys"].latest("u-noor", own=False) is not None
    await w["follow"].unfollow(
        follower_user_id="u-anna", gfs_id="g1", followed_user_id="u-noor"
    )
    await w["pump"]()
    # Its keys for the author are gone with the last local follow.
    assert await w["follower_keys"].latest("u-noor", own=False) is None
    # Asking again reaches nobody: the GFS forwards only a follower's ask.
    assert await w["keys_in"].request_key("u-noor", gfs_id="g1", want_gen=1)
    assert await w["pump"]() == []
    await _post_moment(w)
    assert await w["pump"]() == []
    assert await w["follower_moments"].get(MOMENT_ID) is None


def _published_gens(w) -> list[int]:
    """The key generation of every moment envelope the author published."""
    return [
        b["key_gen"]
        for b in w["sent"]
        if isinstance(b, dict) and "ciphertext" in b and "key_gen" in b
    ]


async def test_a_leaving_household_triggers_one_lazy_rotation(world, monkeypatch):
    """A household leaves: the author's key is only marked. The next publish
    (≥ 6 h after the generation) seals under a NEW generation, which the
    old key can't open; a household following again re-keys once, by pull."""
    w = world
    await w["follow"].follow(
        follower_user_id="u-anna", gfs_id="g1", followed_user_id="u-noor"
    )
    await w["pump"]()
    await _post_moment(w, "m-before")
    assert await w["pump"]() == ["incoming_public_moment"]
    [old_gen] = _published_gens(w)
    await w["follow"].unfollow(
        follower_user_id="u-anna", gfs_id="g1", followed_user_id="u-noor"
    )
    assert await w["pump"]() == ["follow_changed"]  # the leave: marked only
    later = moment_public_key_outbound.time.time() + ROTATE_AT_MOST_EVERY_S + 60
    monkeypatch.setattr(moment_public_key_outbound.time, "time", lambda: later)
    await w["follow"].follow(
        follower_user_id="u-anna", gfs_id="g1", followed_user_id="u-noor"
    )
    # No rotation without a publish. (The same key went to this household
    # seconds ago, so the author's reseal throttle sends nothing now.)
    assert await w["pump"]() == ["follow_changed"]
    await _post_moment(w, "m-after")
    assert await w["pump"]() == [
        "incoming_public_moment",
        "moment_key_request",
        "moment_key",
    ]
    new_gen = _published_gens(w)[-1]
    assert new_gen > old_gen  # what the leaver holds can't open it
    got = await w["follower_moments"].get("m-after")
    assert got is not None and got.content == SECRET_TEXT
    # One more moment: no further rotation, no key traffic.
    await _post_moment(w, "m-again")
    assert await w["pump"]() == ["incoming_public_moment"]
    assert _published_gens(w)[-1] == new_gen
