"""GFS migration 0033 — the space-keyed relay's state goes (v_96, P7).

``space_subscribers`` (with its ``reads_space_item`` labels) is dropped; the
queued space-keyed relay frames nobody can take any more are deleted; the
retired ``space`` media scope's blobs, demand rows and upload counts are
deleted. Channel frames, envelopes, moment frames, channel-scope media and
the inert ``global_spaces`` member-publish columns stay."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import orjson

from tests.migration_chain import apply_through

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)


def _conn(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "gfs.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def _queue(c: sqlite3.Connection, frame: dict, *, frame_type: str = "relay") -> None:
    c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at, frame_type, size_bytes) VALUES('inst-b', ?, 1, 9e9, ?, 10)",
        (orjson.dumps(frame).decode(), frame_type),
    )


def _media(c: sqlite3.Connection, blob: str, scope_kind: str, owner_kind: str) -> None:
    c.execute(
        "INSERT INTO gfs_space_media(blob_id, scope_kind, scope_id, size,"
        " created_at, expires_at, last_fetched_at, owner_kind, owner_id)"
        " VALUES(?, ?, 'scope-1', 100, 1, 9e9, 1, ?, 'owner-1')",
        (blob, scope_kind, owner_kind),
    )


def _seed(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT INTO client_instances(instance_id, display_name, public_key,"
        " status) VALUES('inst-b', 'B', 'aa', 'active')"
    )
    c.execute(
        "INSERT INTO global_spaces(space_id, owning_instance, name,"
        " subscriber_count, content_epoch, writer_key_pk)"
        " VALUES('sp-1', 'owner', 'S', 1, 7, 'wk')"
    )
    c.execute(
        "INSERT INTO space_subscribers(space_id, instance_id, reads_space_item)"
        " VALUES('sp-1', 'inst-b', 7)"
    )
    for event_type in (
        "space_post_public",
        "space_subscriber_key_handoff",
        "space_author_profiles_bundle",
        "space_item",
    ):
        _queue(c, {"space_id": "sp-1", "event_type": event_type, "payload": "x"})
    # What stays: a channel item, a moment frame, a sealed envelope.
    _queue(
        c,
        {
            "channel_id": "c" * 32,
            "event_type": "space_item",
            "epoch": 1,
            "payload": "x",
        },
    )
    _queue(c, {"type": "moment_reaction", "payload": "x"})
    _queue(c, {"sealed": "x"}, frame_type="envelope")
    _media(c, "a" * 64, "space", "household")
    _media(c, "b" * 64, "channel", "client")
    for scope_kind in ("space", "channel"):
        c.execute(
            "INSERT INTO gfs_space_media_demand(blob_id, scope_kind, scope_id,"
            " touched_at) VALUES(?, ?, 'scope-1', 1)",
            ("c" * 64, scope_kind),
        )
        c.execute(
            "INSERT INTO gfs_space_media_uploads(scope_kind, scope_id,"
            " hour_bucket, count) VALUES(?, 'scope-1', 1, 1)",
            (scope_kind,),
        )


def _tables(c: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_the_space_keyed_relay_state_is_dropped(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 32, directory=_GFS_MIGRATIONS)
    _seed(c)
    apply_through(c, 33, directory=_GFS_MIGRATIONS)
    assert "space_subscribers" not in _tables(c)
    indexes = {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_space_subscribers" not in indexes
    assert "idx_instance_spaces" not in indexes
    c.close()


def test_only_the_space_keyed_queue_rows_go(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 32, directory=_GFS_MIGRATIONS)
    _seed(c)
    apply_through(c, 33, directory=_GFS_MIGRATIONS)
    kept = [
        orjson.loads(r[0])
        for r in c.execute("SELECT sealed_json FROM gfs_envelope_queue ORDER BY id")
    ]
    assert kept == [
        {
            "channel_id": "c" * 32,
            "event_type": "space_item",
            "epoch": 1,
            "payload": "x",
        },
        {"type": "moment_reaction", "payload": "x"},
        {"sealed": "x"},
    ]
    c.close()


def test_only_the_space_scope_media_goes_and_the_totals_follow(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 32, directory=_GFS_MIGRATIONS)
    _seed(c)
    assert c.execute("SELECT blobs, bytes FROM gfs_space_media_totals").fetchone() == (
        2,
        200,
    )
    apply_through(c, 33, directory=_GFS_MIGRATIONS)
    assert c.execute("SELECT blob_id, scope_kind FROM gfs_space_media").fetchall() == [
        ("b" * 64, "channel")
    ]
    # The triggers keep the counters right.
    assert c.execute("SELECT blobs, bytes FROM gfs_space_media_totals").fetchone() == (
        1,
        100,
    )
    assert c.execute(
        "SELECT scope_kind FROM gfs_space_media_scope_bytes"
    ).fetchall() == [("channel",)]
    assert c.execute(
        "SELECT owner_kind FROM gfs_space_media_owner_bytes"
    ).fetchall() == [("client",)]
    assert c.execute("SELECT scope_kind FROM gfs_space_media_demand").fetchall() == [
        ("channel",)
    ]
    assert c.execute("SELECT scope_kind FROM gfs_space_media_uploads").fetchall() == [
        ("channel",)
    ]
    c.close()


def test_the_listing_and_its_inert_columns_stay(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 32, directory=_GFS_MIGRATIONS)
    _seed(c)
    apply_through(c, 33, directory=_GFS_MIGRATIONS)
    # No column drops: the member-publish columns are inert, not removed.
    assert c.execute(
        "SELECT space_id, content_epoch, writer_key_pk FROM global_spaces"
    ).fetchone() == ("sp-1", 7, "wk")
    c.close()
