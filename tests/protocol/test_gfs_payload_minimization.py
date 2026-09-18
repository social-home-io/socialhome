"""§27.9 release blocker: the GFS learns neither the content nor the
*relaying household* of a public/global space event.

This exercises the LIVE relay path end to end with real crypto and no
mocked verification:

    SpacePublicOutbound (real encrypt + authority-sign)
        → GfsConnectionService.publish_space_event (real HTTP body)
            → GfsFederationService.publish_event (real SQLite, real verify)
                → fan-out frame to every subscriber

and the sibling ``space_subscriber_key_handoff`` producer.

Every assertion here is one that FAILS against the pre-change code — the
old sender put ``from_instance`` + a household transport ``signature`` in
the publish body, the old GFS authorized on that identity and excluded the
publisher from the fan-out (so ``from_instance`` reached the frame too).
Each test names the old behaviour it guards.

It replaces an earlier version of this file that asserted properties of a
standalone sealed-sender primitive with no production importer, so the
§27.9 gate was vacuous (that module has since been deleted). The real wire
shape is the one below.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
)
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpacePostCreated
from socialhome.domain.federation import GfsConnection
from socialhome.domain.post import LocationData, Post, PostType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.repositories import SqliteGfsFederationRepo
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_public_outbound import SpacePublicOutbound
from socialhome.services.space_subscriber_key_inbound import (
    SpaceSubscriberKeyInbound,
)
from socialhome.services.space_subscriber_key_outbound import (
    SpaceSubscriberKeyOutbound,
)

pytestmark = pytest.mark.security


_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)

# ─── Distinctive markers ─────────────────────────────────────────────────
#
# Every one of these is a string that MUST NOT appear anywhere in the
# serialized publish body or fan-out frame. They are deliberately unique
# so a substring search over the JSON is a sound leak detector.

SPACE_ID = "sp-minimization"
PUBLISHER_INSTANCE = "publisher-household-distinctive.example"
SUBSCRIBER_INSTANCE_NAME = "subscriber-household-distinctive.example"
AUTHOR_USERNAME = "aurelia-distinctive-author"
POST_TEXT = "distinctive-post-text-tulip-42"
LOCATION_LABEL = "distinctive-location-label-bakery"
POST_ID = "post-minimization-1"


#: The cleartext (GFS-visible) keys of a ``space_post_public`` relay payload,
#: enumerated from the producer (``SpacePublicOutbound``). Set equality is
#: asserted, NOT containment: a future field added to the wire envelope must
#: consciously extend this set — and the reviewer then has to argue why the
#: GFS may see it. ``encrypted_payload`` is the AES-GCM ciphertext of the
#: whole inner (author, content, location, post id); everything else is
#: routing (``space_id``), key selection (``epoch``) or the authenticator
#: the GFS verifies over opaque bytes (``authority_sig`` + its suite tag).
POST_PUBLIC_CLEARTEXT_KEYS: frozenset[str] = frozenset(
    {
        "space_id",
        "epoch",
        "encrypted_payload",
        "authority_sig",
        "authority_sig_suite",
    }
)

#: The cleartext keys of a ``space_subscriber_key_handoff`` relay payload
#: (``SpaceSubscriberKeyOutbound``). ``sealed`` is the X25519-sealed content
#: key. NO identity-bearing field is left on the wire: the producer does not
#: ship ``target_instance_id``, so neither the GFS nor a non-target
#: subscriber learns which household is being onboarded — the **seal itself
#: is the gate** (a receiver that can't ``open_keywrap`` the envelope drops
#: it quietly). Receivers still accept the legacy targeted shape from an
#: older seed-holder; this set pins what THIS build emits.
KEY_HANDOFF_CLEARTEXT_KEYS: frozenset[str] = frozenset(
    {
        "space_id",
        "sealed",
        "authority_sig",
        "authority_sig_suite",
    }
)


# ─── Recording doubles ───────────────────────────────────────────────────


class _RecordingResp:
    def __init__(self) -> None:
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingSession:
    """Records the exact JSON body the household POSTs to ``/gfs/publish``."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _RecordingResp()


class _RecordingWsRegistry:
    """A GFS ws-registry stub that always "delivers", recording the frame."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, instance_id: str, frame: dict) -> bool:
        self.sent.append((instance_id, frame))
        return True


class _CaptureGfs:
    """Captures what a producer hands to ``publish_space_event``."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_space_event(
        self, *, space_id, event_type, payload, from_instance
    ) -> int:
        self.calls.append(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
                "from_instance": from_instance,
            }
        )
        return 1


# ─── Fixtures: the real producers ────────────────────────────────────────


@pytest.fixture
async def household(tmp_dir):
    """A real seed-holding household: SQLite, KEK, space repo, content
    encryption, and both GFS-relay producers wired to a capturing GFS."""
    db = AsyncDatabase(tmp_dir / "hfs.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    space_kp = generate_space_keypair()
    author_user_id = derive_user_id(own_kp.public_key, AUTHOR_USERNAME)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES(?, ?, 'Aurelia', 'active')",
        (author_user_id, AUTHOR_USERNAME),
    )
    kek = KeyManager.from_data_dir(tmp_dir)
    space_repo = SqliteSpaceRepo(db, key_manager=kek)
    crypto = SpaceContentEncryption(
        SqliteSpaceKeyRepo(db), kek, own_instance_id=PUBLISHER_INSTANCE
    )
    await space_repo.save(
        Space(
            id=SPACE_ID,
            name="Minimization",
            owner_instance_id=PUBLISHER_INSTANCE,
            owner_username=AUTHOR_USERNAME,
            identity_public_key=space_kp.public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )
    )
    await space_repo.set_space_seed(SPACE_ID, space_kp.private_key)
    await crypto.initialise_for_space(SPACE_ID)

    bus = EventBus()
    gfs = _CaptureGfs()
    post_producer = SpacePublicOutbound(
        bus=bus,
        space_repo=space_repo,
        space_crypto=crypto,
        user_repo=SqliteUserRepo(db),
        gfs_service=gfs,
    )
    post_producer.attach_identity(
        own_instance_id=PUBLISHER_INSTANCE,
        own_instance_public_key=own_kp.public_key,
        own_identity_seed=own_kp.private_key,
    )
    post_producer.wire()
    key_producer = SpaceSubscriberKeyOutbound(
        space_repo=space_repo,
        space_crypto=crypto,
        gfs_service=gfs,
    )
    key_producer.attach_identity(own_instance_id=PUBLISHER_INSTANCE)
    yield {
        "db": db,
        "bus": bus,
        "gfs": gfs,
        "crypto": crypto,
        "space_pk": space_kp.public_key,
        "author_user_id": author_user_id,
        "key_producer": key_producer,
    }
    await db.shutdown()


@pytest.fixture
async def post_payload(household):
    """The REAL ``space_post_public`` relay payload for a post carrying every
    distinctive marker — produced by the production encrypt + authority-sign
    path, not hand-rolled."""
    post = Post(
        id=POST_ID,
        author=household["author_user_id"],
        type=PostType.TEXT,
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        content=POST_TEXT,
        location=LocationData(lat=52.1234, lon=13.4321, label=LOCATION_LABEL),
    )
    await household["bus"].publish(SpacePostCreated(post=post, space_id=SPACE_ID))
    calls = household["gfs"].calls
    assert len(calls) == 1
    assert calls[0]["event_type"] == AUTHORITY_EVENT_SPACE_POST_PUBLIC
    return calls[0]["payload"]


@pytest.fixture
def subscriber_keys():
    """A real subscriber's identity + key-wrap keypairs (the material the GFS
    serves to the seed-holder in a ``new_subscriber`` frame)."""
    id_kp = generate_identity_keypair()
    kw_kp = generate_x25519_keypair()
    return {
        "id_kp": id_kp,
        "kw_kp": kw_kp,
        "instance_id": derive_instance_id(id_kp.public_key),
        "keywrap_sig": b64url_encode(sign_ed25519(id_kp.private_key, kw_kp.public_key)),
    }


@pytest.fixture
async def key_handoff_payload(household, subscriber_keys):
    """The REAL ``space_subscriber_key_handoff`` payload, built by sealing the
    live content key to a real subscriber key-wrap keypair."""
    await household["key_producer"].handle(
        {
            "type": "new_subscriber",
            "space_id": SPACE_ID,
            "subscriber": {
                "instance_id": subscriber_keys["instance_id"],
                "identity_public_key": subscriber_keys["id_kp"].public_key.hex(),
                "keywrap_public_key": subscriber_keys["kw_kp"].public_key.hex(),
                "kem_suite": "x25519",
                "keywrap_sig": subscriber_keys["keywrap_sig"],
            },
        }
    )
    calls = household["gfs"].calls
    assert len(calls) == 1
    assert calls[0]["event_type"] == AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
    return calls[0]["payload"], subscriber_keys["instance_id"]


# ─── Fixtures: the real GFS ──────────────────────────────────────────────


@pytest.fixture
async def gfs(tmp_dir, household):
    """A real :class:`GfsFederationService` over real SQLite, with the space's
    authority key TOFU-pinned and one subscriber registered."""
    db = AsyncDatabase(
        tmp_dir / "gfs.db", migrations_dir=_GFS_MIGRATIONS, batch_timeout_ms=10
    )
    await db.startup()
    repo = SqliteGfsFederationRepo(db)
    # The owning household is a registered instance (the space row FKs to it).
    # It is NOT what authorizes the relay — the authority signature is.
    await repo.upsert_instance(
        ClientInstance(
            instance_id=PUBLISHER_INSTANCE,
            display_name="Publisher",
            public_key=generate_identity_keypair().public_key.hex(),
            inbox_url="https://pub.example/federation/inbox/x",
            status="active",
            auto_accept=True,
        )
    )
    await repo.upsert_instance(
        ClientInstance(
            instance_id=SUBSCRIBER_INSTANCE_NAME,
            display_name="Sub",
            public_key=generate_identity_keypair().public_key.hex(),
            inbox_url="https://sub.example/federation/inbox/x",
            status="active",
            auto_accept=True,
        )
    )
    await repo.upsert_space(
        GlobalSpace(
            space_id=SPACE_ID,
            owning_instance=PUBLISHER_INSTANCE,
            name="Minimization",
            status="active",
            identity_public_key=household["space_pk"].hex(),
        )
    )
    await repo.add_subscriber(space_id=SPACE_ID, instance_id=SUBSCRIBER_INSTANCE_NAME)
    ws = _RecordingWsRegistry()
    yield GfsFederationService(repo, ws_registry=ws), ws
    await db.shutdown()


# ─── Helpers ─────────────────────────────────────────────────────────────


#: Every marker that must be absent from a serialized publish body / frame.
_SECRETS: tuple[str, ...] = (
    PUBLISHER_INSTANCE,
    SUBSCRIBER_INSTANCE_NAME,
    AUTHOR_USERNAME,
    POST_TEXT,
    LOCATION_LABEL,
    POST_ID,
)


def _assert_no_identity_or_content(blob: str, *, author_user_id: str) -> None:
    """Assert the serialized wire bytes leak no household identity, no author
    identity and no post content.

    ``PUBLISHER_INSTANCE`` is the one the OLD code shipped as
    ``from_instance`` (body and frame alike) — that alone fails this against
    the pre-change sender/GFS.
    """
    for marker in (*_SECRETS, author_user_id):
        assert marker not in blob, f"leaked marker on the wire: {marker!r}"
    # The transport-identity fields themselves must be gone as *keys* too —
    # a future regression could ship ``from_instance: ""`` and pass the
    # substring check above.
    for key in ("from_instance", "signature", "ts"):
        assert f'"{key}"' not in blob, (
            f"identity/transport field back on the wire: {key}"
        )


async def _publish_anonymously(tmp_dir, payload: dict, event_type: str) -> dict:
    """Run *payload* through the REAL household sender and return the exact
    JSON body it POSTed to ``/gfs/publish``."""
    db = AsyncDatabase(tmp_dir / "conn.db", batch_timeout_ms=10)
    await db.startup()
    try:
        repo = SqliteGfsConnectionRepo(db)
        await repo.save(
            GfsConnection(
                id="gfs-1",
                gfs_instance_id="gfs-inst",
                display_name="GFS",
                public_key="ab" * 32,
                inbox_url="https://gfs.example",
                status="active",
                paired_at="2026-01-01T00:00:00+00:00",
            )
        )
        await repo.publish_space(SPACE_ID, "gfs-1")
        session = _RecordingSession()
        svc = GfsConnectionService(repo, http_client=session)
        svc.attach_publish_context(
            space_repo=None,
            own_instance_id=PUBLISHER_INSTANCE,
            own_signing_key=generate_identity_keypair().private_key,
        )
        # Pin the capability: this GFS advertised ``anonymous_publish: true``.
        svc._anon_publish["gfs-1"] = True  # noqa: SLF001
        delivered = await svc.publish_space_event(
            space_id=SPACE_ID,
            event_type=event_type,
            payload=payload,
            from_instance=PUBLISHER_INSTANCE,
        )
        assert delivered == 1
        assert len(session.posts) == 1
        url, body = session.posts[0]
        assert url == "https://gfs.example/gfs/publish"
        return body
    finally:
        await db.shutdown()


# ─── The publish body the household sends ────────────────────────────────


async def test_publish_body_is_exactly_the_three_anonymous_keys(tmp_dir, post_payload):
    """The household POSTs ``{space_id, event_type, payload}`` and nothing
    else. Guards: the old sender added ``from_instance`` + a household
    transport ``signature`` to every publish body."""
    body = await _publish_anonymously(
        tmp_dir, post_payload, AUTHORITY_EVENT_SPACE_POST_PUBLIC
    )
    assert set(body) == {"space_id", "event_type", "payload"}


async def test_publish_body_leaks_no_identity_or_content(
    tmp_dir, household, post_payload
):
    """No marker — publisher id, subscriber id, author username/user id, post
    text, location label — survives into the serialized body. Guards: the old
    body named the relaying household in the clear."""
    body = await _publish_anonymously(
        tmp_dir, post_payload, AUTHORITY_EVENT_SPACE_POST_PUBLIC
    )
    _assert_no_identity_or_content(
        json.dumps(body), author_user_id=household["author_user_id"]
    )


async def test_relay_payload_cleartext_keys_are_exactly_the_routing_set(post_payload):
    """Set equality, not containment: a new cleartext field on the relay
    envelope must consciously extend :data:`POST_PUBLIC_CLEARTEXT_KEYS`."""
    assert set(post_payload) == POST_PUBLIC_CLEARTEXT_KEYS


async def test_routing_fields_remain_in_clear(post_payload):
    """``space_id`` + ``epoch`` stay plaintext — the GFS routes on the first
    and the receiver selects the content key with the second."""
    assert post_payload["space_id"] == SPACE_ID
    assert isinstance(post_payload["epoch"], int)


# ─── The frame the GFS fans out ──────────────────────────────────────────


async def test_gfs_fanout_frame_is_identity_free(gfs, household, post_payload):
    """The GFS accepts the identity-free body on the authority signature alone
    and fans out a 4-key frame carrying no household identity.

    Guards two old behaviours at once: the old GFS REQUIRED ``from_instance``
    to authorize, and it copied that id into the frame it pushed."""
    svc, ws = gfs
    delivered = await svc.publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, post_payload
    )
    assert delivered == [SUBSCRIBER_INSTANCE_NAME]
    assert len(ws.sent) == 1
    target, frame = ws.sent[0]
    assert target == SUBSCRIBER_INSTANCE_NAME
    assert set(frame) == {"type", "space_id", "event_type", "payload"}
    assert frame["type"] == "relay"
    _assert_no_identity_or_content(
        json.dumps(frame), author_user_id=household["author_user_id"]
    )


async def test_gfs_forwards_the_payload_byte_identical(gfs, post_payload):
    """The GFS is a pipe for the payload: it never re-encodes, annotates or
    strips a field of the opaque envelope."""
    svc, ws = gfs
    await svc.publish_event(SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, post_payload)
    _target, frame = ws.sent[0]
    assert frame["payload"] == post_payload
    assert json.dumps(frame["payload"], sort_keys=True) == json.dumps(
        post_payload, sort_keys=True
    )


async def test_gfs_relays_to_every_subscriber_including_the_publisher(
    gfs, post_payload
):
    """A publisher that also subscribes still receives the relay — the GFS can
    no longer exclude it, because it no longer knows who published. Guards: the
    old GFS skipped ``from_instance`` in the fan-out."""
    svc, ws = gfs
    await svc._repo.add_subscriber(  # noqa: SLF001
        space_id=SPACE_ID, instance_id=PUBLISHER_INSTANCE
    )
    delivered = await svc.publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, post_payload
    )
    assert sorted(delivered) == sorted([PUBLISHER_INSTANCE, SUBSCRIBER_INSTANCE_NAME])


# ─── What the GFS actually verifies ──────────────────────────────────────


async def test_gfs_verifies_only_the_authority_signature(gfs, post_payload):
    """A mutated ``authority_sig`` is rejected — the space-authority signature
    over the opaque payload is the ONE authenticator."""
    svc, _ws = gfs
    tampered = dict(post_payload)
    sig = tampered["authority_sig"]
    # Flip one base64url character (keeping the length/alphabet valid).
    tampered["authority_sig"] = ("B" if sig[0] != "B" else "C") + sig[1:]
    with pytest.raises(PermissionError):
        await svc.publish_event(SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, tampered)


async def test_gfs_accepts_a_body_with_no_legacy_identity_fields(gfs, post_payload):
    """No ``from_instance``, no ``signature`` — accepted. Guards: the old GFS
    raised ``PermissionError`` on an unidentified publish."""
    svc, _ws = gfs
    delivered = await svc.publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, post_payload
    )
    assert delivered == [SUBSCRIBER_INSTANCE_NAME]


async def test_gfs_rejects_a_payload_with_no_authority_signature(gfs, post_payload):
    """Dropping the authority sig removes the only authenticator → rejected.
    Nothing else in the request can stand in for it."""
    svc, _ws = gfs
    stripped = {k: v for k, v in post_payload.items() if k != "authority_sig"}
    with pytest.raises(PermissionError):
        await svc.publish_event(SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, stripped)


# ─── The subscriber key handoff ──────────────────────────────────────────


async def test_key_handoff_cleartext_keys_are_exactly_the_documented_set(
    key_handoff_payload,
):
    """Set equality on the handoff envelope's visible keys."""
    payload, _target = key_handoff_payload
    assert set(payload) == KEY_HANDOFF_CLEARTEXT_KEYS
    assert set(payload["sealed"]) == {"kem_suite", "eph_pk", "ciphertext"}


async def test_key_handoff_carries_no_identity_bearing_field(
    tmp_dir, household, key_handoff_payload
):
    """The handoff wire carries NO household identity at all — not the
    relaying household, not the target subscriber, not a space member.

    Guards two behaviours at once: the old body named the *publisher*, and
    until this change it named the *target* in the clear, so the GFS and every
    other subscriber learned which household was being onboarded. Receivers
    already treat the seal itself as the gate, so the producer simply stops
    sending it.
    """
    payload, target = key_handoff_payload
    body = await _publish_anonymously(
        tmp_dir, payload, AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
    )
    assert set(body) == {"space_id", "event_type", "payload"}
    blob = json.dumps(body)
    _assert_no_identity_or_content(blob, author_user_id=household["author_user_id"])
    # The target household is gone from the wire — as a value AND as a key
    # (a regression shipping ``target_instance_id: ""`` would pass a pure
    # substring check on the value).
    assert target not in blob
    assert "instance_id" not in blob


async def test_key_handoff_relays_through_the_gfs_identity_free(
    gfs, household, key_handoff_payload
):
    """The handoff takes the same anonymous authority-only path as a post."""
    svc, ws = gfs
    payload, _target = key_handoff_payload
    delivered = await svc.publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF, payload
    )
    assert delivered == [SUBSCRIBER_INSTANCE_NAME]
    _target_iid, frame = ws.sent[0]
    assert set(frame) == {"type", "space_id", "event_type", "payload"}
    assert frame["payload"] == payload
    assert PUBLISHER_INSTANCE not in json.dumps(frame)


# ─── End-to-end: seal → GFS → unseal, with no household id on the wire ────


async def _make_subscriber_household(
    tmp_dir, *, name: str, space_public_key: bytes, keywrap_private_key: bytes
):
    """Boot a real subscriber household: its own SQLite + KEK, a mirrored
    (seedless) space row pinning the space authority key, and the production
    :class:`SpaceSubscriberKeyInbound` wired to its key-wrap private key."""
    data_dir = tmp_dir / name
    data_dir.mkdir()
    db = AsyncDatabase(data_dir / "hfs.db", batch_timeout_ms=10)
    await db.startup()
    kek = KeyManager.from_data_dir(data_dir)
    space_repo = SqliteSpaceRepo(db, key_manager=kek)
    instance_id = f"{name}-household.example"
    crypto = SpaceContentEncryption(
        SqliteSpaceKeyRepo(db), kek, own_instance_id=instance_id
    )
    await space_repo.save(
        Space(
            id=SPACE_ID,
            name="Minimization",
            owner_instance_id=PUBLISHER_INSTANCE,
            owner_username=AUTHOR_USERNAME,
            identity_public_key=space_public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )
    )
    inbound = SpaceSubscriberKeyInbound(space_repo=space_repo, space_crypto=crypto)
    inbound.attach_identity(
        own_instance_id=instance_id,
        keywrap_private_key=keywrap_private_key,
    )
    return db, crypto, inbound


async def test_e2e_untargeted_handoff_reaches_only_the_household_it_was_sealed_to(
    tmp_dir, gfs, household, subscriber_keys, key_handoff_payload, caplog
):
    """Full path with real crypto on both ends: ``new_subscriber`` → seal →
    ``publish_space_event`` → the real GFS ``publish_event`` → fan-out frame →
    :meth:`SpaceSubscriberKeyInbound.handle`.

    The frame names no household at all, so the GFS fans the SAME bytes to
    every subscriber. The target unseals and imports; a bystander subscriber
    fails the unseal and drops quietly at DEBUG.
    """
    caplog.set_level(logging.DEBUG, logger="socialhome")
    svc, ws = gfs
    payload, target_instance_id = key_handoff_payload

    delivered = await svc.publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF, payload
    )
    assert delivered == [SUBSCRIBER_INSTANCE_NAME]
    _iid, frame = ws.sent[0]
    blob = json.dumps(frame)
    _assert_no_identity_or_content(blob, author_user_id=household["author_user_id"])
    assert target_instance_id not in blob
    assert "instance_id" not in blob

    target_db, target_crypto, target_inbound = await _make_subscriber_household(
        tmp_dir,
        name="target",
        space_public_key=household["space_pk"],
        keywrap_private_key=subscriber_keys["kw_kp"].private_key,
    )
    (
        bystander_db,
        bystander_crypto,
        bystander_inbound,
    ) = await _make_subscriber_household(
        tmp_dir,
        name="bystander",
        space_public_key=household["space_pk"],
        keywrap_private_key=generate_x25519_keypair().private_key,
    )
    try:
        await target_inbound.handle(frame)
        await bystander_inbound.handle(frame)

        # The target imported the host's live content key…
        host_key = await household["crypto"].export_current_key(SPACE_ID)
        assert host_key is not None
        assert await target_crypto.export_current_key(SPACE_ID) == host_key
        # …and the bystander, given the identical bytes, got nothing.
        assert await bystander_crypto.export_current_key(SPACE_ID) is None
        # The bystander's drop is quiet — every subscriber sees every other
        # subscriber's handoff, so anything above DEBUG would be pure noise.
        noisy = [
            r
            for r in caplog.records
            if r.name.startswith("socialhome.services.space_subscriber_key_inbound")
            and r.levelno > logging.INFO
        ]
        assert noisy == [], [(r.levelname, r.getMessage()) for r in noisy]
    finally:
        await target_db.shutdown()
        await bystander_db.shutdown()
