"""Tests for BackupService — export + restore + safety guards."""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from social_home.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from social_home.db.database import AsyncDatabase
from social_home.services.backup_service import (
    BackupError,
    BackupRestoreNotEmpty,
    BackupService,
    EXPORTABLE_TABLES,
    NEVER_EXPORT,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('alice', 'alice-id', 'Alice')",
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES('p1', 'alice-id', 'text', 'hello')",
    )
    media = tmp_dir / "media"
    media.mkdir()
    (media / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    yield db, BackupService(db, media), media
    await db.shutdown()


# ─── Allowlist invariants ────────────────────────────────────────────────


def test_secret_tables_in_never_export():
    """Identity / KEK material must never be in the export."""
    assert "instance_identity" in NEVER_EXPORT
    assert "space_keys" in NEVER_EXPORT
    assert "remote_instances" in NEVER_EXPORT
    assert "api_tokens" in NEVER_EXPORT
    assert "push_subscriptions" in NEVER_EXPORT


def test_user_data_tables_in_exportable():
    """Common user-content tables must be in the export."""
    assert "users" in EXPORTABLE_TABLES
    assert "feed_posts" in EXPORTABLE_TABLES
    assert "spaces" in EXPORTABLE_TABLES
    assert "conversations" in EXPORTABLE_TABLES


def test_no_table_in_both_lists():
    """No table is ever both exportable and forbidden."""
    overlap = set(EXPORTABLE_TABLES) & NEVER_EXPORT
    assert overlap == set()


# ─── Export ──────────────────────────────────────────────────────────────


async def test_export_produces_tar_gz(env):
    _, svc, _ = env
    blob = await svc.export_to_bytes()
    assert blob.startswith(b"\x1f\x8b")  # gzip magic
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert "tables/users.json" in names
    assert "tables/feed_posts.json" in names


async def test_export_excludes_secret_tables(env):
    _, svc, _ = env
    blob = await svc.export_to_bytes()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    for forbidden in NEVER_EXPORT:
        assert f"tables/{forbidden}.json" not in names, (
            f"forbidden table {forbidden} should NOT be in backup"
        )


async def test_export_includes_media_files(env):
    _, svc, _ = env
    blob = await svc.export_to_bytes()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    assert "media/img.png" in names


async def test_export_manifest_carries_metadata(env):
    _, svc, _ = env
    blob = await svc.export_to_bytes()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        f = tar.extractfile("manifest.json")
        assert f is not None
        manifest = json.loads(f.read().decode("utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["instance_id"]
    assert "exported_at" in manifest


async def test_export_to_path_writes_file(env, tmp_dir):
    _, svc, _ = env
    target = tmp_dir / "backup" / "out.tar.gz"
    out = await svc.export_to_path(target)
    assert out.exists()
    assert out.read_bytes()[:2] == b"\x1f\x8b"


# ─── Restore guard ───────────────────────────────────────────────────────


async def test_restore_refuses_non_empty_db(env):
    _, svc, _ = env
    blob = await svc.export_to_bytes()
    with pytest.raises(BackupRestoreNotEmpty):
        await svc.restore_from_bytes(blob)


async def test_restore_into_empty_db_works(tmp_dir):
    """Source DB → export → fresh DB → restore round-trips data."""
    # Populate source.
    src = AsyncDatabase(tmp_dir / "src.db", batch_timeout_ms=10)
    await src.startup()
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    await src.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await src.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('alice', 'a', 'Alice')",
    )
    await src.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES('p1', 'a', 'text', 'hi')",
    )
    media_src = tmp_dir / "media-src"
    media_src.mkdir()
    blob = await BackupService(src, media_src).export_to_bytes()
    await src.shutdown()

    # Fresh target.
    media_tgt = tmp_dir / "media-tgt"
    tgt = AsyncDatabase(tmp_dir / "tgt.db", batch_timeout_ms=10)
    await tgt.startup()
    await BackupService(tgt, media_tgt).restore_from_bytes(blob)
    rows = await tgt.fetchall("SELECT username, display_name FROM users")
    assert any(r["username"] == "alice" for r in rows)
    posts = await tgt.fetchall("SELECT id, content FROM feed_posts")
    assert any(p["id"] == "p1" and p["content"] == "hi" for p in posts)
    await tgt.shutdown()


async def test_restore_rejects_missing_manifest(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="some.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))
    with pytest.raises(BackupError):
        await BackupService(db, tmp_dir).restore_from_bytes(blob.getvalue())
    await db.shutdown()


async def test_restore_rejects_schema_mismatch(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        manifest = json.dumps(
            {
                "schema_version": 999,
                "instance_id": "x",
                "exported_at": "now",
                "table_names": [],
            }
        ).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    with pytest.raises(BackupError):
        await BackupService(db, tmp_dir, schema_version=1).restore_from_bytes(
            blob.getvalue()
        )
    await db.shutdown()
