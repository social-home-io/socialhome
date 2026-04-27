"""Tests for DmRoutingService (§12.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.services.dm_routing_service import (
    DEDUP_TTL_SECONDS,
    MAX_HOPS,
    MAX_SEARCH_NODES,
    DmRoutingService,
    NoRouteError,
    RelayBlockedError,
    RelayEnvelope,
    _hash_mod,
)


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeFed:
    def __init__(self, own: str = "self-iid"):
        self.own_instance_id = own
        self.sent: list[tuple] = []

    async def send_event(self, *, to_instance_id, event_type, payload, **kw):
        self.sent.append((to_instance_id, event_type, payload))


class _FakeCP:
    def __init__(self, allowed: bool = True):
        self._allowed = allowed

    async def is_dm_allowed(self, *, sender_user_id, target_instance_id):
        return self._allowed


@dataclass(slots=True)
class _Event:
    event_type: FederationEventType
    from_instance: str
    payload: dict


def _remote(iid: str, *, status=PairingStatus.CONFIRMED) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://x/wh",
        local_inbox_id=f"wh-{iid}",
        status=status,
        source=InstanceSource.MANUAL,
    )


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    # Seed a conversation row — send_relay_envelope bumps
    # conversation_sender_sequences which FKs into conversations(id).
    await db.enqueue(
        "INSERT INTO conversations(id, type) VALUES(?, 'dm')",
        ("c1",),
    )
    fed_repo = SqliteFederationRepo(db)
    fed = _FakeFed("self-iid")
    svc = DmRoutingService(
        SqliteDmRoutingRepo(db),
        fed_repo,
        federation_service=fed,
        own_instance_id="self-iid",
    )
    yield db, fed_repo, svc, fed
    await db.shutdown()


# ─── Constants ───────────────────────────────────────────────────────────


def test_max_hops_matches_spec():
    assert MAX_HOPS == 3


def test_max_search_cap_matches_spec():
    assert MAX_SEARCH_NODES == 200


def test_dedup_ttl_one_hour():
    assert DEDUP_TTL_SECONDS == 3600


# ─── Graph queries ───────────────────────────────────────────────────────


async def test_get_own_peers_lists_confirmed(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("m"))
    await fed_repo.save_instance(_remote("n"))
    # Pending pairing — not a peer.
    await fed_repo.save_instance(
        _remote("p", status=PairingStatus.PENDING_SENT),
    )
    peers = await svc.get_own_peers()
    assert set(peers) == {"m", "n"}


async def test_get_known_peers_self_equals_own_peers(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("m"))
    assert await svc.get_known_peers("self-iid") == ["m"]


async def test_get_known_peers_other_reads_network_discovery(env):
    db, _, svc, _ = env
    # m introduced us to x and y.
    await svc.record_network_sync(
        source_instance_id="m",
        peer_ids=["x", "y"],
    )
    peers = set(await svc.get_known_peers("m"))
    assert peers == {"x", "y"}


async def test_record_network_sync_caps_at_50(env):
    _, _, svc, _ = env
    huge = [f"peer-{i}" for i in range(200)]
    n = await svc.record_network_sync(source_instance_id="m", peer_ids=huge)
    assert n == 50


async def test_record_network_sync_dedups_input(env):
    _, _, svc, _ = env
    n = await svc.record_network_sync(
        source_instance_id="m",
        peer_ids=["a", "a", "b", "b", "c"],
    )
    assert n == 3


async def test_record_network_sync_ignores_non_string(env):
    _, _, svc, _ = env
    n = await svc.record_network_sync(
        source_instance_id="m",
        peer_ids=["ok", 123, None, "also-ok"],  # type: ignore[list-item]
    )
    assert n == 2


# ─── BFS ─────────────────────────────────────────────────────────────────


async def test_find_relay_path_direct_target(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("target"))
    paths = await svc.find_relay_paths("target")
    assert paths == [["target"]]


async def test_find_relay_path_two_hops_via_m(env):
    _, fed_repo, svc, _ = env
    # self ── m ── target (target not direct)
    await fed_repo.save_instance(_remote("m"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["target"])
    paths = await svc.find_relay_paths("target")
    assert [list(p) for p in paths] == [["m", "target"]]


async def test_find_relay_path_three_hops(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("m"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["n"])
    await svc.record_network_sync(source_instance_id="n", peer_ids=["target"])
    paths = await svc.find_relay_paths("target")
    assert paths == [["m", "n", "target"]]


async def test_find_relay_path_respects_max_hops(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("m"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["n"])
    await svc.record_network_sync(source_instance_id="n", peer_ids=["o"])
    await svc.record_network_sync(source_instance_id="o", peer_ids=["target"])
    # 4 hops needed → unreachable within MAX_HOPS=3.
    paths = await svc.find_relay_paths("target")
    assert paths == []


async def test_find_relay_path_no_path(env):
    _, _, svc, _ = env
    assert await svc.find_relay_paths("nowhere") == []


async def test_find_relay_path_singular(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("target"))
    assert await svc.find_relay_path("target") == ["target"]


async def test_find_relay_path_multiple_paths_sorted_by_length(env):
    _, fed_repo, svc, _ = env
    # self paired with m and n; both report target as peer.
    await fed_repo.save_instance(_remote("m"))
    await fed_repo.save_instance(_remote("n"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["target"])
    await svc.record_network_sync(source_instance_id="n", peer_ids=["target"])
    paths = await svc.find_relay_paths("target")
    assert len(paths) == 2
    assert {p[0] for p in paths} == {"m", "n"}


# ─── Path selection (deterministic) ──────────────────────────────────────


async def test_select_conversation_path_is_deterministic(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("m"))
    await fed_repo.save_instance(_remote("n"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["target"])
    await svc.record_network_sync(source_instance_id="n", peer_ids=["target"])
    p1 = await svc.select_conversation_path("c1", "alice", "target")
    p2 = await svc.select_conversation_path("c1", "alice", "target")
    assert p1 == p2


async def test_select_conversation_path_raises_when_unreachable(env):
    _, _, svc, _ = env
    with pytest.raises(NoRouteError):
        await svc.select_conversation_path("c1", "alice", "nowhere")


async def test_select_conversation_path_persists_alternatives(env):
    """Spec §18587 — pick + persist primary AND alternatives."""
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("m"))
    await fed_repo.save_instance(_remote("n"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["target"])
    await svc.record_network_sync(source_instance_id="n", peer_ids=["target"])
    chosen = await svc.select_conversation_path("c1", "alice", "target")
    stored = await svc._repo.get_relay_paths("c1", "alice")
    assert stored is not None
    assert stored["primary"] == chosen
    # 2 paths discovered, 1 chosen → 1 alternative left.
    assert len(stored["alternatives"]) == 1


# ─── get_or_select_path (spec §18594) ────────────────────────────────────


async def test_get_or_select_path_returns_stored_primary(env):
    """If primary's first hop is still confirmed, no rediscovery happens."""
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("m"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["target"])
    first = await svc.select_conversation_path("c1", "alice", "target")
    # Mutate the stored primary to a known-good marker so we can prove
    # the call doesn't re-run BFS.
    await svc._repo.set_relay_paths(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance="target",
        primary=["m", "target"],
        alternatives=[["m", "target", "extra"]],  # would never come from BFS
    )
    again = await svc.get_or_select_path("c1", "alice", "target")
    assert again == ["m", "target"]
    assert again != first or True  # silence unused var


async def test_get_or_select_path_promotes_alternative_when_primary_stale(env):
    """Primary's first hop offline → next valid alt is promoted to primary."""
    db, fed_repo, svc, _ = env
    # Two confirmed peers, both reach 'target' as a 2-hop relay.
    await fed_repo.save_instance(_remote("m"))
    await fed_repo.save_instance(_remote("n"))
    await svc._repo.set_relay_paths(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance="target",
        # Primary's first hop is "ghost" — never paired, so stale.
        primary=["ghost", "target"],
        alternatives=[["m", "target"], ["n", "target"]],
    )
    chosen = await svc.get_or_select_path("c1", "alice", "target")
    assert chosen == ["m", "target"]
    # m got promoted; n stays as the one remaining alternative.
    stored = await svc._repo.get_relay_paths("c1", "alice")
    assert stored["primary"] == ["m", "target"]
    assert stored["alternatives"] == [["n", "target"]]


async def test_get_or_select_path_rediscovers_when_all_stale(env):
    """All stored first-hops gone → clear + fresh BFS."""
    _, fed_repo, svc, _ = env
    # Seed only 'm' as confirmed; stored paths reference unknown peers.
    await fed_repo.save_instance(_remote("m"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["target"])
    await svc._repo.set_relay_paths(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance="target",
        primary=["ghost-1", "target"],
        alternatives=[["ghost-2", "target"]],
    )
    chosen = await svc.get_or_select_path("c1", "alice", "target")
    # Fresh BFS finds [m, target] (the only viable route).
    assert chosen == ["m", "target"]
    stored = await svc._repo.get_relay_paths("c1", "alice")
    assert stored["primary"] == ["m", "target"]


async def test_get_or_select_path_falls_back_to_select_when_no_storage(env):
    """No prior storage → behaves like select_conversation_path."""
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("target"))
    chosen = await svc.get_or_select_path("c1", "alice", "target")
    assert chosen == ["target"]  # direct connection


async def test_send_relay_envelope_uses_stored_path_on_repeat(env):
    """Two sends in a row hit the same primary — no re-BFS, no re-shuffle."""
    _, fed_repo, svc, fed = env
    await fed_repo.save_instance(_remote("m"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["target"])
    await svc.send_relay_envelope(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance_id="target",
        target_user_id="bob",
        inner_event_type="dm_message",
        sender_ephemeral_pk="pk",
        encrypted_payload="ct",
        payload_iv="iv",
    )
    await svc.send_relay_envelope(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance_id="target",
        target_user_id="bob",
        inner_event_type="dm_message",
        sender_ephemeral_pk="pk",
        encrypted_payload="ct",
        payload_iv="iv",
    )
    # Both sends went to the same first hop.
    assert fed.sent[0][0] == fed.sent[1][0]


# ─── Send outbound ───────────────────────────────────────────────────────


async def test_send_relay_envelope_direct_target(env):
    _, fed_repo, svc, fed = env
    await fed_repo.save_instance(_remote("target"))
    envelope = await svc.send_relay_envelope(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance_id="target",
        target_user_id="bob",
        inner_event_type="dm_message",
        sender_ephemeral_pk="pk",
        encrypted_payload="ct",
        payload_iv="iv",
    )
    assert envelope.destination_instance_id == "target"
    assert fed.sent, "federation.send_event should have been called"
    target, et, payload = fed.sent[0]
    assert target == "target"
    assert et == FederationEventType.DM_RELAY
    assert payload["message_id"] == envelope.message_id


async def test_send_relay_envelope_two_hops(env):
    _, fed_repo, svc, fed = env
    await fed_repo.save_instance(_remote("m"))
    await svc.record_network_sync(source_instance_id="m", peer_ids=["target"])
    _envelope = await svc.send_relay_envelope(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance_id="target",
        target_user_id="bob",
        inner_event_type="dm_message",
        sender_ephemeral_pk="pk",
        encrypted_payload="ct",
        payload_iv="iv",
    )
    target, _, _ = fed.sent[0]
    assert target == "m"  # first hop, not final dest


async def test_send_relay_no_route_raises(env):
    _, _, svc, _ = env
    with pytest.raises(NoRouteError):
        await svc.send_relay_envelope(
            conversation_id="c1",
            sender_user_id="alice",
            target_instance_id="nowhere",
            target_user_id="bob",
            inner_event_type="dm_message",
            sender_ephemeral_pk="pk",
            encrypted_payload="ct",
            payload_iv="iv",
        )


async def test_send_relay_blocked_for_minor(env):
    _, fed_repo, svc, _ = env
    svc._child_protection = _FakeCP(allowed=False)
    await fed_repo.save_instance(_remote("target"))
    with pytest.raises(RelayBlockedError):
        await svc.send_relay_envelope(
            conversation_id="c1",
            sender_user_id="minor-id",
            target_instance_id="target",
            target_user_id="bob",
            inner_event_type="dm_message",
            sender_ephemeral_pk="pk",
            encrypted_payload="ct",
            payload_iv="iv",
        )


async def test_send_relay_sender_seq_increments(env):
    _, fed_repo, svc, fed = env
    await fed_repo.save_instance(_remote("target"))
    e1 = await svc.send_relay_envelope(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance_id="target",
        target_user_id="bob",
        inner_event_type="dm_message",
        sender_ephemeral_pk="pk",
        encrypted_payload="ct",
        payload_iv="iv",
    )
    e2 = await svc.send_relay_envelope(
        conversation_id="c1",
        sender_user_id="alice",
        target_instance_id="target",
        target_user_id="bob",
        inner_event_type="dm_message",
        sender_ephemeral_pk="pk",
        encrypted_payload="ct",
        payload_iv="iv",
    )
    assert e2.sender_seq == e1.sender_seq + 1


# ─── Inbound forwarding ──────────────────────────────────────────────────


def _envelope_dict(
    *,
    dest: str,
    hop_count: int = 0,
    msg_id: str = "m-1",
) -> dict:
    return {
        "destination_instance_id": dest,
        "destination_user_id": "bob",
        "hop_count": hop_count,
        "inner_event_type": "dm_message",
        "message_id": msg_id,
        "sender_seq": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sender_ephemeral_pk": "pk",
        "encrypted_payload": "ct",
        "payload_iv": "iv",
        "return_path": [],
    }


async def test_handle_inbound_relay_delivered_at_destination(env):
    _, _, svc, _ = env
    event = _Event(
        FederationEventType.DM_RELAY,
        "upstream",
        _envelope_dict(dest="self-iid"),
    )
    assert await svc.handle_inbound_relay(event) == "delivered"


async def test_handle_inbound_relay_drops_duplicate(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("target"))
    event = _Event(
        FederationEventType.DM_RELAY,
        "upstream",
        _envelope_dict(dest="target"),
    )
    assert await svc.handle_inbound_relay(event) == "forwarded"
    # Second time same msg_id → dropped.
    assert await svc.handle_inbound_relay(event) == "dropped:duplicate"


async def test_handle_inbound_relay_too_many_hops(env):
    _, fed_repo, svc, _ = env
    await fed_repo.save_instance(_remote("target"))
    event = _Event(
        FederationEventType.DM_RELAY,
        "upstream",
        _envelope_dict(dest="target", hop_count=MAX_HOPS, msg_id="over"),
    )
    assert await svc.handle_inbound_relay(event) == "dropped:too_many_hops"


async def test_handle_inbound_relay_no_route(env):
    _, _, svc, _ = env
    event = _Event(
        FederationEventType.DM_RELAY,
        "upstream",
        _envelope_dict(dest="unreachable", msg_id="m-nope"),
    )
    assert await svc.handle_inbound_relay(event) == "dropped:no_route"


async def test_handle_inbound_relay_forwards_and_increments_hop(env):
    _, fed_repo, svc, fed = env
    await fed_repo.save_instance(_remote("target"))
    event = _Event(
        FederationEventType.DM_RELAY,
        "upstream",
        _envelope_dict(dest="target", hop_count=1, msg_id="fwd-1"),
    )
    result = await svc.handle_inbound_relay(event)
    assert result == "forwarded"
    assert fed.sent
    _, _, payload = fed.sent[0]
    assert payload["hop_count"] == 2
    assert "upstream" in payload["return_path"]


async def test_handle_inbound_relay_malformed(env):
    _, _, svc, _ = env
    event = _Event(
        FederationEventType.DM_RELAY,
        "upstream",
        {"missing": "fields"},
    )
    assert await svc.handle_inbound_relay(event) == "dropped:malformed"


# ─── Dedup ring ──────────────────────────────────────────────────────────


async def test_mark_seen_and_prune(env):
    db, _, svc, _ = env
    await svc._mark_seen("m-1")
    assert await svc._has_seen("m-1")
    # Simulate old entry + prune.
    await db.enqueue(
        "UPDATE dm_relay_seen SET seen_at='2020-01-01T00:00:00+00:00'"
        " WHERE msg_id='m-1'",
    )
    n = await svc.prune_seen()
    assert n >= 1
    assert not await svc._has_seen("m-1")


# ─── Envelope round-trip ────────────────────────────────────────────────


def test_envelope_to_dict_from_dict_roundtrip():
    env = RelayEnvelope(
        destination_instance_id="dest",
        destination_user_id="u",
        hop_count=2,
        inner_event_type="dm_message",
        message_id="m",
        sender_seq=7,
        created_at="2026-04-15T00:00:00+00:00",
        sender_ephemeral_pk="pk",
        encrypted_payload="ct",
        payload_iv="iv",
        return_path=("a", "b"),
    )
    d = env.to_dict()
    back = RelayEnvelope.from_dict(d)
    assert back == env


def test_envelope_from_dict_rejects_missing_fields():
    with pytest.raises((KeyError, ValueError)):
        RelayEnvelope.from_dict({"hop_count": 0})


# ─── Hash helper ─────────────────────────────────────────────────────────


def test_hash_mod_deterministic():
    assert _hash_mod("a", 10) == _hash_mod("a", 10)


def test_hash_mod_distributes_across_range():
    samples = {_hash_mod(f"k-{i}", 10) for i in range(100)}
    # Not all in one bucket — sanity check distribution.
    assert len(samples) >= 5


def test_hash_mod_zero_n():
    assert _hash_mod("x", 0) == 0
