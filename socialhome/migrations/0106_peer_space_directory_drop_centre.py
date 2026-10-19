"""0106 — no local-community centre in the paired-household directory.

Follows 0105 (peer space directory local community).

The first, never-released revision of 0105 stored a paired household's
local-community CENTRE (``peer_space_directory.lat`` / ``lon`` plus a
``radius_km`` whose CHECK named them). 0105 was rewritten in place before
the release (owner decision 2026-10-19: the centre never leaves the host),
so a production database never had those columns and this migration does
nothing there. A development database that ran the first revision has
them — and its ``radius_km`` CHECK would refuse every row the current code
writes (a radius with no centre). Here such a database drops the three
columns (the radius first: its CHECK names the other two, and SQLite will
not drop a column another constraint references) and gains 0105's current
one, so both kinds of database end with the same shape. The area values
are dropped with the columns: the table is a cache, refilled by the
host's next snapshot (its next public-space change, or this household's
next home change toward it) — development data only.

A Python migration because SQL cannot say "only if the column exists".
Self-contained (no app imports) so later app edits cannot change it.

CLAUDE.md "audit before a migration":

  (1) Audited: the only SQL on ``peer_space_directory`` is
      ``repositories/peer_space_directory_repo.py``; the only writer of
      the area columns is ``replace_snapshot`` (a delete + insert per
      peer), which no longer names ``lat`` / ``lon``.
  (2) Alternatives rejected: leaving the dead columns (the stale CHECK
      breaks every insert, and stale centres would sit on disk); asking
      developers to delete their database (loses every other table);
      rebuilding the table (the three DROP COLUMNs reach the same shape
      without copying rows).
  (3) Smallest change: a no-op wherever 0105 ran in its released shape;
      the DROP COLUMNs + one ADD COLUMN otherwise.
"""

from __future__ import annotations

import sqlite3

_TABLE = "peer_space_directory"

#: 0105's columns, verbatim (keep the two in step — a test compares the
#: resulting schemas).
_ADD = (
    (
        "ALTER TABLE peer_space_directory ADD COLUMN radius_km REAL\n"
        "    CHECK (radius_km IS NULL OR radius_km BETWEEN 0.5 AND 50)"
    ),
)


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}


def migrate(conn: sqlite3.Connection) -> None:
    cols = _columns(conn)
    if "lat" not in cols and "lon" not in cols:
        return
    # The radius first: its first-revision CHECK names lat / lon.
    for col in ("in_area", "radius_km", "lat", "lon"):
        if col in cols:
            conn.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {col}")
    for stmt in _ADD:
        conn.execute(stmt)
