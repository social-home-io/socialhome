"""Unit tests for :mod:`socialhome.services.moment_reaction_router` (v_67).

In-memory stubs only; the end-to-end rules against the real registry and
SQLite are in ``tests/protocol/test_moment_reaction_routed.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_routing_secret,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.events import MomentReactionChanged
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    RemoteInstance,
)
from socialhome.domain.moment import (
    Moment,
    MomentGfsReactionTarget,
    MomentReactionRoute,
)
from socialhome.federation.moment_reaction_route import (
    build_reaction,
    open_reaction,
    origin_keywrap_fields,
    seal_reaction,
)
from socialhome.services import moment_reaction_router as mrr
from socialhome.services.moment_reaction_router import MomentReactionRouter

FET = FederationEventType
OWN_KEY = generate_identity_keypair()
OWN = derive_instance_id(OWN_KEY.public_key)
OWN_KW = generate_x25519_keypair()
ORIGIN_KW = generate_x25519_keypair()
FUTURE = (datetime.now(UTC) + timedelta(days=3)).isoformat()


def _peer(pid: str, *, version: int = 67, **kw) -> RemoteInstance:
    return RemoteInstance(
        id=pid,
        display_name=pid,
        remote_identity_pk="00" * 32,
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x",
        local_inbox_id="w",
        proto_version=version,
        **kw,
    )


def _moment(mid: str, **over) -> Moment:
    base = {
        "id": mid,
        "author_user_id": "u-far",
        "content": "x",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "origin_instance_id": "origin",
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": FUTURE,
        "hop_count": 2,
        "received_via": "household",
    }
    base.update(over)
    return Moment(**base)


class _Registry:
    def __init__(self):
        self.handlers = {}

    def register(self, event_type, handler):
        self.handlers[event_type] = handler


class _Fed:
    def __init__(self, repo):
        self.own_instance_id = OWN
        self.own_identity_seed = OWN_KEY.private_key
        self.own_identity_pk = OWN_KEY.public_key
        self._repo = repo
        self._event_registry = _Registry()
        self.sent: list[dict] = []
        self.pinned: dict[str, bytes] = {}

    async def peer_supports(self, instance_id, *, min_version):
        peer = await self._repo.get_instance(instance_id)
        return peer is not None and peer.proto_version >= min_version

    async def peer_identity_public_key(self, instance_id):
        return self.pinned.get(instance_id)

    async def send_event(self, **kwargs):
        self.sent.append(kwargs)


class _FedRepo:
    def __init__(self):
        self.peers: dict[str, RemoteInstance] = {}

    async def get_instance(self, instance_id):
        return self.peers.get(instance_id)


class _Moments:
    def __init__(self):
        self.rows: dict[str, Moment] = {}
        self.routes: dict[str, MomentReactionRoute] = {}
        self.applied: list[tuple] = []

    async def get(self, moment_id):
        return self.rows.get(moment_id)

    async def get_reaction_routes(self, ids):
        return {i: self.routes[i] for i in ids if i in self.routes}

    async def apply_reaction_at(self, moment_id, reactor, emoji, *, reacted_at):
        self.applied.append((moment_id, reactor, emoji, reacted_at))
        return True


class _Users:
    def __init__(self):
        self.homes: dict[str, str] = {}
        self.local: dict[str, SimpleNamespace] = {}

    async def get_instance_for_user(self, user_id):
        return self.homes.get(user_id)

    async def get_by_user_id(self, user_id):
        return self.local.get(user_id)

    async def is_blocked(self, a, b):
        return False


class _Bans:
    async def is_banned(self, instance_id):
        return False


class _Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


@pytest.fixture
def router():
    repo = _FedRepo()
    fed = _Fed(repo)
    moments = _Moments()
    users = _Users()
    bus = _Bus()
    r = MomentReactionRouter(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
        moment_repo=moments,
        user_repo=users,
        ban_repo=_Bans(),
        debounce_s=0.01,
    )
    r.attach_identity(
        keywrap_private_key=OWN_KW.private_key,
        keywrap_public_key=OWN_KW.public_key,
        keywrap_sig=b64url_encode(sign_ed25519(OWN_KEY.private_key, OWN_KW.public_key)),
        routing_secret=generate_routing_secret(),
    )
    return SimpleNamespace(
        r=r, fed=fed, repo=repo, moments=moments, users=users, bus=bus
    )


def _local_user(env, name="anna"):
    uid = derive_user_id(OWN_KEY.public_key, f"{name}-anchor")
    env.users.local[uid] = SimpleNamespace(
        user_id=uid, username=name, identity_anchor=f"{name}-anchor"
    )
    env.users.homes[uid] = OWN
    return uid


def _route(via="peer-via", tokens=("tok",)) -> MomentReactionRoute:
    return MomentReactionRoute(
        keywrap_pk=ORIGIN_KW.public_key.hex(),
        via=via,
        tokens=tuple(tokens),
        token_suite="aesgcm-256",
    )


# ── Wiring ────────────────────────────────────────────────────────────────


async def test_nothing_routes_before_the_identity_is_attached():
    r = MomentReactionRouter(
        bus=_Bus(),
        federation_service=_Fed(_FedRepo()),
        federation_repo=_FedRepo(),
        moment_repo=_Moments(),
        user_repo=_Users(),
        ban_repo=_Bans(),
    )
    assert r.origin_keywrap_fields() == {}
    stamped = r.stamp_relayed_create(
        {"origin_keywrap_pk": "aa", "rt": ["x"], "rt_suite": "aesgcm-256"},
        received_hop=1,
        from_instance="a",
        to_instance="b",
    )
    assert "rt" not in stamped and "rt_suite" not in stamped
    assert not await r.queue_reaction(
        moment_id="m", reactor_user_id="u", emoji="👏", occurred_at=datetime.now(UTC)
    )


def test_attach_to_registers_the_routed_handler(router):
    router.r.attach_to(router.fed)
    assert FET.MOMENT_REACTION_ROUTED in router.fed._event_registry.handlers


def test_origin_keywrap_fields_are_the_keywrap_shape(router):
    fields = router.r.origin_keywrap_fields()
    assert fields["origin_keywrap_pk"] == OWN_KW.public_key.hex()
    assert fields["origin_keywrap_suite"] == "x25519"


# ── Relayed creates ───────────────────────────────────────────────────────


def _create(**over):
    base = {
        "moment_id": "m-1",
        "occurred_at": datetime.now(UTC).isoformat(),
        "expires_at": FUTURE,
        "origin_keywrap_pk": "aa",
    }
    base.update(over)
    return base


def test_a_relay_appends_one_token_per_peer(router):
    first = router.r.stamp_relayed_create(
        _create(), received_hop=1, from_instance="origin", to_instance="b"
    )
    second = router.r.stamp_relayed_create(
        _create(), received_hop=1, from_instance="origin", to_instance="c"
    )
    assert len(first["rt"]) == 1 and first["rt_suite"] == "aesgcm-256"
    assert first["rt"] != second["rt"]
    onward = router.r.stamp_relayed_create(
        _create(rt=first["rt"], rt_suite="aesgcm-256"),
        received_hop=2,
        from_instance="b",
        to_instance="d",
    )
    assert onward["rt"][0] == first["rt"][0] and len(onward["rt"]) == 2


@pytest.mark.parametrize(
    ("payload", "hop"),
    [
        (_create(origin_keywrap_pk=None), 1),  # older origin
        (_create(), 2),  # an older relay added no token
        (_create(rt=["x"], rt_suite="chacha"), 2),  # unknown suite
        (_create(expires_at="never"), 1),  # unreadable expiry
    ],
)
def test_a_broken_chain_is_stripped(router, payload, hop):
    stamped = router.r.stamp_relayed_create(
        payload, received_hop=hop, from_instance="a", to_instance="b"
    )
    assert "rt" not in stamped and "rt_suite" not in stamped


async def test_route_from_create_needs_a_relayed_copy(router):
    payload = {"origin_identity_pk": "00", **router.r.origin_keywrap_fields()}
    assert (
        await router.r.route_from_create(
            payload, from_instance="o", origin_instance_id="o", hop_count=2
        )
        is None
    )
    assert (
        await router.r.route_from_create(
            payload, from_instance="b", origin_instance_id="o", hop_count=1
        )
        is None
    )


async def test_route_from_create_refuses_an_unknown_token_suite(router, caplog):
    origin = generate_identity_keypair()
    kw = generate_x25519_keypair()
    payload = {
        "origin_identity_pk": origin.public_key.hex(),
        **origin_keywrap_fields(
            kw.public_key,
            b64url_encode(sign_ed25519(origin.private_key, kw.public_key)),
        ),
        "rt": ["tok"],
        "rt_suite": "chacha",
    }
    oid = derive_instance_id(origin.public_key)
    with caplog.at_level("WARNING"):
        route = await router.r.route_from_create(
            payload, from_instance="b", origin_instance_id=oid, hop_count=2
        )
    assert route is None
    assert "chacha" in caplog.text
    payload["rt_suite"] = "aesgcm-256"
    route = await router.r.route_from_create(
        payload, from_instance="b", origin_instance_id=oid, hop_count=2
    )
    assert route == MomentReactionRoute(
        kw.public_key.hex(), "b", ("tok",), "aesgcm-256"
    )


# ── Reactor side ──────────────────────────────────────────────────────────


async def test_direct_target(router):
    router.users.homes["u-here"] = OWN
    router.users.homes["u-paired"] = "peer-p"
    router.users.homes["u-roster"] = "peer-gone"  # known, not paired
    router.repo.peers["peer-p"] = _peer("peer-p")
    assert await router.r.direct_target("u-here") == OWN
    assert await router.r.direct_target("u-paired") == "peer-p"
    assert await router.r.direct_target("u-roster") is None
    assert await router.r.direct_target("u-unknown") is None


async def test_direct_target_falls_back_to_a_paired_origin(router):
    # #1178 F1: no roster row for the author, but the moment's origin is a
    # confirmed peer — the direct path (with its hide list), never a route.
    router.moments.rows["m"] = _moment("m", origin_instance_id="peer-o")
    router.moments.rows["m-far"] = _moment("m-far", origin_instance_id="far")
    router.repo.peers["peer-o"] = _peer("peer-o")
    assert await router.r.direct_target("u-far", moment_id="m") == "peer-o"
    assert await router.r.direct_target("u-far", moment_id="m-far") is None
    assert await router.r.direct_target("u-far", moment_id="m-gone") is None
    assert await router.r.reach_for([router.moments.rows["m"]]) == {"m": "author"}


async def test_a_paired_origin_is_never_routed(router):
    anna = _local_user(router)
    router.repo.peers["peer-via"] = _peer("peer-via")
    router.repo.peers["origin"] = _peer("origin")
    router.moments.rows["m"] = _moment("m")
    router.moments.routes["m"] = _route()
    assert not await router.r.queue_reaction(
        moment_id="m", reactor_user_id=anna, emoji="👏", occurred_at=datetime.now(UTC)
    )
    assert router.fed.sent == []


async def test_reach_for(router):
    router.users.homes["u-paired"] = "peer-p"
    router.repo.peers["peer-p"] = _peer("peer-p")
    router.repo.peers["peer-via"] = _peer("peer-via")
    router.repo.peers["peer-old"] = _peer("peer-old", version=66)
    router.repo.peers["peer-space"] = _peer(
        "peer-space", source=InstanceSource.SPACE_SESSION
    )
    moments = [
        _moment("own", received_via="self"),
        _moment("paired", author_user_id="u-paired", hop_count=1),
        _moment("routed"),
        _moment("old"),
        _moment("space"),
        _moment("ended", expires_at="2000-01-01T00:00:00+00:00"),
        _moment("gfs", received_via="gfs"),
        _moment("bad-kw"),
    ]
    router.moments.routes.update(
        {
            "routed": _route(),
            "old": _route(via="peer-old"),
            "space": _route(via="peer-space"),
            "ended": _route(),
            "gfs": _route(),
            "bad-kw": MomentReactionRoute("zz", "peer-via", ("t",), "aesgcm-256"),
        }
    )
    assert await router.r.reach_for(moments) == {
        "own": "author",
        "paired": "author",
        "routed": "author",
        "old": "household",
        "space": "household",
        "ended": "household",
        "gfs": "household",
        "bad-kw": "household",
    }


async def test_reactions_are_coalesced_and_sealed_to_the_origin(router):
    anna = _local_user(router)
    router.repo.peers["peer-via"] = _peer("peer-via")
    router.moments.rows["m"] = _moment("m")
    router.moments.routes["m"] = _route()
    now = datetime.now(UTC)
    for emoji in ("👏", "🔥", None, "😂"):
        assert await router.r.queue_reaction(
            moment_id="m", reactor_user_id=anna, emoji=emoji, occurred_at=now
        )
    await asyncio.sleep(0.05)
    assert len(router.fed.sent) == 1  # the latest state only
    sent = router.fed.sent[0]
    assert sent["to_instance_id"] == "peer-via"
    assert sent["event_type"] is FET.MOMENT_REACTION_ROUTED
    assert sent["outbox_key"] == f"moment-reaction:m:{anna}"
    inner = open_reaction(
        sealed=sent["payload"]["sealed"], keywrap_priv=ORIGIN_KW.private_key
    )
    assert inner["emoji"] == "😂" and inner["reactor_user_id"] == anna


async def test_no_route_keeps_the_reaction_home(router):
    anna = _local_user(router)
    router.moments.rows["m"] = _moment("m")
    assert not await router.r.queue_reaction(
        moment_id="m", reactor_user_id=anna, emoji="👏", occurred_at=datetime.now(UTC)
    )
    router.moments.routes["m"] = _route(via="peer-old")
    router.repo.peers["peer-old"] = _peer("peer-old", version=66)
    assert not await router.r.queue_reaction(
        moment_id="m", reactor_user_id=anna, emoji="👏", occurred_at=datetime.now(UTC)
    )
    assert router.fed.sent == []


async def test_a_user_id_not_derived_from_the_household_key_is_not_routed(router):
    router.users.local["u-legacy"] = SimpleNamespace(
        user_id="u-legacy", username="legacy", identity_anchor=None
    )
    router.repo.peers["peer-via"] = _peer("peer-via")
    router.moments.rows["m"] = _moment("m")
    router.moments.routes["m"] = _route()
    await router.r.queue_reaction(
        moment_id="m",
        reactor_user_id="u-legacy",
        emoji="👏",
        occurred_at=datetime.now(UTC),
    )
    await asyncio.sleep(0.05)
    assert router.fed.sent == []


async def test_stop_sends_what_waits_for_its_debounce(router):
    router.r._debounce_s = 60
    anna = _local_user(router)
    router.repo.peers["peer-via"] = _peer("peer-via")
    router.moments.rows["m"] = _moment("m")
    router.moments.routes["m"] = _route()
    await router.r.queue_reaction(
        moment_id="m", reactor_user_id=anna, emoji="👏", occurred_at=datetime.now(UTC)
    )
    assert router.fed.sent == []
    await router.r.stop()
    assert len(router.fed.sent) == 1


# ── Origin side ───────────────────────────────────────────────────────────


def _routed_event(router, emoji="👏", at_ms=None, from_instance="peer-n"):
    reactor = generate_identity_keypair()
    inner = build_reaction(
        seed=reactor.private_key,
        identity_pk=reactor.public_key,
        reactor_instance_id=derive_instance_id(reactor.public_key),
        reactor_user_id=derive_user_id(reactor.public_key, "r"),
        reactor_anchor="r",
        moment_id="m-own",
        author_user_id="u-anna",
        origin_instance_id=OWN,
        emoji=emoji,
        occurred_at_ms=at_ms or int(datetime.now(UTC).timestamp() * 1000),
    )
    return FederationEvent(
        msg_id="x",
        event_type=FET.MOMENT_REACTION_ROUTED,
        from_instance=from_instance,
        to_instance=OWN,
        timestamp=datetime.now(UTC).isoformat(),
        payload={
            "moment_id": "m-own",
            "origin_instance_id": OWN,
            "rt": [],
            "sealed": seal_reaction(origin_keywrap_pub=OWN_KW.public_key, inner=inner),
        },
    )


async def test_origin_applies_and_announces(router):
    router.users.local["u-anna"] = SimpleNamespace(user_id="u-anna")
    router.moments.rows["m-own"] = _moment(
        "m-own", author_user_id="u-anna", origin_instance_id=OWN, received_via="self"
    )
    await router.r._on_routed(_routed_event(router))
    assert len(router.moments.applied) == 1
    assert isinstance(router.bus.events[0], MomentReactionChanged)
    assert router.bus.events[0].author_user_id == "u-anna"


async def test_malformed_routed_payloads_are_refused(router, caplog):
    event = _routed_event(router)
    for bad in (
        {**event.payload, "moment_id": ""},
        {**event.payload, "origin_instance_id": None},
        {**event.payload, "sealed": "x"},
        {**event.payload, "rt": "x"},
    ):
        with caplog.at_level("WARNING"):
            await router.r._on_routed(dataclasses.replace(event, payload=bad))
    assert router.moments.applied == []
    assert caplog.text.count("refusing") == 4


async def test_a_relay_without_its_token_refuses(router):
    event = _routed_event(router)
    payload = {**event.payload, "origin_instance_id": "elsewhere"}
    await router.r._on_routed(
        FederationEvent(
            msg_id="x",
            event_type=FET.MOMENT_REACTION_ROUTED,
            from_instance="peer-n",
            to_instance=OWN,
            timestamp=event.timestamp,
            payload=payload,
        )
    )
    assert router.fed.sent == []


def test_seen_cache_is_bounded_and_expires(router, monkeypatch):
    monkeypatch.setattr(mrr, "SEEN_MAX", 3)
    now = 10**12
    for i in range(5):
        assert router.r._remember_seen(bytes([i]), now)
    assert len(router.r._seen) <= 3
    assert not router.r._remember_seen(bytes([4]), now)
    # Past the window everything is forgotten.
    assert router.r._remember_seen(bytes([4]), now + mrr.MAX_REACTION_AGE_MS + 1)


# ── v_68: through the GFS a public moment came on ────────────────────────


class _Gfs:
    """``MomentReactionGfs`` stand-in."""

    def __init__(self, ok: bool = True, suite: str = "x25519") -> None:
        self.ok = ok
        self.suite = suite
        self.sent: list[dict] = []

    async def target(self, moment):
        if not self.ok or moment.received_via != "gfs":
            return None
        return MomentGfsReactionTarget(
            gfs_id=moment.received_via_gfs_id,
            keywrap_pk=ORIGIN_KW.public_key.hex(),
            keywrap_suite=self.suite,
        )

    async def send(self, target, **kwargs):
        self.sent.append({"target": target, **kwargs})


def _gfs_moment(mid: str = "m-g", **over) -> Moment:
    return _moment(
        mid,
        received_via="gfs",
        received_via_gfs_id="g1",
        is_public=True,
        hop_count=1,
        **over,
    )


async def test_reach_for_a_gfs_moment(router):
    gfs = _Gfs()
    router.r.attach_gfs(gfs)
    moments = [
        _gfs_moment("gfs-ok"),
        _gfs_moment("gfs-ended", expires_at="2000-01-01T00:00:00+00:00"),
    ]
    assert await router.r.reach_for(moments) == {
        "gfs-ok": "author",
        "gfs-ended": "household",
    }
    gfs.ok = False
    assert await router.r.reach_for([_gfs_moment("gfs-no")]) == {"gfs-no": "household"}


async def test_a_gfs_moment_reaction_is_sealed_and_posted_through_its_gfs(router):
    anna = _local_user(router)
    gfs = _Gfs()
    router.r.attach_gfs(gfs)
    router.moments.rows["m-g"] = _gfs_moment()
    now = datetime.now(UTC)
    for emoji in ("👏", "🔥"):
        assert await router.r.queue_reaction(
            moment_id="m-g", reactor_user_id=anna, emoji=emoji, occurred_at=now
        )
    await asyncio.sleep(0.05)
    [sent] = gfs.sent
    assert sent["target"].gfs_id == "g1"
    assert sent["author_user_id"] == "u-far"
    assert sent["retry_key"] == f"m-g|{anna}"
    assert router.fed.sent == []  # nothing on the mesh
    inner = open_reaction(sealed=sent["sealed"], keywrap_priv=ORIGIN_KW.private_key)
    assert (inner["emoji"], inner["origin_instance_id"]) == ("🔥", "origin")


async def test_a_gfs_target_under_an_unknown_suite_is_never_sealed_to(router, caplog):
    """#1179: the author's key is sealed to only under a suite this build
    supports — an unknown one is never read as X25519; nothing is sent."""
    anna = _local_user(router)
    gfs = _Gfs(suite="x25519+mlkem768")
    router.r.attach_gfs(gfs)
    router.moments.rows["m-g"] = _gfs_moment()
    await router.r.queue_reaction(
        moment_id="m-g", reactor_user_id=anna, emoji="👏", occurred_at=datetime.now(UTC)
    )
    await asyncio.sleep(0.05)
    assert gfs.sent == []
    assert "unsupported key-wrap suite" in caplog.text


async def test_a_gfs_moment_stays_home_without_the_transport_or_a_target(router):
    anna = _local_user(router)
    router.moments.rows["m-g"] = _gfs_moment()
    now = datetime.now(UTC)
    assert not await router.r.queue_reaction(
        moment_id="m-g", reactor_user_id=anna, emoji="👏", occurred_at=now
    )
    router.r.attach_gfs(_Gfs(ok=False))
    assert not await router.r.queue_reaction(
        moment_id="m-g", reactor_user_id=anna, emoji="👏", occurred_at=now
    )
    router.moments.rows["m-x"] = _moment("m-x", received_via="self")
    assert not await router.r.queue_reaction(
        moment_id="m-x", reactor_user_id=anna, emoji="👏", occurred_at=now
    )


async def test_a_gfs_moment_of_a_paired_origin_goes_direct(router):
    """#1178 F1 holds for GFS moments too: a paired origin is reached
    directly under the hide list, never through a GFS."""
    anna = _local_user(router)
    gfs = _Gfs()
    router.r.attach_gfs(gfs)
    router.repo.peers["origin"] = _peer("origin")
    router.moments.rows["m-g"] = _gfs_moment()
    assert not await router.r.queue_reaction(
        moment_id="m-g",
        reactor_user_id=anna,
        emoji="👏",
        occurred_at=datetime.now(UTC),
    )
    assert await router.r.direct_target("u-far", moment_id="m-g") == "origin"


def _gfs_frame(sender=None, *, reactor=None, emoji="👏") -> dict:
    reactor = reactor or generate_identity_keypair()
    inner = build_reaction(
        seed=reactor.private_key,
        identity_pk=reactor.public_key,
        reactor_instance_id=derive_instance_id(reactor.public_key),
        reactor_user_id=derive_user_id(reactor.public_key, "r"),
        reactor_anchor="r",
        moment_id="m-own",
        author_user_id="u-anna",
        origin_instance_id=OWN,
        emoji=emoji,
        occurred_at_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    return {
        "type": "moment_reaction",
        "from_instance": sender or derive_instance_id(reactor.public_key),
        "sealed": seal_reaction(origin_keywrap_pub=OWN_KW.public_key, inner=inner),
    }


def _own_public_moment(router, *, public: bool = True) -> None:
    router.users.local["u-anna"] = SimpleNamespace(user_id="u-anna")
    router.moments.rows["m-own"] = _moment(
        "m-own",
        author_user_id="u-anna",
        origin_instance_id=OWN,
        received_via="self",
        is_public=public,
    )


async def test_the_author_applies_a_gfs_frame(router):
    _own_public_moment(router)
    await router.r.on_gfs_reaction(_gfs_frame(), gfs_id="g1")
    assert len(router.moments.applied) == 1
    assert isinstance(router.bus.events[0], MomentReactionChanged)


async def test_the_author_refuses_a_bad_gfs_frame(router, caplog):
    _own_public_moment(router)
    with caplog.at_level("WARNING"):
        await router.r.on_gfs_reaction(_gfs_frame(sender="inst-other"), gfs_id="g1")
        await router.r.on_gfs_reaction({"type": "moment_reaction"}, gfs_id="g1")
        bare = _gfs_frame()
        bare["sealed"] = {"kem_suite": "x25519"}
        await router.r.on_gfs_reaction(bare, gfs_id="g1")
    assert router.moments.applied == []
    assert caplog.text.count("refusing") == 3


async def test_the_author_refuses_a_gfs_frame_for_a_private_moment(router, caplog):
    _own_public_moment(router, public=False)
    with caplog.at_level("WARNING"):
        await router.r.on_gfs_reaction(_gfs_frame(), gfs_id="g1")
    assert router.moments.applied == []
    assert "moment is not public" in caplog.text


async def test_the_author_refuses_gfs_frames_before_attaching_its_key():
    r = MomentReactionRouter(
        bus=_Bus(),
        federation_service=_Fed(_FedRepo()),
        federation_repo=_FedRepo(),
        moment_repo=_Moments(),
        user_repo=_Users(),
        ban_repo=_Bans(),
    )
    await r.on_gfs_reaction(_gfs_frame(), gfs_id="g1")


async def test_a_gfs_over_its_rate_is_refused_before_any_crypto(
    router, monkeypatch, caplog
):
    _own_public_moment(router)
    monkeypatch.setattr(mrr, "ORIGIN_PER_GFS_PER_MIN", 1)
    with caplog.at_level("WARNING"):
        await router.r.on_gfs_reaction(_gfs_frame(), gfs_id="g1")
        await router.r.on_gfs_reaction(_gfs_frame(), gfs_id="g1")
    assert len(router.moments.applied) == 1
    assert "GFS over its reaction rate" in caplog.text
