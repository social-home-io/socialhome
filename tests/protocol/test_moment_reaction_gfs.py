"""Release-blocker protocol tests: reactions reach authors you follow through a
GFS (v_68). Marked ``@pytest.mark.security``.

A reaction to a public moment that came through a GFS is the v_67 sealed
reaction (signed by the reactor's household, padded, sealed to the author
household's key-wrap key), posted to that GFS as exactly ``{instance_id,
sealed}``; the GFS pushes ``{from_instance, sealed}`` to the author's
household only. The rules these tests encode:

* **GFS.** Takes a reaction only from a household that follows the author
  there (never the author's own), only in the bare shape (nothing the GFS
  could read about the reaction rides along), under per-(household, author)
  and per-household limits a retried post doesn't spend; pushes it to the
  author's household on this node, forwards it to the node holding its
  socket, or queues it 24 h (bounded) — never anywhere else.
* **Author.** Applies a ``moment_reaction`` frame only after every origin
  check of v_67 (seal opens, signature, self-certified user, live own
  moment, not local / blocked / banned, not a replay, newer than the stored
  reaction and removal) plus: the household the GFS authenticated is the
  reactor's own, and the moment is public.
* **Follower.** Posts only to the GFS the moment came through, only when it
  proved ``moment_reactions`` and the author's household named its key-wrap
  key; otherwise the reaction stays home and the API says so.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    instance_keywrap_public_key_key,
    instance_public_key_key,
    moment_public_inbound_key,
    moment_reaction_router_key,
    moment_service_key,
)
from socialhome.config import Config
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.federation.federation_service import FederationService
from socialhome.federation.moment_reaction_route import (
    MAX_SEALED_CHARS,
    build_reaction,
    open_reaction,
    seal_reaction,
    verify_reaction,
)
from socialhome.global_server import moment_public_registry as registry_mod
from socialhome.global_server.app_keys import (
    gfs_envelope_relay_key,
    gfs_fed_repo_key,
    gfs_moment_public_registry_key,
)
from socialhome.global_server.cluster_relay import TYPED_FRAME_TYPES
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.envelope_relay import QUEUED_TYPED_FRAME_TYPES
from socialhome.global_server.server import create_gfs_app
from socialhome.global_server.ws_registry import GfsWebSocketRegistry
from socialhome.moment_public_seal import (
    FRAME_MOMENT_REACTION,
    MAX_REACT_SEALED_CHARS,
    verify_wire,
)
from socialhome.services import moment_reaction_router as router_mod
from socialhome.services.gfs_connection_service import GfsConnectionService

pytestmark = pytest.mark.security

REACTOR_KEY = generate_identity_keypair()  # a follower household met via a GFS
REACTOR = derive_instance_id(REACTOR_KEY.public_key)
RITA = derive_user_id(REACTOR_KEY.public_key, "rita-anchor")
AUTHOR_KEY = generate_identity_keypair()  # a far author (follower-side tests)
AUTHOR = derive_instance_id(AUTHOR_KEY.public_key)
AUTHOR_KW = generate_x25519_keypair()
EXPIRES = (datetime.now(UTC) + timedelta(days=5)).isoformat()


# ═══ Social Home side ═════════════════════════════════════════════════════


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "moments.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json))
        return _Resp(202)


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    own = app[federation_service_key].own_instance_id
    own_pk = app[instance_public_key_key]
    anna = derive_user_id(own_pk, "anna-anchor")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, identity_anchor)"
        " VALUES(?,?,?,?)",
        ("anna", anna, "Anna", "anna-anchor"),
    )
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at, received_via, is_public) VALUES(?,?,?,?,?,'self',1)",
        ("m-anna", anna, "by anna", own, EXPIRES),
    )
    sent: list = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    router = app[moment_reaction_router_key]
    router._debounce_s = 0
    return app, db, sent, own, anna, router


def _now_ms(delta_s: float = 0) -> int:
    return int((datetime.now(UTC).timestamp() + delta_s) * 1000)


def _inner(own: str, anna: str, *, emoji: str | None = "👏", at_ms=None, **over):
    key = over.pop("key", REACTOR_KEY)
    return build_reaction(
        seed=key.private_key,
        identity_pk=key.public_key,
        reactor_instance_id=over.pop(
            "reactor_home", derive_instance_id(key.public_key)
        ),
        reactor_user_id=derive_user_id(key.public_key, "rita-anchor"),
        reactor_anchor="rita-anchor",
        moment_id=over.pop("moment_id", "m-anna"),
        author_user_id=anna,
        origin_instance_id=own,
        emoji=emoji,
        occurred_at_ms=at_ms if at_ms is not None else _now_ms(),
    )


def _frame(app, inner: dict, *, sender: str = REACTOR, kw=None) -> dict:
    return {
        "type": FRAME_MOMENT_REACTION,
        "from_instance": sender,
        "sealed": seal_reaction(
            origin_keywrap_pub=kw or app[instance_keywrap_public_key_key],
            inner=inner,
        ),
    }


async def _deliver(app, frame: dict, gfs_id: str = "g1") -> None:
    await app[moment_public_inbound_key].handle(frame, gfs_id=gfs_id)


async def _reactions(db) -> list[tuple]:
    rows = await db.fetchall(
        "SELECT moment_id, reactor_user_id, emoji FROM moment_reactions", ()
    )
    return [tuple(r) for r in rows]


# ── Author: a follower's reaction through the GFS lands ───────────────────


async def test_author_applies_a_gfs_reaction_and_its_removal(env):
    app, db, sent, own, anna, _router = env
    await _deliver(app, _frame(app, _inner(own, anna, at_ms=_now_ms(-5))))
    assert await _reactions(db) == [("m-anna", RITA, "👏")]
    await _deliver(app, _frame(app, _inner(own, anna, emoji=None)))
    assert await _reactions(db) == []
    assert sent == []  # nothing goes back, to anyone


# ── Author: refusals ──────────────────────────────────────────────────────


async def test_a_spoofed_from_instance_is_refused(env, caplog):
    """The household the GFS authenticated must be the reactor's own: a
    follower can't pass another household's reaction off as its own (or
    its own as another's)."""
    app, db, _sent, own, anna, _router = env
    await _deliver(app, _frame(app, _inner(own, anna), sender="inst-someone-else"))
    assert await _reactions(db) == []
    assert "not the reactor's household" in caplog.text


async def test_a_reaction_to_a_moment_that_is_not_public_is_refused(env, caplog):
    """Only a public moment ever crossed a GFS."""
    app, db, _sent, own, anna, _router = env
    await db.enqueue("UPDATE moments SET is_public=0 WHERE id='m-anna'")
    await _deliver(app, _frame(app, _inner(own, anna)))
    assert await _reactions(db) == []
    assert "not public" in caplog.text


async def test_a_replayed_frame_is_refused(env):
    app, db, _sent, own, anna, _router = env
    frame = _frame(app, _inner(own, anna, at_ms=_now_ms(-30)))
    await _deliver(app, frame)
    await _deliver(app, _frame(app, _inner(own, anna, emoji=None, at_ms=_now_ms(-10))))
    await _deliver(app, frame)  # the GFS (or anyone) re-sends the add
    assert await _reactions(db) == []


async def test_a_replayed_add_does_not_undo_a_removal_after_a_restart(env):
    app, db, _sent, own, anna, router = env
    add = _frame(app, _inner(own, anna, at_ms=_now_ms(-60)))
    await _deliver(app, add)
    await _deliver(app, _frame(app, _inner(own, anna, emoji=None, at_ms=_now_ms(-30))))
    router._seen.clear()  # the replay cache is in memory
    await _deliver(app, add)
    assert await _reactions(db) == []


async def test_adds_and_removals_apply_in_the_order_they_were_made(env):
    app, db, _sent, own, anna, _router = env
    for emoji, at in (("👏", -60), ("🔥", -120), (None, -30), ("😂", -45)):
        await _deliver(
            app, _frame(app, _inner(own, anna, emoji=emoji, at_ms=_now_ms(at)))
        )
    assert await _reactions(db) == []
    await _deliver(app, _frame(app, _inner(own, anna, emoji="😮", at_ms=_now_ms(-5))))
    assert await _reactions(db) == [("m-anna", RITA, "😮")]


def _forged_signature(own, anna):
    inner = _inner(own, anna)
    inner["emoji"] = "🔥"  # changed after signing
    return inner


def _claims_another_household(own, anna):
    other = generate_identity_keypair()
    return _inner(own, anna, reactor_home=derive_instance_id(other.public_key))


def _too_old(own, anna):
    return _inner(own, anna, at_ms=_now_ms(-26 * 3600))


def _another_moment(own, anna):
    return _inner(own, anna, moment_id="m-unknown")


@pytest.mark.parametrize(
    "make", [_forged_signature, _claims_another_household, _too_old, _another_moment]
)
async def test_author_refuses_a_forged_or_stale_reaction(env, make):
    app, db, _sent, own, anna, _router = env
    inner = make(own, anna)
    await _deliver(app, _frame(app, inner, sender=inner["reactor_instance_id"]))
    assert await _reactions(db) == []


async def test_a_seal_to_another_key_or_a_malformed_frame_is_refused(env, caplog):
    app, db, _sent, own, anna, _router = env
    await _deliver(app, _frame(app, _inner(own, anna), kw=AUTHOR_KW.public_key))
    await _deliver(app, {"type": FRAME_MOMENT_REACTION, "from_instance": REACTOR})
    bad = _frame(app, _inner(own, anna))
    bad["sealed"]["kem_suite"] = "x448"
    await _deliver(app, bad)
    assert await _reactions(db) == []
    assert "malformed moment_reaction frame" in caplog.text


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        pytest.param(
            "INSERT INTO household_instance_bans(instance_id, reason)"
            " VALUES(?, 'spam')",
            ("reactor",),
            id="reactor household banned",
        ),
        pytest.param(
            "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
            ("anna", "rita"),
            id="author blocked the reactor",
        ),
    ],
)
async def test_author_refuses_by_local_policy(env, sql, params):
    app, db, _sent, own, anna, _router = env
    values = {"anna": anna, "rita": RITA, "reactor": REACTOR}
    await db.enqueue(sql, tuple(values[p] for p in params))
    await _deliver(app, _frame(app, _inner(own, anna)))
    assert await _reactions(db) == []


async def test_one_gfs_is_held_to_a_rate_before_any_crypto(env, monkeypatch):
    app, db, _sent, own, anna, _router = env
    monkeypatch.setattr(router_mod, "ORIGIN_PER_GFS_PER_MIN", 1)
    await _deliver(app, _frame(app, _inner(own, anna, emoji="👏", at_ms=_now_ms(-9))))
    await _deliver(app, _frame(app, _inner(own, anna, emoji="🔥", at_ms=_now_ms(-1))))
    assert await _reactions(db) == [("m-anna", RITA, "👏")]
    # Another GFS has its own budget.
    await _deliver(
        app, _frame(app, _inner(own, anna, emoji="🔥", at_ms=_now_ms())), gfs_id="g2"
    )
    assert await _reactions(db) == [("m-anna", RITA, "🔥")]


# ── Follower: only through the GFS the moment came on, only when it can ───


async def _gfs_moment(app, db, *, keywrap: bool = True, gfs_id: str = "g1") -> None:
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES(?, ?, 'G', ?, ?, 'active',"
        " datetime('now'))",
        (gfs_id, gfs_id, "aa" * 32, f"https://{gfs_id}.example"),
    )
    anna = (await db.fetchone("SELECT user_id FROM users WHERE username='anna'", ()))[0]
    await db.enqueue(
        "INSERT INTO moment_public_follows(follower_user_id, followed_user_id,"
        " gfs_id, followed_instance_pk, followed_username, followed_display_name,"
        " followed_keywrap_pk) VALUES(?, 'u-noor', ?, ?, 'noor', 'Noor', ?)",
        (
            anna,
            gfs_id,
            AUTHOR_KEY.public_key.hex(),
            AUTHOR_KW.public_key.hex() if keywrap else None,
        ),
    )
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at, received_via, received_via_gfs_id, is_public)"
        " VALUES('m-noor', 'u-noor', 'bees', ?, ?, 'gfs', ?, 1)",
        (AUTHOR, EXPIRES, gfs_id),
    )


@pytest.fixture
def follower(env, monkeypatch):
    app, db, sent, _own, anna, router = env
    proven = {"g1"}

    async def _supported(_self, gfs_id):
        return gfs_id in proven

    monkeypatch.setattr(GfsConnectionService, "moment_reactions_supported", _supported)
    session = _Session()
    router._gfs._http_client = session
    return app, db, sent, anna, router, session, proven


async def test_follower_seals_its_reaction_to_the_author_through_the_gfs(follower):
    app, db, sent, anna, _router, session, _proven = follower
    await _gfs_moment(app, db)
    moments = app[moment_service_key]
    assert await moments.reaction_reach([await moments.get_moment("m-noor")]) == {
        "m-noor": "author"
    }
    await moments.react("m-noor", reactor_user_id=anna, emoji="👏")
    await asyncio.sleep(0.05)
    [(url, body)] = session.posts
    assert url == "https://g1.example/gfs/moments/users/u-noor/react"
    # Exactly the household and the seal (+ its wire signature): nothing the
    # GFS could read names the moment, the person or the emoji.
    assert set(body) == {"instance_id", "sealed", "signature"}
    own = app[federation_service_key].own_instance_id
    assert body["instance_id"] == own
    assert verify_wire(body, app[instance_public_key_key].hex())
    for secret in (anna, "m-noor", "👏"):
        assert secret not in json.dumps(body, ensure_ascii=False)
    inner = open_reaction(sealed=body["sealed"], keywrap_priv=AUTHOR_KW.private_key)
    assert verify_reaction(inner) is None
    assert (inner["moment_id"], inner["reactor_user_id"], inner["emoji"]) == (
        "m-noor",
        anna,
        "👏",
    )
    assert inner["origin_instance_id"] == AUTHOR
    assert sent == []  # never to the author's household directly
    # A removal is the same size on the wire.
    await moments.clear_reaction("m-noor", reactor_user_id=anna)
    await asyncio.sleep(0.05)
    removal = session.posts[-1][1]["sealed"]["ciphertext"]
    assert len(removal) == len(body["sealed"]["ciphertext"])


async def test_a_gfs_without_the_capability_is_not_used(follower):
    app, db, sent, anna, _router, session, proven = follower
    proven.clear()
    await _gfs_moment(app, db)
    moments = app[moment_service_key]
    await moments.react("m-noor", reactor_user_id=anna, emoji="👏")
    await asyncio.sleep(0.05)
    assert session.posts == [] and sent == []
    assert await moments.reaction_reach([await moments.get_moment("m-noor")]) == {
        "m-noor": "household"
    }


async def test_an_author_that_named_no_keywrap_key_keeps_reactions_home(follower):
    app, db, sent, anna, _router, session, _proven = follower
    await _gfs_moment(app, db, keywrap=False)
    moments = app[moment_service_key]
    await moments.react("m-noor", reactor_user_id=anna, emoji="👏")
    await asyncio.sleep(0.05)
    assert session.posts == [] and sent == []
    assert await moments.reaction_reach([await moments.get_moment("m-noor")]) == {
        "m-noor": "household"
    }


async def test_only_the_gfs_the_moment_came_through_is_used(follower):
    """Another proven GFS the household also follows the author through is
    never used for this moment."""
    app, db, _sent, anna, _router, session, proven = follower
    await _gfs_moment(app, db, gfs_id="g2")  # arrived on g2, which isn't proven
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g1', 'g1', 'G', ?, "
        "'https://g1.example', 'active', datetime('now'))",
        ("aa" * 32,),
    )
    await app[moment_service_key].react("m-noor", reactor_user_id=anna, emoji="👏")
    await asyncio.sleep(0.05)
    assert session.posts == []
    proven.add("g2")
    await app[moment_service_key].react("m-noor", reactor_user_id=anna, emoji="🔥")
    await asyncio.sleep(0.05)
    assert [u for u, _b in session.posts] == [
        "https://g2.example/gfs/moments/users/u-noor/react"
    ]


# ═══ GFS side ═════════════════════════════════════════════════════════════


def _gfs_config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


@pytest.fixture
async def gfs(tmp_dir):
    app = create_gfs_app(_gfs_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


def _sign(kp, body: dict) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return {**body, "signature": b64url_encode(sign_ed25519(kp.private_key, canonical))}


class _Frames(list):
    offline: set


@pytest.fixture
def frames(monkeypatch) -> _Frames:
    out = _Frames()
    out.offline = set()

    async def _send(self, instance_id, frame):
        if instance_id in out.offline:
            return False
        out.append((instance_id, frame))
        return True

    monkeypatch.setattr(GfsWebSocketRegistry, "send", _send)
    return out


@pytest.fixture
async def world(gfs):
    """An author household with one registered user, a follower household
    following them, and a stranger household following nobody."""
    keys = {}
    for iid in ("inst-author", "inst-follower", "inst-stranger"):
        kp = generate_identity_keypair()
        keys[iid] = kp
        await gfs._app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key=kp.public_key.hex(),
                status="active",
            )
        )
    resp = await gfs.post(
        "/gfs/moments/users/register",
        json=_sign(
            keys["inst-author"],
            {
                "user_id": "u-author",
                "instance_id": "inst-author",
                "username": "alice",
                "display_name": "Alice",
                "home_instance_pk": keys["inst-author"].public_key.hex(),
            },
        ),
    )
    assert resp.status == 201
    resp = await gfs.post(
        "/gfs/moments/users/u-author/follow",
        json=_sign(
            keys["inst-follower"],
            {
                "instance_id": "inst-follower",
                "follower_instance_id": "inst-follower",
                "follower_user_id": "u-fan",
                "moment_suites": {"kem": ["x25519"], "seal": ["aesgcm-256"]},
            },
        ),
    )
    assert resp.status == 201
    return keys


def _sealed(n: int = 0) -> dict:
    return seal_reaction(
        origin_keywrap_pub=AUTHOR_KW.public_key,
        inner={"kind": "moment_reaction", "n": n},
    )


async def _react(gfs, keys, *, sender="inst-follower", author="u-author", body=None):
    return await gfs.post(
        f"/gfs/moments/users/{author}/react",
        json=_sign(keys[sender], body or {"instance_id": sender, "sealed": _sealed()}),
    )


async def test_gfs_pushes_a_followers_reaction_to_the_author_only(gfs, world, frames):
    frames.clear()
    sealed = _sealed()
    resp = await _react(
        gfs, world, body={"instance_id": "inst-follower", "sealed": sealed}
    )
    assert resp.status == 202
    assert frames == [
        (
            "inst-author",
            {
                "type": "moment_reaction",
                "from_instance": "inst-follower",
                "sealed": sealed,
            },
        )
    ]


async def test_gfs_refuses_a_non_follower_and_the_authors_own_household(
    gfs, world, frames
):
    frames.clear()
    assert (await _react(gfs, world, sender="inst-stranger")).status == 403
    assert (await _react(gfs, world, sender="inst-author")).status == 403
    assert (await _react(gfs, world, author="u-nobody")).status == 403
    assert frames == []


@pytest.mark.parametrize(
    "extra",
    [
        {"moment_id": "m-1"},
        {"emoji": "👏"},
        {"reactor_user_id": "u-fan"},
        {"op": "remove"},
    ],
)
async def test_gfs_refuses_anything_beyond_the_bare_seal(gfs, world, frames, extra):
    """Nothing about the reaction may ride next to the seal — the GFS would
    read it (and push it on)."""
    frames.clear()
    body = {"instance_id": "inst-follower", "sealed": _sealed(), **extra}
    assert (await _react(gfs, world, body=body)).status == 422
    assert frames == []


@pytest.mark.parametrize(
    "sealed",
    [
        {"kem_suite": "x25519", "eph_pk": "e"},
        {"kem_suite": "x25519", "eph_pk": "e", "ciphertext": "c" * 3001},
        {"kem_suite": "x25519", "eph_pk": "e", "ciphertext": "c", "who": "u-fan"},
        "not-a-seal",
    ],
)
async def test_gfs_refuses_a_malformed_seal(gfs, world, frames, sealed):
    frames.clear()
    body = {"instance_id": "inst-follower", "sealed": sealed}
    assert (await _react(gfs, world, body=body)).status == 422
    assert frames == []


async def test_gfs_refuses_a_spoofed_signer(gfs, world, frames):
    """Signed by another household than the ``instance_id`` it names."""
    frames.clear()
    resp = await gfs.post(
        "/gfs/moments/users/u-author/react",
        json=_sign(
            world["inst-stranger"],
            {"instance_id": "inst-follower", "sealed": _sealed()},
        ),
    )
    assert resp.status == 401
    assert frames == []


async def test_gfs_rate_limits_per_author_and_per_household(
    gfs, world, frames, monkeypatch
):
    monkeypatch.setattr(registry_mod, "REACTIONS_PER_AUTHOR_PER_MINUTE", 2)
    frames.clear()
    statuses = []
    for n in range(3):
        resp = await _react(
            gfs, world, body={"instance_id": "inst-follower", "sealed": _sealed(n)}
        )
        statuses.append(resp.status)
    assert statuses == [202, 202, 429]
    assert len(frames) == 2
    monkeypatch.setattr(registry_mod, "REACTIONS_PER_AUTHOR_PER_MINUTE", 100)
    monkeypatch.setattr(registry_mod, "REACTIONS_PER_HOUSEHOLD_PER_MINUTE", 2)
    resp = await _react(
        gfs, world, body={"instance_id": "inst-follower", "sealed": _sealed(9)}
    )
    assert resp.status == 429
    assert resp.headers["Retry-After"] == "60"


async def test_a_retried_post_is_not_pushed_twice_nor_charged(
    gfs, world, frames, monkeypatch
):
    monkeypatch.setattr(registry_mod, "REACTIONS_PER_AUTHOR_PER_MINUTE", 1)
    frames.clear()
    body = {"instance_id": "inst-follower", "sealed": _sealed()}
    for _ in range(3):
        assert (await _react(gfs, world, body=body)).status == 202
    assert len(frames) == 1
    # …and the one budgeted slot is still its own: a new reaction is over.
    resp = await _react(
        gfs, world, body={"instance_id": "inst-follower", "sealed": _sealed(1)}
    )
    assert resp.status == 429


async def test_an_offline_author_gets_the_reaction_when_it_reconnects(
    gfs, world, frames
):
    frames.offline.add("inst-author")
    frames.clear()
    sealed = _sealed()
    resp = await _react(
        gfs, world, body={"instance_id": "inst-follower", "sealed": sealed}
    )
    assert resp.status == 202  # online or not is not told back
    assert frames == []
    frames.offline.clear()
    assert await gfs._app[gfs_envelope_relay_key].drain("inst-author") == 1
    assert frames == [
        (
            "inst-author",
            {
                "type": "moment_reaction",
                "from_instance": "inst-follower",
                "sealed": sealed,
            },
        )
    ]


async def test_queued_reactions_for_an_offline_author_are_bounded(
    gfs, world, frames, monkeypatch
):
    monkeypatch.setattr(
        registry_mod, "QUEUED_REACTIONS_PER_AUTHOR_HOUSEHOLD_PER_HOUR", 1
    )
    frames.offline.add("inst-author")
    for n in range(3):
        resp = await _react(
            gfs, world, body={"instance_id": "inst-follower", "sealed": _sealed(n)}
        )
        assert resp.status == 202
    frames.offline.clear()
    assert await gfs._app[gfs_envelope_relay_key].drain("inst-author") == 1


class _FanOut:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.forwarded: list[dict] = []

    def seen_frame(self, _frame) -> bool:
        return False

    def forward(self, **kwargs) -> bool:
        self.forwarded.append(kwargs)
        return self.accept


async def test_a_reaction_reaches_the_node_holding_the_authors_socket(
    gfs, world, frames
):
    """Cluster: the author's socket is on another node — the reaction is
    forwarded once as a single-household item (its own typed frame), with
    the author's household as the one offline candidate, and nothing is
    queued here."""
    registry = gfs._app[gfs_moment_public_registry_key]
    fan_out = _FanOut()
    registry._fan_out_fwd = fan_out
    frames.offline.add("inst-author")
    sealed = _sealed()
    resp = await _react(
        gfs, world, body={"instance_id": "inst-follower", "sealed": sealed}
    )
    assert resp.status == 202
    assert fan_out.forwarded == [
        {
            "kind": "instance",
            "key": "inst-author",
            "frame": {
                "event_type": "moment_reaction",
                "from_instance": "inst-follower",
                "sealed": sealed,
            },
            "queue_for": ["inst-author"],
        }
    ]
    frames.offline.clear()
    assert await gfs._app[gfs_envelope_relay_key].drain("inst-author") == 0


def test_the_reaction_frame_may_cross_a_cluster_and_be_queued():
    """Another node pushes a forwarded ``moment_reaction`` only to the one
    household it names (``instance`` audience), and a queued copy drains as
    its own frame type."""
    assert FRAME_MOMENT_REACTION in TYPED_FRAME_TYPES["instance"]
    assert FRAME_MOMENT_REACTION not in TYPED_FRAME_TYPES["followers"]
    assert FRAME_MOMENT_REACTION in QUEUED_TYPED_FRAME_TYPES


def test_the_gfs_and_the_mesh_take_the_same_seal_size():
    assert MAX_REACT_SEALED_CHARS == MAX_SEALED_CHARS


def test_the_gfs_budgets():
    assert registry_mod.REACTIONS_PER_AUTHOR_PER_MINUTE == 30
    assert registry_mod.REACTIONS_PER_HOUSEHOLD_PER_MINUTE == 120


async def test_gfs_info_declares_moment_reactions(gfs):
    info = await (await gfs.get("/gfs/info")).json()
    assert info["capabilities"]["moment_reactions"] is True
