"""Migration 0049 — repair the synthetic ``uid-<username>`` household admin.

The first user of every standalone / ha household was minted with a made-up
``user_id`` (``uid-alice``) instead of a ``derive_user_id(instance_pk,
username)`` one. A synthetic id can never satisfy the public-space relay's
per-author self-cert, so every post that admin wrote was dropped by remote
subscribers.

Rewriting the id means rewriting it *everywhere*: ~20 columns carry an
``REFERENCES users(user_id)`` FK and ~90 more carry a bare user-id string
(``space_posts.author``, ``space_members.user_id``, ``tasks.created_by``, …)
with no FK because the value may legitimately be a remote user. These tests
pin that nothing is orphaned, that non-synthetic rows are untouched, that a
never-booted DB is a clean no-op, and that a re-run changes nothing.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.db.migrations import discover_migrations, run_migrations

_MIG = (
    Path(__file__).resolve().parents[2]
    / "socialhome"
    / "migrations"
    / "0049_derived_admin_user_id.py"
)

#: Migrations up to (but excluding) the one under test.
_VERSION = 49


def _load_migrate():
    spec = importlib.util.spec_from_file_location("mig0049", _MIG)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


@pytest.fixture
def kp():
    return generate_identity_keypair()


@pytest.fixture
def conn(tmp_path):
    """The REAL pre-0049 schema: every shipped migration below 0049.

    Deliberately not a hand-written subset — the migration walks the live
    schema, so the test has to give it the live schema. ``isolation_level=None``
    + ``foreign_keys=ON`` mirrors ``AsyncDatabase._open`` exactly, including
    the fact that the runner's ``with conn`` opens no transaction there.
    """
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    for mig in discover_migrations():
        if mig.version >= _VERSION:
            break
        mig.apply(c)
        c.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (mig.version, mig.description),
        )
    yield c
    c.close()


def _seed_identity(conn: sqlite3.Connection, kp) -> None:
    conn.execute(
        "INSERT INTO instance_identity"
        "(instance_id, identity_private_key, identity_public_key, routing_secret)"
        " VALUES(?,?,?,?)",
        (
            derive_instance_id(kp.public_key),
            kp.private_key.hex(),
            kp.public_key.hex(),
            "aa" * 32,
        ),
    )


def _seed_user(conn: sqlite3.Connection, username: str, user_id: str) -> None:
    conn.execute(
        "INSERT INTO users(username, user_id, display_name, is_admin,"
        " identity_anchor, handle) VALUES(?,?,?,1,?,?)",
        (username, user_id, username.title(), username, username),
    )


def _seed_children(conn: sqlite3.Connection, user_id: str, suffix: str) -> None:
    """Rows in a representative spread: FK'd, FK-less, and an app table."""
    # FK → users(user_id)
    conn.execute(
        "INSERT INTO notifications(id, user_id, type, title)"
        " VALUES(?,?,'mention','hi')",
        (f"n-{suffix}", user_id),
    )
    conn.execute(
        "INSERT INTO installed_apps(app_id, name, version, bundle_path,"
        " bundle_sha256, source_url, installed_by, installed_at)"
        " VALUES(?,'Chess','1.0','p','sha','http://x',?,'2026-01-01T00:00:00+00:00')",
        (f"chess-{suffix}", user_id),
    )
    # No FK — the column may hold a REMOTE user id, which is exactly why
    # enumerating only ``PRAGMA foreign_key_list`` would orphan these.
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,'Space','inst',?,'ab')",
        (f"sp-{suffix}", "alice"),
    )
    conn.execute(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,'owner')",
        (f"sp-{suffix}", user_id),
    )
    conn.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES(?,?,?,'text','hello')",
        (f"sp-post-{suffix}", f"sp-{suffix}", user_id),
    )
    conn.execute(
        "INSERT INTO task_lists(id, name, created_by) VALUES(?,'Chores',?)",
        (f"tl-{suffix}", user_id),
    )
    conn.execute(
        "INSERT INTO tasks(id, list_id, title, created_by) VALUES(?,?,'Bins',?)",
        (f"t-{suffix}", f"tl-{suffix}", user_id),
    )


#: (table, column) pairs seeded by :func:`_seed_children`.
_CHILD_COLS = (
    ("notifications", "user_id"),
    ("installed_apps", "installed_by"),
    ("space_members", "user_id"),
    ("space_posts", "author"),
    ("task_lists", "created_by"),
    ("tasks", "created_by"),
)


def _values(conn: sqlite3.Connection) -> dict[tuple[str, str], list[str]]:
    return {
        (t, c): sorted(r[0] for r in conn.execute(f"SELECT {c} FROM {t}"))
        for t, c in _CHILD_COLS
    }


# ── The repair ───────────────────────────────────────────────────────────────


def test_rewrites_admin_id_and_every_reference(conn, kp):
    _seed_identity(conn, kp)
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")

    _load_migrate()(conn)

    expected = derive_user_id(kp.public_key, "alice")
    row = conn.execute(
        "SELECT user_id, identity_anchor FROM users WHERE username='alice'"
    ).fetchone()
    assert row["user_id"] == expected
    # The derivation input is stored, so ``user_id ==
    # derive_user_id(pk, identity_anchor)`` holds for the repaired row too.
    assert row["identity_anchor"] == "alice"

    for (table, col), vals in _values(conn).items():
        assert vals == [expected], f"{table}.{col} was not rewritten"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_no_rows_are_lost(conn, kp):
    _seed_identity(conn, kp)
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")
    before = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t, _ in _CHILD_COLS
    }
    before["users"] = conn.execute("SELECT count(*) FROM users").fetchone()[0]

    _load_migrate()(conn)

    after = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t, _ in _CHILD_COLS
    }
    after["users"] = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    assert after == before


def test_leaves_correctly_derived_users_untouched(conn, kp):
    _seed_identity(conn, kp)
    good = derive_user_id(kp.public_key, "bob")
    _seed_user(conn, "bob", good)
    _seed_children(conn, good, "b")
    # A remote author id that merely lives in an FK-less column must survive.
    conn.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('sp-post-remote','sp-b','remote-user-id','text','x')"
    )
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")

    _load_migrate()(conn)

    assert (
        conn.execute("SELECT user_id FROM users WHERE username='bob'").fetchone()[0]
        == good
    )
    for (table, col), vals in _values(conn).items():
        assert good in vals, f"{table}.{col} lost bob's id"
    assert (
        conn.execute(
            "SELECT author FROM space_posts WHERE id='sp-post-remote'"
        ).fetchone()[0]
        == "remote-user-id"
    )


def test_no_instance_identity_is_a_clean_noop(conn):
    """A DB that never booted far enough to mint an identity must not crash."""
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")

    _load_migrate()(conn)

    assert (
        conn.execute("SELECT user_id FROM users WHERE username='alice'").fetchone()[0]
        == "uid-alice"
    )
    for (table, col), vals in _values(conn).items():
        assert vals == ["uid-alice"], f"{table}.{col} changed on the no-op path"


def test_is_idempotent(conn, kp):
    _seed_identity(conn, kp)
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")

    migrate = _load_migrate()
    migrate(conn)
    snapshot = _values(conn)
    migrate(conn)

    assert _values(conn) == snapshot
    assert conn.execute("SELECT user_id FROM users WHERE username='alice'").fetchone()[
        0
    ] == derive_user_id(kp.public_key, "alice")


def test_nothing_to_repair_is_a_noop(conn, kp):
    _seed_identity(conn, kp)
    good = derive_user_id(kp.public_key, "bob")
    _seed_user(conn, "bob", good)
    _seed_children(conn, good, "b")

    _load_migrate()(conn)

    assert _values(conn) == {(t, c): [good] for t, c in _CHILD_COLS}


def test_repairs_several_synthetic_admins(conn, kp):
    """A household that mirrored more than one HA person keeps them distinct."""
    _seed_identity(conn, kp)
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")
    _seed_user(conn, "bob", "uid-bob")
    _seed_children(conn, "uid-bob", "b")

    _load_migrate()(conn)

    ids = dict(conn.execute("SELECT username, user_id FROM users"))
    assert ids == {
        "alice": derive_user_id(kp.public_key, "alice"),
        "bob": derive_user_id(kp.public_key, "bob"),
    }
    authors = sorted(r[0] for r in conn.execute("SELECT author FROM space_posts"))
    assert authors == sorted(ids.values())


# ── Runs for real in the pipeline ────────────────────────────────────────────


def test_full_migration_pipeline_applies_cleanly(tmp_path):
    """The whole run, on the same connection shape production uses."""
    c = sqlite3.connect(tmp_path / "full.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    applied = run_migrations(c)
    assert _VERSION in {m.version for m in applied}
    # Enforcement must still be on for the rest of the boot.
    assert int(c.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


def test_remote_users_cache_is_left_alone(conn, kp):
    """A peer's identically-named admin must not be rewritten to OUR id.

    Two households that both took the headless default username produced the
    *same* synthetic string, so a ``uid-admin`` in ``remote_users`` may well
    be the peer's admin. It re-syncs from their profile broadcast once they
    upgrade; claiming it as ours would be identity confusion.
    """
    _seed_identity(conn, kp)
    _seed_user(conn, "admin", "uid-admin")
    _seed_children(conn, "uid-admin", "a")
    conn.execute(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id) VALUES('peer-instance','Peer','ab','k','k',"
        "'http://peer/inbox','inbox-1')"
    )
    conn.execute(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('uid-admin','peer-instance','admin','Peer Admin')"
    )

    _load_migrate()(conn)

    assert conn.execute("SELECT user_id FROM remote_users").fetchone()[0] == "uid-admin"
    assert conn.execute("SELECT user_id FROM users WHERE username='admin'").fetchone()[
        0
    ] == derive_user_id(kp.public_key, "admin")


# ── Pre-flight: a derived-id collision must never brick the boot ─────────────


def test_conflicting_derived_id_outside_users_skips_instead_of_raising(
    conn, kp, caplog
):
    """A UNIQUE conflict anywhere in the sweep is a *skip*, not a crash.

    The pre-flight used to cover ``users`` only, so a row elsewhere already
    holding the derived id (here ``space_members(space_id, user_id)``) blew up
    mid-sweep with a bare ``IntegrityError`` — which the runner turns into a
    failed ``AsyncDatabase.startup``, i.e. a household that cannot boot at all,
    on every attempt. A household that merely still cannot federate is
    strictly better, and a later run retries.
    """
    import logging

    _seed_identity(conn, kp)
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")
    derived = derive_user_id(kp.public_key, "alice")
    conn.execute(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-a',?,'member')",
        (derived,),
    )

    with caplog.at_level(logging.WARNING):
        _load_migrate()(conn)

    # Untouched — the repair is a no-op a later run can retry.
    assert (
        conn.execute("SELECT user_id FROM users WHERE username='alice'").fetchone()[0]
        == "uid-alice"
    )
    for (table, col), vals in _values(conn).items():
        assert "uid-alice" in vals, f"{table}.{col} was partially rewritten"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "space_members.user_id" in blob and "alice" in blob


# ── A user whose *username* looks like the retired id ────────────────────────


def test_a_user_named_like_the_retired_id_is_not_renamed(conn, kp):
    """``users.username`` / ``identity_anchor`` are outside the sweep.

    A local user literally called ``uid-alice`` would otherwise be renamed to
    the admin's derived id, their anchor rewritten and ``platform_users``
    cascaded — inflicting the exact bug this migration repairs, silently and
    permanently (``derive_user_id(pk, anchor) != user_id`` for them forever).
    """
    _seed_identity(conn, kp)
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")
    victim_id = derive_user_id(kp.public_key, "uid-alice")
    _seed_user(conn, "uid-alice", victim_id)
    conn.execute(
        "INSERT INTO platform_users(username, display_name) VALUES('uid-alice','V')"
    )

    _load_migrate()(conn)

    row = conn.execute(
        "SELECT username, user_id, identity_anchor FROM users WHERE user_id=?",
        (victim_id,),
    ).fetchone()
    assert row["username"] == "uid-alice"
    assert row["identity_anchor"] == "uid-alice"
    # Their self-cert still reproduces: derive(pk, anchor) == user_id.
    assert derive_user_id(kp.public_key, row["identity_anchor"]) == row["user_id"]
    assert (
        conn.execute(
            "SELECT count(*) FROM platform_users WHERE username='uid-alice'"
        ).fetchone()[0]
        == 1
    )
    # The admin was still repaired.
    assert conn.execute("SELECT user_id FROM users WHERE username='alice'").fetchone()[
        0
    ] == derive_user_id(kp.public_key, "alice")


# ── Ids embedded in JSON ────────────────────────────────────────────────────


def _seed_json_sites(conn: sqlite3.Connection, user_id: str) -> None:
    """A row in each shape the JSON sweep has to understand."""
    conn.execute(
        "UPDATE tasks SET assignees_json=? WHERE id='t-a'",
        (f'["{user_id}", "other-user"]',),
    )
    conn.execute(
        "INSERT INTO calendars(id, name, owner_username) VALUES('cal-a','Cal','alice')"
    )
    conn.execute(
        "INSERT INTO calendar_events(id, calendar_id, summary, start_dt, end_dt,"
        " attendees_json, created_by) VALUES('ev-a','cal-a','Dinner',"
        "'2026-01-01T18:00:00+00:00','2026-01-01T19:00:00+00:00',?,?)",
        (f'["{user_id}"]', user_id),
    )
    conn.execute(
        "INSERT INTO call_sessions(id, initiator_user_id, call_type,"
        " participant_user_ids) VALUES('call-a',?, 'audio', ?)",
        (user_id, f'["{user_id}", "other-user"]'),
    )
    conn.execute(
        "INSERT INTO highlights(id, author_user_id, highlight_date, audience_kind,"
        " audience_json, expires_at) VALUES('hl-a',?, '2026-01-01','users',?,"
        "'2026-02-01T00:00:00+00:00')",
        (user_id, f'["{user_id}", "other-user"]'),
    )
    conn.execute(
        "INSERT INTO feed_posts(id, author, type, content, reactions)"
        " VALUES('fp-a',?, 'text','hi',?)",
        (user_id, json.dumps({"❤": [user_id, "other-user"]})),
    )
    conn.execute(
        "UPDATE users SET preferences_json=? WHERE username='alice'",
        (
            json.dumps(
                {
                    "highlights": {
                        "default_audience": {
                            "kind": "users",
                            "ids": [user_id, "other-user"],
                        }
                    }
                }
            ),
        ),
    )


def test_ids_inside_json_columns_are_repaired(conn, kp):
    """Whole-cell matching is blind to an id inside a JSON document.

    Left unrepaired, every task assigned to the admin shows as unassigned,
    their call history loses them as a participant, and a ``users``-scoped
    highlight audience silently stops including them.
    """
    _seed_identity(conn, kp)
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")
    _seed_json_sites(conn, "uid-alice")

    _load_migrate()(conn)

    new_id = derive_user_id(kp.public_key, "alice")
    assert json.loads(
        conn.execute("SELECT assignees_json FROM tasks WHERE id='t-a'").fetchone()[0]
    ) == [new_id, "other-user"]
    assert json.loads(
        conn.execute(
            "SELECT attendees_json FROM calendar_events WHERE id='ev-a'"
        ).fetchone()[0]
    ) == [new_id]
    assert json.loads(
        conn.execute(
            "SELECT participant_user_ids FROM call_sessions WHERE id='call-a'"
        ).fetchone()[0]
    ) == [new_id, "other-user"]
    assert json.loads(
        conn.execute("SELECT audience_json FROM highlights WHERE id='hl-a'").fetchone()[
            0
        ]
    ) == [new_id, "other-user"]
    assert json.loads(
        conn.execute("SELECT reactions FROM feed_posts WHERE id='fp-a'").fetchone()[0]
    ) == {"❤": [new_id, "other-user"]}
    prefs = json.loads(
        conn.execute(
            "SELECT preferences_json FROM users WHERE username='alice'"
        ).fetchone()[0]
    )
    assert prefs["highlights"]["default_audience"]["ids"] == [new_id, "other-user"]


def test_malformed_json_is_left_alone_not_crashed_on(conn, kp, caplog):
    """A blob that does not parse is already broken; the boot must not die."""
    import logging

    _seed_identity(conn, kp)
    _seed_user(conn, "alice", "uid-alice")
    _seed_children(conn, "uid-alice", "a")
    conn.execute("UPDATE tasks SET assignees_json='[uid-alice' WHERE id='t-a'")

    with caplog.at_level(logging.WARNING):
        _load_migrate()(conn)

    assert (
        conn.execute("SELECT assignees_json FROM tasks WHERE id='t-a'").fetchone()[0]
        == "[uid-alice"
    )
    assert conn.execute("SELECT user_id FROM users WHERE username='alice'").fetchone()[
        0
    ] == derive_user_id(kp.public_key, "alice")
    assert any("not valid JSON" in r.getMessage() for r in caplog.records)
