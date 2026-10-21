"""Tests for :class:`MomentPublicRegistry` (GFS broker)."""

from __future__ import annotations

import time

import orjson
import pytest

from socialhome.global_server.domain import GfsMomentKeyMail
from socialhome.global_server.moment_public_registry import (
    MomentPublicRegistry,
    suites_can_open,
)
from socialhome.global_server.repositories import (
    SqliteGfsFederationRepo,
    SqliteGfsMomentFollowRepo,
    SqliteGfsMomentKeyMailboxRepo,
    SqliteGfsUserRegistrationRepo,
)
from socialhome.global_server.ws_registry import GfsWebSocketRegistry


class _StubWs:
    """Minimal stand-in for ``aiohttp.web.WebSocketResponse``."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)


def _sent_field(ws: _StubWs, key: str) -> list:
    """Decoded values of ``key`` across every frame a stub WS received.

    Parses each frame so assertions are robust to JSON formatting
    (compact vs spaced) — the registry serialises with orjson, which is
    always compact.
    """
    return [orjson.loads(s).get(key) for s in ws.sent]


@pytest.fixture
async def gfs_setup(gfs_db):
    # Seed two paired instances on the GFS — the author and the follower.
    await gfs_db.enqueue(
        "INSERT INTO client_instances("
        "instance_id, display_name, public_key, status) "
        "VALUES('inst-author','Author','aa'*32,'active')"
    )
    await gfs_db.enqueue(
        "INSERT INTO client_instances("
        "instance_id, display_name, public_key, status) "
        "VALUES('inst-follower','Follower','bb'*32,'active')"
    )
    ws_registry = GfsWebSocketRegistry()
    return {
        "users": SqliteGfsUserRegistrationRepo(gfs_db),
        "follows": SqliteGfsMomentFollowRepo(gfs_db),
        "ws": ws_registry,
    }


def _make_registry(gfs_setup) -> MomentPublicRegistry:
    return MomentPublicRegistry(
        gfs_setup["users"],
        gfs_setup["follows"],
        gfs_setup["ws"],
    )


async def test_register_user_then_directory_lists_them(gfs_setup):
    reg = _make_registry(gfs_setup)
    user = await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
        picture_url=None,
    )
    assert user.status == "active"
    listing = await reg.list_directory()
    assert [u.user_id for u in listing] == ["u-author"]
    got = await reg.get_registration("u-author")
    assert got is not None and got.username == "alice"


async def test_deregister_removes_row(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    assert await reg.deregister_user("u-author") is True
    assert await reg.deregister_user("u-author") is False  # idempotent


async def test_add_follow_pushes_follow_changed_to_author_ws(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    follow = await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    assert follow.followed_user_id == "u-author"
    # Author received the follow_changed frame.
    assert "follow_changed" in _sent_field(author_ws, "type")
    assert "add" in _sent_field(author_ws, "action")


async def test_add_follow_unknown_author_raises(gfs_setup):
    reg = _make_registry(gfs_setup)
    with pytest.raises(LookupError):
        await reg.add_follow(
            follower_user_id="u-follower",
            follower_instance_id="inst-follower",
            followed_user_id="u-ghost",
        )


async def test_remove_follow_pushes_follow_changed_remove(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    ok = await reg.remove_follow(
        follower_user_id="u-follower", followed_user_id="u-author"
    )
    assert ok is True
    assert "remove" in _sent_field(author_ws, "action")
    # Idempotent on a second call.
    ok2 = await reg.remove_follow(
        follower_user_id="u-follower", followed_user_id="u-author"
    )
    assert ok2 is False


async def test_fan_out_moment_pushes_to_each_unique_follower_instance(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
        moment_suites='{"kem":["x25519"],"seal":["aesgcm-256"]}',
    )
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]
    delivered = await reg.fan_out_moment(
        envelope={
            "moment_id": "m-1",
            "author_user_id": "u-author",
            "ciphertext": "c:t",
            "signature": "stub",
        }
    )
    assert delivered == 1
    assert "incoming_public_moment" in _sent_field(follower_ws, "type")


async def test_fan_out_moment_drops_author_with_no_followers(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    delivered = await reg.fan_out_moment(
        envelope={"moment_id": "m-1", "author_user_id": "u-author"}
    )
    assert delivered == 0


async def test_fan_out_moment_missing_author_logs_and_returns_zero(gfs_setup):
    reg = _make_registry(gfs_setup)
    delivered = await reg.fan_out_moment(envelope={"moment_id": "m-1"})
    assert delivered == 0


async def test_fan_out_delete_pushes_tombstone(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
        moment_suites='{"kem":["x25519"],"seal":["aesgcm-256"]}',
    )
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]
    delivered = await reg.fan_out_delete(
        envelope={"moment_id": "m-1", "author_user_id": "u-author"}
    )
    assert delivered == 1
    assert "incoming_public_moment_delete" in _sent_field(follower_ws, "type")


async def test_follower_count_reads_repo(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    assert await reg.follower_count("u-author") == 0
    await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    assert await reg.follower_count("u-author") == 1


# ── Key routing (v_62) ────────────────────────────────────────────────────


async def _with_follow(gfs_setup, *, instances=None) -> MomentPublicRegistry:
    reg = MomentPublicRegistry(
        gfs_setup["users"], gfs_setup["follows"], gfs_setup["ws"], instances
    )
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    await reg.add_follow(
        follower_user_id="u-fan",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    return reg


async def test_without_an_instance_repo_no_follower_keys_are_named(gfs_setup):
    """No household directory wired: the follow notice carries no keys and
    a key request is dropped (nothing to seal to)."""
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    reg = await _with_follow(gfs_setup)
    assert "follower" not in orjson.loads(author_ws.sent[-1])
    author_ws.sent.clear()
    await reg.request_key(
        follower_instance_id="inst-follower", followed_user_id="u-author", want_gen=0
    )
    assert author_ws.sent == []


async def test_an_inactive_follower_household_is_never_named(gfs_setup, gfs_db):
    await gfs_db.enqueue(
        "UPDATE client_instances SET status='banned' WHERE instance_id='inst-follower'"
    )
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    await _with_follow(gfs_setup, instances=SqliteGfsFederationRepo(gfs_db))
    assert "follower" not in orjson.loads(author_ws.sent[-1])


async def test_key_request_for_a_suspended_author_is_dropped(gfs_setup, gfs_db):
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    reg = await _with_follow(gfs_setup, instances=SqliteGfsFederationRepo(gfs_db))
    await gfs_db.enqueue(
        "UPDATE gfs_user_registrations SET status='suspended' WHERE user_id='u-author'"
    )
    author_ws.sent.clear()
    await reg.request_key(
        follower_instance_id="inst-follower", followed_user_id="u-author", want_gen=0
    )
    assert author_ws.sent == []


async def test_deliveries_missing_an_author_or_target_are_skipped(gfs_setup):
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]
    reg = await _with_follow(gfs_setup)
    delivered = await reg.deliver_keys(
        sender_instance_id="inst-author",
        deliveries=[{"author_user_id": "", "to": "inst-follower"}, {"to": ""}],
    )
    assert delivered == 0
    assert follower_ws.sent == []


def test_suites_can_open():
    assert suites_can_open('{"kem":["x25519"],"seal":["aesgcm-256"]}')
    for raw in (None, "", "nope", "[]", '{"seal":"aesgcm-256"}', '{"seal":["x"]}'):
        assert not suites_can_open(raw)


async def test_without_a_mailbox_keys_reach_only_online_followers(gfs_setup):
    reg = await _with_follow(gfs_setup)
    assert await reg.on_follower_connected("inst-follower") == 0
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]
    delivered = await reg.deliver_keys(
        sender_instance_id="inst-author",
        deliveries=[{"author_user_id": "u-author", "to": "inst-follower"}],
    )
    assert delivered == 1
    assert _sent_field(follower_ws, "type") == ["moment_key"]


# ── Cluster + offline delivery (§24.10) ───────────────────────────────────
#
# The real three-node path lives in ``test_cluster_fanout.py``; these pin
# each branch with in-memory fakes.

_SUITES = '{"kem":["x25519"],"seal":["aesgcm-256"]}'
_ENVELOPE = {"moment_id": "m-1", "author_user_id": "u-author", "ciphertext": "c:t"}
_PUSH = {"type": "incoming_public_moment", "payload": _ENVELOPE}
_FORWARDED = {"event_type": "incoming_public_moment", "payload": _ENVELOPE}


class _FanOut:
    """``ClusterRelayFanOut`` stand-in."""

    def __init__(self, *, accept: bool = True, seen=()) -> None:
        self.accept = accept
        self.seen = [dict(s) for s in seen]
        self.forwarded: list[dict] = []
        self.households: list[tuple[str, dict]] = []

    def seen_frame(self, frame: dict) -> bool:
        return frame in self.seen

    def forward(self, **kwargs) -> bool:
        self.forwarded.append(kwargs)
        return self.accept

    def forward_to_household(self, instance_id: str, push: dict) -> bool:
        self.households.append((instance_id, push))
        return True


class _Cluster:
    def __init__(self) -> None:
        self.hints: list[str] = []

    def hint_drain(self, instance_id: str) -> None:
        self.hints.append(instance_id)


class _Relay:
    def __init__(self) -> None:
        self.queued: list[tuple[list[str], dict]] = []

    async def queue_relay_many(self, targets, frame) -> int:
        self.queued.append((list(targets), frame))
        return len(targets)


async def _cluster_world(gfs_setup, gfs_db, *, fan_out=None, seen: bool = True):
    """An author with one able follower household (offline) and the
    registry wired to fakes. *seen*: the follower held a session lately."""
    relay, cluster = _Relay(), _Cluster()
    mailbox = SqliteGfsMomentKeyMailboxRepo(gfs_db)
    reg = MomentPublicRegistry(
        gfs_setup["users"],
        gfs_setup["follows"],
        gfs_setup["ws"],
        SqliteGfsFederationRepo(gfs_db),
        mailbox,
        relay=relay,  # type: ignore[arg-type]
        fan_out=fan_out,
        cluster=cluster,  # type: ignore[arg-type]
    )
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    await reg.add_follow(
        follower_user_id="u-fan",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
        moment_suites=_SUITES,
    )
    if seen:
        await SqliteGfsFederationRepo(gfs_db).mark_relay_seen(
            "inst-follower", at=int(time.time())
        )
    return reg, relay, cluster, mailbox


async def test_on_one_node_a_moment_is_queued_for_a_seen_offline_follower(
    gfs_setup, gfs_db
):
    """No cluster to forward to: the recently-seen follower no socket here
    holds gets the moment queued at once, as its own typed frame."""
    fan_out = _FanOut(accept=False)
    reg, relay, _cluster, _mb = await _cluster_world(gfs_setup, gfs_db, fan_out=fan_out)
    assert await reg.fan_out_moment(envelope=dict(_ENVELOPE)) == 0
    assert relay.queued == [(["inst-follower"], _PUSH)]
    assert fan_out.forwarded == [
        {
            "kind": "followers",
            "key": "u-author",
            "frame": _FORWARDED,
            "queue_for": ["inst-follower"],
        }
    ]


async def test_a_forwarded_moment_leaves_the_offline_copy_to_the_settle(
    gfs_setup, gfs_db
):
    """Forwarded: the origin queues nothing itself — ``ClusterRelayFanOut``
    queues the candidates no peer holds once their answers are in."""
    fan_out = _FanOut()
    reg, relay, _cluster, _mb = await _cluster_world(gfs_setup, gfs_db, fan_out=fan_out)
    await reg.fan_out_moment(envelope=dict(_ENVELOPE))
    assert relay.queued == []
    assert [f["queue_for"] for f in fan_out.forwarded] == [["inst-follower"]]


async def test_a_follower_not_seen_lately_gets_no_offline_copy(gfs_setup, gfs_db):
    fan_out = _FanOut(accept=False)
    reg, relay, _cluster, _mb = await _cluster_world(
        gfs_setup, gfs_db, fan_out=fan_out, seen=False
    )
    await reg.fan_out_moment(envelope=dict(_ENVELOPE))
    assert relay.queued == []
    assert [f["queue_for"] for f in fan_out.forwarded] == [[]]


async def test_a_replayed_moment_is_fanned_out_once(gfs_setup, gfs_db):
    """The same bytes again — here, or after another node forwarded them —
    go nowhere."""
    fan_out = _FanOut()
    reg, _relay, _cluster, _mb = await _cluster_world(
        gfs_setup, gfs_db, fan_out=fan_out
    )
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]
    assert await reg.fan_out_moment(envelope=dict(_ENVELOPE)) == 1
    assert await reg.fan_out_moment(envelope=dict(_ENVELOPE)) == 0
    assert len(follower_ws.sent) == 1
    assert len(fan_out.forwarded) == 1
    other = _FanOut(
        seen=[{**_FORWARDED, "event_type": "incoming_public_moment_delete"}]
    )
    reg2, *_rest = await _cluster_world(gfs_setup, gfs_db, fan_out=other)
    assert await reg2.fan_out_delete(envelope=dict(_ENVELOPE)) == 0
    assert other.forwarded == []


async def test_a_key_for_a_household_elsewhere_wakes_the_node_holding_it(
    gfs_setup, gfs_db
):
    """No socket here: the key stays undelivered in the shared mailbox and
    the household rides the next drain hint."""
    reg, _relay, cluster, mailbox = await _cluster_world(gfs_setup, gfs_db)
    delivered = await reg.deliver_keys(
        sender_instance_id="inst-author",
        deliveries=[
            {"author_user_id": "u-author", "to": "inst-follower", "key_gen": 7}
        ],
    )
    assert delivered == 0
    assert cluster.hints == ["inst-follower"]
    mail = await mailbox.get("u-author", "inst-follower")
    assert mail is not None and not mail.delivered


async def test_a_hinted_mailbox_push_that_fails_never_hints_again(gfs_setup, gfs_db):
    """The drain hint itself runs ``on_follower_connected``: a push that
    finds no socket must not hint once more (two nodes would ping-pong)."""
    reg, _relay, cluster, mailbox = await _cluster_world(gfs_setup, gfs_db)
    await mailbox.put(
        GfsMomentKeyMail(
            author_user_id="u-author",
            follower_instance_id="inst-follower",
            key_gen=7,
            key_step=0,
            delivery="{}",
        )
    )
    assert await reg.on_follower_connected("inst-follower") == 0
    assert cluster.hints == []


async def test_a_key_request_answered_elsewhere_hands_the_kept_key_back(
    gfs_setup, gfs_db
):
    """The mailbox holds the key, already delivered once, and the follower's
    socket is on another node: the row is marked undelivered for that node
    to push, and the author is not asked."""
    fan_out = _FanOut()
    reg, _relay, cluster, mailbox = await _cluster_world(
        gfs_setup, gfs_db, fan_out=fan_out
    )
    await mailbox.put(
        GfsMomentKeyMail(
            author_user_id="u-author",
            follower_instance_id="inst-follower",
            key_gen=7,
            key_step=0,
            delivery="{}",
            delivered=True,
        )
    )
    await reg.request_key(
        follower_instance_id="inst-follower", followed_user_id="u-author", want_gen=7
    )
    assert cluster.hints == ["inst-follower"]
    mail = await mailbox.get("u-author", "inst-follower")
    assert mail is not None and not mail.delivered
    # Only the follow notice from ``_cluster_world`` — no key request.
    assert [f["type"] for _iid, f in fan_out.households] == ["follow_changed"]


async def test_author_frames_go_to_the_node_holding_the_authors_socket(
    gfs_setup, gfs_db
):
    """``follow_changed`` (here: the add in ``_cluster_world``) and
    ``moment_key_request`` find no author socket here, so each is forwarded
    once; with the author connected here, nothing is forwarded."""
    fan_out = _FanOut()
    reg, _relay, _cluster, _mb = await _cluster_world(
        gfs_setup, gfs_db, fan_out=fan_out
    )
    await reg.request_key(
        follower_instance_id="inst-follower", followed_user_id="u-author", want_gen=9
    )
    assert [(iid, f["type"]) for iid, f in fan_out.households] == [
        ("inst-author", "follow_changed"),
        ("inst-author", "moment_key_request"),
    ]
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    await reg.remove_follow(follower_user_id="u-fan", followed_user_id="u-author")
    assert len(fan_out.households) == 2
    assert _sent_field(author_ws, "type") == ["follow_changed"]
