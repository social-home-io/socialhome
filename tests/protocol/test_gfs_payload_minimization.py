"""§27.9 release blocker: the GFS learns neither the content nor the
*relaying household* of a space-keyed relay event — GFS side.

Since v_96 the household no longer sends the space-keyed relay
(``POST /gfs/publish``, ``space_post_public``): a global space's content and
its authority items ride the opaque channel, whose blindness is pinned by
``test_gfs_global_channel_blind`` / ``test_gfs_private_channel_blind``, and
the household-sender removal by ``test_gfs_multi_listing_blind``. What stays
here, until the GFS drops the legacy route, is the GFS side of it, driven
with real crypto and no mocked verification:

    an authority-signed, encrypted ``space_post_public`` envelope
        → GfsFederationService.publish_event (real SQLite, real verify)
            → fan-out frame to every subscriber

— the frame is identity-free and byte-identical, the publisher is not
excluded, only the authority signature authorizes. Plus two guards: no
household module produces or opens a subscriber key handoff (v_96), and the
v_44 authority cert names no member and no reason.

Dropped with the household sender (v_96): the publish-body checks
(``{space_id, event_type, payload}`` only, never an identity field whatever
the GFS advertises, no marker in the body) and the producer's cleartext-key
set — there is no household publish body or producer any more.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.domain.space_item import pad_json_object
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.repositories import SqliteGfsFederationRepo
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.space_crypto_service import SpaceContentEncryption

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
    """Records the exact JSON body the household POSTs."""

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


# ─── Fixtures: the real producers ────────────────────────────────────────


@pytest.fixture
async def household(tmp_dir):
    """A seed-holding household's space: its authority key pair, its content
    key and an author identity."""
    db = AsyncDatabase(tmp_dir / "hfs.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    space_kp = generate_space_keypair()
    kek = KeyManager.from_data_dir(tmp_dir)
    await SqliteSpaceRepo(db, key_manager=kek).save(
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
    crypto = SpaceContentEncryption(
        SqliteSpaceKeyRepo(db), kek, own_instance_id=PUBLISHER_INSTANCE
    )
    await crypto.initialise_for_space(SPACE_ID)
    yield {
        "db": db,
        "crypto": crypto,
        "space_seed": space_kp.private_key,
        "space_pk": space_kp.public_key,
        "author_user_id": derive_user_id(own_kp.public_key, AUTHOR_USERNAME),
    }
    await db.shutdown()


@pytest.fixture
async def post_payload(household):
    """A ``space_post_public`` envelope for a post carrying every distinctive
    marker: the padded inner encrypted under the real content key, the
    envelope authority-signed with the real space seed (the shape the
    removed host relay produced)."""
    inner = {
        "post_id": POST_ID,
        "space_id": SPACE_ID,
        "author_user_id": household["author_user_id"],
        "author_username": AUTHOR_USERNAME,
        "origin_instance_id": PUBLISHER_INSTANCE,
        "content": POST_TEXT,
        "location": {"lat": 52.1234, "lon": 13.4321, "label": LOCATION_LABEL},
        "link_preview": {
            "url": PREVIEW_URL,
            "title": PREVIEW_TITLE,
            "thumbnail_url": PREVIEW_IMAGE,
        },
    }
    epoch, ct = await household["crypto"].encrypt(SPACE_ID, pad_json_object(inner))
    envelope = {"space_id": SPACE_ID, "epoch": epoch, "encrypted_payload": ct}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=SPACE_ID,
            payload=strip_authority_sig_fields(envelope),
            space_seed=household["space_seed"],
        )
    )
    return envelope


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
