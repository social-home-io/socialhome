"""Migration 0149 — a paired household's home already received at 4 dp
is rewritten to the ~3 km cell centre (``coarse_home_for_peer``); our own
home, and rows without a home, are untouched."""

from __future__ import annotations

import importlib
import random
import sqlite3

import pytest

from socialhome.domain.home_location import coarse_home_for_peer
from tests.migration_chain import apply_through

_VERSION = 149

_migration = importlib.import_module("socialhome.migrations.0149_coarsen_peer_homes")


def _peer(c: sqlite3.Connection, iid: str, lat, lon) -> None:
    c.execute(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, home_lat, home_lon)"
        " VALUES(?,?,?,'k1','k2','https://x/wh',?,'confirmed','manual',?,?)",
        (iid, iid, iid * 8, f"wh-{iid}", lat, lon),
    )


def _home(c: sqlite3.Connection, iid: str) -> tuple:
    row = c.execute(
        "SELECT home_lat, home_lon FROM remote_instances WHERE id=?", (iid,)
    ).fetchone()
    return row["home_lat"], row["home_lon"]


@pytest.fixture
def db(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO instance_identity(id, instance_id, identity_private_key,"
        " identity_public_key, routing_secret, home_lat, home_lon)"
        " VALUES('self', 'me', 'sk', 'pk', ?, 47.3769, 8.5417)",
        ("a" * 64,),
    )
    _peer(c, "precise", 52.5201, 13.4049)
    _peer(c, "nohome", None, None)
    _peer(c, "half", 48.1372, None)
    _peer(c, "bad", 123.0, 8.5)
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_a_precise_peer_home_becomes_the_cell_centre(db):
    assert _home(db, "precise") == coarse_home_for_peer(52.5201, 13.4049)


def test_rows_without_a_home_stay_empty(db):
    assert _home(db, "nohome") == (None, None)


def test_a_half_or_bad_peer_home_is_cleared(db):
    """No usable home means no home — never a precise one left behind."""
    assert _home(db, "half") == (None, None)
    assert _home(db, "bad") == (None, None)


def test_our_own_home_keeps_4_dp(db):
    row = db.execute(
        "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'"
    ).fetchone()
    assert (row["home_lat"], row["home_lon"]) == (47.3769, 8.5417)


def test_running_it_again_changes_nothing(db):
    before = _home(db, "precise")
    _migration.migrate(db)
    assert _home(db, "precise") == before


def test_its_grid_matches_the_domain_helper():
    """The migration carries its own copy of the grid (no app imports, so a
    later edit of the helper cannot change what 0149 did) — it must agree
    with ``coarse_home_for_peer`` today."""
    rng = random.Random(149)
    points = [(rng.uniform(-90, 90), rng.uniform(-180, 180)) for _ in range(5000)]
    points += [(89.9999, 10.0), (-89.9999, -10.0), (1.2345, 180.0), (1.2, -180.0)]
    for lat, lon in points:
        assert _migration._coarse(lat, lon) == coarse_home_for_peer(lat, lon)
