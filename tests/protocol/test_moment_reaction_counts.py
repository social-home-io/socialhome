"""Release-blocker protocol tests: everyone sees the full reaction count (v_69).

Marked ``@pytest.mark.security``.

Only a moment's author household holds every reaction to it, so it
re-broadcasts the totals — per-emoji counts and the total, never who — at
fixed checkpoints and only on change. The rules these tests encode, against
the real application registry, SQLite and the GFS app:

* **Author.** Sends ``MOMENT_REACTIONS_COUNTED`` (signed with its identity
  key, ``moment-counts:v1:``) at +10 min / +6 h / +3 d, only when the totals
  changed, only to v_69 peers; after a restart one catch-up per moment of
  the last day that has reactions, none for older moments.
* **Receiver.** Keeps totals only when the signature verifies against the
  pinned or derived origin key under a known suite, for a live moment held
  here by that author and origin, newer than the kept ones; a tombstone
  drops them. Budgets per new (moment, rev).
* **Relay.** Once per (moment, rev), only if it holds the moment from the
  mesh or relayed its create, only to v_69 peers, never a ``no_relay`` one.
* **GFS.** Takes ``/gfs/moments/counts`` only as the bare encrypted envelope
  from the author's own household under today's step, within its budgets;
  pushes it only to connected followers that declared ``ops: ["counts"]``
  and never queues it.
* **Follower.** Keeps GFS totals only when the envelope verifies and opens
  under a step key it already holds — without one it drops them and asks
  for nothing.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    instance_public_key_key,
    moment_public_inbound_key,
    moment_public_outbound_key,
    moment_reaction_counts_key,
    moment_service_key,
)
from socialhome.config import Config
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation.federation_service import FederationService
from socialhome.global_server import moment_public_registry as registry_mod
from socialhome.global_server.app_keys import (
    gfs_envelope_relay_key,
    gfs_fed_repo_key,
    gfs_moment_public_registry_key,
)
from socialhome.global_server.cluster_relay import (
    EPHEMERAL_FRAME_TYPES,
    TYPED_FRAME_TYPES,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.envelope_relay import QUEUED_TYPED_FRAME_TYPES
from socialhome.global_server.server import create_gfs_app
from socialhome.global_server.ws_registry import GfsWebSocketRegistry
from socialhome.moment_counts import counts_signing_bytes, sign_counts, verify_counts
from socialhome.moment_public_seal import (
    COUNTS_BODY_BYTES,
    ENVELOPE_FIELDS,
    FRAME_MOMENT_COUNTS,
    open_body,
    seal_body,
    sign_wire,
)
from socialhome.rate_limiter import RateLimiter
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.moment_public_key_inbound import MomentPublicKeyInbound
from socialhome.services.moment_reaction_counts import (
    GFS_RETRY_S,
    MESH_SENDS_PER_TICK,
    NEW_REVS_PER_ORIGIN_PER_MIN,
)

pytestmark = pytest.mark.security

FET = FederationEventType
V69 = FederationCapability.MIN_FOR_MOMENT_REACTION_COUNTS

ORIGIN_KEY = generate_identity_keypair()  # an author household two hops away
ORIGIN = derive_instance_id(ORIGIN_KEY.public_key)
NOOR = derive_user_id(ORIGIN_KEY.public_key, "noor-anchor")
NEIGHBOUR = "peer-neighbour"  # paired, v_69
OTHER = "peer-other"  # paired, v_69
OLD = "peer-old"  # paired, v_68
EXPIRES = (datetime.now(UTC) + timedelta(days=5)).isoformat()


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


async def _seed_peer(db, instance_id: str, proto_version: int) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            generate_identity_keypair().public_key.hex(),
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
            proto_version,
        ),
    )


async def _moment(db, mid, author, origin, via, *, created=None, public=0):
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " created_at, expires_at, received_via, is_public, hop_count)"
        " VALUES(?,?,?,?,?,?,?,?,2)",
        (
            mid,
            author,
            "hello",
            origin,
            (created or datetime.now(UTC)).isoformat(),
            EXPIRES,
            via,
            public,
        ),
    )


async def _react(db, mid: str, who: str, emoji: str) -> None:
    await db.enqueue(
        "INSERT INTO moment_reactions(moment_id, reactor_user_id, emoji)"
        " VALUES(?,?,?) ON CONFLICT(moment_id, reactor_user_id)"
        " DO UPDATE SET emoji=excluded.emoji",
        (mid, who, emoji),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    client = await aiohttp_client(app)
    db = app[db_key]
    own = app[federation_service_key].own_instance_id
    anna = derive_user_id(app[instance_public_key_key], "anna-anchor")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, identity_anchor)"
        " VALUES(?,?,?,?)",
        ("anna", anna, "Anna", "anna-anchor"),
    )
    for peer, version in ((NEIGHBOUR, V69), (OTHER, V69), (OLD, V69 - 1)):
        await _seed_peer(db, peer, version)
    sent: list[tuple[str, FederationEventType, dict, dict]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **kw):
        sent.append((to_instance_id, event_type, payload, kw))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    counts = app[moment_reaction_counts_key]
    return app, db, sent, own, anna, counts, client


async def _dispatch(app, payload, *, from_instance=NEIGHBOUR) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(
        FET.MOMENT_REACTIONS_COUNTED
    )
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=FET.MOMENT_REACTIONS_COUNTED,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload=payload,
            )
        )


def _now_ms(delta_s: float = 0) -> int:
    return int((time.time() + delta_s) * 1000)


def _signed(*, key=ORIGIN_KEY, rev=None, counts=None, total=3, **over) -> dict:
    payload = sign_counts(
        seed=key.private_key,
        identity_pk=key.public_key,
        moment_id=over.pop("moment_id", "m-noor"),
        author_user_id=over.pop("author", NOOR),
        origin_instance_id=over.pop("origin", ORIGIN),
        rev=rev if rev is not None else _now_ms(),
        counts={"🔥": 2, "👏": 1} if counts is None else counts,
        total=total,
        no_relay=over.pop("no_relay", False),
    )
    payload.update(over)
    return payload


async def _summary(db, mid: str = "m-noor"):
    row = await db.fetchone("SELECT reaction_summary FROM moments WHERE id=?", (mid,))
    return json.loads(row[0]) if row and row[0] else None


# ═══ Author: checkpoints, only on change, only to v_69 ════════════════════


async def test_author_sends_signed_totals_at_checkpoints_only_on_change(env):
    app, db, sent, own, anna, counts, _client = env
    start = datetime.now(UTC)
    await _moment(db, "m-anna", anna, own, "self", created=start)
    await _react(db, "m-anna", "u-x", "🔥")
    await _react(db, "m-anna", "u-y", "🔥")
    await _react(db, "m-anna", "u-z", "👏")

    def at(offset_s: float) -> None:
        counts._clock_ms = lambda: int((start.timestamp() + offset_s) * 1000)

    at(5 * 60)
    assert await counts.tick() == 0  # before +10 min
    at(11 * 60)
    assert await counts.tick() == 1
    to = sorted(s[0] for s in sent)
    assert to == [NEIGHBOUR, OTHER]  # never the v_68 peer
    _peer, event_type, payload, kw = sent[0]
    assert event_type is FET.MOMENT_REACTIONS_COUNTED
    assert payload["counts"] == {"🔥": 2, "👏": 1} and payload["total"] == 3
    assert payload["hop_count"] == 1 and payload["origin_instance_id"] == own
    assert verify_counts(identity_pk=app[instance_public_key_key], payload=payload)
    assert kw["outbox_key"] == "moment-counts:m-anna"  # latest-only queued
    assert "reactor_user_id" not in json.dumps(payload)  # never who
    sent.clear()
    at(12 * 60)
    assert await counts.tick() == 0  # no new checkpoint
    at(6 * 3600 + 60)
    assert await counts.tick() == 0  # +6 h, unchanged
    await _react(db, "m-anna", "u-w", "😮")
    at(3 * 86_400 + 60)  # +3 d — the moment lives 5 d, so followers keep it
    assert await counts.tick() == 1
    assert sent[0][2]["total"] == 4


async def test_no_three_day_checkpoint_once_followers_no_longer_keep_it(env):
    _app, db, sent, own, anna, counts, _client = env
    start = datetime.now(UTC)
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " created_at, expires_at, received_via) VALUES(?,?,?,?,?,?,'self')",
        (
            "m-short",
            anna,
            "x",
            own,
            start.isoformat(),
            (start + timedelta(days=2)).isoformat(),
        ),
    )
    await _react(db, "m-short", "u-x", "🔥")
    counts._clock_ms = lambda: int((start.timestamp() + 7 * 3600) * 1000)
    assert await counts.tick() == 1  # the +6 h one
    sent.clear()
    await _react(db, "m-short", "u-y", "🔥")
    counts._clock_ms = lambda: int((start.timestamp() + 3 * 86_400 + 60) * 1000)
    assert await counts.tick() == 0


async def test_a_restart_catches_up_recent_moments_once_and_old_ones_never(env):
    _app, db, sent, own, anna, counts, _client = env
    now = datetime.now(UTC)
    await _moment(db, "m-recent", anna, own, "self", created=now - timedelta(hours=2))
    await _moment(db, "m-old", anna, own, "self", created=now - timedelta(days=2))
    await _moment(db, "m-quiet", anna, own, "self", created=now - timedelta(hours=3))
    await _react(db, "m-recent", "u-x", "🔥")
    await _react(db, "m-old", "u-x", "🔥")
    counts._started_ms = _now_ms()
    counts._done.clear()
    counts._sent.clear()
    counts._clock_ms = _now_ms
    assert await counts.tick() == 1  # m-recent only (m-quiet has none)
    assert {s[2]["moment_id"] for s in sent} == {"m-recent"}
    sent.clear()
    assert await counts.tick() == 0


async def test_a_protected_authors_totals_are_marked_no_relay(env, monkeypatch):
    _app, db, sent, own, anna, counts, _client = env
    start = datetime.now(UTC)
    await _moment(db, "m-anna", anna, own, "self", created=start)
    await _react(db, "m-anna", "u-x", "🔥")

    async def _protected(_self, _user_id):
        return True

    monkeypatch.setattr(type(counts), "_is_protected", _protected)
    counts._clock_ms = lambda: int((start.timestamp() + 11 * 60) * 1000)
    assert await counts.tick() == 1
    assert all(s[2].get("no_relay") is True for s in sent)


# ═══ Receiver: only genuine, newer totals for a moment held here ══════════


async def test_receiver_keeps_genuine_totals_and_relays_them(env):
    app, db, sent, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    payload = _signed(rev=_now_ms())
    await _dispatch(app, payload)
    assert await _summary(db) == {
        "rev": payload["rev"],
        "counts": {"🔥": 2, "👏": 1},
        "total": 3,
    }
    # Relayed one hop on, to the other v_69 peer only.
    assert [(s[0], s[2]["hop_count"]) for s in sent] == [(OTHER, 2)]
    assert sent[0][3]["outbox_key"] == "moment-counts:m-noor"


def _forged_key():
    return _signed(key=generate_identity_keypair())


def _another_author():
    return _signed(author="u-mallory")


def _unknown_suite():
    p = _signed()
    p["counts_sig_suite"] = "ed448"
    return p


def _inflated():
    p = _signed()
    p["counts"] = {"🔥": 999}
    p["total"] = 999
    return p


def _malformed_counts():
    return _signed(counts={"not an emoji": 1}, total=1)


@pytest.mark.parametrize(
    "make", [_forged_key, _another_author, _unknown_suite, _inflated, _malformed_counts]
)
async def test_forged_or_malformed_totals_are_refused(env, make, caplog):
    app, db, sent, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await _dispatch(app, make())
    assert await _summary(db) is None
    assert sent == []


async def test_totals_naming_another_origin_household_are_refused(env):
    """A household signing totals for a moment another household authored:
    its key derives to itself, not to the moment's origin."""
    app, db, _sent, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    mallory = generate_identity_keypair()
    await _dispatch(
        app, _signed(key=mallory, origin=derive_instance_id(mallory.public_key))
    )
    assert await _summary(db) is None


async def test_older_or_replayed_totals_change_nothing(env):
    app, db, _sent, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    newer = _signed(rev=_now_ms(), counts={"🔥": 5}, total=5)
    await _dispatch(app, newer)
    await _dispatch(app, _signed(rev=newer["rev"] - 1000, counts={"🔥": 1}, total=1))
    await _dispatch(app, newer)  # a replay
    assert await _summary(db) == {"rev": newer["rev"], "counts": {"🔥": 5}, "total": 5}


async def test_totals_from_the_future_are_refused(env):
    app, db, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await _dispatch(app, _signed(rev=_now_ms(3600)))
    assert await _summary(db) is None


async def test_totals_for_an_own_or_unknown_moment_are_not_kept(env):
    app, db, sent, own, anna, *_ = env
    # Our own moment: the origin is us — refused outright.
    await _moment(db, "m-anna", anna, own, "self")
    await _dispatch(app, _signed(moment_id="m-anna"))
    assert await _summary(db, "m-anna") is None
    # Unknown moment: nothing kept, and nothing relayed (never relayed it).
    await _dispatch(app, _signed(moment_id="m-unknown"))
    assert sent == []


async def test_a_tombstone_wipes_the_totals_and_refuses_new_ones(env):
    app, db, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await _dispatch(app, _signed(rev=_now_ms(-10)))
    assert await _summary(db) is not None
    await db.enqueue(
        "UPDATE moments SET content='', deleted_at=datetime('now') WHERE id='m-noor'"
    )
    assert await _summary(db) is None
    await _dispatch(app, _signed(rev=_now_ms()))
    assert await _summary(db) is None


async def test_the_per_moment_budget_holds_a_flooding_origin(env):
    app, db, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    base = _now_ms(-100)
    for i in range(6):
        await _dispatch(app, _signed(rev=base + i, counts={"🔥": i + 1}, total=i + 1))
    kept = await _summary(db)
    assert kept is not None and kept["total"] == 4  # the 5th and 6th refused


# ═══ Relay: once per rev, only when held or relayed, only to v_69 ═════════


async def test_a_household_that_never_held_or_relayed_it_passes_nothing_on(env):
    app, _db, sent, *_ = env
    await _dispatch(app, _signed())
    assert sent == []


async def test_a_household_that_relayed_the_create_relays_the_totals(env):
    app, _db, sent, *_ = env
    outbound = app[federation_service_key]  # noqa: F841 — keeps the app alive
    counts = app[moment_reaction_counts_key]
    counts._outbound._remember_relay(("origin", "m-noor", ORIGIN), 1)
    payload = _signed()
    await _dispatch(app, payload)
    assert [s[0] for s in sent] == [OTHER]
    sent.clear()
    await _dispatch(app, payload, from_instance=OTHER)  # another copy, same rev
    assert sent == []


async def test_totals_for_another_origins_moment_are_neither_kept_nor_relayed(env):
    """A household (validly) signing totals under someone else's moment id —
    the one it relayed, or one held here — gets nowhere."""
    app, db, sent, *_ = env
    mallory = generate_identity_keypair()
    m_origin = derive_instance_id(mallory.public_key)
    counts = app[moment_reaction_counts_key]
    # Relayed: we passed on ORIGIN's create, not Mallory's.
    counts._outbound._remember_relay(("origin", "m-noor", ORIGIN), 1)
    await _dispatch(app, _signed(key=mallory, origin=m_origin))
    assert sent == []
    # Held: the moment is ORIGIN's.
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await _dispatch(app, _signed(key=mallory, origin=m_origin))
    assert await _summary(db) is None
    assert sent == []


async def test_a_forger_cannot_spend_another_origins_moment_budget(env):
    app, db, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    mallory = generate_identity_keypair()
    m_origin = derive_instance_id(mallory.public_key)
    base = _now_ms(-100)
    for i in range(6):
        await _dispatch(app, _signed(key=mallory, origin=m_origin, rev=base + i))
    await _dispatch(app, _signed(rev=base + 10, counts={"🔥": 9}, total=9))
    kept = await _summary(db)
    assert kept is not None and kept["total"] == 9


async def test_a_gfs_only_copy_is_not_relayed_to_the_mesh(env):
    app, db, sent, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "gfs", public=1)
    await _dispatch(app, _signed())
    assert await _summary(db) is not None  # kept here…
    assert sent == []  # …but the mesh never carried it through us


async def test_no_relay_totals_are_refused_when_relayed(env):
    app, db, sent, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await _dispatch(app, _signed(no_relay=True))  # from a neighbour, not ORIGIN
    assert await _summary(db) is None
    assert sent == []


async def test_a_relay_at_the_hop_cap_stops(env):
    app, db, sent, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await _dispatch(app, _signed(hop_count=3))
    assert sent == []


def test_hop_count_is_outside_the_signature():
    p = _signed()
    assert counts_signing_bytes(p) == counts_signing_bytes({**p, "hop_count": 3})


# ═══ API: the full count, with the viewer's own reaction overlaid ═════════


async def test_api_shows_the_authors_totals_and_marks_my_reaction(env):
    app, db, _sent, _own, anna, _counts, _client = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await _dispatch(app, _signed(rev=_now_ms(-120), counts={"🔥": 7}, total=9))
    await _react(db, "m-noor", anna, "👏")  # after the totals were sent
    await _react(db, "m-noor", "u-housemate", "🔥")
    svc = app[moment_service_key]
    moment = await svc.get_moment("m-noor")
    view = (
        await svc.reaction_totals(
            [moment], viewer_user_id=anna, reach={"m-noor": "author"}
        )
    )["m-noor"]
    # Never over-counted: whether Anna is in them already can't be told, so
    # the totals stand until the next checkpoint (adversarial review #1184).
    assert (view.total, view.counts) == (9, {"🔥": 7})
    assert view.my_reaction == "👏" and isinstance(view.totals_rev, int)
    # Where reactions stay here, the household's own reactions show instead.
    local = (
        await svc.reaction_totals(
            [moment], viewer_user_id=anna, reach={"m-noor": "household"}
        )
    )["m-noor"]
    assert (local.total, local.counts, local.totals_rev) == (
        2,
        {"👏": 1, "🔥": 1},
        None,
    )


# ═══ GFS: bare envelope, author's household, today's step, live only ══════


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


_SUITES = {"kem": ["x25519"], "seal": ["aesgcm-256"]}


@pytest.fixture
async def world(gfs):
    """An author household with one registered user; a follower household
    that takes totals, one that declared none, and a stranger."""
    keys = {}
    for iid in ("inst-author", "inst-fan", "inst-plain", "inst-stranger"):
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
    for iid, suites in (
        ("inst-fan", {**_SUITES, "ops": ["counts"]}),
        ("inst-plain", _SUITES),
    ):
        resp = await gfs.post(
            "/gfs/moments/users/u-author/follow",
            json=_sign(
                keys[iid],
                {
                    "instance_id": iid,
                    "follower_instance_id": iid,
                    "follower_user_id": f"u-{iid}",
                    "moment_suites": suites,
                },
            ),
        )
        assert resp.status == 201, await resp.text()
    return keys


_SEQ = iter(range(1_000_000))


def _envelope(
    kp, *, moment_id="m-1", author="u-author", sender="inst-author", gen=None
):
    gen = int(time.time()) - 60 if gen is None else gen
    n = next(_SEQ)  # a fresh ciphertext each time, as a real seal has
    return _sign(
        kp,
        {
            "author_user_id": author,
            "instance_id": sender,
            "moment_id": moment_id,
            "key_gen": gen,
            "key_step": 0,
            "seal_suite": "aesgcm-256",
            "ciphertext": f"bm9uY2U:Y2lwaGVydGV4dA{n}",
        },
    )


async def _counts(gfs, body):
    return await gfs.post("/gfs/moments/counts", json=body)


async def test_gfs_pushes_totals_only_to_connected_followers_that_take_them(
    gfs, world, frames
):
    frames.clear()
    env = _envelope(world["inst-author"])
    resp = await _counts(gfs, env)
    assert resp.status == 202
    assert frames == [("inst-fan", {"type": FRAME_MOMENT_COUNTS, "payload": env})]


async def test_gfs_never_queues_totals_for_an_offline_follower(gfs, world, frames):
    frames.offline.add("inst-fan")
    resp = await _counts(gfs, _envelope(world["inst-author"]))
    assert resp.status == 202
    frames.offline.clear()
    frames.clear()
    assert await gfs._app[gfs_envelope_relay_key].drain("inst-fan") == 0
    assert frames == []


async def test_gfs_refuses_totals_from_another_household(gfs, world, frames):
    resp = await _counts(gfs, _envelope(world["inst-stranger"], sender="inst-stranger"))
    assert resp.status == 403


async def test_gfs_refuses_an_earlier_days_step(gfs, world, frames):
    resp = await _counts(
        gfs, _envelope(world["inst-author"], gen=int(time.time()) - 3 * 86_400)
    )
    assert resp.status == 409


async def test_gfs_refuses_anything_but_the_bare_envelope(gfs, world, frames):
    kp = world["inst-author"]
    env = _envelope(kp)
    body = {k: v for k, v in env.items() if k != "signature"}
    body["counts"] = {"🔥": 3}  # plaintext totals — never
    resp = await _counts(gfs, _sign(kp, body))
    assert resp.status == 422


async def test_gfs_holds_one_moment_to_three_updates_a_day(gfs, world, frames):
    kp = world["inst-author"]
    for n in range(3):
        env = _envelope(kp)
        assert (await _counts(gfs, env)).status == 202, n
    assert (await _counts(gfs, _envelope(kp))).status == 429
    # Another moment still goes.
    assert (await _counts(gfs, _envelope(kp, moment_id="m-2"))).status == 202


async def test_gfs_holds_an_author_household_to_ten_a_minute(gfs, world, frames):
    kp = world["inst-author"]
    for n in range(10):
        assert (await _counts(gfs, _envelope(kp, moment_id=f"m-{n}"))).status == 202
    assert (await _counts(gfs, _envelope(kp, moment_id="m-x"))).status == 429


async def test_a_retried_post_is_pushed_once_and_spends_nothing(gfs, world, frames):
    kp = world["inst-author"]
    env = _envelope(kp)
    frames.clear()
    for _ in range(5):
        assert (await _counts(gfs, env)).status == 202
    assert len(frames) == 1
    assert (await _counts(gfs, _envelope(kp))).status == 202  # budget intact


class _FanOut:
    def __init__(self) -> None:
        self.forwarded: list[dict] = []

    def seen_frame(self, _frame) -> bool:
        return False

    def forward(self, **kwargs) -> bool:
        self.forwarded.append(kwargs)
        return True


async def test_gfs_forwards_totals_to_the_cluster_without_queueing(gfs, world, frames):
    registry = gfs._app[gfs_moment_public_registry_key]
    fan_out = _FanOut()
    registry._fan_out_fwd = fan_out
    env = _envelope(world["inst-author"])
    assert (await _counts(gfs, env)).status == 202
    assert fan_out.forwarded == [
        {
            "kind": "count_followers",
            "key": "u-author",
            "frame": {"event_type": FRAME_MOMENT_COUNTS, "payload": env},
        }
    ]


async def test_updates_another_node_took_count_against_the_day(gfs, world, frames):
    registry = gfs._app[gfs_moment_public_registry_key]
    kp = world["inst-author"]
    for _ in range(3):
        registry.note_forwarded_counts({"payload": _envelope(kp)})
    assert (await _counts(gfs, _envelope(kp))).status == 429


async def test_gfs_refuses_an_unknown_op_in_a_declaration(gfs, world):
    resp = await gfs.post(
        "/gfs/moments/suites",
        json=_sign(
            world["inst-plain"],
            {
                "instance_id": "inst-plain",
                "moment_suites": {**_SUITES, "ops": ["everything"]},
            },
        ),
    )
    assert resp.status == 422


def test_the_counts_frame_is_live_only_on_every_node():
    assert FRAME_MOMENT_COUNTS in TYPED_FRAME_TYPES["count_followers"]
    assert FRAME_MOMENT_COUNTS not in TYPED_FRAME_TYPES["followers"]
    assert FRAME_MOMENT_COUNTS in EPHEMERAL_FRAME_TYPES
    assert FRAME_MOMENT_COUNTS not in QUEUED_TYPED_FRAME_TYPES


def test_the_gfs_counts_budgets():
    assert registry_mod.COUNTS_PER_HOUSEHOLD_PER_MINUTE == 10
    assert registry_mod.COUNTS_PER_MOMENT_PER_DAY == 3


async def test_gfs_info_declares_moment_counts(gfs):
    info = await (await gfs.get("/gfs/info")).json()
    assert info["capabilities"]["moment_counts"] is True


# ═══ Follower: keeps GFS totals only with a key it holds ══════════════════

STEP_KEY = bytes(range(32))


async def _gfs_follow(db, anna) -> None:
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g1', 'g1', 'G', ?, "
        "'https://g1.example', 'active', datetime('now'))",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO moment_public_follows(follower_user_id, followed_user_id,"
        " gfs_id, followed_instance_pk, followed_username, followed_display_name)"
        " VALUES(?, ?, 'g1', ?, 'noor', 'Noor')",
        (anna, NOOR, ORIGIN_KEY.public_key.hex()),
    )
    await _moment(db, "m-noor", NOOR, ORIGIN, "gfs", public=1)


def _gfs_counts(body_over=None, *, key=ORIGIN_KEY, sender=ORIGIN) -> dict:
    body = {
        "op": "counts",
        "author_user_id": NOOR,
        "rev": _now_ms(),
        "counts": {"🔥": 4},
        "total": 6,
        "sent_at": int(time.time()),
        **(body_over or {}),
    }
    return {
        "type": FRAME_MOMENT_COUNTS,
        "payload": sign_wire(
            key.private_key,
            {
                "author_user_id": NOOR,
                "instance_id": sender,
                **seal_body(
                    step_key=STEP_KEY,
                    author_user_id=NOOR,
                    key_gen=1,
                    key_step=0,
                    moment_id="m-noor",
                    body=body,
                ),
            },
        ),
    }


@pytest.fixture
def follower(env, monkeypatch):
    app, db, _sent, _own, anna, *_ = env
    asked: list = []
    held = {"key": STEP_KEY}

    async def _key_for(_self, _author, _gen, _step):
        return held["key"]

    async def _request(_self, *a, **kw):
        asked.append((a, kw))

    monkeypatch.setattr(MomentPublicKeyInbound, "key_for", _key_for)
    monkeypatch.setattr(MomentPublicKeyInbound, "request_key", _request)
    return app, db, anna, held, asked


async def test_follower_keeps_totals_through_the_gfs(follower):
    app, db, anna, _held, _asked = follower
    await _gfs_follow(db, anna)
    await app[moment_public_inbound_key].handle(_gfs_counts(), gfs_id="g1")
    kept = await _summary(db)
    assert kept is not None and kept["counts"] == {"🔥": 4} and kept["total"] == 6


async def test_follower_without_the_step_key_drops_totals_and_asks_nothing(
    follower,
):
    app, db, anna, held, asked = follower
    await _gfs_follow(db, anna)
    held["key"] = None
    await app[moment_public_inbound_key].handle(_gfs_counts(), gfs_id="g1")
    assert await _summary(db) is None
    assert asked == []


@pytest.mark.parametrize(
    "frame",
    [
        lambda: _gfs_counts(key=generate_identity_keypair()),  # not the author
        lambda: _gfs_counts({"op": "create"}),
        lambda: _gfs_counts({"author_user_id": "u-mallory"}),
        lambda: _gfs_counts({"sent_at": int(time.time()) - 30 * 3600}),
        lambda: _gfs_counts({"counts": {"🔥": 9}, "total": 2}),
        lambda: _gfs_counts(sender="inst-elsewhere"),  # not the moment's origin
    ],
)
async def test_follower_refuses_forged_or_malformed_gfs_totals(follower, frame):
    app, db, anna, _held, _asked = follower
    await _gfs_follow(db, anna)
    await app[moment_public_inbound_key].handle(frame(), gfs_id="g1")
    assert await _summary(db) is None


# ═══ Author → GFS: the bare envelope, only to a GFS that proved it ════════


class _Resp:
    status = 202

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        #: host → HTTP status to answer (202 otherwise).
        self.status_for: dict[str, int] = {}

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json))
        resp = _Resp()
        resp.status = self.status_for.get(url.split("/gfs")[0], 202)
        return resp


async def test_author_posts_sealed_totals_only_to_a_gfs_that_proved_it(
    env, monkeypatch
):
    app, db, _sent, own, anna, *_ = env
    for gid in ("g1", "g2"):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
            " public_key, inbox_url, status, paired_at) VALUES(?, ?, 'G', ?, ?,"
            " 'active', datetime('now'))",
            (gid, gid, "aa" * 32, f"https://{gid}.example"),
        )
        await db.enqueue(
            "INSERT INTO moment_public_registrations(user_id, gfs_id) VALUES(?, ?)",
            (anna, gid),
        )
    await _moment(db, "m-anna", anna, own, "self", public=1)

    async def _yes(_self, _gfs_id):
        return True

    async def _only_g1(_self, gfs_id):
        return gfs_id == "g1"

    monkeypatch.setattr(GfsConnectionService, "encrypted_moments_supported", _yes)
    monkeypatch.setattr(GfsConnectionService, "moment_counts_supported", _only_g1)
    outbound = app[moment_public_outbound_key]
    session = _Session()
    outbound._http_client = session
    moment = await app[moment_reaction_counts_key]._moments.get("m-anna")
    sent = await outbound.send_counts(moment, rev=123, counts={"🔥": 2}, total=2)
    assert sent == (frozenset({"g1"}), frozenset())
    [(url, body)] = session.posts
    assert url == "https://g1.example/gfs/moments/counts"
    assert set(body) == ENVELOPE_FIELDS  # nothing the GFS could read
    gen, step, key = await outbound._keys.publish_key(anna)
    assert (body["key_gen"], body["key_step"]) == (gen, step)  # today's step
    opened = open_body(step_key=key, envelope=body)
    assert opened["op"] == "counts"
    assert (opened["counts"], opened["total"], opened["rev"]) == ({"🔥": 2}, 2, 123)


# ═══ Adversarial review of #1184: budgets, pacing, padding, overlay ═══════


async def test_replayed_old_totals_spend_none_of_the_origins_minute(env):
    """A neighbour replays genuinely signed but stale totals of the origin
    (moments not held here, two days old): refused before any budget, so
    the origin's real fresh totals still land."""
    app, db, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    old = _now_ms(-2 * 86400)
    for i in range(10):
        await _dispatch(app, _signed(moment_id=f"m-old-{i}", rev=old + i))
    await _dispatch(app, _signed(rev=_now_ms(), counts={"🔥": 9}, total=9))
    assert (await _summary(db))["total"] == 9


async def test_replayed_unheld_fresh_totals_spend_none_of_the_origins_minute(env):
    """Fresh genuine totals for moments neither held nor relayed here are
    neither kept nor relayed — so they cost the origin nothing."""
    app, db, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    for i in range(10):
        await _dispatch(app, _signed(moment_id=f"m-else-{i}", rev=_now_ms(-60) + i))
    await _dispatch(app, _signed(rev=_now_ms(), counts={"🔥": 9}, total=9))
    assert (await _summary(db))["total"] == 9


async def test_replayed_older_revs_spend_none_of_the_moments_day(env):
    """Receivers keep only the newest rev. Older genuine revs replayed by a
    neighbour are not newer than the kept one: refused before the day
    budget, so the next real checkpoint is still taken."""
    app, db, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    base = _now_ms(-3600)
    await _dispatch(app, _signed(rev=base + 10, counts={"🔥": 3}, total=3))
    for i in range(3):
        await _dispatch(app, _signed(rev=base + i, counts={"🔥": 1}, total=1))
    await _dispatch(app, _signed(rev=_now_ms(), counts={"🔥": 8}, total=8))
    assert (await _summary(db))["total"] == 8


async def test_a_replay_of_the_kept_rev_after_a_restart_spends_nothing(env):
    """The kept rev itself, replayed once the in-memory seen set is gone (a
    restart), is not newer than what the database holds: refused, free."""
    app, db, sent, *_ = env
    counts = app[moment_reaction_counts_key]
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    kept = _signed(rev=_now_ms(-3600), counts={"🔥": 3}, total=3)
    await _dispatch(app, kept)
    counts._seen_revs.clear()
    sent.clear()
    for _ in range(5):
        await _dispatch(app, kept, from_instance=OTHER)
    assert sent == []  # not relayed again either
    for i in range(3):
        await _dispatch(
            app, _signed(rev=_now_ms(-60) + i, counts={"🔥": 4 + i}, total=4 + i)
        )
    assert (await _summary(db))["total"] == 6


async def test_totals_older_than_a_day_are_neither_kept_nor_relayed(env):
    app, db, sent, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await _dispatch(app, _signed(rev=_now_ms(-26 * 3600)))
    assert await _summary(db) is None
    assert sent == []


async def test_eleven_moments_at_one_checkpoint_all_reach_a_receiver(env):
    """The author sends at most MESH_SENDS_PER_TICK a tick — under every
    receiver's per-origin minute budget — and the rest the next tick, so
    eleven moments due together all get through."""
    _app, db, sent, own, anna, counts, _client = env
    start = datetime.now(UTC) - timedelta(minutes=11)
    for i in range(11):
        await _moment(db, f"m-{i}", anna, own, "self", created=start)
        await _react(db, f"m-{i}", "u-x", "🔥")
    clock = [int(time.time() * 1000)]
    counts._clock_ms = lambda: clock[0]
    first = await counts.tick()
    assert 0 < first <= MESH_SENDS_PER_TICK < NEW_REVS_PER_ORIGIN_PER_MIN
    clock[0] += 61_000
    assert first + await counts.tick() == 11
    assert {s[2]["moment_id"] for s in sent} == {f"m-{i}" for i in range(11)}
    clock[0] += 61_000
    assert await counts.tick() == 0  # each sent once


async def test_paced_sends_all_pass_the_receivers_minute_budget(env):
    """Receiver side of the same burst: MESH_SENDS_PER_TICK a minute from
    one origin, for two minutes, are all kept."""
    app, db, *_ = env
    counts = app[moment_reaction_counts_key]
    mono = [1000.0]
    counts._limiter = RateLimiter(monotonic=lambda: mono[0])
    for i in range(16):
        await _moment(db, f"m-{i}", NOOR, ORIGIN, "household")
    for i in range(16):
        if i == MESH_SENDS_PER_TICK:
            mono[0] += 61
        await _dispatch(app, _signed(moment_id=f"m-{i}", rev=_now_ms() + i))
    missing = [i for i in range(16) if await _summary(db, f"m-{i}") is None]
    assert missing == []


async def test_a_mesh_send_that_fails_is_retried_next_tick(env, monkeypatch):
    """'Last sent' is recorded only once the update was handed to the
    outbox: a send that raised is tried again."""
    _app, db, sent, own, anna, counts, _client = env
    start = datetime.now(UTC) - timedelta(minutes=11)
    await _moment(db, "m-anna", anna, own, "self", created=start)
    await _react(db, "m-anna", "u-x", "🔥")
    cls = type(counts._outbound)
    real = cls.fan_counts
    calls = []

    async def _boom(_self, payload, *, author_user_id):
        calls.append(payload)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cls, "fan_counts", _boom)
    assert await counts.tick() == 0
    assert len(calls) == 1
    monkeypatch.setattr(cls, "fan_counts", real)
    assert await counts.tick() == 1
    assert {s[2]["moment_id"] for s in sent} == {"m-anna"}


async def test_a_gfs_429_is_retried_and_only_a_2xx_counts_as_sent(env, monkeypatch):
    app, db, _sent, own, anna, counts, _client = env
    for gid in ("g1", "g2"):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
            " public_key, inbox_url, status, paired_at) VALUES(?, ?, 'G', ?, ?,"
            " 'active', datetime('now'))",
            (gid, gid, "aa" * 32, f"https://{gid}.example"),
        )
        await db.enqueue(
            "INSERT INTO moment_public_registrations(user_id, gfs_id) VALUES(?, ?)",
            (anna, gid),
        )
    start = datetime.now(UTC) - timedelta(minutes=11)
    await _moment(db, "m-anna", anna, own, "self", created=start, public=1)
    await _react(db, "m-anna", "u-x", "🔥")

    async def _yes(_self, _gfs_id):
        return True

    monkeypatch.setattr(GfsConnectionService, "encrypted_moments_supported", _yes)
    monkeypatch.setattr(GfsConnectionService, "moment_counts_supported", _yes)
    outbound = app[moment_public_outbound_key]
    session = _Session()
    session.status_for = {"https://g2.example": 429}
    outbound._http_client = session
    clock = [int(time.time() * 1000)]
    counts._clock_ms = lambda: clock[0]
    await counts.tick()
    hosts = [u.split("/gfs")[0] for u, _ in session.posts]
    assert sorted(hosts) == ["https://g1.example", "https://g2.example"]
    session.posts.clear()
    session.status_for = {}
    await counts.tick()
    assert session.posts == []  # not before the retry interval
    clock[0] += GFS_RETRY_S * 1000 + 1000
    await counts.tick()
    # Only the GFS that refused is posted again — g1 already has them.
    assert [u.split("/gfs")[0] for u, _ in session.posts] == ["https://g2.example"]
    session.posts.clear()
    clock[0] += GFS_RETRY_S * 1000 + 1000
    await counts.tick()
    assert session.posts == []


def _counts_ciphertext_len(counts: dict, total: int) -> int:
    return len(
        seal_body(
            step_key=b"k" * 32,
            author_user_id=NOOR,
            key_gen=1,
            key_step=1,
            moment_id="m",
            body={
                "op": "counts",
                "author_user_id": NOOR,
                "rev": _now_ms(),
                "counts": counts,
                "total": total,
                "sent_at": int(time.time()),
            },
            pad_to=COUNTS_BODY_BYTES,
        )["ciphertext"]
    )


def test_sealed_totals_are_one_size_whatever_they_say():
    """The GFS sees the ciphertext length: padded to one fixed size, it
    can't tell one reaction from a hundred or one emoji from eight."""
    worst = dict.fromkeys(
        ("👨‍👩‍👧‍👦", "🏳️‍🌈", "👩🏽‍🚀", "🧑🏿‍🤝‍🧑🏻", "❤️‍🔥", "🫶🏾", "🙋🏽‍♀️", "🤦🏼‍♂️"),
        125_000,
    )
    sizes = {
        _counts_ciphertext_len({"🔥": 1}, 1),
        _counts_ciphertext_len({"🔥": 100}, 100),
        _counts_ciphertext_len(dict.fromkeys("🔥👏😂❤🎉😮😢🙏", 1), 8),
        _counts_ciphertext_len(worst, 1_000_000),
        _counts_ciphertext_len({}, 0),
    }
    assert len(sizes) == 1, sizes


async def test_author_posts_totals_padded_to_one_size(env, monkeypatch):
    app, db, _sent, own, anna, *_ = env
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at) VALUES('g1', 'g1', 'G', ?,"
        " 'https://g1.example', 'active', datetime('now'))",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO moment_public_registrations(user_id, gfs_id) VALUES(?, 'g1')",
        (anna,),
    )
    await _moment(db, "m-anna", anna, own, "self", public=1)

    async def _yes(_self, _gfs_id):
        return True

    monkeypatch.setattr(GfsConnectionService, "encrypted_moments_supported", _yes)
    monkeypatch.setattr(GfsConnectionService, "moment_counts_supported", _yes)
    outbound = app[moment_public_outbound_key]
    session = _Session()
    outbound._http_client = session
    moment = await app[moment_reaction_counts_key]._moments.get("m-anna")
    await outbound.send_counts(moment, rev=_now_ms(), counts={"🔥": 1}, total=1)
    await outbound.send_counts(
        moment,
        rev=_now_ms() + 1,
        counts=dict.fromkeys("🔥👏😂❤🎉😮😢🙏", 125_000),
        total=1_000_000,
    )
    a, b = (body for _url, body in session.posts)
    assert len(a["ciphertext"]) == len(b["ciphertext"])
    _gen, _step, key = await outbound._keys.publish_key(anna)
    assert open_body(step_key=key, envelope=a)["counts"] == {"🔥": 1}


async def test_changing_my_emoji_after_the_totals_never_counts_me_twice(env):
    """Anna's 🔥 is in the author's totals (5); she switches to 👏 later.
    The view must not show 6: the totals' numbers stand (one checkpoint
    stale at most), her emoji is marked as hers."""
    app, db, _sent, _own, anna, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    await db.enqueue(
        "INSERT INTO moment_reactions(moment_id, reactor_user_id, emoji, reacted_at)"
        " VALUES('m-noor', ?, '🔥', datetime('now','-1 hour'))",
        (anna,),
    )
    await _dispatch(app, _signed(rev=_now_ms(-60), counts={"🔥": 5}, total=5))
    await db.enqueue(
        "UPDATE moment_reactions SET emoji='👏', reacted_at=datetime('now')"
        " WHERE moment_id='m-noor' AND reactor_user_id=?",
        (anna,),
    )
    svc = app[moment_service_key]
    m = await svc.get_moment("m-noor")
    reach = {"m-noor": "author"}
    v = (await svc.reaction_totals([m], viewer_user_id=anna, reach=reach))["m-noor"]
    assert (v.total, v.counts, v.my_reaction) == (5, {"🔥": 5}, "👏")


async def test_my_newer_reaction_never_adds_a_ninth_emoji(env):
    app, db, _sent, _own, anna, *_ = env
    await _moment(db, "m-noor", NOOR, ORIGIN, "household")
    eight = dict.fromkeys("🔥👏😂❤🎉😮😢🙏", 1)
    await _dispatch(app, _signed(rev=_now_ms(-120), counts=eight, total=8))
    await _react(db, "m-noor", anna, "🥳")
    svc = app[moment_service_key]
    m = await svc.get_moment("m-noor")
    reach = {"m-noor": "author"}
    v = (await svc.reaction_totals([m], viewer_user_id=anna, reach=reach))["m-noor"]
    assert v.total == 8 and v.counts == eight and v.my_reaction == "🥳"
