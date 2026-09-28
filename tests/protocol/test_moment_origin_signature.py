"""Release-blocker protocol tests: relayed moments carry their origin's signature.

Marked ``@pytest.mark.security``.

A moment travels up to three hops, and every relay re-sends it under its
own envelope signature, so the §24.11 pipeline only proves who relayed it.
The rule these tests encode, against the real application registry and
SQLite:

    A relayed ``MOMENT_CREATED`` / ``MOMENT_DELETED`` (sender is not the
    claimed origin) is applied and relayed onward only when it carries a
    valid signature by the origin household's identity key — the key this
    household pins for it, or a shipped key that derives to the origin's
    instance id. An unsigned relay is accepted only from an origin this
    household knows to run a build older than the signature (legacy
    window). Unknown suites are refused. Direct deliveries are bound by the
    envelope signature and need no origin signature.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation.federation_service import FederationService
from socialhome.federation.moment_origin import sign_moment_origin

pytestmark = pytest.mark.security

FET = FederationEventType
NEW = FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE

ORIGIN_KEY = generate_identity_keypair()
RELAY_KEY = generate_identity_keypair()
FOF_KEY = generate_identity_keypair()  # friend-of-friend: never paired with us
ORIGIN = "peer-origin"  # paired; its key is pinned
RELAY = "peer-relay"  # paired; delivers the relayed moments
ONWARD = "peer-onward"  # paired; observes onward relay
FOF = derive_instance_id(FOF_KEY.public_key)
EXPIRES = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()


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


async def _seed_peer(db, instance_id: str, pk: bytes, proto_version: int) -> None:
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
            proto_version,
        ),
    )


async def _seed_user(db, user_id: str, instance_id: str) -> None:
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, user_id, user_id),
    )


async def _seed_moment(db, moment_id: str, author: str, origin: str) -> None:
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at) VALUES(?,?,?,?,?)",
        (moment_id, author, f"by {author}", origin, EXPIRES),
    )


def _env_factory(origin_version: int):
    @pytest.fixture
    async def _env(aiohttp_client, tmp_dir, monkeypatch):
        app = create_app(_config(tmp_dir))
        await aiohttp_client(app)
        db = app[db_key]
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            ("anna", "u-anna", "Anna"),
        )
        await _seed_peer(db, ORIGIN, ORIGIN_KEY.public_key, origin_version)
        await _seed_peer(db, RELAY, RELAY_KEY.public_key, NEW)
        await _seed_peer(db, ONWARD, generate_identity_keypair().public_key, NEW)
        await _seed_user(db, "u-olga", ORIGIN)
        await _seed_user(db, "u-rita", RELAY)
        await _seed_moment(db, "m-olga", "u-olga", ORIGIN)
        sent: list[tuple[str, FederationEventType, dict]] = []

        async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
            sent.append((to_instance_id, event_type, payload))

        monkeypatch.setattr(FederationService, "send_event", _record_send)
        return app, db, sent

    return _env


env = _env_factory(NEW)
legacy_env = _env_factory(NEW - 1)


async def _moments(db) -> dict[str, tuple]:
    rows = await db.fetchall(
        "SELECT id, author_user_id, content, origin_instance_id FROM moments", ()
    )
    return {r[0]: tuple(r) for r in rows}


async def _send(app, event_type, payload, *, from_instance=RELAY) -> None:
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


def _create(moment_id="m-new", author="u-olga", origin=ORIGIN, **extra) -> dict:
    return {
        "moment_id": moment_id,
        "author_user_id": author,
        "origin_instance_id": origin,
        "content": "hello from the origin",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "expires_at": EXPIRES,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "hop_count": 1,
        **extra,
    }


def _delete(moment_id="m-olga", author="u-olga", origin=ORIGIN) -> dict:
    return {
        "moment_id": moment_id,
        "author_user_id": author,
        "origin_instance_id": origin,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "hop_count": 1,
    }


def _signed(event_type, payload, key=ORIGIN_KEY, *, hop_count=2) -> dict:
    """Sign as the origin would (hop 1), then bump the hop like a relay."""
    signed = sign_moment_origin(
        seed=key.private_key,
        identity_pk=key.public_key,
        event_type=event_type,
        payload=payload,
    )
    return {**signed, "hop_count": hop_count}


def _relayed(sent) -> list[tuple[str, FederationEventType, dict]]:
    return [s for s in sent if s[0] == ONWARD]


# ── Refused: forged relays change nothing and travel nowhere ─────────────


def _tampered() -> dict:
    return {**_signed(FET.MOMENT_CREATED, _create()), "content": "not what she said"}


def _unknown_suite() -> dict:
    return {**_signed(FET.MOMENT_CREATED, _create()), "origin_sig_suite": "rot13"}


def _no_suite() -> dict:
    p = _signed(FET.MOMENT_CREATED, _create())
    p.pop("origin_sig_suite")
    return p


def _pinned_key_mismatch() -> dict:
    # Signed by some key, shipping that key — but we pin ORIGIN's key.
    return _signed(FET.MOMENT_CREATED, _create(), key=RELAY_KEY)


def _fof_key_does_not_derive() -> dict:
    # An unpaired origin id whose shipped key is not the one it derives from.
    return _signed(FET.MOMENT_CREATED, _create(author="u-fay", origin=FOF), RELAY_KEY)


def _fof_unsigned() -> dict:
    return _create(author="u-fay", origin=FOF, hop_count=2)


def _create_sig_as_delete() -> dict:
    signed = _signed(FET.MOMENT_CREATED, _create(moment_id="m-olga"))
    return {k: signed[k] for k in (*_delete(), "origin_sig", "origin_sig_suite")}


_FORGED = [
    pytest.param(
        FET.MOMENT_CREATED, lambda: _create(hop_count=2), id="create unsigned"
    ),
    pytest.param(FET.MOMENT_CREATED, _tampered, id="create content tampered"),
    pytest.param(FET.MOMENT_CREATED, _pinned_key_mismatch, id="create wrong key"),
    pytest.param(FET.MOMENT_CREATED, _unknown_suite, id="create unknown suite"),
    pytest.param(FET.MOMENT_CREATED, _no_suite, id="create suite stripped"),
    pytest.param(
        FET.MOMENT_CREATED, _fof_key_does_not_derive, id="unpaired origin wrong key"
    ),
    pytest.param(FET.MOMENT_CREATED, _fof_unsigned, id="unpaired origin unsigned"),
    pytest.param(FET.MOMENT_DELETED, lambda: _delete(), id="delete unsigned"),
    pytest.param(
        FET.MOMENT_DELETED,
        lambda: _signed(FET.MOMENT_DELETED, _delete(), key=RELAY_KEY),
        id="delete wrong key",
    ),
    pytest.param(
        FET.MOMENT_DELETED, _create_sig_as_delete, id="create signature as delete"
    ),
]


@pytest.mark.parametrize(("event_type", "build"), _FORGED)
async def test_forged_relayed_moment_is_refused_and_not_relayed(env, event_type, build):
    app, db, sent = env
    before = await _moments(db)
    await _send(app, event_type, build())
    assert await _moments(db) == before
    assert sent == []


# ── Accepted: genuine relays land and travel on verbatim ────────────────


async def test_genuine_relayed_moment_is_stored_and_relayed_verbatim(env):
    app, db, sent = env
    payload = _signed(FET.MOMENT_CREATED, _create())
    await _send(app, FET.MOMENT_CREATED, payload)
    assert (await _moments(db))["m-new"] == (
        "m-new",
        "u-olga",
        "hello from the origin",
        ORIGIN,
    )
    onward = _relayed(sent)
    assert [s[1] for s in onward] == [FET.MOMENT_CREATED]
    forwarded = onward[0][2]
    assert forwarded["hop_count"] == 3
    for field in ("origin_sig", "origin_sig_suite", "origin_identity_pk"):
        assert forwarded[field] == payload[field]


async def test_relayed_moment_from_unpaired_origin_verifies_by_derivation(env):
    app, db, sent = env
    payload = _signed(FET.MOMENT_CREATED, _create(author="u-fay", origin=FOF), FOF_KEY)
    await _send(app, FET.MOMENT_CREATED, payload)
    assert (await _moments(db))["m-new"][3] == FOF
    assert [s[1] for s in _relayed(sent)] == [FET.MOMENT_CREATED]


async def test_genuine_relayed_origin_delete_removes_the_moment(env):
    app, db, sent = env
    await _send(app, FET.MOMENT_DELETED, _signed(FET.MOMENT_DELETED, _delete()))
    assert "m-olga" not in await _moments(db)
    assert [s[1] for s in _relayed(sent)] == [FET.MOMENT_DELETED]


async def test_direct_delivery_from_the_origin_needs_no_origin_signature(env):
    app, db, _ = env
    await _send(app, FET.MOMENT_CREATED, _create(), from_instance=ORIGIN)
    await _send(app, FET.MOMENT_DELETED, _delete(), from_instance=ORIGIN)
    moments = await _moments(db)
    assert "m-new" in moments
    assert "m-olga" not in moments


# ── Legacy window: origins known to run an older build ──────────────────


async def test_unsigned_relay_from_a_legacy_origin_is_accepted(legacy_env):
    app, db, sent = legacy_env
    await _send(app, FET.MOMENT_CREATED, _create(hop_count=2))
    await _send(app, FET.MOMENT_DELETED, _delete())
    moments = await _moments(db)
    assert "m-new" in moments
    assert "m-olga" not in moments
    assert [s[1] for s in _relayed(sent)] == [FET.MOMENT_CREATED, FET.MOMENT_DELETED]


async def test_a_legacy_origin_signature_must_still_verify(legacy_env):
    """The legacy window covers a MISSING signature, never a bad one."""
    app, db, sent = legacy_env
    before = await _moments(db)
    await _send(app, FET.MOMENT_CREATED, _tampered())
    await _send(app, FET.MOMENT_CREATED, _unknown_suite())
    assert await _moments(db) == before
    assert sent == []
