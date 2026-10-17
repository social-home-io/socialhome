"""Release-blocker protocol test: a friend's status shows up right after
pairing.

Two real households, each a full app over its own SQLite file. Household
**A** has a member with a status; when A confirms the pairing with **B** its
real :class:`UsersSyncOutbound` builds the ``USERS_SYNC`` roster snapshot,
and B's real inbound handler applies it. B caches the status without waiting
for the member's next status change — and a member hidden from B never
reaches it, status included. Only the transport is stubbed: the envelope's
payload is handed from A's ``send_event`` to B's registered handler.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, event_bus_key, federation_service_key
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import FederationEvent, FederationEventType

pytestmark = pytest.mark.security

FET = FederationEventType


def _config(tmp_path, name: str) -> Config:
    d = tmp_path / name
    d.mkdir()
    return Config(
        data_dir=str(d),
        db_path=str(d / "sh.db"),
        media_path=str(d / "media"),
        apps_path=str(d / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=1,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://t.example"})},
        ),
    )


async def _identity(app) -> tuple[str, bytes]:
    row = await app[db_key].fetchone(
        "SELECT identity_public_key FROM instance_identity"
    )
    return app[federation_service_key].own_instance_id, bytes.fromhex(
        row["identity_public_key"]
    )


async def _pin(app, peer_id: str, peer_pk: bytes) -> None:
    await app[db_key].enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            peer_id,
            peer_id,
            peer_pk.hex(),
            "k1",
            "k2",
            f"https://{peer_id}/wh",
            f"wh-{peer_id}",
            "confirmed",
            "manual",
        ),
    )


@pytest.fixture
async def houses(aiohttp_client, tmp_path):
    a = create_app(_config(tmp_path, "a"))
    b = create_app(_config(tmp_path, "b"))
    for app in (a, b):
        await aiohttp_client(app)
    a_id, a_pk = await _identity(a)
    b_id, b_pk = await _identity(b)
    await _pin(a, b_id, b_pk)
    await _pin(b, a_id, a_pk)
    return a, b, a_id, a_pk, b_id


async def _add_member(app, pk: bytes, username: str, **status) -> str:
    user_id = derive_user_id(pk, username)
    await app[db_key].enqueue(
        "INSERT INTO users(username, user_id, display_name, status_emoji,"
        " status_text, status_expires_at) VALUES(?,?,?,?,?,?)",
        (
            username,
            user_id,
            username.title(),
            status.get("emoji"),
            status.get("text"),
            status.get("expires_at"),
        ),
    )
    return user_id


async def _pair_and_deliver(monkeypatch, a, b, a_id: str, b_id: str) -> dict:
    """A confirms the pairing with B; its USERS_SYNC lands on B."""
    sent: list[tuple[str, FederationEventType, dict]] = []
    fed = a[federation_service_key]

    async def _capture(self, *, to_instance_id, event_type, payload, **_kw):
        assert self is fed
        sent.append((to_instance_id, event_type, payload))

    # Transport stub (the service is slotted, so patch the class); everything
    # else — the snapshot builder, its filters, B's handler — is real.
    monkeypatch.setattr(type(fed), "send_event", _capture)
    await a[event_bus_key].publish(PairingConfirmed(instance_id=b_id))
    syncs = [s for s in sent if s[1] is FET.USERS_SYNC]
    assert len(syncs) == 1 and syncs[0][0] == b_id
    payload = syncs[0][2]
    handlers = b[federation_service_key]._event_registry.handlers_for(FET.USERS_SYNC)
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id=uuid.uuid4().hex,
                event_type=FET.USERS_SYNC,
                from_instance=a_id,
                to_instance=b_id,
                timestamp=datetime.now(UTC).isoformat(),
                payload=payload,
            )
        )
    return payload


async def _status_on(app, user_id: str):
    row = await app[db_key].fetchone(
        "SELECT status_json FROM remote_users WHERE user_id=?", (user_id,)
    )
    return row


async def test_a_friends_status_lands_with_the_pairing_roster(houses, monkeypatch):
    a, b, a_id, a_pk, b_id = houses
    later = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    earlier = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    alice = await _add_member(
        a, a_pk, "alice", emoji="🌴", text="On holiday", expires_at=later
    )
    max_ = await _add_member(a, a_pk, "max", text="Stale", expires_at=earlier)
    plain = await _add_member(a, a_pk, "pia")

    await _pair_and_deliver(monkeypatch, a, b, a_id, b_id)

    row = await _status_on(b, alice)
    assert row is not None
    assert json.loads(row["status_json"]) == {
        "emoji": "🌴",
        "text": "On holiday",
        "expires_at": later,
    }
    # An expired status is never resurrected; no status stays empty.
    assert (await _status_on(b, max_))["status_json"] is None
    assert (await _status_on(b, plain))["status_json"] is None


async def test_a_member_hidden_from_the_peer_never_ships_a_status(houses, monkeypatch):
    a, b, a_id, a_pk, b_id = houses
    secret = await _add_member(a, a_pk, "sam", text="Secret plans")
    await a[db_key].enqueue(
        "INSERT INTO peer_user_visibility(instance_id, user_id, visible) VALUES(?,?,0)",
        (b_id, secret),
    )
    await _add_member(a, a_pk, "alice", text="Here")

    payload = await _pair_and_deliver(monkeypatch, a, b, a_id, b_id)

    assert "Secret plans" not in json.dumps(payload, ensure_ascii=False)
    assert await _status_on(b, secret) is None
