"""Migration 0108 — superseded before release: it changes nothing (the
directory keeps no verdict; a development database that ran its first
revision is cleaned by 0110)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 108


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
    yield c
    c.close()


def _schema(c) -> list[tuple]:
    return list(c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name"))


def test_it_changes_nothing(conn):
    before = _schema(conn)
    apply_through(conn, _VERSION)
    assert _schema(conn) == before
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 108
