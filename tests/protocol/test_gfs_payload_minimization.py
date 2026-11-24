"""§27.9 release blocker: the GFS learns neither the content nor the
*relaying household* of a public/global space event.

This exercises the LIVE relay path end to end with real crypto and no
mocked verification:

    SpacePublicOutbound (real encrypt + authority-sign)
        → GfsConnectionService.publish_space_event (real HTTP body)
            → GfsFederationService.publish_event (real SQLite, real verify)
                → fan-out frame to every subscriber

The sibling ``space_subscriber_key_handoff`` producer is gone (v_96: a
follower is a member and gets the key by federation); a guard below proves
no household module produces or opens one any more.

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

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
)
from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpacePostCreated
from socialhome.domain.federation import GfsConnection
from socialhome.domain.link_preview import LinkPreview
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
#: The author-built link card rides inside the encrypted inner too — its
#: URL, title and image reference must be as invisible to the GFS as the text.
PREVIEW_URL = "https://distinctive-preview-site.example/article"
PREVIEW_TITLE = "distinctive-preview-title-heron"
PREVIEW_IMAGE = "api/media/distinctivepreviewimage.webp"
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

# ─── Recording doubles ───────────────────────────────────────────────────


class _RecordingResp:
    def __init__(self) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}

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
        self, *, space_id, event_type, payload, skip=None, compressed_payloads=None
    ) -> int:
        self.calls.append(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
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
            features=SpaceFeatures(allow_subscribers=True),
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
    yield {
        "db": db,
        "bus": bus,
        "gfs": gfs,
        "crypto": crypto,
        "space_pk": space_kp.public_key,
        "author_user_id": author_user_id,
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
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        content=POST_TEXT,
        location=LocationData(lat=52.1234, lon=13.4321, label=LOCATION_LABEL),
        link_preview=LinkPreview(
            url=PREVIEW_URL, title=PREVIEW_TITLE, thumbnail_url=PREVIEW_IMAGE
        ),
    )
    await household["bus"].publish(SpacePostCreated(post=post, space_id=SPACE_ID))
    calls = household["gfs"].calls
    assert len(calls) == 1
    assert calls[0]["event_type"] == AUTHORITY_EVENT_SPACE_POST_PUBLIC
    return calls[0]["payload"]


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
            status="active",
            auto_accept=True,
        )
    )
    await repo.upsert_instance(
        ClientInstance(
            instance_id=SUBSCRIBER_INSTANCE_NAME,
            display_name="Sub",
            public_key=generate_identity_keypair().public_key.hex(),
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
    PREVIEW_URL,
    "distinctive-preview-site",
    PREVIEW_TITLE,
    PREVIEW_IMAGE,
    "distinctivepreviewimage",
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
        svc = GfsConnectionService(repo, http_client=session, publish_client=session)
        svc.attach_publish_context(
            space_repo=None,
            own_instance_id=PUBLISHER_INSTANCE,
            own_signing_key=generate_identity_keypair().private_key,
        )
        # Pin the capability: this GFS advertised ``anonymous_publish: true``.
        svc._anon_publish["gfs-1"] = True
        delivered = await svc.publish_space_event(
            space_id=SPACE_ID,
            event_type=event_type,
            payload=payload,
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


class _CapabilitySession:
    """A GFS whose ``/gfs/info`` is in one of the capability states a
    household can meet: ``old`` (no capability block), ``stripped`` (the
    bare flag without its signature), ``down`` (unreachable, then back as
    ``old``). Records every ``/gfs/publish`` body."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.posts: list[dict] = []

    def get(self, url, **_kw):
        if self.state == "down":
            raise aiohttp.ClientError("gfs down")
        info = {"server_name": "GFS"}
        if self.state == "stripped":
            info["anonymous_publish"] = True
        return _InfoResp(info)

    def post(self, url, *, json=None, **_kw):
        self.posts.append(json or {})
        return _RecordingResp()


class _InfoResp(_RecordingResp):
    def __init__(self, body: dict) -> None:
        super().__init__()
        self._body = body

    async def json(self, **_kw):
        return self._body


_IDENTITY_KEYS = frozenset({"from_instance", "signature", "ts", "instance_id"})


@pytest.mark.parametrize("state", ["old", "stripped", "down", "anonymous"])
async def test_no_publish_body_ever_carries_identity(
    tmp_dir, post_payload, monkeypatch, state
):
    """Whatever the GFS advertises — nothing, a stripped flag, an
    unreachable ``/gfs/info``, or the signed capability — no
    ``/gfs/publish`` body the household sends, first attempt or retry,
    carries ``from_instance``, a household signature or any other identity
    field. There is no identified fallback left to reach."""
    from socialhome.services import gfs_publish_retry

    monkeypatch.setattr(
        gfs_publish_retry, "GFS_PUBLISH_RETRY_BACKOFF_S", (0.0, 0.0, 0.0, 0.0)
    )
    db = AsyncDatabase(tmp_dir / "caps.db", batch_timeout_ms=10)
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
        session = _CapabilitySession(state)
        svc = GfsConnectionService(repo, http_client=session, publish_client=session)
        svc.attach_publish_context(
            space_repo=None,
            own_instance_id=PUBLISHER_INSTANCE,
            own_signing_key=generate_identity_keypair().private_key,
        )
        if state == "anonymous":
            svc._anon_publish["gfs-1"] = True
        await svc.start()
        await svc.publish_space_event(
            space_id=SPACE_ID,
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            payload=post_payload,
        )
        if state == "down":
            session.state = "old"  # back, but still without the capability
            svc._info_failed_at.clear()
        for _ in range(100):
            if not svc._publish_retry._queues:
                break
            await asyncio.sleep(0.01)
        await svc.stop()
    finally:
        await db.shutdown()

    if state == "anonymous":
        assert len(session.posts) == 1
    else:
        assert session.posts == []
    for body in session.posts:
        assert set(body) == {"space_id", "event_type", "payload"}
        assert not _IDENTITY_KEYS & set(body)
        assert PUBLISHER_INSTANCE not in json.dumps(body)


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
    svc, _ws = gfs
    await svc._repo.add_subscriber(space_id=SPACE_ID, instance_id=PUBLISHER_INSTANCE)
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


# ─── The subscriber key handoff is gone (v_96) ────────────────────────────


_PKG = Path(__file__).resolve().parents[2] / "socialhome"


def test_no_household_module_produces_or_opens_a_key_handoff() -> None:
    """The handoff once had to carry no identity on the GFS wire. Since v_96
    no household sends or opens one at all (a follower is a member; the key
    reaches it by federation), which this guard pins: outside the GFS
    (``global_server/``, removed in P7) and the shared signing-domain
    constants (``authority_sig.py``), no module names the event type or
    the removed sender / receiver."""
    banned = (
        "space_subscriber_key_handoff",
        "AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF",
        "space_author_profiles_bundle",
        "AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE",
        "SpaceSubscriberKeyOutbound",
        "SpaceSubscriberKeyInbound",
    )
    hits = []
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG)
        if rel.parts[0] == "global_server" or rel.as_posix() == "authority_sig.py":
            continue
        text = path.read_text(encoding="utf-8")
        hits += [(rel.as_posix(), b) for b in banned if b in text]
    assert hits == []


# ─── v_44: the authority cert names no member and no reason ─────────────


#: Every key the owner-signed authority cert carries — and so everything a
#: connection server learns from a rotation: which space, which owner (it
#: already knows both), which key, and how many rotations happened.
AUTHORITY_CERT_KEYS: frozenset[str] = frozenset(
    {
        "space_id",
        "owner_instance_id",
        "owner_pk",
        "authority_pk",
        "authority_key_suite",
        "key_epoch",
        "issued_at",
        "cert_sig_suite",
        "cert_sig",
    }
)


async def test_publish_after_rotation_carries_only_the_cert_facts(tmp_dir):
    """The owner's publish after a rotation adds exactly one field — the
    cert — and nothing in it (or in the body) names the revoked household,
    any member, or why the key rotated."""
    db = AsyncDatabase(tmp_dir / "rot.db", batch_timeout_ms=10)
    await db.startup()
    try:
        owner = generate_identity_keypair()
        owner_id = derive_instance_id(owner.public_key)
        spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x03" * 32))
        await spaces.save(
            Space(
                id=SPACE_ID,
                name="Rot",
                owner_instance_id=owner_id,
                owner_username="anna",
                identity_public_key="aa" * 32,
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.GLOBAL,
                join_mode=JoinMode.OPEN,
            )
        )
        k2 = generate_space_keypair()
        await spaces.rotate_authority_key(
            SPACE_ID,
            public_key_hex=k2.public_key.hex(),
            seed=k2.private_key,
            key_epoch=1,
        )
        from socialhome.capabilities_sig import sign_capabilities

        gfs_kp = generate_identity_keypair()
        conns = SqliteGfsConnectionRepo(db)
        await conns.save(
            GfsConnection(
                id="g1",
                gfs_instance_id="gfs",
                display_name="GFS",
                public_key=gfs_kp.public_key.hex(),
                inbox_url="https://gfs.example",
                status="active",
                paired_at="2025-01-01T00:00:00+00:00",
            )
        )
        caps = {"anonymous_publish": True, "authority_rotation": True}
        caps_sig, caps_suite = sign_capabilities(gfs_kp.private_key, "gfs", caps)

        class _Session(_RecordingSession):
            def get(self, url, **_kw):
                resp = _RecordingResp()

                async def _json():
                    return {
                        "capabilities": caps,
                        "capabilities_sig": caps_sig,
                        "capabilities_sig_suite": caps_suite,
                    }

                resp.json = _json  # type: ignore[attr-defined]
                return resp

            def post(self, url, *, json=None, **_kw):
                self.posts.append((url, json or {}))
                resp = _RecordingResp()

                async def _json():
                    return {"status": "active"}

                resp.json = _json  # type: ignore[attr-defined]
                return resp

        session = _Session()
        svc = GfsConnectionService(conns, http_client=session)
        svc.attach_publish_context(
            space_repo=spaces,
            own_instance_id=owner_id,
            own_signing_key=owner.private_key,
        )
        await svc.publish_space(SPACE_ID, "g1")
        body = session.posts[-1][1]
        assert set(body["authority_cert"]) == AUTHORITY_CERT_KEYS
        blob = json.dumps(body)
        for marker in (PUBLISHER_INSTANCE, SUBSCRIBER_INSTANCE_NAME, AUTHOR_USERNAME):
            assert marker not in blob
        assert "reason" not in body["authority_cert"]
    finally:
        await db.shutdown()


def test_public_directory_never_serves_the_owner_cert():
    """The unauthenticated listing / detail show the current key and its
    epoch only: the owner's household identity key and the rotation time
    stay inside the GFS."""
    from socialhome.authority_cert import sign_authority_cert
    from socialhome.global_server.routes.relay import public_space_dict

    owner = generate_identity_keypair()
    owner_id = derive_instance_id(owner.public_key)
    k2 = generate_space_keypair()
    cert = sign_authority_cert(
        space_id=SPACE_ID,
        owner_instance_id=owner_id,
        owner_seed=owner.private_key,
        owner_pk_hex=owner.public_key.hex(),
        authority_pk_hex=k2.public_key.hex(),
        key_epoch=1790000000,
    )
    body = public_space_dict(
        GlobalSpace(
            space_id=SPACE_ID,
            owning_instance=owner_id,
            identity_public_key=k2.public_key.hex(),
            authority_cert=cert,
            authority_rotation_seq=3,
        )
    )
    blob = json.dumps(body)
    assert "authority_cert" not in body
    assert owner.public_key.hex() not in blob
    assert cert["issued_at"] not in blob
    assert cert["cert_sig"] not in blob
    # No wall-clock epoch (it would date the revocation) — only the GFS's
    # own re-pin counter.
    assert "authority_key_epoch" not in body
    assert "1790000000" not in blob
    assert body["authority_rotation_seq"] == 3
    assert body["identity_public_key"] == k2.public_key.hex()
