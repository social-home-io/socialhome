"""Tests for socialhome.services.user_move_service (move-out, Task 5).

Exercises the inbound USER_MOVED / USER_IDENTITY_RESOLVE handlers and the
outbound push against the *real* crypto verify path + a real ``SqliteUserRepo``
so the binding checks are genuinely run, not mocked away.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from socialhome.crypto import (
    b64url_encode,
    build_move_link,
    build_user_identity_assertion,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.move_link import MoveLink
from socialhome.domain.user import RemoteUser
from socialhome.federation.event_dispatch_registry import EventDispatchRegistry
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.user_move_service import UserMoveService


class FakeFederationService:
    """A minimal federation-service stand-in for the move service.

    Carries a real :class:`EventDispatchRegistry` (so ``attach_to`` exercises
    the same registration path production uses), a pinned-key map for
    ``peer_identity_public_key``, a proto-version map for ``peer_supports``,
    and a recorder of every outbound :meth:`send_event` call.
    """

    def __init__(self) -> None:
        self._event_registry = EventDispatchRegistry()
        self._pinned: dict[str, bytes] = {}
        self._versions: dict[str, int] = {}
        self._confirmed: set[str] = set()
        self.sent: list[dict] = []

    async def is_confirmed_peer(self, instance_id: str) -> bool:
        return instance_id in self._confirmed

    async def peer_identity_public_key(self, instance_id: str) -> bytes | None:
        return self._pinned.get(instance_id)

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        return self._versions.get(instance_id, 1) >= min_version

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> None:
        self.sent.append(
            {
                "to_instance_id": to_instance_id,
                "event_type": event_type,
                "payload": payload,
                "space_id": space_id,
            }
        )


def _build_scenario(*, username: str = "pascal", display_name: str = "Pascal"):
    """Mint a realistic move-out scenario (one portable P spans both homes)."""
    user_kp = generate_identity_keypair()
    old_home_kp = generate_identity_keypair()
    new_home_kp = generate_identity_keypair()

    anchor = "deadbeefcafef00d" * 2
    old_instance_id = derive_instance_id(old_home_kp.public_key)
    new_instance_id = derive_instance_id(new_home_kp.public_key)
    old_user_id = derive_user_id(old_home_kp.public_key, anchor)
    new_user_id = derive_user_id(new_home_kp.public_key, anchor)
    issued = datetime.now(UTC).isoformat()

    new_home_assertion = build_user_identity_assertion(
        instance_seed=new_home_kp.private_key,
        user_id=new_user_id,
        instance_id=new_instance_id,
        username=username,
        display_name=display_name,
        issued_at=issued,
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        identity_anchor=anchor,
    )
    link = build_move_link(
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        old_home_instance_seed=old_home_kp.private_key,
        old_user_id=old_user_id,
        old_instance_id=old_instance_id,
        new_instance_public_key=new_home_kp.public_key,
        new_home_assertion=new_home_assertion,
        issued_at=issued,
    )
    return {
        "user_kp": user_kp,
        "old_home_kp": old_home_kp,
        "new_home_kp": new_home_kp,
        "old_instance_id": old_instance_id,
        "new_instance_id": new_instance_id,
        "old_user_id": old_user_id,
        "new_user_id": new_user_id,
        "link": link,
    }


@pytest.fixture
async def env(tmp_dir):
    """A real SqliteUserRepo over a migrated SQLite db + a fake fed service."""
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.user_repo = SqliteUserRepo(db)
    e.fed = FakeFederationService()
    e.svc = UserMoveService(user_repo=e.user_repo)
    e.svc.attach_to(e.fed)
    yield e
    await db.shutdown()


async def _seed_old_row(env, scenario, *, store_p: bytes | None) -> None:
    """Insert the moved user's old-home peer + remote_users row.

    Pins the old home's instance key (so ``peer_identity_public_key``
    resolves) and optionally stores the per-user P binding.
    """
    env.fed._pinned[scenario["old_instance_id"]] = scenario["old_home_kp"].public_key
    await env.db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            scenario["old_instance_id"],
            "Old Home",
            scenario["old_home_kp"].public_key.hex(),
            "k1",
            "k2",
            "https://old.example/federation/inbox/x",
            "local-inbox",
            "confirmed",
            "manual",
        ),
    )
    await env.user_repo.upsert_remote(
        RemoteUser(
            user_id=scenario["old_user_id"],
            instance_id=scenario["old_instance_id"],
            remote_username="pascal",
            display_name="Pascal",
        ),
    )
    if store_p is not None:
        await env.user_repo.set_remote_user_identity_key(
            scenario["old_user_id"], public_key_hex=store_p.hex()
        )


async def _seed_new_row(env, scenario) -> None:
    """Insert the destination (new-home) ``remote_users`` row + its instance."""
    await env.db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            scenario["new_instance_id"],
            "New Home",
            scenario["new_home_kp"].public_key.hex(),
            "k1",
            "k2",
            "https://new.example/federation/inbox/x",
            "local-inbox-new",
            "confirmed",
            "manual",
        ),
    )
    await env.user_repo.upsert_remote(
        RemoteUser(
            user_id=scenario["new_user_id"],
            instance_id=scenario["new_instance_id"],
            remote_username="pascal",
            display_name="Pascal",
        ),
    )


def _moved_event(scenario, *, payload: dict | None = None) -> FederationEvent:
    return FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.USER_MOVED,
        from_instance=scenario["old_instance_id"],
        to_instance="us",
        timestamp=datetime.now(UTC).isoformat(),
        payload=payload
        if payload is not None
        else {"move_link": scenario["link"].to_wire_dict()},
    )


# ─── 1. happy path ────────────────────────────────────────────────────────


async def test_inbound_user_moved_records_redirect(env):
    """A valid signed move-link records the redirect; resolve follows it."""
    s = _build_scenario()
    await _seed_old_row(env, s, store_p=s["user_kp"].public_key)
    # The destination identity is a known remote row (paired new home) so the
    # redirect chain resolves to the tip.
    await _seed_new_row(env, s)

    await env.fed._event_registry.dispatch(_moved_event(s))

    resolved = await env.user_repo.resolve_current_identity(s["old_user_id"])
    assert resolved == (s["new_user_id"], s["new_instance_id"])


# ─── 2. forged user signature → fail-soft, no record ────────────────────────


async def test_inbound_user_moved_forged_user_sig_not_recorded(env, caplog):
    """A forged user_signature does not raise and does not record the move."""
    import dataclasses

    s = _build_scenario()
    await _seed_old_row(env, s, store_p=s["user_kp"].public_key)
    forged = dataclasses.replace(s["link"], user_signature=b64url_encode(b"\x00" * 64))

    with caplog.at_level("WARNING"):
        await env.fed._event_registry.dispatch(
            _moved_event(s, payload={"move_link": forged.to_wire_dict()})
        )

    # Still resolves to the OLD identity — no redirect was written.
    resolved = await env.user_repo.resolve_current_identity(s["old_user_id"])
    assert resolved == (s["old_user_id"], s["old_instance_id"])
    assert any(r.levelname == "WARNING" for r in caplog.records)


# ─── 3. no stored P binding → fail-soft, no record ──────────────────────────


async def test_inbound_user_moved_no_stored_p_not_recorded(env, caplog):
    """Without a stored P binding for the old id we cannot verify → no record."""
    s = _build_scenario()
    await _seed_old_row(env, s, store_p=None)

    with caplog.at_level("WARNING"):
        await env.fed._event_registry.dispatch(_moved_event(s))

    resolved = await env.user_repo.resolve_current_identity(s["old_user_id"])
    assert resolved == (s["old_user_id"], s["old_instance_id"])
    assert any(r.levelname == "WARNING" for r in caplog.records)


# ─── 4. malformed payload → no raise, no record ─────────────────────────────


async def test_inbound_user_moved_malformed_payload(env):
    """A payload missing/with a non-dict move_link is a no-op, never raises."""
    s = _build_scenario()
    await _seed_old_row(env, s, store_p=s["user_kp"].public_key)

    # No "move_link" key.
    await env.fed._event_registry.dispatch(_moved_event(s, payload={}))
    # Non-dict move_link.
    await env.fed._event_registry.dispatch(
        _moved_event(s, payload={"move_link": "not-a-dict"})
    )
    # A dict that is not a valid move-link wire shape.
    await env.fed._event_registry.dispatch(
        _moved_event(s, payload={"move_link": {"suite": "ed25519"}})
    )

    resolved = await env.user_repo.resolve_current_identity(s["old_user_id"])
    assert resolved == (s["old_user_id"], s["old_instance_id"])


# ─── 5. handle_resolve_request ──────────────────────────────────────────────


async def test_handle_resolve_request_returns_link_for_moved_user(env):
    """A moved user's stored link is returned; unknown users return None."""
    s = _build_scenario()
    await _seed_old_row(env, s, store_p=s["user_kp"].public_key)
    await env.fed._event_registry.dispatch(_moved_event(s))

    resp = await env.svc.handle_resolve_request({"old_user_id": s["old_user_id"]})
    assert resp is not None
    got = MoveLink.from_wire_dict(resp["move_link"])
    assert got.new_user_id == s["new_user_id"]

    assert await env.svc.handle_resolve_request({"old_user_id": "unknown"}) is None
    assert await env.svc.handle_resolve_request({}) is None


async def test_on_resolve_request_sends_link_back_to_confirmed_peer(env):
    """The inbound resolve handler replies to a CONFIRMED sender with the link."""
    s = _build_scenario()
    await _seed_old_row(env, s, store_p=s["user_kp"].public_key)
    await env.fed._event_registry.dispatch(_moved_event(s))
    env.fed._confirmed.add("asker")

    resolve_evt = FederationEvent(
        msg_id="r1",
        event_type=FederationEventType.USER_IDENTITY_RESOLVE,
        from_instance="asker",
        to_instance="us",
        timestamp=datetime.now(UTC).isoformat(),
        payload={"old_user_id": s["old_user_id"]},
    )
    await env.fed._event_registry.dispatch(resolve_evt)

    assert len(env.fed.sent) == 1
    sent = env.fed.sent[0]
    assert sent["to_instance_id"] == "asker"
    assert sent["event_type"] == FederationEventType.USER_IDENTITY_RESOLVE
    assert (
        MoveLink.from_wire_dict(sent["payload"]["move_link"]).new_user_id
        == (s["new_user_id"])
    )


async def test_on_resolve_request_drops_non_confirmed_peer(env, caplog):
    """A resolve request from a non-confirmed/unknown peer is dropped, no reply."""
    s = _build_scenario()
    await _seed_old_row(env, s, store_p=s["user_kp"].public_key)
    await env.fed._event_registry.dispatch(_moved_event(s))
    # "asker" is deliberately NOT in env.fed._confirmed.

    resolve_evt = FederationEvent(
        msg_id="r1",
        event_type=FederationEventType.USER_IDENTITY_RESOLVE,
        from_instance="asker",
        to_instance="us",
        timestamp=datetime.now(UTC).isoformat(),
        payload={"old_user_id": s["old_user_id"]},
    )
    with caplog.at_level("WARNING"):
        await env.fed._event_registry.dispatch(resolve_evt)

    assert env.fed.sent == []
    assert any(
        "non-confirmed peer" in r.message and r.levelname == "WARNING"
        for r in caplog.records
    )


# ─── 6. push_move_link gates on proto version ───────────────────────────────


async def test_push_move_link_gates_on_proto_version(env):
    """push sends to a v_27 peer and skips a v_26 peer."""
    s = _build_scenario()
    env.fed._versions["peer-v27"] = FederationCapability.MIN_FOR_USER_MOVE
    env.fed._versions["peer-v26"] = FederationCapability.MIN_FOR_USER_MOVE - 1

    sent_to = await env.svc.push_move_link(
        s["link"], peer_instance_ids=["peer-v27", "peer-v26"]
    )

    assert sent_to == ["peer-v27"]
    assert len(env.fed.sent) == 1
    sent = env.fed.sent[0]
    assert sent["to_instance_id"] == "peer-v27"
    assert sent["event_type"] == FederationEventType.USER_MOVED
    assert (
        MoveLink.from_wire_dict(sent["payload"]["move_link"]).old_user_id
        == s["old_user_id"]
    )


# ─── 7. attach_to registers both event types ───────────────────────────────


async def test_attach_to_registers_both_event_types():
    """attach_to wires a handler for USER_MOVED and USER_IDENTITY_RESOLVE."""

    class _Repo:
        pass

    fed = FakeFederationService()
    svc = UserMoveService(user_repo=_Repo())
    svc.attach_to(fed)

    assert fed._event_registry.handler_count(FederationEventType.USER_MOVED) == 1
    assert (
        fed._event_registry.handler_count(FederationEventType.USER_IDENTITY_RESOLVE)
        == 1
    )


# ─── move_link_json persists the wire shape ─────────────────────────────────


async def test_recorded_move_link_json_round_trips(env):
    """The stored move_link JSON is the link's wire dict (json.dumps)."""
    s = _build_scenario()
    await _seed_old_row(env, s, store_p=s["user_kp"].public_key)
    await env.fed._event_registry.dispatch(_moved_event(s))

    stored = await env.user_repo.get_move_link(s["old_user_id"])
    assert stored is not None
    assert json.loads(stored) == s["link"].to_wire_dict()
