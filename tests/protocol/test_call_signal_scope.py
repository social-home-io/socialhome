"""Release-blocker protocol tests: call signals bind to a participant's household.

Marked ``@pytest.mark.security``.

``CALL_*`` signals carry a bare ``call_id`` and the participant they speak
for (``hanger_user``, ``decliner_user``, ``reporter_user``). The rule
these tests encode, against the real application registry and SQLite:

    A household may end a call or report its quality only for its own
    user who is a participant of that call. Never for a local member,
    never for another household's user, never for a call it isn't in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType

pytestmark = pytest.mark.security

FET = FederationEventType

PEER = "peer-bob"
OTHER = "peer-dora"


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "calls.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, user_id: str, username: str) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            "00" * 32,
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, username, username),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    await _seed_peer(db, PEER, "u-bob", "bob")
    await _seed_peer(db, OTHER, "u-dora", "dora")
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('c-ab', 'dm')", ())
    # Anna called Bob; Dora (another household) is not in the call.
    await db.enqueue(
        "INSERT INTO call_sessions(id, conversation_id, initiator_user_id,"
        " callee_user_id, call_type, status, participant_user_ids)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            "call-ab",
            "c-ab",
            "u-anna",
            "u-bob",
            "audio",
            "active",
            json.dumps(["u-anna", "u-bob"]),
        ),
    )
    return app, db


async def _state(db) -> dict[str, list[tuple]]:
    queries = {
        "calls": "SELECT id, status FROM call_sessions ORDER BY id",
        "quality": "SELECT call_id, reporter_user_id FROM call_quality_samples"
        " ORDER BY 1, 2",
    }
    return {
        name: [tuple(r) for r in await db.fetchall(sql, ())]
        for name, sql in queries.items()
    }


async def _send(app, event_type, payload, *, from_instance=PEER) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


_ATTACKS = [
    pytest.param(FET.CALL_HANGUP, "hanger_user", "u-anna", PEER, id="hangup as local"),
    pytest.param(FET.CALL_HANGUP, "hanger_user", "u-bob", OTHER, id="hangup spoofed"),
    pytest.param(FET.CALL_HANGUP, "hanger_user", None, PEER, id="hangup unnamed"),
    pytest.param(FET.CALL_DECLINE, "decliner_user", "u-dora", OTHER, id="outsider"),
    pytest.param(
        FET.CALL_QUALITY, "reporter_user", "u-anna", PEER, id="quality as local"
    ),
    pytest.param(
        FET.CALL_QUALITY, "reporter_user", "u-dora", OTHER, id="quality by outsider"
    ),
]


@pytest.mark.parametrize(("event_type", "field", "user_id", "sender"), _ATTACKS)
async def test_call_signal_outside_its_scope_changes_nothing(
    env, event_type, field, user_id, sender
):
    app, db = env
    before = await _state(db)
    payload: dict = {"call_id": "call-ab", "rtt_ms": 10, "sampled_at": 1}
    if user_id is not None:
        payload[field] = user_id
    await _send(app, event_type, payload, from_instance=sender)
    assert await _state(db) == before


async def test_the_participants_household_reports_and_hangs_up(env):
    app, db = env
    await _send(
        app,
        FET.CALL_QUALITY,
        {"call_id": "call-ab", "reporter_user": "u-bob", "rtt_ms": 10},
    )
    assert ("call-ab", "u-bob") in (await _state(db))["quality"]
    await _send(app, FET.CALL_HANGUP, {"call_id": "call-ab", "hanger_user": "u-bob"})
    assert ("call-ab", "ended") in (await _state(db))["calls"]
