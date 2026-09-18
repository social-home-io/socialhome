"""Tests for socialhome.db.migrations — discover_migrations and run_migrations."""

from __future__ import annotations

import re
import sqlite3

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.db.migrations import (
    MIGRATIONS_DIR,
    MigrationError,
    discover_migrations,
    run_migrations,
)


def test_discover_empty_directory(tmp_path):
    """discover_migrations returns an empty list when the directory is empty."""
    result = discover_migrations(tmp_path)
    assert result == []


def test_discover_missing_directory(tmp_path):
    """discover_migrations returns an empty list when the directory doesn't exist."""
    missing = tmp_path / "no_such_dir"
    result = discover_migrations(missing)
    assert result == []


def test_discover_valid_sql_file(tmp_path):
    """discover_migrations picks up a valid NNNN_description.sql file."""
    (tmp_path / "0001_initial.sql").write_text("CREATE TABLE t (id TEXT);")
    result = discover_migrations(tmp_path)
    assert len(result) == 1
    assert result[0].version == 1
    assert result[0].description == "initial"
    assert result[0].is_python is False


def test_discover_valid_python_file(tmp_path):
    """discover_migrations picks up a valid NNNN_description.py file."""
    (tmp_path / "0002_migrate.py").write_text("def migrate(conn):\n    pass\n")
    result = discover_migrations(tmp_path)
    assert len(result) == 1
    assert result[0].version == 2
    assert result[0].is_python is True


def test_discover_ignores_readme(tmp_path):
    """discover_migrations silently ignores README files and non-matching names."""
    (tmp_path / "README.md").write_text("docs")
    (tmp_path / "0001_init.sql").write_text("SELECT 1;")
    result = discover_migrations(tmp_path)
    assert len(result) == 1


def test_discover_duplicate_raises(tmp_path):
    """discover_migrations raises MigrationError when two files share a version."""
    (tmp_path / "0001_alpha.sql").write_text("SELECT 1;")
    (tmp_path / "0001_beta.sql").write_text("SELECT 2;")
    with pytest.raises(MigrationError, match="Duplicate migration version 1"):
        discover_migrations(tmp_path)


def test_discover_bad_sql_filename_raises(tmp_path):
    """A .sql file with a name that doesn't match NNNN_desc.sql raises MigrationError."""
    (tmp_path / "bad_name.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="bad_name.sql"):
        discover_migrations(tmp_path)


def test_discover_ordered(tmp_path):
    """discover_migrations returns migrations sorted by version number."""
    (tmp_path / "0003_third.sql").write_text("SELECT 3;")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;")
    (tmp_path / "0002_second.sql").write_text("SELECT 2;")
    result = discover_migrations(tmp_path)
    assert [m.version for m in result] == [1, 2, 3]


def test_run_migrations_applies_all(tmp_path):
    """run_migrations applies all pending migrations and stamps schema_version."""
    (tmp_path / "0001_create_test.sql").write_text(
        "CREATE TABLE IF NOT EXISTS test_run (id INTEGER PRIMARY KEY);"
    )
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    applied = run_migrations(conn, directory=tmp_path)
    assert len(applied) == 1
    assert applied[0].version == 1
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert int(row[0]) == 1
    conn.close()


def test_run_migrations_idempotent(tmp_path):
    """run_migrations is a no-op when called a second time with the same migrations."""
    (tmp_path / "0001_create_test.sql").write_text(
        "CREATE TABLE IF NOT EXISTS test_idempotent (id INTEGER PRIMARY KEY);"
    )
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    run_migrations(conn, directory=tmp_path)
    applied_second = run_migrations(conn, directory=tmp_path)
    assert applied_second == []
    conn.close()


def test_run_python_migration(tmp_path):
    """run_migrations executes a Python migration's migrate(conn) callable."""
    (tmp_path / "0001_py_test.py").write_text(
        "def migrate(conn):\n    conn.execute('CREATE TABLE py_test (x TEXT);')\n"
    )
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    applied = run_migrations(conn, directory=tmp_path)
    assert len(applied) == 1
    # Table should exist
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='py_test'"
    ).fetchone()
    assert row is not None
    conn.close()


def test_0036_backfills_roster_sequence_from_config_sequence(tmp_path):
    """Migration 0036 seeds roster_sequence from each existing space's
    config_sequence (continuity: existing member_versions were sourced from
    config_sequence, so a fresh 0 would emit stale roster events)."""
    import shutil

    staged = tmp_path / "migrations"
    staged.mkdir()
    # Stage every real migration BEFORE 0036.
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        m = re.match(r"^(\d{4})_", path.name)
        if m and int(m.group(1)) < 36:
            shutil.copy(path, staged / path.name)

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    run_migrations(conn, directory=staged)

    # Seed a pre-0036 space with a non-zero config_sequence (no roster column).
    conn.execute(
        """
        INSERT INTO spaces(
            id, name, owner_instance_id, owner_username,
            identity_public_key, config_sequence
        ) VALUES('sp-bf', 'BF', 'inst-x', 'alice', 'aabb', 4)
        """
    )
    conn.commit()

    # Now stage + apply 0036.
    for path in MIGRATIONS_DIR.iterdir():
        if path.name.startswith("0036_"):
            shutil.copy(path, staged / path.name)
    applied = run_migrations(conn, directory=staged)
    assert any(m.version == 36 for m in applied)

    row = conn.execute(
        "SELECT config_sequence, roster_sequence FROM spaces WHERE id='sp-bf'"
    ).fetchone()
    assert row["config_sequence"] == 4
    assert row["roster_sequence"] == 4  # backfilled to match
    conn.close()


@pytest.mark.asyncio
async def test_0040_user_identity_columns(tmp_path):
    """Migration 0040 adds per-user identity key columns (Phase 1 of
    independent user identity). Mirrors instance_identity key family.
    NULL-defaulted, no backfill."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()

    # Check users table has the new columns
    users_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(users)")}
    required_users_cols = {
        "user_identity_public_key",
        "user_identity_private_key",
        "user_pq_algorithm",
        "user_pq_public_key",
        "user_pq_private_key",
    }
    assert required_users_cols <= users_cols, (
        f"Missing columns in users table: {required_users_cols - users_cols}"
    )

    # Check remote_users table has the new columns
    remote_users_cols = {
        r["name"] for r in await db.fetchall("PRAGMA table_info(remote_users)")
    }
    required_remote_users_cols = {
        "user_identity_public_key",
        "user_pq_public_key",
    }
    assert required_remote_users_cols <= remote_users_cols, (
        f"Missing columns in remote_users table: "
        f"{required_remote_users_cols - remote_users_cols}"
    )

    await db.shutdown()


@pytest.mark.asyncio
async def test_0041_identity_anchor_columns(tmp_path):
    """Migration 0041 adds identity_anchor (immutable user UUID) to users and
    remote_users. Existing users backfill to their current username so user_id
    is unchanged."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()

    # Check users table has identity_anchor
    users_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(users)")}
    assert "identity_anchor" in users_cols, "identity_anchor missing from users table"

    # Check remote_users table has identity_anchor
    remote_users_cols = {
        r["name"] for r in await db.fetchall("PRAGMA table_info(remote_users)")
    }
    assert "identity_anchor" in remote_users_cols, (
        "identity_anchor missing from remote_users table"
    )

    await db.shutdown()


def test_0041_backfills_identity_anchor_to_username(tmp_path):
    """Migration 0041 backfills every existing ``users`` row's NULL
    ``identity_anchor`` to its current username, so the user_id derivation
    (which keys off the anchor when present) is unchanged for legacy accounts.

    Applies migrations incrementally: everything up to 0040 runs, a user row
    with NULL identity_anchor is seeded, then 0041 runs and the backfill is
    asserted. This is the make-or-break statement — a silent regression here
    would re-key every existing user_id."""
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    try:
        all_migrations = discover_migrations(MIGRATIONS_DIR)
        pre = [m for m in all_migrations if m.version < 41]
        the_0041 = [m for m in all_migrations if m.version == 41]
        assert the_0041, "migration 0041 not found"

        # Apply everything up to (but not including) 0041.
        _ensure_schema_version_for_test(conn)
        for migration in pre:
            with conn:
                migration.apply(conn)

        # The column does not exist yet — seed a legacy row (anchor NULL).
        users_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        assert "identity_anchor" not in users_cols, (
            "identity_anchor must not exist before 0041"
        )
        conn.execute(
            "INSERT INTO users (username, user_id, display_name) VALUES (?,?,?)",
            ("legacy_alice", "uid-alice", "Alice"),
        )
        conn.commit()

        # Apply 0041.
        with conn:
            the_0041[0].apply(conn)

        row = conn.execute(
            "SELECT identity_anchor FROM users WHERE username = ?",
            ("legacy_alice",),
        ).fetchone()
        assert row["identity_anchor"] == "legacy_alice", (
            "0041 must backfill identity_anchor to the username for legacy rows"
        )
    finally:
        conn.close()


# Tables rebuilt by 0042 to add ON UPDATE CASCADE on their users(username) /
# platform_users(username) FK. Each maps to its username-bearing column.
_LOCAL_CASCADE_TABLES = {
    "presence": "username",
    "post_drafts": "username",
    "space_aliases": "local_username",
    "conversation_members": "username",
    "calendars": "owner_username",
}


def _seed_user(conn: sqlite3.Connection, username: str) -> None:
    """Insert a minimal valid ``users`` row (0041 added identity_anchor)."""
    conn.execute(
        "INSERT INTO users (username, user_id, display_name, identity_anchor) "
        "VALUES (?,?,?,?)",
        (username, f"uid-{username}", username.title(), username),
    )


def _seed_local_children(conn: sqlite3.Connection, username: str) -> None:
    """Insert one child row in each users(username)-FK table for ``username``."""
    conn.execute(
        "INSERT INTO presence (username, entity_id) VALUES (?,?)",
        (username, "ent-1"),
    )
    conn.execute(
        "INSERT INTO post_drafts (id, username, context) VALUES (?,?,?)",
        ("draft-1", username, "household_feed"),
    )
    conn.execute(
        "INSERT INTO space_aliases (space_id, local_username, alias) VALUES (?,?,?)",
        ("sp-1", username, "Ali"),
    )
    conn.execute(
        "INSERT INTO conversations (id, type) VALUES (?,?)",
        ("conv-1", "dm"),
    )
    conn.execute(
        "INSERT INTO conversation_members (conversation_id, username) VALUES (?,?)",
        ("conv-1", username),
    )
    conn.execute(
        "INSERT INTO calendars (id, name, owner_username) VALUES (?,?,?)",
        ("cal-1", "Mine", username),
    )


@pytest.mark.asyncio
async def test_0042_username_rename_cascades(tmp_path):
    """Migration 0042 rebuilds every users(username)/platform_users(username)-FK
    child table with ON UPDATE CASCADE, so renaming the parent username
    propagates to every child row (the point of a mutable username)."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    conn = db._conn  # the live, FK-enforcing migration connection

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    _seed_user(conn, "alice")
    _seed_local_children(conn, "alice")
    # platform_tokens hangs off platform_users(username), renamed separately.
    conn.execute(
        "INSERT INTO platform_users (username, display_name) VALUES (?,?)",
        ("palice", "PAlice"),
    )
    conn.execute(
        "INSERT INTO platform_tokens (token_id, username, token_hash) VALUES (?,?,?)",
        ("tok-1", "palice", "hash-1"),
    )

    conn.execute("UPDATE users SET username='alice2' WHERE username='alice'")
    conn.execute("UPDATE platform_users SET username='palice2' WHERE username='palice'")

    for table, col in _LOCAL_CASCADE_TABLES.items():
        new = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col}='alice2'"  # noqa: S608
        ).fetchone()[0]
        old = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col}='alice'"  # noqa: S608
        ).fetchone()[0]
        assert new == 1, f"{table}.{col} did not cascade the rename"
        assert old == 0, f"{table}.{col} still holds the old username"

    pt_new = conn.execute(
        "SELECT COUNT(*) FROM platform_tokens WHERE username='palice2'"
    ).fetchone()[0]
    pt_old = conn.execute(
        "SELECT COUNT(*) FROM platform_tokens WHERE username='palice'"
    ).fetchone()[0]
    assert pt_new == 1, "platform_tokens.username did not cascade the rename"
    assert pt_old == 0, "platform_tokens.username still holds the old username"

    await db.shutdown()


@pytest.mark.asyncio
async def test_0042_preserves_data(tmp_path):
    """The 0042 rebuild changes only the FK clause — every rebuilt table keeps
    its full column set (a dropped/renamed column would silently break the
    services that read these rows)."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    conn = db._conn

    expected_cols = {
        "presence": {
            "username",
            "entity_id",
            "state",
            "zone_name",
            "latitude",
            "longitude",
            "gps_accuracy_m",
            "updated_at",
        },
        "post_drafts": {
            "id",
            "username",
            "context",
            "type",
            "content",
            "media_url",
            "updated_at",
        },
        "space_aliases": {"space_id", "local_username", "alias", "updated_at"},
        "conversation_members": {
            "conversation_id",
            "username",
            "joined_at",
            "last_read_at",
            "history_visible_from",
            "deleted_at",
        },
        "calendars": {"id", "name", "color", "owner_username", "calendar_type"},
        "platform_tokens": {
            "token_id",
            "username",
            "token_hash",
            "created_at",
            "expires_at",
            "last_used_at",
        },
    }
    for table, cols in expected_cols.items():
        actual = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert actual == cols, (
            f"{table} column set drifted after rebuild: "
            f"missing {cols - actual}, extra {actual - cols}"
        )

    await db.shutdown()


@pytest.mark.asyncio
async def test_0042_on_delete_still_cascades(tmp_path):
    """The rebuild must preserve the pre-existing ON DELETE CASCADE: deleting
    the parent user still purges its child rows (0042 only ADDS ON UPDATE)."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    conn = db._conn

    _seed_user(conn, "bob")
    _seed_local_children(conn, "bob")
    conn.execute(
        "INSERT INTO platform_users (username, display_name) VALUES (?,?)",
        ("pbob", "PBob"),
    )
    conn.execute(
        "INSERT INTO platform_tokens (token_id, username, token_hash) VALUES (?,?,?)",
        ("tok-b", "pbob", "hash-b"),
    )

    conn.execute("DELETE FROM users WHERE username='bob'")
    conn.execute("DELETE FROM platform_users WHERE username='pbob'")

    for table, col in _LOCAL_CASCADE_TABLES.items():
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col}='bob'"  # noqa: S608
        ).fetchone()[0]
        assert remaining == 0, f"{table}.{col} did not ON DELETE CASCADE"

    pt_remaining = conn.execute(
        "SELECT COUNT(*) FROM platform_tokens WHERE username='pbob'"
    ).fetchone()[0]
    assert pt_remaining == 0, "platform_tokens did not ON DELETE CASCADE"

    await db.shutdown()


def test_0042_rebuild_preserves_calendar_with_existing_event(tmp_path):
    """``calendars`` is a *parent* of ``calendar_events`` — its rebuild drops &
    recreates the table while a child row points at it. Applying 0042
    incrementally (after seeding a calendar + event under the old schema) proves
    the row copy preserves the calendar, the dependent event survives, and FK
    enforcement is back ON with no dangling reference after the migration."""
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        all_migrations = discover_migrations(MIGRATIONS_DIR)
        pre = [m for m in all_migrations if m.version < 42]
        the_0042 = [m for m in all_migrations if m.version == 42]
        assert the_0042, "migration 0042 not found"

        _ensure_schema_version_for_test(conn)
        for migration in pre:
            with conn:
                migration.apply(conn)

        _seed_user(conn, "carol")
        conn.execute(
            "INSERT INTO calendars (id, name, owner_username) VALUES (?,?,?)",
            ("cal-c", "Carol", "carol"),
        )
        conn.execute(
            "INSERT INTO calendar_events "
            "(id, calendar_id, summary, start_dt, end_dt, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (
                "evt-c",
                "cal-c",
                "Lunch",
                "2026-01-01T12:00",
                "2026-01-01T13:00",
                "uid-carol",
            ),
        )
        conn.commit()

        with conn:
            the_0042[0].apply(conn)

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute("SELECT COUNT(*) FROM calendars WHERE id='cal-c'").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT calendar_id FROM calendar_events WHERE id='evt-c'"
            ).fetchone()["calendar_id"]
            == "cal-c"
        )

        # And the new ON UPDATE CASCADE now propagates a rename to the event-
        # bearing calendar's owner.
        conn.execute("UPDATE users SET username='carol2' WHERE username='carol'")
        assert (
            conn.execute(
                "SELECT owner_username FROM calendars WHERE id='cal-c'"
            ).fetchone()["owner_username"]
            == "carol2"
        )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_0043_handle_columns_and_backfill(tmp_path):
    """Migration 0043 adds public handle columns to users and remote_users.
    The handle column is backfilled with username for existing users, and
    a per-household, case-insensitive UNIQUE NOCASE index is created."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()

    # Check users table has handle column
    users_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(users)")}
    assert "handle" in users_cols, "handle missing from users table"

    # Check remote_users table has handle column
    remote_users_cols = {
        r["name"] for r in await db.fetchall("PRAGMA table_info(remote_users)")
    }
    assert "handle" in remote_users_cols, "handle missing from remote_users table"

    # Check that UNIQUE NOCASE index exists for users.handle
    indices = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='users'"
    )
    index_names = {r["name"] for r in indices}
    assert any("handle" in name for name in index_names), (
        f"No handle index found in users; indices: {index_names}"
    )

    await db.shutdown()


@pytest.mark.asyncio
async def test_0043_handle_case_insensitive_uniqueness(tmp_path):
    """Migration 0043's UNIQUE NOCASE index enforces case-insensitive
    uniqueness on users.handle. Inserting a user with handle='alice',
    then attempting handle='ALICE' must fail."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    conn = db._conn

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    # Insert first user with lowercase handle
    conn.execute(
        "INSERT INTO users (username, user_id, display_name, identity_anchor, handle) "
        "VALUES (?,?,?,?,?)",
        ("alice", "uid-alice", "Alice", "alice", "alice"),
    )
    conn.commit()

    # Attempt to insert with uppercase variant - should fail
    try:
        conn.execute(
            "INSERT INTO users (username, user_id, display_name, identity_anchor, handle) "
            "VALUES (?,?,?,?,?)",
            ("ALICE", "uid-alice2", "ALICE", "ALICE", "ALICE"),
        )
        conn.commit()
        # If we reach here, the constraint didn't work
        assert False, (
            "UNIQUE NOCASE constraint on handle did not prevent case-variant insert"
        )
    except sqlite3.IntegrityError:
        # Expected: constraint violation
        pass

    # Verify only one user exists
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 1, "Expected 1 user after failed duplicate handle insert"

    await db.shutdown()


def test_0043_case_colliding_usernames_do_not_abort(tmp_path):
    """Migration 0043 must handle case-colliding usernames (e.g. 'Bob' and 'bob')
    that can coexist in the case-SENSITIVE PRIMARY KEY users.username.

    The backfill handle=username then tries to CREATE UNIQUE INDEX ... COLLATE NOCASE,
    which would abort the migration if two rows have handles differing only in case.

    The fix: after backfill, UPDATE any handle row that is NOT the lowest-rowid
    member of its case-insensitive group, suffixing it with '_' + rowid to
    de-collide before creating the index. The lowest-rowid member keeps its bare
    handle; the others are suffixed and can pick new handles in Settings later.
    """
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    try:
        all_migrations = discover_migrations(MIGRATIONS_DIR)
        pre = [m for m in all_migrations if m.version < 43]
        the_0043 = [m for m in all_migrations if m.version == 43]
        assert the_0043, "migration 0043 not found"

        # Apply everything up to (but not including) 0043.
        _ensure_schema_version_for_test(conn)
        for migration in pre:
            with conn:
                migration.apply(conn)

        # Verify the handle column doesn't exist yet.
        users_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        assert "handle" not in users_cols, "handle must not exist before 0043"

        # Seed two users with usernames differing only in case (case-SENSITIVE, so both
        # are valid PRIMARY KEY values). This is the edge case 0043 must survive.
        conn.execute(
            "INSERT INTO users (username, user_id, display_name, identity_anchor) "
            "VALUES (?,?,?,?)",
            ("Bob", "uid-bob", "Bob", "Bob"),
        )
        conn.execute(
            "INSERT INTO users (username, user_id, display_name, identity_anchor) "
            "VALUES (?,?,?,?)",
            ("bob", "uid-bob-lower", "bob", "bob"),
        )
        conn.commit()

        # Apply 0043.
        with conn:
            the_0043[0].apply(conn)

        # The migration must succeed (no exception above).

        # Both rows now have non-NULL handles.
        row1 = conn.execute(
            "SELECT rowid, username, handle FROM users WHERE username='Bob'"
        ).fetchone()
        row2 = conn.execute(
            "SELECT rowid, username, handle FROM users WHERE username='bob'"
        ).fetchone()
        assert row1["handle"] is not None, "Bob's handle must be non-NULL after 0043"
        assert row2["handle"] is not None, "bob's handle must be non-NULL after 0043"

        # The two handles must be DISTINCT case-insensitively (no collisions).
        assert row1["handle"].lower() != row2["handle"].lower(), (
            f"Handles must differ case-insensitively, got {row1['handle']!r} and {row2['handle']!r}"
        )

        # The lowest-rowid row kept its bare handle (username as-is).
        min_rowid = min(row1["rowid"], row2["rowid"])
        if row1["rowid"] == min_rowid:
            assert row1["handle"] == "Bob", (
                f"Lowest-rowid row (Bob, rowid {min_rowid}) should keep bare handle, got {row1['handle']!r}"
            )
            assert row2["handle"].startswith("bob_") and row2["handle"].endswith(
                str(row2["rowid"])
            ), (
                f"Higher-rowid row (bob, rowid {row2['rowid']}) should have suffixed handle, got {row2['handle']!r}"
            )
        else:
            assert row2["handle"] == "bob", (
                f"Lowest-rowid row (bob, rowid {min_rowid}) should keep bare handle, got {row2['handle']!r}"
            )
            assert row1["handle"].startswith("Bob_") and row1["handle"].endswith(
                str(row1["rowid"])
            ), (
                f"Higher-rowid row (Bob, rowid {row1['rowid']}) should have suffixed handle, got {row1['handle']!r}"
            )

    finally:
        conn.close()


@pytest.mark.asyncio
async def test_0044_move_redirect_columns(tmp_path):
    """Migration 0044 adds the move-out redirect columns to remote_users
    (MO-1). Four additive NULL-defaulted columns, no backfill."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()

    remote_users_cols = {
        r["name"] for r in await db.fetchall("PRAGMA table_info(remote_users)")
    }
    required = {
        "moved_to_user_id",
        "moved_to_instance_id",
        "move_issued_at",
        "move_link",
    }
    assert required <= remote_users_cols, (
        f"Missing move-redirect columns in remote_users: {required - remote_users_cols}"
    )

    await db.shutdown()


def _ensure_schema_version_for_test(conn: sqlite3.Connection) -> None:
    """Create the schema_version stamp table the runner relies on (the
    incremental backfill test applies migrations without run_migrations)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            description TEXT,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


@pytest.mark.asyncio
async def test_0051_allow_subscribers_columns_default_closed(tmp_path):
    """Migration 0051 adds ``allow_subscribers`` to ``spaces`` and to
    ``public_space_cache``, NOT NULL and defaulting to **0**.

    The default is the whole security property: every space that existed
    before this migration stops being publicly readable until its owner opts
    in. A default of 1 would silently keep relaying content under a model the
    owner never agreed to, so it is asserted at the DDL level — a row written
    without the column must read back as 0.
    """
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    try:
        for table in ("spaces", "public_space_cache"):
            cols = {
                r["name"]: r for r in await db.fetchall(f"PRAGMA table_info({table})")
            }
            assert "allow_subscribers" in cols, f"{table} missing allow_subscribers"
            col = cols["allow_subscribers"]
            assert col["notnull"] == 1, f"{table}.allow_subscribers must be NOT NULL"
            assert str(col["dflt_value"]) == "0", (
                f"{table}.allow_subscribers must default to 0 (fail-closed); "
                f"got {col['dflt_value']!r}"
            )

        # …and the default really applies to a row inserted without it.
        await db.enqueue(
            "INSERT INTO public_space_cache(space_id, instance_id, name)"
            " VALUES(?, ?, ?)",
            ("sp-default", "inst-default", "Defaulted"),
        )
        row = await db.fetchone(
            "SELECT allow_subscribers FROM public_space_cache WHERE space_id=?",
            ("sp-default",),
        )
        assert row["allow_subscribers"] == 0
    finally:
        await db.shutdown()
