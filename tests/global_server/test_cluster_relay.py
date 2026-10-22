"""Unit tests for :mod:`socialhome.global_server.cluster_relay` — the
cluster-wide fan-out of broadcast relay items.

The real multi-node path (three nodes on one DB, real HTTP + WebSockets)
lives in ``test_cluster_fanout.py``; these pin the wire shape, the dedupe,
the ``held`` bookkeeping and every fail-soft branch with in-memory fakes.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from socialhome.global_server import cluster_relay as mod
from socialhome.global_server.cluster_relay import (
    AUDIENCE_CHANNEL,
    AUDIENCE_FOLLOWERS,
    AUDIENCE_INSTANCE,
    AUDIENCE_SPACE,
    ClusterRelayFanOut,
    ForwardedRelay,
    held_from_replies,
    wire_size,
)
from socialhome.global_server.federation import SeenPayloadCache

_A = "homea2222222222222222222222222aa"
_B = "homeb2222222222222222222222222bb"
_C = "homec2222222222222222222222222cc"
_FRAME = {"space_id": "sp", "event_type": "space_post_public", "payload": {"x": 1}}


class _Registry:
    """``GfsWebSocketRegistry`` stand-in: who is connected, what was sent."""

    def __init__(self, online=(), *, failing=()) -> None:
        self.online = set(online)
        self.failing = set(failing)
        self.sent: list[tuple[str, dict]] = []

    def is_connected(self, instance_id: str) -> bool:
        return instance_id in self.online

    def connected_instances(self) -> set[str]:
        return set(self.online)

    async def send(self, instance_id: str, frame: dict) -> bool:
        if instance_id in self.failing or instance_id not in self.online:
            return False
        self.sent.append((instance_id, frame))
        return True


class _Relay:
    def __init__(self, *, fail: bool = False) -> None:
        self.queued: list[tuple[list[str], dict]] = []
        self.fail = fail

    async def queue_relay_many(self, targets, frame) -> int:
        if self.fail:
            raise RuntimeError("db down")
        self.queued.append((list(targets), frame))
        return len(targets)


class _Cluster:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.items: list[tuple[ForwardedRelay, object]] = []

    def forward_relay(self, item, *, on_settled=None) -> bool:
        self.items.append((item, on_settled))
        return self.accept


def _fan_out(registry=None, relay=None, cluster=None) -> ClusterRelayFanOut:
    svc = ClusterRelayFanOut(
        ws_registry=registry or _Registry(), relay=relay or _Relay()
    )
    if cluster is not None:
        svc.attach_cluster(cluster)
    return svc


async def _settle(svc: ClusterRelayFanOut) -> None:
    await svc.close(timeout=2)


def _item(frame=None, *, kind=AUDIENCE_SPACE, key="sp", skip="", queue=False):
    frame = frame or _FRAME
    return ForwardedRelay(
        kind=kind,
        key=key,
        skip=skip,
        queue=queue,
        frame=frame,
    )


# ─── Wire shape ──────────────────────────────────────────────────────────


def test_the_dedupe_key_is_a_stable_digest_of_the_frame():
    reordered = dict(reversed(_FRAME.items()))
    assert _item().digest() == _item(reordered).digest()
    assert len(_item().digest()) == 32
    assert _item().digest() != _item({**_FRAME, "payload": {"x": 2}}).digest()


def test_no_dedupe_key_is_taken_off_the_wire():
    """Each node digests the frame itself: an id on the wire could name
    other bytes and mute a legitimate item."""
    assert "id" not in _item().to_wire()


def test_wire_size_counts_the_canonical_encoding():
    assert wire_size({"b": 1, "a": "é"}) == len('{"a":"\\u00e9","b":1}')


def test_an_item_round_trips_over_the_wire():
    item = _item(kind=AUDIENCE_CHANNEL, key="ch1", skip=_A, queue=True)
    assert item.audience == "channel:ch1"
    assert ForwardedRelay.from_wire(item.to_wire()) == item


def test_an_item_without_the_optional_fields_reads_their_defaults():
    wire = _item().to_wire()
    del wire["skip"], wire["q"]
    assert ForwardedRelay.from_wire(wire) == _item()


@pytest.mark.parametrize(
    "change",
    [
        {"aud": ["space"]},
        {"aud": ["planet", "sp"]},
        {"aud": "space:sp"},
        {"aud": ["space", ""]},
        {"aud": ["space", "x" * 129]},
        {"aud": ["space", "line\nbreak"]},
        {"skip": "not-an-instance-id"},
        {"skip": None},
        {"q": "yes"},
        {"frame": []},
        {"frame": {"space_id": "sp"}},
        {"frame": {**_FRAME, "type": "envelope"}},
    ],
)
def test_a_malformed_item_is_refused(change):
    assert ForwardedRelay.from_wire({**_item().to_wire(), **change}) is None


def test_a_non_dict_item_is_refused():
    assert ForwardedRelay.from_wire(["aud"]) is None


def test_held_is_the_union_of_the_answering_peers():
    replies = [
        None,
        {"status": "ok"},  # an older node: no ``held`` at all
        {"status": "ok", "held": {"space:sp": [_A, "junk", 7], "space:x": [_C]}},
        {"status": "ok", "held": {"space:sp": [_B], "channel:c": "nope"}},
        {"status": "ok", "held": {"space:sp": "not a list"}},
        {"status": "ok", "held": []},
    ]
    assert held_from_replies(replies, "space:sp") == frozenset({_A, _B})


# ─── Origin side ─────────────────────────────────────────────────────────


def test_an_unknown_audience_kind_cannot_be_registered():
    with pytest.raises(ValueError):
        _fan_out().register_audience("planet", lambda key: None)


def test_forward_without_a_cluster_returns_false():
    svc = _fan_out()
    assert svc.forward(kind=AUDIENCE_SPACE, key="sp", frame=_FRAME) is False
    assert not svc.seen_frame(_FRAME)


def test_a_refused_forward_is_not_remembered():
    cluster = _Cluster(accept=False)
    svc = _fan_out(cluster=cluster)
    assert svc.forward_to_space("sp", _FRAME) is False
    assert not svc.seen_frame(_FRAME)


def test_a_broadcast_forward_asks_for_nothing_back():
    cluster = _Cluster()
    svc = _fan_out(cluster=cluster)
    assert svc.forward_to_space("sp", _FRAME) is True
    ((item, on_settled),) = cluster.items
    assert item == _item()
    assert on_settled is None
    assert svc.seen_frame(_FRAME)


async def test_a_member_forward_queues_only_what_no_peer_holds():
    cluster, relay = _Cluster(), _Relay()
    svc = _fan_out(relay=relay, cluster=cluster)
    assert svc.forward(
        kind=AUDIENCE_SPACE, key="sp", frame=_FRAME, skip=_C, queue_for=[_A, _B]
    )
    ((item, on_settled),) = cluster.items
    assert (item.queue, item.skip) == (True, _C)
    on_settled(frozenset({_B}))
    await _settle(svc)
    assert relay.queued == [([_A], _FRAME)]
    # Everyone held: nothing to queue.
    on_settled(frozenset({_A, _B}))
    await _settle(svc)
    assert len(relay.queued) == 1


async def test_forwards_holding_too_many_offline_candidates_are_refused(
    monkeypatch,
):
    """A member item whose candidates would push the not-yet-settled total
    past the cap is not forwarded: the caller queues them at once."""
    monkeypatch.setattr(mod, "RELAY_FORWARD_MAX_PENDING_TARGETS", 2)
    cluster = _Cluster()
    svc = _fan_out(cluster=cluster)
    assert svc.forward(kind=AUDIENCE_SPACE, key="sp", frame=_FRAME, queue_for=[_A])
    other = {**_FRAME, "payload": {"x": 5}}
    assert not svc.forward(
        kind=AUDIENCE_SPACE, key="sp", frame=other, queue_for=[_B, _C]
    )
    assert len(cluster.items) == 1
    cluster.items[0][1](frozenset({_A}))  # settled: room again
    assert svc.forward(kind=AUDIENCE_SPACE, key="sp", frame=other, queue_for=[_B, _C])
    await _settle(svc)


async def test_a_failed_offline_queue_is_logged(caplog):
    cluster = _Cluster()
    svc = _fan_out(relay=_Relay(fail=True), cluster=cluster)
    svc.forward(kind=AUDIENCE_SPACE, key="sp", frame=_FRAME, queue_for=[_A])
    with caplog.at_level(logging.WARNING):
        cluster.items[0][1](frozenset())
        await _settle(svc)
    assert "not queued" in caplog.text


# ─── Receiving side ──────────────────────────────────────────────────────


def _receiver(online=(), *, failing=(), audience=(_A, _B, _C)):
    registry, relay = _Registry(online, failing=failing), _Relay()
    svc = _fan_out(registry=registry, relay=relay)
    lookups: list[str] = []

    async def _resolve(key: str) -> list[str]:
        lookups.append(key)
        return list(audience)

    svc.register_audience(AUDIENCE_SPACE, _resolve)
    return svc, registry, relay, lookups


async def test_apply_pushes_to_the_local_sockets_only_and_reports_held():
    svc, registry, relay, lookups = _receiver(online={_A, _B})
    held = await svc.apply([_item(skip=_B, queue=True)])
    await _settle(svc)
    assert held == {"space:sp": [_A, _B]}  # the publisher is held too
    assert registry.sent == [(_A, {"type": "relay", **_FRAME})]
    assert relay.queued == []
    assert lookups == ["sp"]


async def test_apply_reports_nothing_held_for_a_broadcast():
    svc, registry, _relay, _l = _receiver(online={_A})
    assert await svc.apply([_item()]) == {}
    await _settle(svc)
    assert [t for t, _f in registry.sent] == [_A]


async def test_apply_resolves_each_audience_once_per_frame():
    svc, registry, _relay, lookups = _receiver(online={_A})
    other = {**_FRAME, "payload": {"x": 2}}
    await svc.apply([_item(), _item(other)])
    await _settle(svc)
    assert lookups == ["sp"]
    assert len(registry.sent) == 2


async def test_apply_never_pushes_one_item_twice_but_still_reports_held():
    svc, registry, _relay, _l = _receiver(online={_A})
    item = _item(queue=True)
    assert await svc.apply([item]) == {"space:sp": [_A]}
    assert await svc.apply([item]) == {"space:sp": [_A]}
    await _settle(svc)
    assert len(registry.sent) == 1
    assert svc.seen_frame(_FRAME)


async def test_apply_skips_an_item_this_node_forwarded_itself():
    svc, registry, _relay, _l = _receiver(online={_A})
    svc.attach_cluster(_Cluster())
    svc.forward_to_space("sp", _FRAME)
    await svc.apply([_item()])
    await _settle(svc)
    assert registry.sent == []


async def test_a_failed_push_of_a_held_member_item_is_queued_here():
    svc, _registry, relay, _l = _receiver(online={_A, _B}, failing={_B})
    await svc.apply([_item(queue=True)])
    await _settle(svc)
    assert relay.queued == [([_B], _FRAME)]


async def test_a_failed_push_of_a_broadcast_is_not_queued():
    svc, _registry, relay, _l = _receiver(online={_A}, failing={_A})
    await svc.apply([_item()])
    await _settle(svc)
    assert relay.queued == []


async def test_an_audience_without_a_resolver_is_skipped():
    svc, registry, _relay, _l = _receiver(online={_A})
    assert await svc.apply([_item(kind=AUDIENCE_CHANNEL, key="c", queue=True)]) == {}
    await _settle(svc)
    assert registry.sent == []


async def test_a_failing_audience_lookup_is_logged_and_skipped(caplog):
    svc = _fan_out(registry=_Registry({_A}))

    async def _boom(key: str) -> list[str]:
        raise RuntimeError("db down")

    svc.register_audience(AUDIENCE_SPACE, _boom)
    with caplog.at_level(logging.WARNING):
        assert await svc.apply([_item(queue=True)]) == {}
    assert "lookup failed" in caplog.text


async def test_apply_refuses_a_frame_when_saturated(monkeypatch, caplog):
    monkeypatch.setattr(mod, "RELAY_FORWARD_MAX_INFLIGHT", 1)
    gate = asyncio.Event()

    class _Slow(_Registry):
        async def send(self, instance_id, frame):
            await gate.wait()
            return await super().send(instance_id, frame)

    registry = _Slow({_A})
    svc = _fan_out(registry=registry)

    async def _resolve(key: str) -> list[str]:
        return [_A]

    svc.register_audience(AUDIENCE_SPACE, _resolve)
    await svc.apply([_item()])
    assert svc.in_flight == 1
    other = _item({**_FRAME, "payload": {"x": 3}}, queue=True)
    with caplog.at_level(logging.WARNING):
        assert await svc.apply([other]) == {}
        assert await svc.apply([other]) == {}  # inside the warn interval
    assert caplog.text.count("refused") == 1
    gate.set()
    await _settle(svc)
    assert len(registry.sent) == 1


async def test_close_cancels_work_that_outlives_the_grace():
    blocker = asyncio.Event()

    class _Stuck(_Registry):
        async def send(self, instance_id, frame):
            await blocker.wait()
            return True

    svc = _fan_out(registry=_Stuck({_A}))

    async def _resolve(key: str) -> list[str]:
        return [_A]

    svc.register_audience(AUDIENCE_SPACE, _resolve)
    await svc.apply([_item()])
    await svc.close(timeout=0.01)
    assert svc.in_flight == 0
    await svc.close()  # nothing left: a no-op


async def test_an_item_only_the_publisher_holds_here_is_pushed_to_nobody():
    svc, registry, _relay, _l = _receiver(online={_B})
    assert await svc.apply([_item(skip=_B, queue=True)]) == {"space:sp": [_B]}
    await _settle(svc)
    assert registry.sent == []


# ─── Typed kinds: public moments + single-household pushes ──────────────

_ENVELOPE = {"author_user_id": "u-a", "moment_id": "m", "ciphertext": "c"}
_MOMENT = {"event_type": "incoming_public_moment", "payload": _ENVELOPE}
_MOMENT_PUSH = {"type": "incoming_public_moment", "payload": _ENVELOPE}
_NOTICE = {"event_type": "follow_changed", "action": "add", "followed_user_id": "u"}


def test_typed_items_round_trip_and_push_as_their_own_type():
    moment = _item(_MOMENT, kind=AUDIENCE_FOLLOWERS, key="u-a", queue=True)
    assert ForwardedRelay.from_wire(moment.to_wire()) == moment
    assert moment.push_frame() == _MOMENT_PUSH
    # Queued as the socket frame itself: the drain sends it unchanged.
    assert moment.queued_frame() == _MOMENT_PUSH
    notice = _item(_NOTICE, kind=AUDIENCE_INSTANCE, key=_A)
    assert ForwardedRelay.from_wire(notice.to_wire()) == notice
    assert notice.push_frame() == {
        "type": "follow_changed",
        "action": "add",
        "followed_user_id": "u",
    }
    # A space item keeps its relay shape.
    assert _item().push_frame() == {"type": "relay", **_FRAME}
    assert _item().queued_frame() == _FRAME


@pytest.mark.parametrize(
    ("kind", "frame"),
    [
        # A typed kind is pushed only as one of its own frame types.
        (AUDIENCE_FOLLOWERS, {"event_type": "envelope", "sealed": {}}),
        (AUDIENCE_FOLLOWERS, {"event_type": "follow_changed"}),
        (AUDIENCE_FOLLOWERS, {"payload": _ENVELOPE}),
        (AUDIENCE_INSTANCE, {"event_type": "moment_key", "payload": {}}),
        (AUDIENCE_INSTANCE, {"event_type": "relay", "space_id": "sp"}),
        (AUDIENCE_INSTANCE, {**_NOTICE, "type": "envelope"}),
    ],
)
def test_a_typed_item_with_a_foreign_frame_type_is_refused(kind, frame):
    wire = _item(frame, kind=kind, key=_A).to_wire()
    assert ForwardedRelay.from_wire(wire) is None


def test_a_single_household_push_is_forwarded_but_never_remembered():
    """``forward_to_household`` strips the socket ``type`` into the frame;
    the item is not remembered, so the same request a minute later is
    forwarded (and pushed) again."""
    cluster = _Cluster()
    svc = _fan_out(cluster=cluster)
    push = {"type": "moment_key_request", "want_gen": 3}
    assert svc.forward_to_household(_A, push) is True
    assert svc.forward_to_household(_A, push) is True
    items = [item for item, _cb in cluster.items]
    assert (
        items
        == [
            _item(
                {"want_gen": 3, "event_type": "moment_key_request"},
                kind=AUDIENCE_INSTANCE,
                key=_A,
            )
        ]
        * 2
    )
    assert not svc.seen_frame(items[0].frame)


async def test_a_single_household_push_reaches_it_each_time_if_held_here():
    svc, registry, _relay, _l = _receiver(online={_A})
    notice = _item(_NOTICE, kind=AUDIENCE_INSTANCE, key=_A)
    assert await svc.apply([notice]) == {}
    await _settle(svc)
    assert (
        await svc.apply([notice, _item(_NOTICE, kind=AUDIENCE_INSTANCE, key=_B)]) == {}
    )
    await _settle(svc)
    want = {"type": "follow_changed", "action": "add", "followed_user_id": "u"}
    assert registry.sent == [(_A, want), (_A, want)]


async def test_an_instance_audience_names_a_well_formed_household_only():
    svc, registry, _relay, _l = _receiver(online={"not-an-id"})
    await svc.apply([_item(_NOTICE, kind=AUDIENCE_INSTANCE, key="not-an-id")])
    await _settle(svc)
    assert registry.sent == []


async def test_a_moment_push_that_fails_is_queued_as_its_socket_frame():
    """A follower reported held whose socket then failed gets the moment
    queued by the receiver — as the typed frame, not a ``relay``."""
    registry, relay = _Registry({_A, _B}, failing={_B}), _Relay()
    svc = _fan_out(registry=registry, relay=relay)

    async def _followers(key: str) -> list[str]:
        assert key == "u-a"
        return [_A, _B]

    svc.register_audience(AUDIENCE_FOLLOWERS, _followers)
    moment = _item(_MOMENT, kind=AUDIENCE_FOLLOWERS, key="u-a", queue=True)
    assert await svc.apply([moment]) == {"followers:u-a": [_A, _B]}
    await _settle(svc)
    assert registry.sent == [(_A, _MOMENT_PUSH)]
    assert relay.queued == [([_B], _MOMENT_PUSH)]
    # Deduped like any broadcast: the same bytes again push nothing.
    await svc.apply([moment])
    await _settle(svc)
    assert registry.sent == [(_A, _MOMENT_PUSH)]


async def test_the_origin_queues_a_moment_no_peer_holds_as_its_socket_frame():
    cluster, relay = _Cluster(), _Relay()
    svc = _fan_out(relay=relay, cluster=cluster)
    assert svc.forward(
        kind=AUDIENCE_FOLLOWERS, key="u-a", frame=_MOMENT, queue_for=[_A, _B]
    )
    ((_item_sent, on_settled),) = cluster.items
    on_settled(frozenset({_A}))
    await _settle(svc)
    assert relay.queued == [([_B], _MOMENT_PUSH)]


# ─── GFS-wide notices (audience ``all``) ──────────────────────────────────

_RENAME_PUSH = {"type": "server_info_updated", "server_name": "New"}
_RENAME = {"server_name": "New", "event_type": "server_info_updated"}


def test_a_gfs_wide_notice_is_forwarded_and_never_remembered():
    cluster = _Cluster()
    svc = _fan_out(cluster=cluster)
    assert svc.forward_to_all(_RENAME_PUSH) is True
    assert svc.forward_to_all(_RENAME_PUSH) is True
    items = [item for item, _cb in cluster.items]
    assert items == [_item(_RENAME, kind=mod.AUDIENCE_ALL, key="*")] * 2
    assert ForwardedRelay.from_wire(items[0].to_wire()) == items[0]
    assert not svc.seen_frame(items[0].frame)


async def test_a_gfs_wide_notice_reaches_every_socket_here_each_time():
    svc, registry, _relay, _l = _receiver(online={_A, _B})
    notice = _item(_RENAME, kind=mod.AUDIENCE_ALL, key="*")
    assert await svc.apply([notice]) == {}
    await _settle(svc)
    await svc.apply([notice])
    await _settle(svc)
    assert sorted(registry.sent) == sorted([(_A, _RENAME_PUSH), (_B, _RENAME_PUSH)] * 2)


async def test_a_gfs_wide_notice_with_another_key_or_type_reaches_nobody():
    svc, registry, _relay, _l = _receiver(online={_A})
    await svc.apply([_item(_RENAME, kind=mod.AUDIENCE_ALL, key="sp")])
    await _settle(svc)
    assert registry.sent == []
    wire = _item(
        {"event_type": "envelope", "sealed": {}}, kind=mod.AUDIENCE_ALL, key="*"
    ).to_wire()
    assert ForwardedRelay.from_wire(wire) is None


# ─── Public-highlight signals (delivered, never queued) ───────────────────

_SIGNAL_PUSH = {"type": "highlight_signal", "kind": "offer", "session_id": "t.1"}
_SIGNAL = {"kind": "offer", "session_id": "t.1", "event_type": "highlight_signal"}


async def test_a_signal_to_a_socket_here_is_delivered_at_once():
    registry, cluster = _Registry({_A}), _Cluster()
    svc = _fan_out(registry=registry, cluster=cluster)
    assert await svc.deliver_to_household(_A, _SIGNAL_PUSH) is True
    assert registry.sent == [(_A, _SIGNAL_PUSH)]
    assert cluster.items == []


async def test_a_signal_on_a_single_node_reaches_only_a_socket_here():
    svc = _fan_out()
    assert await svc.deliver_to_household(_A, _SIGNAL_PUSH) is False


@pytest.mark.parametrize(
    ("held", "reached"), [({_A}, True), ({_B}, False), (set(), False)]
)
async def test_a_signal_is_delivered_iff_another_node_holds_the_socket(held, reached):
    """Forwarded once, as an ``instance`` item that asks for ``held`` —
    the answer is whether one of the peers holds the author's socket."""
    cluster = _Cluster()
    svc = _fan_out(cluster=cluster)
    pending = asyncio.ensure_future(svc.deliver_to_household(_A, _SIGNAL_PUSH))
    while not cluster.items:
        await asyncio.sleep(0)
    ((item, on_settled),) = cluster.items
    assert item == _item(_SIGNAL, kind=AUDIENCE_INSTANCE, key=_A, queue=True)
    on_settled(frozenset(held))
    on_settled(frozenset({_A}))  # a late second settle changes nothing
    assert await pending is reached
    assert not svc.seen_frame(item.frame)


async def test_a_signal_the_cluster_cannot_take_is_not_delivered():
    svc = _fan_out(cluster=_Cluster(accept=False))
    assert await svc.deliver_to_household(_A, _SIGNAL_PUSH) is False


async def test_a_signal_nobody_answers_for_is_not_delivered_after_the_timeout():
    svc = _fan_out(cluster=_Cluster())
    assert await svc.deliver_to_household(_A, _SIGNAL_PUSH, timeout=0.01) is False


async def test_a_push_goes_to_a_socket_here_or_is_handed_to_the_cluster():
    registry, cluster = _Registry({_A}), _Cluster()
    svc = _fan_out(registry=registry, cluster=cluster)
    assert await svc.push_to_household(_A, _SIGNAL_PUSH) is True
    assert cluster.items == []
    assert await svc.push_to_household(_B, _SIGNAL_PUSH) is True
    ((item, _cb),) = cluster.items
    assert (item.key, item.queue) == (_B, False)
    assert await _fan_out().push_to_household(_B, _SIGNAL_PUSH) is False


async def test_a_held_signal_whose_push_fails_is_never_queued():
    """Reported held, then the socket dropped: a highlight signal belongs to
    a viewer waiting now — never an offline copy."""
    registry, relay = _Registry({_A}, failing={_A}), _Relay()
    svc = _fan_out(registry=registry, relay=relay)
    signal = _item(_SIGNAL, kind=AUDIENCE_INSTANCE, key=_A, queue=True)
    assert ForwardedRelay.from_wire(signal.to_wire()) == signal
    assert not signal.queueable
    assert await svc.apply([signal]) == {f"instance:{_A}": [_A]}
    await _settle(svc)
    assert relay.queued == []


class _RoundCluster(_Cluster):
    """A cluster whose next hint round the test completes by hand."""

    def __init__(self, *, full: bool = False) -> None:
        super().__init__()
        self.full = full
        self.rounds: list[asyncio.Future[None]] = []

    def next_hint_round(self):
        if self.full:
            return None
        fut = asyncio.get_running_loop().create_future()
        self.rounds.append(fut)
        return fut


async def test_waiting_like_an_undelivered_signal_takes_one_cluster_round():
    cluster = _RoundCluster()
    svc = _fan_out(cluster=cluster)
    waiting = asyncio.ensure_future(svc.wait_like_undelivered())
    while not cluster.rounds:
        await asyncio.sleep(0)
    assert not waiting.done()
    cluster.rounds[0].set_result(None)
    await waiting
    # Nothing was forwarded: no frame for anybody.
    assert cluster.items == []


@pytest.mark.security
async def test_waiting_like_an_undelivered_signal_ignores_the_sockets_here():
    """A dead link naming a household connected HERE must not be refused
    faster — that would tell an outsider the household is online."""
    cluster = _RoundCluster()
    svc = _fan_out(registry=_Registry({_A}), cluster=cluster)
    waiting = asyncio.ensure_future(svc.wait_like_undelivered())
    while not cluster.rounds:
        await asyncio.sleep(0)
    assert not waiting.done()
    cluster.rounds[0].set_result(None)
    await waiting


@pytest.mark.parametrize("setup", ["single_node", "waiters_full"])
async def test_waiting_like_an_undelivered_signal_is_immediate_without_a_round(
    setup,
):
    cluster = None if setup == "single_node" else _RoundCluster(full=True)
    svc = _fan_out(cluster=cluster)
    await asyncio.wait_for(svc.wait_like_undelivered(), timeout=1)


async def test_waiting_like_an_undelivered_signal_ends_at_the_timeout():
    svc = _fan_out(cluster=_RoundCluster())
    await asyncio.wait_for(svc.wait_like_undelivered(timeout=0.01), timeout=1)


@pytest.mark.security
async def test_a_kind_with_its_own_seen_cache_dedupes_in_it():
    """Public moments are remembered as long as their key step lasts, in the
    registry's cache: what this node forwards or is forwarded lands there
    (so a re-post to THIS node is a no-op too), and the ten-minute cache of
    the other kinds stays untouched."""
    registry, cluster = _Registry({_A}), _Cluster()
    svc = _fan_out(registry=registry, cluster=cluster)
    long_cache = SeenPayloadCache(ttl_s=90_000.0, cap=10)

    async def _followers(key: str) -> list[str]:
        return [_A]

    svc.register_audience(AUDIENCE_FOLLOWERS, _followers, seen=long_cache)
    moment = _item(_MOMENT, kind=AUDIENCE_FOLLOWERS, key="u-a")
    await svc.apply([moment])
    await _settle(svc)
    assert long_cache.seen(moment.digest())
    assert not svc.seen_frame(_MOMENT)
    forwarded = {**_MOMENT, "payload": {"x": "forwarded here"}}
    assert svc.forward(kind=AUDIENCE_FOLLOWERS, key="u-a", frame=forwarded)
    assert long_cache.seen(SeenPayloadCache.digest(forwarded))
    await svc.apply([moment])
    await _settle(svc)
    assert len(registry.sent) == 1
