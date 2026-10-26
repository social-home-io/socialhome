"""Tests for :class:`MomentPublicRegistry` (GFS broker)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import orjson
import pytest

from socialhome.global_server import federation as federation_mod
from socialhome.global_server import moment_public_registry as registry_mod
from socialhome.global_server.domain import GfsMomentKeyMail
from socialhome.global_server.moment_public_registry import (
    MOMENT_PUBLISHES_PER_MINUTE,
    MOMENT_SEEN_MAX,
    MOMENT_SEEN_TTL_S,
    MOMENT_STEP_SLACK_S,
    MomentPublicRegistry,
    envelope_step_current,
    suites_can_open,
)
from socialhome.global_server.repositories import (
    SqliteGfsFederationRepo,
    SqliteGfsMomentFollowRepo,
    SqliteGfsMomentKeyMailboxRepo,
    SqliteGfsUserRegistrationRepo,
)
from socialhome.global_server.ws_registry import GfsWebSocketRegistry
from socialhome.moment_public_seal import RATCHET_STEP_S


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
    def __init__(self, nodes: int = 1) -> None:
        self.hints: list[str] = []
        self.nodes = nodes

    def live_node_count(self) -> int:
        return self.nodes

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


# ─── Replay window (#1111 follow-up) ─────────────────────────────────────

_DAY = RATCHET_STEP_S
_SLACK = MOMENT_STEP_SLACK_S


@pytest.mark.parametrize(
    ("key_step", "now", "current"),
    [
        (0, 1_000, True),  # sealed today
        (0, 1_000 + _DAY + _SLACK, True),  # the step's end + the slack
        (0, 1_000 + _DAY + _SLACK + 1, False),  # a day-old step: a re-post
        (3, 1_000 + 3 * _DAY - _SLACK, True),  # an author clock a bit ahead
        (3, 1_000 + 3 * _DAY - _SLACK - 1, False),  # a step not begun yet
        (1, 1_000 + 10 * _DAY, False),
    ],
)
def test_an_envelope_counts_only_on_its_key_steps_day(key_step, now, current):
    assert envelope_step_current(1_000, key_step, now) is current


def test_a_moment_is_remembered_as_long_as_its_step_can_be_current():
    assert MOMENT_SEEN_TTL_S == _DAY + _SLACK


@pytest.mark.security
async def test_a_replay_hours_later_is_still_fanned_out_once(
    gfs_setup, gfs_db, monkeypatch
):
    """Before, the GFS forgot a moment after ten minutes and fanned a
    re-posted capture out to every follower again (each then dropped it)."""
    reg, _relay, _cluster, _mb = await _cluster_world(gfs_setup, gfs_db)
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]
    assert await reg.fan_out_moment(envelope=dict(_ENVELOPE)) == 1
    later = time.monotonic() + 6 * 3600
    monkeypatch.setattr(
        federation_mod,
        "time",
        SimpleNamespace(monotonic=lambda: later, time=time.time),
    )
    assert await reg.fan_out_moment(envelope=dict(_ENVELOPE)) == 0
    assert len(follower_ws.sent) == 1


# ─── Per-household publish budget ─────────────────────────────────────────


@pytest.mark.security
async def test_one_household_publishes_at_most_its_budget_a_minute(gfs_setup, gfs_db):
    reg, *_rest = await _cluster_world(gfs_setup, gfs_db)
    for _ in range(MOMENT_PUBLISHES_PER_MINUTE):
        assert reg.allow_publish("inst-author")
    assert not reg.allow_publish("inst-author")
    # Another household keeps its own budget.
    assert reg.allow_publish("inst-other")


@pytest.mark.security
async def test_the_publish_budget_is_cluster_wide(gfs_setup, gfs_db):
    """Behind a round-robin balancer each of N nodes allows 1/N of it."""
    reg, _relay, cluster, _mb = await _cluster_world(gfs_setup, gfs_db)
    cluster.nodes = 3
    share = -(-MOMENT_PUBLISHES_PER_MINUTE // 3)
    for _ in range(share):
        assert reg.allow_publish("inst-author")
    assert not reg.allow_publish("inst-author")


def test_the_publish_budget_leaves_the_seen_cache_to_everyone_else():
    """A full day of one household's budget fills under a quarter of the
    25 h seen cache — it can't push other authors' moments out of it."""
    per_window = MOMENT_PUBLISHES_PER_MINUTE * MOMENT_SEEN_TTL_S / 60
    assert per_window < MOMENT_SEEN_MAX / 4


# ─── The mailbox never moves a key back ───────────────────────────────────


@pytest.mark.security
async def test_an_older_key_after_a_newer_one_is_neither_kept_nor_pushed(
    gfs_setup, gfs_db
):
    """A re-posted capture of an older signed ``/gfs/moments/keys`` body
    (e.g. by a TLS-terminating proxy in front of the GFS) arrives after the
    newer key: the mailbox keeps the newer one and the follower is not sent
    the older one."""
    reg, _relay, _cluster, mailbox = await _cluster_world(gfs_setup, gfs_db)
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]

    def _delivery(gen: int, step: int) -> dict:
        return {
            "author_user_id": "u-author",
            "to": "inst-follower",
            "key_gen": gen,
            "key_step": step,
        }

    async def _deliver(gen: int, step: int) -> int:
        return await reg.deliver_keys(
            sender_instance_id="inst-author", deliveries=[_delivery(gen, step)]
        )

    assert await _deliver(8, 2) == 1
    assert await _deliver(7, 9) == 0  # an older generation
    assert await _deliver(8, 1) == 0  # the same generation, an older step
    kept = await mailbox.get("u-author", "inst-follower")
    assert kept is not None and (kept.key_gen, kept.key_step) == (8, 2)
    assert [p["key_gen"] for p in _sent_field(follower_ws, "payload")] == [8]
    # The same key again (the author re-sealing on a re-follow) still goes,
    # and a newer one replaces it.
    assert await _deliver(8, 2) == 1
    assert await _deliver(8, 3) == 1
    kept = await mailbox.get("u-author", "inst-follower")
    assert kept is not None and (kept.key_gen, kept.key_step) == (8, 3)


# ── Reactions (v_68) ──────────────────────────────────────────────────────

_SEALED = {"kem_suite": "x25519", "eph_pk": "e", "ciphertext": "n:c"}
_REACTION = {
    "type": "moment_reaction",
    "from_instance": "inst-follower",
    "sealed": _SEALED,
}


async def test_a_reaction_goes_to_the_authors_socket_here(gfs_setup, gfs_db):
    reg, relay, _cluster, _mb = await _cluster_world(gfs_setup, gfs_db)
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    outcome = await reg.relay_reaction(
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
        sealed=dict(_SEALED),
    )
    assert outcome == "accepted"
    assert [orjson.loads(s) for s in author_ws.sent] == [_REACTION]
    assert relay.queued == []


async def test_on_one_node_a_reaction_is_queued_for_an_offline_author(
    gfs_setup, gfs_db
):
    reg, relay, _cluster, _mb = await _cluster_world(gfs_setup, gfs_db)
    await reg.relay_reaction(
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
        sealed=dict(_SEALED),
    )
    assert relay.queued == [(["inst-author"], _REACTION)]


async def test_a_forward_refused_queues_the_reaction_here(gfs_setup, gfs_db):
    fan_out = _FanOut(accept=False)
    reg, relay, _cluster, _mb = await _cluster_world(gfs_setup, gfs_db, fan_out=fan_out)
    await reg.relay_reaction(
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
        sealed=dict(_SEALED),
    )
    assert fan_out.forwarded[0]["kind"] == "instance"
    assert relay.queued == [(["inst-author"], _REACTION)]


async def test_a_reaction_forwarded_to_another_node_charges_no_queue_budget(
    gfs_setup, gfs_db
):
    """#1179 F2: the registry keeps no queued-reaction budget of its own —
    every reaction that found no socket here names the author's household
    as the offline candidate, and only a row actually queued counts (the
    queue's own reaction cap, ``RELAY_QUEUE_MAX_REACTIONS_PER_RECIPIENT``)."""
    fan_out = _FanOut()
    reg, relay, _cluster, _mb = await _cluster_world(gfs_setup, gfs_db, fan_out=fan_out)
    for n in range(25):
        await reg.relay_reaction(
            follower_instance_id="inst-follower",
            followed_user_id="u-author",
            sealed={**_SEALED, "ciphertext": f"n:{n}"},
        )
    assert [f["queue_for"] for f in fan_out.forwarded] == [["inst-author"]] * 25
    assert relay.queued == []


async def test_the_reaction_budgets_are_shared_across_the_cluster(
    gfs_setup, gfs_db, monkeypatch
):
    """Each node allows its share of the per-(household, author) limit."""
    monkeypatch.setattr(registry_mod, "REACTIONS_PER_AUTHOR_PER_MINUTE", 4)
    reg, _relay, cluster, _mb = await _cluster_world(gfs_setup, gfs_db)
    cluster.nodes = 2
    outcomes = [
        await reg.relay_reaction(
            follower_instance_id="inst-follower",
            followed_user_id="u-author",
            sealed={**_SEALED, "ciphertext": f"n:{n}"},
        )
        for n in range(3)
    ]
    assert outcomes == ["accepted", "accepted", "rate_limited"]


async def test_a_suspended_author_takes_no_reactions(gfs_setup, gfs_db):
    reg, relay, _cluster, _mb = await _cluster_world(gfs_setup, gfs_db)
    await gfs_db.enqueue(
        "UPDATE gfs_user_registrations SET status='suspended' WHERE user_id='u-author'"
    )
    outcome = await reg.relay_reaction(
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
        sealed=dict(_SEALED),
    )
    assert outcome == "not_following"
    assert relay.queued == []
