"""Migration 0091 — lifting a space's archive drops the §25.6 echo
(``space_instances.applied_seq``) for that space, so the next periodic sync
streams in full and delivers what the archive refused."""

from __future__ import annotations

import sqlite3

from socialhome.db.migrations import discover_migrations

_VERSION = 91


def _apply_through(conn: sqlite3.Connection, last: int) -> None:
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    for mig in discover_migrations():
        if mig.version in done:
            continue
        if mig.version > last:
            break
        mig.apply(conn)
        conn.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (mig.version, mig.description),
        )


def _db(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    _apply_through(c, _VERSION)
    for sid in ("sp", "other"):
        c.execute(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key, archived) VALUES(?, ?, 'host', 'anna', 'ab', 1)",
            (sid, sid),
        )
        for peer in ("h1", "h2"):
            c.execute(
                "INSERT INTO space_instances(space_id, instance_id, applied_seq)"
                " VALUES(?, ?, 40)",
                (sid, peer),
            )
    return c


def _echo(c: sqlite3.Connection, sid: str) -> list:
    return [
        r[0]
        for r in c.execute(
            "SELECT applied_seq FROM space_instances WHERE space_id=?"
            " ORDER BY instance_id",
            (sid,),
        )
    ]


def test_unarchiving_drops_the_spaces_echo(tmp_path):
    c = _db(tmp_path)
    c.execute("UPDATE spaces SET archived=0 WHERE id='sp'")
    assert _echo(c, "sp") == [None, None]
    assert _echo(c, "other") == [40, 40]


def test_other_space_updates_keep_the_echo(tmp_path):
    c = _db(tmp_path)
    c.execute("UPDATE spaces SET archived=1, name='renamed' WHERE id='sp'")
    c.execute("UPDATE spaces SET name='again' WHERE id='sp'")
    assert _echo(c, "sp") == [40, 40]
    # An upsert that lifts the archive (a host's refreshed metadata) fires too.
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'sp', 'host', 'anna', 'ab')"
        " ON CONFLICT(id) DO UPDATE SET archived=0"
    )
    assert _echo(c, "sp") == [None, None]
