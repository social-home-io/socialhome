"""0110 — no local-community verdict in the paired-household directory.

Follows 0109 (local area attempts).

Two never-released revisions stored, per paired household, the host's
verdict on whether the home that household shared with it lies inside a
local community: ``peer_space_directory.in_area`` (first revision of 0105)
and ``area_edge`` (first revision of 0108). The verdict is gone (owner
decision 2026-10-19): a join carries the joiner's home rounded to ~1 km and
the host decides then, so the directory keeps only ``radius_km``. 0105 and
0108 were rewritten in place, so a production database never had the two
columns and this migration does nothing there; a development database that
ran the first revisions drops them here, ``area_edge`` first (its CHECK
names ``in_area``, and SQLite will not drop a column another constraint
references). The values are a cache the host's next snapshot refills.

A Python migration because SQL cannot say "only if the column exists".
Self-contained (no app imports) so later app edits cannot change it.

CLAUDE.md "audit before a migration":

  (1) Audited: the only SQL on ``peer_space_directory`` is
      ``repositories/peer_space_directory_repo.py``, which no longer names
      either column; nothing else reads them.
  (2) Alternatives rejected: leaving the dead columns (harmless to inserts,
      but a stale per-household location verdict would sit on disk — the
      model says the host keeps nothing about a joiner's home); asking
      developers to delete their database.
  (3) Smallest change: a no-op wherever 0105 / 0108 ran in their released
      shape; two DROP COLUMN otherwise.
"""

from __future__ import annotations

import sqlite3

_TABLE = "peer_space_directory"


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
    for col in ("area_edge", "in_area"):
        if col in cols:
            conn.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {col}")
