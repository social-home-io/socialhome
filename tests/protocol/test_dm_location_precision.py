"""Protocol test: a DM location never carries raw device precision.

CLAUDE.md GPS rule — coordinates are truncated to 4 decimal places
before *any* storage or transmission. For ``type='location'`` DM
messages that means:

* the sender's DB row and the outbound ``DM_MESSAGE`` (inspected by
  decrypting ``encrypted_payload`` with the pair's session key) carry
  only 4-dp coordinates and a bucketed accuracy;
* the coordinates never appear in plaintext on the envelope;
* a receiver re-rounds whatever a peer sends (a peer that skipped the
  rounding cannot get raw precision into our DB) and refuses a
  malformed pin outright — on first delivery and on an edit re-fan.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.user import RemoteUser
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure import EventBus, KeyManager
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteFederationRepo,
    SqliteOutboxRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.services.dm_service import DmService
from socialhome.services.federation_inbound_service import FederationInboundService
from socialhome.services.user_service import UserService

pytestmark = pytest.mark.security

RAW_LAT = 52.370216789123
RAW_LON = 4.895167912345
RAW_ACCURACY = 3.71
RAW_STRINGS = ("52.370216", "4.895167", "3.71")
SESSION_KEY = b"\x07" * 32

#: ISO-8601 timestamps (``2026-10-04T09:33:13.714350+00:00``). Their
#: seconds can contain a short needle such as "3.71", so the leak scan
#: blanks them; a coordinate never travels as a timestamp.
_ISO_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?"
)


def _scannable(value: object) -> str:
    """JSON text of ``value`` with timestamps blanked, for leak scans."""
    text = value if isinstance(value, str) else json.dumps(value)
    return _ISO_TS.sub("<ts>", text)


class _CapturingClient:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def post(self, url, *, json=None, **kw):
        self.bodies.append(json)
        return _Resp()


class _Resp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
async def sender(tmp_dir):
    db = AsyncDatabase(tmp_dir / "sender.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    own_iid = derive_instance_id(own_kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (own_iid, own_kp.private_key.hex(), own_kp.public_key.hex(), "aa" * 32),
    )
    fed_repo = SqliteFederationRepo(db)
    kek = KeyManager.from_data_dir(tmp_dir)
    bus = EventBus()
    fed = FederationService(
        db=db,
        federation_repo=fed_repo,
        outbox_repo=SqliteOutboxRepo(db),
        key_manager=kek,
        bus=bus,
        own_instance_id=own_iid,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(SESSION_KEY)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://peer.invalid/wh",
        local_inbox_id="wh-peer",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)
    capture = _CapturingClient()
    fed._http_client = capture

    user_repo = SqliteUserRepo(db)
    conv_repo = SqliteConversationRepo(db)
    await UserService(
        user_repo, bus, own_instance_public_key=own_kp.public_key
    ).provision(username="anna", display_name="Anna")
    bob = RemoteUser(
        user_id="uid-bob-remote",
        instance_id=peer.id,
        remote_username="bob",
        display_name="Bob",
    )
    await user_repo.upsert_remote(bob)
    dm_svc = DmService(conv_repo, user_repo, bus, own_instance_id=own_iid)
    dm_svc.attach_federation(fed, fed_repo, own_instance_id=own_iid)
    dm = await dm_svc.create_dm(creator_username="anna", other_user_id=bob.user_id)
    yield dm_svc, dm, conv_repo, capture
    await db.shutdown()


def _decrypted_dm_payloads(capture: _CapturingClient) -> list[dict]:
    enc = FederationEncoder(b"\x00" * 32)
    out = []
    for body in capture.bodies:
        if body.get("event_type") != FederationEventType.DM_MESSAGE.value:
            continue
        # Only the plaintext routing fields are meaningful here: base64
        # ciphertext matches a short string like "3.71" by chance. The
        # decrypted payload is checked by the callers.
        routing = {k: v for k, v in body.items() if k != "encrypted_payload"}
        raw = _scannable(routing)
        for leak in RAW_STRINGS + ("52.3702", "4.8952"):
            assert leak not in raw, f"coordinate {leak} visible on the envelope"
        out.append(
            json.loads(enc.decrypt_payload(body["encrypted_payload"], SESSION_KEY))
        )
    return out


async def test_outbound_location_is_rounded_in_db_and_encrypted_payload(sender):
    dm_svc, dm, conv_repo, capture = sender
    msg = await dm_svc.send_message(
        dm.id,
        sender_username="anna",
        type="location",
        content=json.dumps(
            {"lat": RAW_LAT, "lon": RAW_LON, "accuracy_m": RAW_ACCURACY}
        ),
    )

    stored = await conv_repo.get_message(msg.id)
    assert json.loads(stored.content) == {
        "lat": 52.3702,
        "lon": 4.8952,
        "label": None,
        "accuracy_m": 25,
    }
    for leak in RAW_STRINGS:
        assert leak not in _scannable(stored.content)

    payloads = _decrypted_dm_payloads(capture)
    assert len(payloads) == 1
    plaintext = _scannable(payloads[0])
    for leak in RAW_STRINGS:
        assert leak not in plaintext
    assert payloads[0]["type"] == "location"
    assert json.loads(payloads[0]["content"])["lat"] == 52.3702


async def test_outbound_location_edit_is_rounded(sender):
    dm_svc, dm, conv_repo, capture = sender
    msg = await dm_svc.send_message(
        dm.id, sender_username="anna", type="location", content='{"lat":1,"lon":2}'
    )
    await dm_svc.edit_message(
        msg.id,
        editor_username="anna",
        new_content=json.dumps({"lat": RAW_LAT, "lon": RAW_LON}),
    )
    stored = await conv_repo.get_message(msg.id)
    assert json.loads(stored.content)["lon"] == 4.8952
    edits = _decrypted_dm_payloads(capture)
    assert len(edits) == 2
    for leak in RAW_STRINGS:
        assert leak not in _scannable(edits[1])


# ── Inbound ───────────────────────────────────────────────────────────────


@pytest.fixture
async def receiver(db, bus, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("local", "u-local", "Local"),
    )
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES('peer-a', 'peer-a', ?, 'k1',"
        " 'k2', 'https://peer-a/wh', 'wh-peer-a', 'confirmed', 'manual')",
        ("00" * 32,),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-remote', 'peer-a', 'remote', 'Remote')",
        (),
    )
    repo = SqliteConversationRepo(db)
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=repo,
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=media_dir,
    )
    return svc, repo


def _inbound(message_id: str, content: str, **extra) -> FederationEvent:
    return FederationEvent(
        msg_id=f"env-{message_id}-{len(extra)}",
        event_type=FederationEventType.DM_MESSAGE,
        from_instance="peer-a",
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={
            "conversation_id": "conv-loc",
            "message_id": message_id,
            "sender_user_id": "u-remote",
            "sender_display_name": "Remote",
            "type": "location",
            "content": content,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "recipient_user_ids": ["u-local"],
            **extra,
        },
    )


async def test_inbound_raw_precision_is_rerounded_before_storing(receiver):
    svc, repo = receiver
    await svc._on_dm_message(
        _inbound(
            "m-raw",
            json.dumps(
                {
                    "lat": RAW_LAT,
                    "lon": RAW_LON,
                    "accuracy_m": RAW_ACCURACY,
                    "label": "Dam",
                    "extra": "dropped",
                }
            ),
        )
    )
    stored = await repo.get_message("m-raw")
    assert stored is not None
    assert json.loads(stored.content) == {
        "lat": 52.3702,
        "lon": 4.8952,
        "label": "Dam",
        "accuracy_m": 25,
    }
    # An edit re-fan with raw precision is re-rounded too.
    await svc._on_dm_message(
        _inbound(
            "m-raw",
            json.dumps({"lat": -33.856784123, "lon": 151.215297987}),
            edited_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    edited = await repo.get_message("m-raw")
    assert json.loads(edited.content)["lat"] == -33.8568
    assert "856784" not in edited.content


@pytest.mark.parametrize(
    "content",
    ["meet me at the station", '{"lat": 123, "lon": 4}', '{"lon": 4}', ""],
)
async def test_inbound_malformed_location_is_refused(receiver, content):
    svc, repo = receiver
    await svc._on_dm_message(_inbound("m-bad", content))
    assert await repo.get_message("m-bad") is None
    # Nothing was set up for it either.
    assert await repo.get("conv-loc") is None


def test_leak_scan_ignores_timestamps_that_contain_a_needle():
    """Regression: a payload timestamp like ``…T09:33:13.714350+00:00``
    contains the short needle "3.71" and made the leak scan fail at
    random. Timestamps can never carry a coordinate, so the scan blanks
    them first — while a real coordinate leak still trips it."""
    clean = {"created_at": "2026-10-04T09:33:13.714350+00:00", "type": "location"}
    assert all(leak not in _scannable(clean) for leak in RAW_STRINGS)
    leaky = {"created_at": "2026-10-04T09:33:13.714350+00:00", "accuracy_m": 3.71}
    assert any(leak in _scannable(leaky) for leak in RAW_STRINGS)
