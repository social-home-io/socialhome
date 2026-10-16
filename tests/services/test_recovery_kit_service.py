"""Tests for RecoveryKitService — build + restore of the trust-layer kit.

The kit captures instance_identity / remote_instances / space_keys + the
``.kek_salt`` behind a passphrase so the SAME instance_id can be rebuilt on
fresh hardware. These tests exercise the round-trip, the fail-closed guards
(non-empty target, wrong passphrase, KEK self-test), and the no-identity case.
"""

from __future__ import annotations

import os

import orjson
import pytest

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.recovery_crypto import (
    RecoveryKitError,
    seal_kit,
    unseal_kit,
)
from socialhome.services.recovery_kit_service import (
    RECOVERED_AT_KEY,
    TRUST_TABLES,
    RecoveryKitService,
    RecoveryRestoreError,
)

PASS = "correct horse battery staple"


async def _make_db(data_dir):
    db = AsyncDatabase(data_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    return db


def _write_salt(data_dir) -> None:
    (data_dir / ".kek_salt").write_bytes(os.urandom(KeyManager.KEK_BYTES))


async def _seed_identity(db, km, iid, kp):
    seed = os.urandom(32)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, km.encrypt(seed), kp.public_key.hex(), "aa" * 32),
    )
    return seed


async def _seed_remote_instance(db, km):
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id) VALUES(?,?,?,?,?,?,?)",
        (
            "peer-iid",
            "Peer Home",
            "bb" * 32,
            km.encrypt(os.urandom(32)),
            km.encrypt(os.urandom(32)),
            "https://peer.example/inbox",
            "inbox-local-1",
        ),
    )


async def _seed_space_key(db, km):
    # space_keys FKs spaces(id). Seed the parent spaces row FIRST (FKs ON), then
    # the child space_keys row — this is exactly the parents-first ordering the
    # restore relies on. Fill only the NOT NULL, no-default spaces columns;
    # content_key_hex is KEK-wrapped via the fixture's KeyManager.
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        ("space-1", "Family", "peer-iid", "alice", "cc" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_keys(space_id, epoch, content_key_hex) VALUES(?,?,?)",
        ("space-1", 1, km.encrypt(os.urandom(32))),
    )


@pytest.fixture
async def populated(tmp_dir):
    """A data dir + db with a full trust layer seeded."""
    data_dir = tmp_dir / "src"
    data_dir.mkdir()
    _write_salt(data_dir)
    km = KeyManager.from_data_dir(data_dir)
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = await _make_db(data_dir)
    seed = await _seed_identity(db, km, iid, kp)
    await _seed_remote_instance(db, km)
    await _seed_space_key(db, km)
    yield data_dir, db, iid, seed
    await db.shutdown()


@pytest.fixture
async def empty_target(tmp_dir):
    """A fresh data dir + db with no identity and no .kek_salt yet."""
    data_dir = tmp_dir / "dst"
    data_dir.mkdir()
    db = await _make_db(data_dir)
    yield data_dir, db
    await db.shutdown()


# ─── API surface ───────────────────────────────────────────────────────────


def test_trust_tables_constant():
    # Ordered parents-first: spaces precedes space_keys so the FK resolves
    # during a clean insert with FK enforcement ON.
    assert TRUST_TABLES == (
        "instance_identity",
        "remote_instances",
        "spaces",
        "space_keys",
    )
    assert TRUST_TABLES.index("spaces") < TRUST_TABLES.index("space_keys")


# ─── reset_trust_layer ───────────────────────────────────────────────────────


async def test_reset_trust_layer_empties_all_tables(populated):
    """After reset, every trust table is empty (auto-minted rows wiped)."""
    src_dir, src_db, _iid, _seed = populated
    # Sanity: all four tables are populated before the reset.
    for table in TRUST_TABLES:
        assert (await src_db.fetchone(f"SELECT 1 FROM {table} LIMIT 1")) is not None

    await RecoveryKitService(src_db, src_dir).reset_trust_layer()

    for table in TRUST_TABLES:
        assert (await src_db.fetchone(f"SELECT 1 FROM {table} LIMIT 1")) is None


async def test_build_reset_restore_round_trip(populated, empty_target):
    """A build→reset→restore round-trip on the SAME box: reset makes it empty
    so restore_kit proceeds, restoring the original instance_id."""
    src_dir, src_db, iid, _seed = populated

    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)

    svc = RecoveryKitService(src_db, src_dir)
    await svc.reset_trust_layer()
    restored_iid = await svc.restore_kit(kit, PASS)
    assert restored_iid == iid

    ident = await src_db.fetchone(
        "SELECT instance_id FROM instance_identity WHERE id='self'"
    )
    assert ident["instance_id"] == iid


# ─── Round-trip ─────────────────────────────────────────────────────────────


async def test_round_trip(populated, empty_target):
    src_dir, src_db, iid, seed = populated
    dst_dir, dst_db = empty_target

    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)
    assert isinstance(kit, bytes)

    restored_iid = await RecoveryKitService(dst_db, dst_dir).restore_kit(kit, PASS)
    assert restored_iid == iid

    # Trust rows are back.
    ident = await dst_db.fetchone(
        "SELECT instance_id, identity_private_key FROM instance_identity"
        " WHERE id='self'"
    )
    assert ident["instance_id"] == iid
    remotes = await dst_db.fetchall("SELECT id FROM remote_instances")
    assert [r["id"] for r in remotes] == ["peer-iid"]
    # The parent spaces row came back too (carries owned-space signing
    # authority and satisfies the space_keys FK).
    spaces = await dst_db.fetchall("SELECT id, name FROM spaces")
    assert [(r["id"], r["name"]) for r in spaces] == [("space-1", "Family")]
    keys = await dst_db.fetchall("SELECT space_id, epoch FROM space_keys")
    assert [(r["space_id"], r["epoch"]) for r in keys] == [("space-1", 1)]

    # FK enforcement stayed ON throughout — no PRAGMA games.
    fk = await dst_db.fetchval("PRAGMA foreign_keys")
    assert fk == 1

    # A fresh KeyManager on the restored data dir decrypts the wrapped seed.
    km2 = KeyManager.from_data_dir(dst_dir)
    assert km2.decrypt(ident["identity_private_key"]) == seed
    assert len(seed) == 32

    rec = await dst_db.fetchone(
        "SELECT value FROM instance_config WHERE key=?", (RECOVERED_AT_KEY,)
    )
    assert rec is not None and rec["value"]


# ─── Fail-closed guards ──────────────────────────────────────────────────────


async def test_refuse_non_empty_target(populated):
    src_dir, src_db, _iid, _seed = populated
    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)
    # Restore back into the SAME (already-populated) instance must refuse.
    with pytest.raises(RecoveryRestoreError, match="not empty"):
        await RecoveryKitService(src_db, src_dir).restore_kit(kit, PASS)


async def test_refuse_box_with_peers_but_no_identity(populated, empty_target):
    # A partially-provisioned box (a peer row, no identity) must still refuse —
    # else the salt overwrite would orphan the peer's undecryptable wrapped keys.
    src_dir, src_db, _iid, _seed = populated
    dst_dir, dst_db = empty_target
    km = KeyManager.from_data_dir(dst_dir)
    await _seed_remote_instance(dst_db, km)
    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)
    with pytest.raises(RecoveryRestoreError, match="not empty"):
        await RecoveryKitService(dst_db, dst_dir).restore_kit(kit, PASS)


async def test_malformed_payload_rejected(empty_target):
    # An authenticated kit whose payload shape is wrong must raise the domain
    # error, never a bare KeyError/AttributeError. Seal a bogus payload.
    dst_dir, dst_db = empty_target
    bad = seal_kit(
        orjson.dumps({"kek_salt": "not-b64!!", "tables": "nope"}),
        PASS,
        instance_id="x" * 16,
        created_at="2026-06-14T00:00:00+00:00",
    )
    with pytest.raises(RecoveryRestoreError):
        await RecoveryKitService(dst_db, dst_dir).restore_kit(bad, PASS)
    # Nothing was written.
    assert (
        await dst_db.fetchone("SELECT 1 FROM instance_identity WHERE id='self'")
    ) is None


async def test_wrong_passphrase_no_rows(populated, empty_target):
    src_dir, src_db, _iid, _seed = populated
    dst_dir, dst_db = empty_target
    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)

    with pytest.raises(RecoveryKitError):
        await RecoveryKitService(dst_db, dst_dir).restore_kit(kit, "wrong-pass")

    # No identity row leaked into the target.
    row = await dst_db.fetchone("SELECT 1 FROM instance_identity WHERE id='self'")
    assert row is None
    keys = await dst_db.fetchall("SELECT 1 FROM space_keys")
    assert keys == []


async def test_kek_self_test_failure_before_insert(populated, empty_target):
    src_dir, src_db, _iid, _seed = populated
    dst_dir, dst_db = empty_target

    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)

    # Mutate the payload's kek_salt to a different random 32 bytes and re-seal
    # so the runtime KEK can't decrypt the wrapped identity seed.
    header, payload = unseal_kit(kit, PASS)
    data = orjson.loads(payload)
    data["kek_salt"] = b64url_encode(os.urandom(KeyManager.KEK_BYTES))
    tampered = seal_kit(
        orjson.dumps(data),
        PASS,
        instance_id=header["instance_id"],
        created_at=header["created_at"],
    )

    with pytest.raises(RecoveryRestoreError, match="KEK self-test failed"):
        await RecoveryKitService(dst_db, dst_dir).restore_kit(tampered, PASS)

    # Fail closed: nothing inserted.
    row = await dst_db.fetchone("SELECT 1 FROM instance_identity WHERE id='self'")
    assert row is None
    keys = await dst_db.fetchall("SELECT 1 FROM space_keys")
    assert keys == []


async def test_bad_salt_length_rejected(populated, empty_target):
    src_dir, src_db, _iid, _seed = populated
    dst_dir, dst_db = empty_target

    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)
    header, payload = unseal_kit(kit, PASS)
    data = orjson.loads(payload)
    data["kek_salt"] = b64url_encode(os.urandom(16))  # wrong length
    tampered = seal_kit(
        orjson.dumps(data),
        PASS,
        instance_id=header["instance_id"],
        created_at=header["created_at"],
    )
    with pytest.raises(RecoveryRestoreError):
        await RecoveryKitService(dst_db, dst_dir).restore_kit(tampered, PASS)


async def test_build_no_identity_raises(empty_target):
    dst_dir, dst_db = empty_target
    with pytest.raises(RecoveryRestoreError, match="no identity"):
        await RecoveryKitService(dst_db, dst_dir).build_kit(PASS)


async def test_build_missing_salt_raises(tmp_dir):
    data_dir = tmp_dir / "nosalt"
    data_dir.mkdir()
    # Seed an identity directly but DO NOT write .kek_salt.
    db = await _make_db(data_dir)
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, "nonce:ct", kp.public_key.hex(), "aa" * 32),
    )
    try:
        with pytest.raises(RecoveryRestoreError):
            await RecoveryKitService(db, data_dir).build_kit(PASS)
    finally:
        await db.shutdown()


async def _reseal_with_payload(kit, data):
    header, _ = unseal_kit(kit, PASS)
    return seal_kit(
        orjson.dumps(data),
        PASS,
        instance_id=header["instance_id"],
        created_at=header["created_at"],
    )


async def test_space_keys_restore_without_fk_error(populated, empty_target):
    """Regression for the rejected FK-suspend approach: with ``spaces`` in
    TRUST_TABLES ahead of ``space_keys``, the child FK resolves naturally and
    restore completes with FK enforcement ON the whole time — no PRAGMA OFF, no
    orphaned space_keys row."""
    src_dir, src_db, _iid, _seed = populated
    dst_dir, dst_db = empty_target

    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)
    # Must not raise an FK error.
    await RecoveryKitService(dst_db, dst_dir).restore_kit(kit, PASS)

    keys = await dst_db.fetchall("SELECT space_id, epoch FROM space_keys")
    assert [(r["space_id"], r["epoch"]) for r in keys] == [("space-1", 1)]
    # The parent it references is present (not orphaned).
    parent = await dst_db.fetchone("SELECT id FROM spaces WHERE id=?", ("space-1",))
    assert parent is not None
    # Enforcement was never toggled off.
    fk = await dst_db.fetchval("PRAGMA foreign_keys")
    assert fk == 1


async def test_insert_failure_rolls_back(populated, empty_target):
    """A hard SQL error mid-insert rolls back the whole insert (no partial
    trust layer); FK enforcement is unaffected (it was never toggled)."""
    src_dir, src_db, _iid, _seed = populated
    dst_dir, dst_db = empty_target
    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)

    _header, payload = unseal_kit(kit, PASS)
    data = orjson.loads(payload)
    # Add a row referencing a column that does not exist → OperationalError,
    # which INSERT OR IGNORE does NOT swallow → rollback path.
    data["tables"]["space_keys"].append(
        {"space_id": "x", "epoch": 9, "no_such_column": "boom"}
    )
    tampered = await _reseal_with_payload(kit, data)

    with pytest.raises(Exception):  # noqa: B017 — sqlite OperationalError
        await RecoveryKitService(dst_db, dst_dir).restore_kit(tampered, PASS)

    # Nothing committed.
    row = await dst_db.fetchone("SELECT 1 FROM instance_identity WHERE id='self'")
    assert row is None
    # FK enforcement restored for subsequent requests.
    fk = await dst_db.fetchval("PRAGMA foreign_keys")
    assert fk == 1


async def test_header_instance_id_fallback(populated, empty_target):
    """When the self row carries no instance_id, the header value is returned."""
    src_dir, src_db, iid, _seed = populated
    dst_dir, dst_db = empty_target
    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)

    _header, payload = unseal_kit(kit, PASS)
    data = orjson.loads(payload)
    # Drop instance_id from the self row but keep the (decryptable) seed so the
    # self-test still passes; the returned id falls back to the header.
    for r in data["tables"]["instance_identity"]:
        if r.get("id") == "self":
            r.pop("instance_id", None)
    tampered = await _reseal_with_payload(kit, data)

    restored = await RecoveryKitService(dst_db, dst_dir).restore_kit(tampered, PASS)
    assert restored == iid  # header instance_id


async def test_no_self_row_rejected(populated, empty_target):
    src_dir, src_db, _iid, _seed = populated
    dst_dir, dst_db = empty_target
    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)

    _header, payload = unseal_kit(kit, PASS)
    data = orjson.loads(payload)
    data["tables"]["instance_identity"] = []  # no self row
    tampered = await _reseal_with_payload(kit, data)

    with pytest.raises(RecoveryRestoreError, match="no instance_identity self row"):
        await RecoveryKitService(dst_db, dst_dir).restore_kit(tampered, PASS)


async def test_restored_salt_matches_source(populated, empty_target):
    """Restore overwrites any startup-minted salt with the kit's salt."""
    src_dir, src_db, _iid, _seed = populated
    dst_dir, dst_db = empty_target

    # Pre-mint a different random salt in the target (simulating startup).
    _write_salt(dst_dir)
    src_salt = (src_dir / ".kek_salt").read_bytes()
    pre = (dst_dir / ".kek_salt").read_bytes()
    assert pre != src_salt

    kit = await RecoveryKitService(src_db, src_dir).build_kit(PASS)
    await RecoveryKitService(dst_db, dst_dir).restore_kit(kit, PASS)

    assert (dst_dir / ".kek_salt").read_bytes() == src_salt
    assert b64url_decode(b64url_encode(src_salt)) == src_salt
