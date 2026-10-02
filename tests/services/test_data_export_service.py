"""Tests for DataExportService (§25.8.7)."""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.services.data_export_service import (
    DataExport,
    DataExportService,
    EXPORTABLE_QUERIES,
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
    for username, uid in [("alice", "alice-id"), ("bob", "bob-id")]:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, uid, username.title()),
        )
    yield db, DataExportService(db)
    await db.shutdown()


# ─── Empty user export ───────────────────────────────────────────────────


async def test_export_only_returns_users_row_when_no_other_data(env):
    _, svc = env
    out = await svc.export_for_user("alice-id")
    assert isinstance(out, DataExport)
    assert out.user_id == "alice-id"
    assert "users" in out.tables
    assert len(out.tables["users"]) == 1


# ─── Includes authored content ──────────────────────────────────────────


async def test_export_includes_user_authored_posts(env):
    db, svc = env
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES(?, ?, 'text', ?)",
        ("p1", "alice-id", "hello"),
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES(?, ?, 'text', ?)",
        ("p2", "bob-id", "bobs post"),
    )
    out = await svc.export_for_user("alice-id")
    assert "feed_posts" in out.tables
    posts = out.tables["feed_posts"]
    assert len(posts) == 1
    assert posts[0]["id"] == "p1"


async def test_export_excludes_other_users_data(env):
    db, svc = env
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES(?, ?, 'text', ?)",
        ("p1", "bob-id", "not for alice"),
    )
    out = await svc.export_for_user("alice-id")
    assert "feed_posts" not in out.tables


# ─── Two-condition WHERE (e.g. dm_contact_requests / call_sessions) ─────


async def test_export_handles_two_param_clauses(env):
    db, svc = env
    await db.enqueue(
        "INSERT INTO dm_contact_requests(id, from_user_id, to_user_id)"
        " VALUES('r1', 'alice-id', 'bob-id')",
    )
    await db.enqueue(
        "INSERT INTO dm_contact_requests(id, from_user_id, to_user_id)"
        " VALUES('r2', 'bob-id', 'alice-id')",
    )
    out = await svc.export_for_user("alice-id")
    assert "dm_contact_requests" in out.tables
    assert len(out.tables["dm_contact_requests"]) == 2


# ─── Skip unknown tables silently ───────────────────────────────────────


async def test_export_skips_table_that_doesnt_exist(env, monkeypatch):
    """A table missing from the schema is logged and skipped."""
    _, svc = env
    # Inject a fake table reference at the start.
    monkeypatch.setattr(
        "socialhome.services.data_export_service.EXPORTABLE_QUERIES",
        (("definitely_not_a_table", "WHERE x = ?"),) + EXPORTABLE_QUERIES,
    )
    out = await svc.export_for_user("alice-id")
    assert "definitely_not_a_table" not in out.tables


# ─── export_to_bytes ────────────────────────────────────────────────────


async def test_export_to_bytes_is_valid_json(env):
    _, svc = env
    blob = await svc.export_to_bytes("alice-id")
    parsed = json.loads(blob.decode("utf-8"))
    assert parsed["user_id"] == "alice-id"
    assert "exported_at" in parsed
    assert "tables" in parsed


# ─── Coverage of EXPORTABLE_QUERIES ─────────────────────────────────────


def test_exportable_queries_cover_user_facing_surfaces():
    """Smoke check: the allowlist mentions the major surfaces."""
    names = {t for t, _ in EXPORTABLE_QUERIES}
    for required in (
        "users",
        "feed_posts",
        "feed_comments",
        "conversation_messages",
        "tasks",
        "timetables",
        "space_timetables",
        "calendar_events",
        "gallery_albums",
        "gallery_items",
        "notifications",
        "push_subscriptions",
    ):
        assert required in names


# ─── Shopping list items ────────────────────────────────────────────────


async def test_export_includes_shopping_items_created_by_user(env):
    """Shopping items are linked to a user by ``created_by`` (not added_by)."""
    db, svc = env
    await db.enqueue(
        "INSERT INTO shopping_list_items(id, text, created_by, store)"
        " VALUES('i1', 'bread', 'alice-id', 'Bakery')",
    )
    await db.enqueue(
        "INSERT INTO shopping_list_items(id, text, created_by)"
        " VALUES('i2', 'milk', 'bob-id')",
    )
    out = await svc.export_for_user("alice-id")
    assert "shopping_list_items" in out.tables
    items = out.tables["shopping_list_items"]
    assert [i["id"] for i in items] == ["i1"]


async def test_export_includes_timetables_created_by_user(env):
    db, svc = env
    for tid, owner in (("tt1", "alice-id"), ("tt2", "bob-id")):
        await db.enqueue(
            "INSERT INTO timetables(id, name, created_by) VALUES(?, ?, ?)",
            (tid, f"TT {tid}", owner),
        )
    out = await svc.export_for_user("alice-id")
    assert [t["id"] for t in out.tables["timetables"]] == ["tt1"]


# ─── Space moderation queue (§4.3) ─────────────────────────────────────


async def test_export_includes_own_moderation_submissions_only(env):
    """The subject's own queued items (any status) are theirs to export;
    another member's submissions in the same space are not."""
    db, svc = env
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'S', 'i', 'alice', 'aa')"
    )
    for item_id, who, status in (
        ("m1", "alice-id", "pending"),
        ("m2", "alice-id", "rejected"),
        ("m3", "bob-id", "pending"),
    ):
        await db.enqueue(
            "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
            " submitted_by, payload_json, current_snapshot, reviewed_by,"
            " expires_at, status)"
            " VALUES(?, 'sp', 'posts', 'create', ?, ?, '{\"content\": \"theirs\"}',"
            " 'uid-mod', '2099-01-01', ?)",
            (item_id, who, '{"content": "' + item_id + '"}', status),
        )
    out = await svc.export_for_user("alice-id")
    rows = out.tables["space_moderation_queue"]
    assert sorted(r["id"] for r in rows) == ["m1", "m2"]
    assert {r["status"] for r in rows} == {"pending", "rejected"}
    # Their own words stay; somebody else's (the edited item's snapshot)
    # and the moderator's id do not.
    assert all("payload_json" in r for r in rows)
    assert all("current_snapshot" not in r and "reviewed_by" not in r for r in rows)
    bob = await svc.export_for_user("bob-id")
    assert [r["id"] for r in bob.tables["space_moderation_queue"]] == ["m3"]
