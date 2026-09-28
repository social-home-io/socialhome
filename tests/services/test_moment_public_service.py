"""Smoke tests for the public-Momentum service / repos / inbound handler.

These exercise the registration + follow round-trips and the inbound
verify path. The HTTP round-trip to the GFS is mocked at the service
edge — the real wire path lives behind the persistent WS supervisor
and gets exercised end-to-end in :file:`tests/scenarios/`.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock


from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.domain.events import MomentCreated
from socialhome.federation.owner_bound_id import MOMENT_KIND, mint_owner_bound_id
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.services.moment_public_inbound import MomentPublicInbound

# A 32-byte Ed25519 seed and the matching 32-byte public-key hex
# derived once for the inbound signature-verify tests.
_AUTHOR_SEED = b"\x11" * 32


def _author_pk_hex() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    sk = Ed25519PrivateKey.from_private_bytes(_AUTHOR_SEED)
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _signed_envelope(payload: dict, *, seed: bytes = _AUTHOR_SEED) -> dict:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signed = dict(payload)
    signed["signature"] = b64url_encode(sign_ed25519(seed, canonical))
    return signed


# ── Repos ────────────────────────────────────────────────────────────────


async def test_registration_repo_upsert_then_list_then_default_share(db):
    repo = SqliteMomentPublicRegistrationRepo(db)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    reg = await repo.upsert(user_id="u1", gfs_id="g1", default_share=True)
    assert reg.user_id == "u1" and reg.gfs_id == "g1"
    rows = await repo.list_for_user("u1")
    assert len(rows) == 1 and rows[0].default_share is True
    await repo.set_default_share(user_id="u1", gfs_id="g1", default_share=False)
    after = await repo.get(user_id="u1", gfs_id="g1")
    assert after is not None and after.default_share is False


async def test_follow_repo_round_trip(db):
    repo = SqliteMomentPublicFollowRepo(db)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    follow = await repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    assert follow.followed_user_id == "u-remote"
    pk = await repo.lookup_followed_pk(
        follower_user_id="u1", followed_user_id="u-remote", gfs_id="g1"
    )
    assert pk == _author_pk_hex()
    rows = await repo.list_for_follower("u1")
    assert len(rows) == 1


# ── Inbound ──────────────────────────────────────────────────────────────


async def test_inbound_persists_and_publishes_on_valid_signature(db):
    bus = EventBus()
    captured: list[MomentCreated] = []
    bus.subscribe(MomentCreated, lambda e: captured.append(e))
    follow_repo = SqliteMomentPublicFollowRepo(db)
    from socialhome.repositories.moment_repo import SqliteMomentRepo

    moment_repo = SqliteMomentRepo(db)

    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    await follow_repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    inbound = MomentPublicInbound(
        bus=bus, moment_repo=moment_repo, follow_repo=follow_repo
    )
    envelope = _signed_envelope(
        {
            "moment_id": "m-1",
            "author_user_id": "u-remote",
            "author_username": "bob",
            "author_display_name": "Bob",
            "content": "hello",
            "media_url": None,
            "media_type": None,
            "duration_ms": None,
            "parent_moment_id": None,
            "origin_instance_id": "inst-remote",
            "created_at": "2026-05-06T12:00:00Z",
            "expires_at": "2026-05-07T12:00:00Z",
        }
    )
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": envelope}, gfs_id="g1"
    )
    assert len(captured) == 1
    saved = await moment_repo.get("m-1")
    assert saved is not None
    assert saved.received_via == "gfs"
    assert saved.received_via_gfs_id == "g1"
    assert saved.is_public is True


async def test_inbound_drops_when_signature_does_not_match(db):
    bus = EventBus()
    captured: list[MomentCreated] = []
    bus.subscribe(MomentCreated, lambda e: captured.append(e))
    follow_repo = SqliteMomentPublicFollowRepo(db)
    from socialhome.repositories.moment_repo import SqliteMomentRepo

    moment_repo = SqliteMomentRepo(db)

    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    # Cache the *real* author pk; sign the envelope with a different seed
    # so verification must fail.
    await follow_repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    inbound = MomentPublicInbound(
        bus=bus, moment_repo=moment_repo, follow_repo=follow_repo
    )
    forged = _signed_envelope(
        {
            "moment_id": "m-2",
            "author_user_id": "u-remote",
            "content": "forged",
            "media_url": None,
            "media_type": None,
            "duration_ms": None,
            "parent_moment_id": None,
            "origin_instance_id": "inst-remote",
            "created_at": "2026-05-06T12:00:00Z",
            "expires_at": "2026-05-07T12:00:00Z",
        },
        seed=b"\x42" * 32,  # different signing seed → bad signature
    )
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": forged}, gfs_id="g1"
    )
    assert captured == []
    assert await moment_repo.get("m-2") is None


# ── Outbound (skip — needs the full app fixture; see scenarios) ──────────
# Smoke-only: verify wire() subscribes without exploding.


async def test_outbound_wire_subscribes_to_bus():
    from socialhome.services.moment_public_outbound import MomentPublicOutbound

    bus = EventBus()
    sub = MomentPublicOutbound(
        bus=bus,
        moment_repo=MagicMock(),
        registration_repo=MagicMock(),
        user_repo=MagicMock(),
        gfs_repo=MagicMock(),
    )
    sub.wire()  # should not raise; subscribers are just stored


# ── Inbound delete-path coverage ────────────────────────────────────────


async def test_inbound_delete_removes_local_row_on_valid_signature(db):
    from socialhome.repositories.moment_public_repo import (
        SqliteMomentPublicFollowRepo,
    )
    from socialhome.repositories.moment_repo import SqliteMomentRepo
    from socialhome.domain.events import MomentDeleted
    from socialhome.domain.moment import Moment

    bus = EventBus()
    deletes: list[MomentDeleted] = []
    bus.subscribe(MomentDeleted, lambda e: deletes.append(e))
    follow_repo = SqliteMomentPublicFollowRepo(db)
    moment_repo = SqliteMomentRepo(db)

    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','ff'*32,'https://gfs1.example','active', datetime('now'))"
    )
    await follow_repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    await moment_repo.save(
        Moment(
            id="m-9",
            author_user_id="u-remote",
            content="seed",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="inst-remote",
            created_at="2026-05-06T12:00:00Z",
            expires_at="2026-05-07T12:00:00Z",
            is_public=True,
            received_via="gfs",
            received_via_gfs_id="g1",
        )
    )
    inbound = MomentPublicInbound(
        bus=bus, moment_repo=moment_repo, follow_repo=follow_repo
    )
    envelope = _signed_envelope(
        {
            "moment_id": "m-9",
            "author_user_id": "u-remote",
            "instance_id": "inst-remote",
        }
    )
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": envelope},
        gfs_id="g1",
    )
    assert len(deletes) == 1
    assert await moment_repo.get("m-9") is None


async def test_inbound_delete_rejects_bad_signature(db):
    from socialhome.repositories.moment_public_repo import (
        SqliteMomentPublicFollowRepo,
    )
    from socialhome.repositories.moment_repo import SqliteMomentRepo
    from socialhome.domain.events import MomentDeleted
    from socialhome.domain.moment import Moment

    bus = EventBus()
    deletes: list[MomentDeleted] = []
    bus.subscribe(MomentDeleted, lambda e: deletes.append(e))
    follow_repo = SqliteMomentPublicFollowRepo(db)
    moment_repo = SqliteMomentRepo(db)

    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','ff'*32,'https://gfs1.example','active', datetime('now'))"
    )
    await follow_repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    await moment_repo.save(
        Moment(
            id="m-9",
            author_user_id="u-remote",
            content="seed",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="inst-remote",
            created_at="2026-05-06T12:00:00Z",
            expires_at="2026-05-07T12:00:00Z",
            is_public=True,
            received_via="gfs",
            received_via_gfs_id="g1",
        )
    )
    inbound = MomentPublicInbound(
        bus=bus, moment_repo=moment_repo, follow_repo=follow_repo
    )
    forged = _signed_envelope(
        {"moment_id": "m-9", "author_user_id": "u-remote"},
        seed=b"\x42" * 32,
    )
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": forged},
        gfs_id="g1",
    )
    assert deletes == []
    assert await moment_repo.get("m-9") is not None


async def test_inbound_ignores_unknown_frame_type(db):
    from socialhome.repositories.moment_public_repo import (
        SqliteMomentPublicFollowRepo,
    )
    from socialhome.repositories.moment_repo import SqliteMomentRepo

    bus = EventBus()
    inbound = MomentPublicInbound(
        bus=bus,
        moment_repo=SqliteMomentRepo(db),
        follow_repo=SqliteMomentPublicFollowRepo(db),
    )
    # Should be a no-op (no exception).
    await inbound.handle({"type": "irrelevant", "payload": {}}, gfs_id="g1")


# ── Deletes stick ────────────────────────────────────────────────────────


async def _public_inbound_env(db):
    from socialhome.repositories.moment_repo import SqliteMomentRepo

    bus = EventBus()
    created: list[MomentCreated] = []
    bus.subscribe(MomentCreated, lambda e: created.append(e))
    follow_repo = SqliteMomentPublicFollowRepo(db)
    moment_repo = SqliteMomentRepo(db)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    await follow_repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    inbound = MomentPublicInbound(
        bus=bus, moment_repo=moment_repo, follow_repo=follow_repo
    )
    return inbound, moment_repo, created


def _public_create(moment_id: str, author: str = "u-remote") -> dict:
    return _signed_envelope(
        {
            "moment_id": moment_id,
            "author_user_id": author,
            "content": "hello",
            "media_url": None,
            "media_type": None,
            "duration_ms": None,
            "parent_moment_id": None,
            "origin_instance_id": "inst-remote",
            "created_at": "2026-05-06T12:00:00Z",
            "expires_at": "2099-05-07T12:00:00Z",
        }
    )


async def test_inbound_public_delete_before_create_keeps_it_deleted(db):
    inbound, moment_repo, created = await _public_inbound_env(db)
    delete = _signed_envelope(
        {
            "moment_id": "m-late",
            "author_user_id": "u-remote",
            "origin_instance_id": "inst-remote",
        }
    )
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": delete}, gfs_id="g1"
    )
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create("m-late")},
        gfs_id="g1",
    )
    assert await moment_repo.get("m-late") is None
    assert created == []


async def test_inbound_public_replay_after_delete_is_refused(db):
    inbound, moment_repo, created = await _public_inbound_env(db)
    create = {"type": "incoming_public_moment", "payload": _public_create("m-r")}
    await inbound.handle(create, gfs_id="g1")
    assert await moment_repo.get("m-r") is not None
    delete = _signed_envelope(
        {"moment_id": "m-r", "author_user_id": "u-remote", "instance_id": "inst-remote"}
    )
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": delete}, gfs_id="g1"
    )
    created.clear()
    await inbound.handle(create, gfs_id="g1")
    assert await moment_repo.get("m-r") is None
    assert created == []


async def test_inbound_public_delete_naming_another_author_is_refused(db):
    inbound, moment_repo, _ = await _public_inbound_env(db)
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create("m-bob")},
        gfs_id="g1",
    )
    # Signed by the right key, but claiming a different author for the id.
    await db.enqueue("UPDATE moments SET author_user_id='u-other' WHERE id='m-bob'")
    delete = _signed_envelope({"moment_id": "m-bob", "author_user_id": "u-remote"})
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": delete}, gfs_id="g1"
    )
    assert await moment_repo.get("m-bob") is not None


async def test_inbound_public_claim_of_another_authors_bound_id_is_refused(db):
    """v_36: a moment id commits to its author. A validly signed create or
    delete naming another author's bound id is neither stored nor
    tombstoned, so the real author's moment still lands."""
    inbound, moment_repo, created = await _public_inbound_env(db)
    theirs = mint_owner_bound_id(MOMENT_KIND, space_id="", owner_user_id="u-owner")
    delete = _signed_envelope({"moment_id": theirs, "author_user_id": "u-remote"})
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": delete}, gfs_id="g1"
    )
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create(theirs)},
        gfs_id="g1",
    )
    assert await moment_repo.get(theirs, include_deleted=True) is None
    assert created == []
    own = mint_owner_bound_id(MOMENT_KIND, space_id="", owner_user_id="u-remote")
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create(own)},
        gfs_id="g1",
    )
    assert await moment_repo.get(own) is not None


# ── A stored moment binds its author and origin (household-path parity) ──


async def _stored(moment_repo, moment_id: str):
    return await moment_repo.get(moment_id, include_deleted=True)


async def test_inbound_public_create_naming_another_author_for_a_held_id_is_refused(
    db, caplog
):
    """A legacy id this household already holds keeps its author: a validly
    signed create from somebody else for that id is refused, not upserted."""
    inbound, moment_repo, created = await _public_inbound_env(db)
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create("m-held")},
        gfs_id="g1",
    )
    await db.enqueue("UPDATE moments SET author_user_id='u-first' WHERE id='m-held'")
    created.clear()
    with caplog.at_level(logging.WARNING):
        await inbound.handle(
            {"type": "incoming_public_moment", "payload": _public_create("m-held")},
            gfs_id="g1",
        )
    row = await _stored(moment_repo, "m-held")
    assert row is not None and row.author_user_id == "u-first"
    assert created == []
    assert "belongs to another author" in caplog.text


async def test_inbound_public_create_naming_another_origin_for_a_held_id_is_refused(db):
    inbound, moment_repo, created = await _public_inbound_env(db)
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create("m-o")},
        gfs_id="g1",
    )
    await db.enqueue(
        "UPDATE moments SET origin_instance_id='inst-first', content='orig'"
        " WHERE id='m-o'"
    )
    created.clear()
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create("m-o")},
        gfs_id="g1",
    )
    row = await _stored(moment_repo, "m-o")
    assert row is not None and row.content == "orig"
    assert created == []


async def test_inbound_public_redelivery_from_the_same_author_still_lands(db):
    inbound, moment_repo, created = await _public_inbound_env(db)
    frame = {"type": "incoming_public_moment", "payload": _public_create("m-again")}
    await inbound.handle(frame, gfs_id="g1")
    await inbound.handle(frame, gfs_id="g1")
    assert await moment_repo.get("m-again") is not None
    assert len(created) == 2


async def test_inbound_public_delete_naming_another_origin_is_refused(db):
    """A delete must name the stored origin too — else it is not applied
    and leaves no tombstone."""
    inbound, moment_repo, _ = await _public_inbound_env(db)
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create("m-d")},
        gfs_id="g1",
    )
    for instance in ("inst-elsewhere", None):
        env = {"moment_id": "m-d", "author_user_id": "u-remote"}
        if instance:
            env["instance_id"] = instance
        await inbound.handle(
            {
                "type": "incoming_public_moment_delete",
                "payload": _signed_envelope(env),
            },
            gfs_id="g1",
        )
    row = await _stored(moment_repo, "m-d")
    assert row is not None and row.deleted_at is None


async def test_inbound_public_early_delete_binds_its_origin(db):
    """A delete for a moment not held yet tombstones it under the sending
    household's origin, so the create from that author stays refused."""
    inbound, moment_repo, created = await _public_inbound_env(db)
    delete = _signed_envelope(
        {"moment_id": "m-e", "author_user_id": "u-remote", "instance_id": "inst-remote"}
    )
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": delete}, gfs_id="g1"
    )
    row = await _stored(moment_repo, "m-e")
    assert row is not None and row.origin_instance_id == "inst-remote"
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _public_create("m-e")},
        gfs_id="g1",
    )
    assert await moment_repo.get("m-e") is None
    assert created == []
