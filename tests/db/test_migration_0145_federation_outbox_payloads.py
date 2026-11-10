"""Migration 0145 — one stored payload (``federation_outbox_payloads``) for
the per-recipient ref rows of ``federation_outbox``."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 145


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO federation_outbox(id, instance_id, event_type, payload_json)"
        " VALUES('old', 'peer', 'dm_message', '{\"msg_id\":\"old\"}')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def _payload(conn, pid: str = "p1") -> None:
    conn.execute(
        "INSERT INTO federation_outbox_payloads(id, event_type, space_id,"
        " payload_enc) VALUES(?, 'space_post_created', 's1', 'n:ct')",
        (pid,),
    )


def _ref(conn, rid: str, peer: str, pid: str = "p1") -> None:
    conn.execute(
        "INSERT INTO federation_outbox(id, instance_id, event_type, payload_json,"
        " payload_id) VALUES(?, ?, 'space_post_created', '', ?)",
        (rid, peer, pid),
    )


def _payloads(conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM federation_outbox_payloads")]


def test_an_existing_row_keeps_its_envelope(conn):
    row = conn.execute(
        "SELECT payload_id, payload_json FROM federation_outbox WHERE id='old'"
    ).fetchone()
    assert row["payload_id"] is None
    assert row["payload_json"] == '{"msg_id":"old"}'


def test_a_ref_row_needs_a_stored_payload(conn):
    with pytest.raises(sqlite3.IntegrityError, match="unknown payload_id"):
        _ref(conn, "r1", "peer-a", "missing")


def test_the_payload_goes_with_its_last_ref(conn):
    _payload(conn)
    for i, peer in enumerate(("a", "b", "c")):
        _ref(conn, f"r{i}", peer)
    conn.execute("DELETE FROM federation_outbox WHERE id='r0'")
    conn.execute("DELETE FROM federation_outbox WHERE instance_id='b'")
    assert _payloads(conn) == ["p1"]
    conn.execute("DELETE FROM federation_outbox WHERE id='r2'")
    assert _payloads(conn) == []


def test_deleting_a_legacy_row_leaves_payloads_alone(conn):
    _payload(conn)
    _ref(conn, "r1", "a")
    conn.execute("DELETE FROM federation_outbox WHERE id='old'")
    assert _payloads(conn) == ["p1"]


def test_the_ref_lookup_uses_the_partial_index(conn):
    plan = " ".join(
        r["detail"]
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM federation_outbox WHERE payload_id='p1'"
        )
    )
    assert "idx_federation_outbox_payload" in plan


def test_a_stored_payload_has_a_nullable_msg_id(conn):
    """NULL for a failed send's payload; set for a broadcast's multi payload,
    whose every envelope carries it."""
    _payload(conn)
    conn.execute(
        "INSERT INTO federation_outbox_payloads(id, event_type, space_id,"
        " payload_enc, msg_id) VALUES('p2', 'space_post_created', 's1', 'n:ct',"
        " 'm-1')"
    )
    rows = conn.execute(
        "SELECT id, msg_id FROM federation_outbox_payloads ORDER BY id"
    ).fetchall()
    assert [(r["id"], r["msg_id"]) for r in rows] == [("p1", None), ("p2", "m-1")]
