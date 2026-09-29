"""Release-blocker protocol tests: a ``no_relay`` moment stays one hop.

Marked ``@pytest.mark.security``.

A protected account's moments reach its directly paired households and no
further (§CP.R): its household marks them ``no_relay`` inside the origin
signature (v_38, :mod:`socialhome.federation.moment_origin`), so a relay can
neither strip the mark nor forge it. Against the real application registry
and SQLite:

* a direct ``no_relay`` delivery (sender == origin) is stored, never relayed;
* a ``no_relay`` moment arriving via a relay is refused — not stored, not
  relayed onward;
* a relay that strips ``no_relay`` breaks the origin signature, so the
  moment is refused too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import OURS
from socialhome.federation.federation_service import FederationService
from socialhome.federation.moment_origin import NO_RELAY_FIELD, sign_moment_origin

pytestmark = pytest.mark.security

FET = FederationEventType
ORIGIN_KEY = generate_identity_keypair()
ORIGIN = "peer-origin"
RELAY = "peer-relay"
ONWARD = "peer-onward"
EXPIRES = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "norelay.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, pk: bytes) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            pk.hex(),
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
            OURS,
        ),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    await _seed_peer(db, ORIGIN, ORIGIN_KEY.public_key)
    await _seed_peer(db, RELAY, generate_identity_keypair().public_key)
    await _seed_peer(db, ONWARD, generate_identity_keypair().public_key)
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-mia', ?, 'mia', 'Mia')",
        (ORIGIN,),
    )
    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    return app, db, sent


async def _deliver(app, payload, *, from_instance) -> None:
    for handler in app[federation_service_key]._event_registry.handlers_for(
        FET.MOMENT_CREATED
    ):
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=FET.MOMENT_CREATED,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


def _no_relay_moment(moment_id: str, *, hop_count: int) -> dict:
    payload = {
        "moment_id": moment_id,
        "author_user_id": "u-mia",
        "origin_instance_id": ORIGIN,
        "content": "only for my friends",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "expires_at": EXPIRES,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        NO_RELAY_FIELD: True,
        "hop_count": 1,
    }
    signed = sign_moment_origin(
        seed=ORIGIN_KEY.private_key,
        identity_pk=ORIGIN_KEY.public_key,
        event_type=FET.MOMENT_CREATED,
        payload=payload,
    )
    return {**signed, "hop_count": hop_count}


async def _stored(db, moment_id: str) -> bool:
    return (
        await db.fetchone("SELECT 1 FROM moments WHERE id=?", (moment_id,))
    ) is not None


def _relayed(sent, moment_id: str) -> list:
    return [s for s in sent if s[2].get("moment_id") == moment_id]


async def test_direct_no_relay_moment_is_stored_but_never_relayed(env):
    app, db, sent = env
    await _deliver(app, _no_relay_moment("m-direct", hop_count=1), from_instance=ORIGIN)
    assert await _stored(db, "m-direct")
    assert _relayed(sent, "m-direct") == []


async def test_relayed_no_relay_moment_is_refused(env):
    app, db, sent = env
    await _deliver(app, _no_relay_moment("m-hop", hop_count=2), from_instance=RELAY)
    assert not await _stored(db, "m-hop")
    assert _relayed(sent, "m-hop") == []


async def test_a_relay_cannot_strip_no_relay(env):
    app, db, sent = env
    stripped = _no_relay_moment("m-strip", hop_count=2)
    stripped.pop(NO_RELAY_FIELD)
    await _deliver(app, stripped, from_instance=RELAY)
    assert not await _stored(db, "m-strip")
    assert _relayed(sent, "m-strip") == []
