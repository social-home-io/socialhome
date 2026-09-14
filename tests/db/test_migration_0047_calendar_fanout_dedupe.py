"""Migration 0047 — collapse duplicated household calendar fan-out rows.

A household event shared with N members is one ``calendar_events`` row per
member's calendar, all stamped with the same ``client_event_uuid`` (0004).
A SPA bug POSTed brand-new rows on every *edit* instead of PATCHing, so a
group accumulated copies on the same calendar (12 rows across 5 calendars
in the production report).

These tests pin the destructive half — the right survivor is kept, RSVPs on
losing rows are re-homed rather than cascaded away, and the rows the
predicate must not touch (``remote_invite`` mirrors, ``NULL`` uuids) are
left alone — plus the partial unique index that stops it recurring.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from socialhome.db.migrations import discover_migrations, run_migrations

_MIG = (
    Path(__file__).resolve().parents[2]
    / "socialhome"
    / "migrations"
    / "0047_calendar_fanout_dedupe.sql"
)

#: The pre-0047 shape: ``calendar_events`` from 0001 plus 0002's ``tz`` and
#: 0004's ``client_event_uuid``, trimmed to the columns these tests touch,
#: and ``calendar_event_rsvps`` verbatim from 0001 (the FK + PK are the
#: whole point of the re-homing step).
_SCHEMA = """
CREATE TABLE calendars (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    color           TEXT NOT NULL DEFAULT '#4A90E2',
    owner_username  TEXT NOT NULL,
    calendar_type   TEXT NOT NULL DEFAULT 'personal'
                    CHECK(calendar_type IN ('personal','space'))
);
CREATE TABLE calendar_events (
    id              TEXT PRIMARY KEY,
    calendar_id     TEXT NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    summary         TEXT NOT NULL,
    start_dt        TEXT NOT NULL,
    end_dt          TEXT NOT NULL,
    mirrored_from   TEXT,
    origin          TEXT NOT NULL DEFAULT 'local'
                    CHECK(origin IN ('local','remote_invite')),
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    client_event_uuid TEXT
);
CREATE TABLE calendar_event_rsvps (
    event_id      TEXT NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    occurrence_at TEXT NOT NULL,
    status        TEXT NOT NULL CHECK(
        status IN ('accepted','declined','tentative')
    ),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, user_id, occurrence_at)
);
"""


def _apply(conn: sqlite3.Connection) -> None:
    conn.executescript(_MIG.read_text(encoding="utf-8"))


def _add_event(
    conn: sqlite3.Connection,
    event_id: str,
    calendar_id: str,
    *,
    uuid: str | None,
    created_at: str,
    origin: str = "local",
    summary: str = "Dinner",
    mirrored_from: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO calendar_events (id, calendar_id, summary, start_dt,"
        " end_dt, origin, mirrored_from, created_by, created_at, updated_at,"
        " client_event_uuid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            calendar_id,
            summary,
            "2026-05-01T18:00:00+00:00",
            "2026-05-01T19:00:00+00:00",
            origin,
            mirrored_from,
            "u-alice",
            created_at,
            created_at,
            uuid,
        ),
    )


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(_SCHEMA)
    for n in range(1, 6):
        c.execute(
            "INSERT INTO calendars (id, name, owner_username) VALUES (?, ?, ?)",
            (f"cal-{n}", f"Calendar {n}", f"u-{n}"),
        )
    yield c
    c.close()


def _seed_fanout(conn: sqlite3.Connection) -> None:
    """The production shape: 12 rows across 5 calendars, one uuid.

    cal-1 got four copies, cal-2 three, cal-3 two, cal-4 two, cal-5 one.
    ``created_at`` is distinct everywhere so "oldest" is unambiguous.
    Both components are zero-padded: an unpadded ``09:0{n}:00`` emits
    ``09:010:00`` from the tenth row on, which SQLite stores happily as
    a text column but which is not a timestamp — and this is the
    fixture named after the production shape, so it has to look like
    production data.
    """
    layout = {"cal-1": 4, "cal-2": 3, "cal-3": 2, "cal-4": 2, "cal-5": 1}
    n = 0
    for calendar_id, copies in layout.items():
        for copy in range(copies):
            n += 1
            _add_event(
                conn,
                f"ev-{n}",
                calendar_id,
                uuid="uuid-shared",
                # Later copies are strictly newer than the original.
                created_at=f"2026-05-{copy + 1:02d} 09:{n:02d}:00",
            )


def test_fanout_group_collapses_to_one_row_per_calendar(conn):
    """12 rows across 5 calendars become 5 — the oldest on each survives."""
    _seed_fanout(conn)
    assert conn.execute("SELECT count(*) FROM calendar_events").fetchone()[0] == 12

    _apply(conn)

    rows = conn.execute(
        "SELECT calendar_id, id, created_at FROM calendar_events"
        " WHERE client_event_uuid='uuid-shared' ORDER BY calendar_id"
    ).fetchall()
    assert len(rows) == 5
    assert [r["calendar_id"] for r in rows] == [
        "cal-1",
        "cal-2",
        "cal-3",
        "cal-4",
        "cal-5",
    ]
    # The survivor on cal-1 is ev-1 (2026-05-01), not ev-2/3/4.
    assert {(r["calendar_id"], r["id"]) for r in rows} == {
        ("cal-1", "ev-1"),
        ("cal-2", "ev-5"),
        ("cal-3", "ev-8"),
        ("cal-4", "ev-10"),
        ("cal-5", "ev-12"),
    }


def test_rsvp_on_a_losing_row_is_rehomed_onto_the_survivor(conn):
    """An RSVP must survive the de-dup — the FK would cascade it away."""
    _seed_fanout(conn)
    conn.execute(
        "INSERT INTO calendar_event_rsvps"
        " (event_id, user_id, occurrence_at, status, updated_at)"
        " VALUES ('ev-3', 'u-bob', '2026-05-01T18:00:00+00:00', 'accepted',"
        " '2026-05-02 10:00:00')"
    )

    _apply(conn)

    rows = conn.execute(
        "SELECT event_id, status FROM calendar_event_rsvps WHERE user_id='u-bob'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["event_id"] == "ev-1", "RSVP was cascaded away, not re-homed"
    assert rows[0]["status"] == "accepted"


def test_colliding_rsvp_does_not_raise_and_keeps_the_survivors_status(conn):
    """The survivor may already hold a row for that (user, occurrence)."""
    _seed_fanout(conn)
    occ = "2026-05-01T18:00:00+00:00"
    conn.execute(
        "INSERT INTO calendar_event_rsvps"
        " (event_id, user_id, occurrence_at, status, updated_at)"
        f" VALUES ('ev-1', 'u-bob', '{occ}', 'accepted', '2026-05-01 10:00:00')"
    )
    conn.execute(
        "INSERT INTO calendar_event_rsvps"
        " (event_id, user_id, occurrence_at, status, updated_at)"
        f" VALUES ('ev-4', 'u-bob', '{occ}', 'declined', '2026-05-04 10:00:00')"
    )

    _apply(conn)

    rows = conn.execute(
        "SELECT event_id, status FROM calendar_event_rsvps WHERE user_id='u-bob'"
    ).fetchall()
    assert len(rows) == 1
    assert (rows[0]["event_id"], rows[0]["status"]) == ("ev-1", "accepted")


def test_remote_invite_row_with_the_same_uuid_survives(conn):
    """A peer's mirror row shares the uuid but is out of scope."""
    _seed_fanout(conn)
    _add_event(
        conn,
        "ev-mirror",
        "cal-5",
        uuid="uuid-shared",
        created_at="2026-05-09 09:00:00",
        origin="remote_invite",
    )

    _apply(conn)

    assert (
        conn.execute(
            "SELECT count(*) FROM calendar_events WHERE id='ev-mirror'"
        ).fetchone()[0]
        == 1
    )


def test_null_uuid_rows_are_never_touched(conn):
    """Legacy / imported rows have no uuid — duplicates there are real events."""
    for n in range(1, 4):
        _add_event(
            conn,
            f"ev-null-{n}",
            "cal-1",
            uuid=None,
            created_at=f"2026-05-0{n} 08:00:00",
        )

    _apply(conn)

    assert (
        conn.execute(
            "SELECT count(*) FROM calendar_events WHERE client_event_uuid IS NULL"
        ).fetchone()[0]
        == 3
    )


def test_clean_database_is_untouched(conn):
    """One row per calendar already — the migration is a no-op."""
    for n in range(1, 6):
        _add_event(
            conn,
            f"ev-clean-{n}",
            f"cal-{n}",
            uuid="uuid-clean",
            created_at="2026-05-01 09:00:00",
        )
    before = [
        tuple(r)
        for r in conn.execute("SELECT id, calendar_id FROM calendar_events ORDER BY id")
    ]

    _apply(conn)

    after = [
        tuple(r)
        for r in conn.execute("SELECT id, calendar_id FROM calendar_events ORDER BY id")
    ]
    assert after == before


def test_index_blocks_a_second_local_copy(conn):
    """The guard: the SPA can no longer mint a duplicate on one calendar."""
    _add_event(
        conn, "ev-a", "cal-1", uuid="uuid-guard", created_at="2026-05-01 09:00:00"
    )

    _apply(conn)

    with pytest.raises(sqlite3.IntegrityError):
        _add_event(
            conn, "ev-b", "cal-1", uuid="uuid-guard", created_at="2026-05-02 09:00:00"
        )


def test_index_allows_remote_invite_and_null_uuid_rows(conn):
    """The predicate scopes the guard to the local fan-out set only."""
    _add_event(
        conn, "ev-a", "cal-1", uuid="uuid-guard", created_at="2026-05-01 09:00:00"
    )

    _apply(conn)

    _add_event(
        conn,
        "ev-mirror",
        "cal-1",
        uuid="uuid-guard",
        created_at="2026-05-02 09:00:00",
        origin="remote_invite",
    )
    _add_event(conn, "ev-null-1", "cal-1", uuid=None, created_at="2026-05-03 09:00:00")
    _add_event(conn, "ev-null-2", "cal-1", uuid=None, created_at="2026-05-04 09:00:00")

    assert conn.execute("SELECT count(*) FROM calendar_events").fetchone()[0] == 4


def test_discovered_as_version_47():
    """The runner picks it up in order, with no duplicate-version clash."""
    versions = [m.version for m in discover_migrations()]
    assert 47 in versions
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))


def test_newest_answer_wins_between_two_losing_rows(conn):
    """Two losers both hold a reply — the most recent one must land.

    The survivor has no row for that ``(user_id, occurrence_at)``, so
    ``INSERT OR IGNORE`` keeps whichever loser the join emits first.
    Without ``ORDER BY r.updated_at DESC`` that is arbitrary and can
    resurrect a stale answer the member has since changed.
    """
    _seed_fanout(conn)
    occ = "2026-05-01T18:00:00+00:00"
    # ev-2, ev-3 and ev-4 are all losers on cal-1 (ev-1 survives).
    conn.execute(
        "INSERT INTO calendar_event_rsvps"
        " (event_id, user_id, occurrence_at, status, updated_at)"
        f" VALUES ('ev-2', 'u-bob', '{occ}', 'accepted', '2026-05-02 10:00:00')"
    )
    conn.execute(
        "INSERT INTO calendar_event_rsvps"
        " (event_id, user_id, occurrence_at, status, updated_at)"
        f" VALUES ('ev-4', 'u-bob', '{occ}', 'declined', '2026-05-09 10:00:00')"
    )

    _apply(conn)

    rows = conn.execute(
        "SELECT event_id, status FROM calendar_event_rsvps WHERE user_id='u-bob'"
    ).fetchall()
    assert len(rows) == 1
    assert (rows[0]["event_id"], rows[0]["status"]) == ("ev-1", "declined")


def test_lowest_id_survives_a_created_at_tie(conn):
    """Same-second copies: ``id`` breaks the tie deterministically."""
    for n in ("ev-c", "ev-a", "ev-b"):
        _add_event(conn, n, "cal-1", uuid="uuid-tie", created_at="2026-05-01 09:00:00")

    _apply(conn)

    rows = conn.execute(
        "SELECT id FROM calendar_events WHERE client_event_uuid='uuid-tie'"
    ).fetchall()
    assert [r["id"] for r in rows] == ["ev-a"]


def test_mirrored_row_with_a_fanout_uuid_is_left_alone(conn):
    """A space mirror carries ``mirrored_from`` — never a fan-out copy.

    ``SpaceRsvpMirrorBridge`` writes ``origin='local'`` +
    ``mirrored_from=<source id>``; a PATCH can stamp a
    ``client_event_uuid`` on it. Collapsing it into the fan-out would
    delete the user's mirror of a space event — silent data loss.
    """
    _seed_fanout(conn)
    _add_event(
        conn,
        "ev-space-mirror",
        "cal-1",
        uuid="uuid-shared",
        created_at="2026-05-09 09:00:00",
        mirrored_from="space-ev-1",
    )

    _apply(conn)

    assert (
        conn.execute(
            "SELECT count(*) FROM calendar_events WHERE id='ev-space-mirror'"
        ).fetchone()[0]
        == 1
    )
    # ...and the index does not constrain it either: a second mirror with
    # the same uuid on the same calendar still inserts.
    _add_event(
        conn,
        "ev-space-mirror-2",
        "cal-1",
        uuid="uuid-shared",
        created_at="2026-05-10 09:00:00",
        mirrored_from="space-ev-2",
    )


def test_index_predicate_matches_the_dedupe_scope(conn):
    """The guard's WHERE clause is the de-dup's WHERE clause, verbatim."""
    _apply(conn)

    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='ux_calendar_events_fanout'"
    ).fetchone()[0]
    normalised = " ".join(sql.split()).lower()
    assert "client_event_uuid is not null" in normalised
    assert "origin = 'local'" in normalised
    assert "mirrored_from is null" in normalised


def _migrate_to(conn: sqlite3.Connection, version: int) -> None:
    """Apply the real migration chain up to (and including) ``version``."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    current = int(
        conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    )
    for m in discover_migrations():
        if m.version > version:
            break
        if m.version <= current:
            continue
        m.apply(conn)
        conn.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (m.version, m.description),
        )


def test_real_chain_dedupes_and_leaves_no_orphan_rsvps(tmp_path):
    """0047 against the REAL pre-0047 schema, on the real upgrade path.

    The hand-rolled ``_SCHEMA`` above is a fast model, not production: it
    cannot catch a column-name drift, and its fixture forces
    ``PRAGMA foreign_keys=ON`` — a state the real chain does not
    guarantee (0046 runs ``PRAGMA foreign_keys=OFF`` and its restore is a
    silent no-op inside a transaction). This test seeds through
    ``run_migrations`` itself and asserts the post-state production
    actually reaches: no duplicates, the RSVP re-homed, no orphan RSVP
    rows, and a clean ``PRAGMA foreign_key_check``.
    """
    # ``isolation_level=None`` mirrors ``AsyncDatabase._open`` — the
    # pragma juggling in 0046 behaves differently under the legacy
    # implicit-transaction mode, and this test is about production.
    conn = sqlite3.connect(tmp_path / "real.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_to(conn, 46)
    assert (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='ux_calendar_events_fanout'"
        ).fetchone()[0]
        == 0
    ), "0047 must not have run yet"

    conn.execute(
        "INSERT INTO users (username, user_id, display_name)"
        " VALUES ('alice', 'uid-alice', 'Alice')"
    )
    conn.execute(
        "INSERT INTO calendars (id, name, color, owner_username, calendar_type)"
        " VALUES ('cal-alice', 'Alice', '#4A90E2', 'alice', 'personal')"
    )
    for n, created in ((1, "2026-05-01 09:00:00"), (2, "2026-05-02 09:00:00")):
        conn.execute(
            "INSERT INTO calendar_events (id, calendar_id, summary, start_dt,"
            " end_dt, origin, created_by, created_at, updated_at,"
            " client_event_uuid) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"ev-{n}",
                "cal-alice",
                "Dinner",
                "2026-05-10T18:00:00+00:00",
                "2026-05-10T19:00:00+00:00",
                "local",
                "uid-alice",
                created,
                created,
                "uuid-shared",
            ),
        )
    conn.execute(
        "INSERT INTO calendar_event_rsvps"
        " (event_id, user_id, occurrence_at, status, updated_at)"
        " VALUES ('ev-2', 'uid-alice', '2026-05-10T18:00:00+00:00',"
        " 'accepted', '2026-05-03 09:00:00')"
    )

    run_migrations(conn)

    events = conn.execute(
        "SELECT id FROM calendar_events WHERE client_event_uuid='uuid-shared'"
    ).fetchall()
    assert [r["id"] for r in events] == ["ev-1"]

    rsvps = conn.execute("SELECT event_id, status FROM calendar_event_rsvps").fetchall()
    assert [(r["event_id"], r["status"]) for r in rsvps] == [("ev-1", "accepted")]

    orphans = conn.execute(
        "SELECT count(*) FROM calendar_event_rsvps r"
        " WHERE NOT EXISTS (SELECT 1 FROM calendar_events e WHERE e.id = r.event_id)"
    ).fetchone()[0]
    assert orphans == 0, "losing rows' RSVPs were left behind as orphans"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_real_chain_leaves_no_orphans_with_fk_enforcement_off(tmp_path):
    """0047 must not depend on the ambient ``foreign_keys`` pragma.

    0046 turns enforcement OFF and restores it in a ``finally`` that is a
    silent no-op when a transaction is open, so whether ``ON DELETE
    CASCADE`` fires during 0047 is path-dependent. The explicit
    ``DELETE FROM calendar_event_rsvps`` makes the outcome identical
    either way — without it this database ends up permanently failing
    ``PRAGMA foreign_key_check``.
    """
    conn = sqlite3.connect(tmp_path / "nofk.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    _migrate_to(conn, 46)
    conn.execute(
        "INSERT INTO users (username, user_id, display_name)"
        " VALUES ('alice', 'uid-alice', 'Alice')"
    )
    conn.execute(
        "INSERT INTO calendars (id, name, color, owner_username, calendar_type)"
        " VALUES ('cal-alice', 'Alice', '#4A90E2', 'alice', 'personal')"
    )
    for n, created in ((1, "2026-05-01 09:00:00"), (2, "2026-05-02 09:00:00")):
        conn.execute(
            "INSERT INTO calendar_events (id, calendar_id, summary, start_dt,"
            " end_dt, origin, created_by, created_at, updated_at,"
            " client_event_uuid) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"ev-{n}",
                "cal-alice",
                "Dinner",
                "2026-05-10T18:00:00+00:00",
                "2026-05-10T19:00:00+00:00",
                "local",
                "uid-alice",
                created,
                created,
                "uuid-shared",
            ),
        )
    conn.execute(
        "INSERT INTO calendar_event_rsvps"
        " (event_id, user_id, occurrence_at, status, updated_at)"
        " VALUES ('ev-2', 'uid-alice', '2026-05-10T18:00:00+00:00',"
        " 'accepted', '2026-05-03 09:00:00')"
    )

    # The state 0046 can leave behind.
    conn.execute("PRAGMA foreign_keys=OFF")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0

    run_migrations(conn)

    rsvps = conn.execute("SELECT event_id FROM calendar_event_rsvps").fetchall()
    assert [r["event_id"] for r in rsvps] == ["ev-1"]
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
