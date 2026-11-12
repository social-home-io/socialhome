"""Migration 0146 — vote stamps, the close's stamp, and the touch triggers
that put a schedule answer, an RSVP and a bazaar bid on the incremental
space sync.

Additive: two NULL-default columns and triggers; no existing row changes.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 146


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    for post_id, kind in (
        ("p-poll", "poll"),
        ("p-date", "schedule"),
        ("p-bike", "bazaar"),
    ):
        c.execute(
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES(?,'sp','u-anna',?,'')",
            (post_id, kind),
        )
    c.execute(
        "INSERT INTO space_polls(post_id, question, closed) VALUES('p-poll','Q',1)"
    )
    c.execute(
        "INSERT INTO space_poll_options(id, post_id, text) VALUES('o-1','p-poll','Yes')"
    )
    c.execute(
        "INSERT INTO space_poll_votes(option_id, voter_user_id) VALUES('o-1','u-b')"
    )
    c.execute(
        "INSERT INTO space_schedule_poll_meta(post_id, title) VALUES('p-date','When')"
    )
    c.execute(
        "INSERT INTO space_schedule_slots(id, post_id, slot_date)"
        " VALUES('s-1','p-date','2026-12-01')"
    )
    c.execute(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,"
        " title, end_time, currency) VALUES('p-bike','sp','u-anna','offer',"
        " 'Bike','2099-01-01T00:00:00+00:00','EUR')"
    )
    c.execute(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, end_dt,"
        " created_by) VALUES('ev-1','sp','Picnic','2026-12-01T10:00:00+00:00',"
        " '2026-12-01T12:00:00+00:00','u-anna')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def _post_seq(c: sqlite3.Connection, post_id: str) -> int:
    return c.execute(
        "SELECT sync_seq FROM space_posts WHERE id=?", (post_id,)
    ).fetchone()[0]


def _event_seq(c: sqlite3.Connection) -> int:
    return c.execute(
        "SELECT sync_seq FROM space_calendar_events WHERE id='ev-1'"
    ).fetchone()[0]


def test_an_existing_poll_and_its_votes_are_left_as_they_were(conn):
    row = conn.execute("SELECT * FROM space_polls WHERE post_id='p-poll'").fetchone()
    assert row["closed"] == 1
    assert row["vote_stamps_json"] is None and row["closed_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM space_poll_votes").fetchone()[0] == 1


def test_a_vote_stamp_or_a_close_stamp_re_stamps_the_post(conn):
    before = _post_seq(conn, "p-poll")
    conn.execute("UPDATE space_polls SET vote_stamps_json='{}' WHERE post_id='p-poll'")
    after = _post_seq(conn, "p-poll")
    assert after > before
    conn.execute("UPDATE space_polls SET closed_at='2026-11-12 09:00:00.000000'")
    assert _post_seq(conn, "p-poll") > after


def test_rewriting_a_poll_with_what_it_holds_stamps_nothing(conn):
    conn.execute("UPDATE space_polls SET vote_stamps_json='{}', closed_at='t'")
    before = _post_seq(conn, "p-poll")
    conn.execute(
        "UPDATE space_polls SET vote_stamps_json='{}', closed_at='t', closed=1"
    )
    assert _post_seq(conn, "p-poll") == before


def test_a_schedule_answer_re_stamps_its_post(conn):
    seq = _post_seq(conn, "p-date")
    conn.execute(
        "INSERT INTO space_schedule_responses(slot_id, user_id, availability)"
        " VALUES('s-1','u-b','yes')"
    )
    added = _post_seq(conn, "p-date")
    assert added > seq
    conn.execute("UPDATE space_schedule_responses SET availability='no'")
    changed = _post_seq(conn, "p-date")
    assert changed > added
    # The same answer again (a re-delivered event rewrites ``responded_at``).
    conn.execute(
        "UPDATE space_schedule_responses SET availability='no',"
        " responded_at='2026-11-12T09:00:00+00:00'"
    )
    assert _post_seq(conn, "p-date") == changed
    conn.execute("DELETE FROM space_schedule_responses")
    assert _post_seq(conn, "p-date") > changed
    assert _post_seq(conn, "p-poll") is not None  # other posts untouched


def test_an_rsvp_re_stamps_its_event(conn):
    seq = _event_seq(conn)
    conn.execute(
        "INSERT INTO space_calendar_rsvps(event_id, user_id, occurrence_at, status)"
        " VALUES('ev-1','u-b','2026-12-01T10:00:00+00:00','going')"
    )
    added = _event_seq(conn)
    assert added > seq
    conn.execute("UPDATE space_calendar_rsvps SET status='maybe'")
    changed = _event_seq(conn)
    assert changed > added
    conn.execute(
        "UPDATE space_calendar_rsvps SET status='maybe',"
        " updated_at='2026-11-12T09:00:00+00:00'"
    )
    assert _event_seq(conn) == changed
    conn.execute("DELETE FROM space_calendar_rsvps")
    assert _event_seq(conn) > changed


def test_a_bid_re_stamps_its_listings_post(conn):
    seq = _post_seq(conn, "p-bike")
    conn.execute(
        "INSERT INTO bazaar_bids(id, listing_post_id, bidder_user_id, amount)"
        " VALUES('b-1','p-bike','u-b',40)"
    )
    placed = _post_seq(conn, "p-bike")
    assert placed > seq
    conn.execute("UPDATE bazaar_bids SET withdrawn=1 WHERE id='b-1'")
    withdrawn = _post_seq(conn, "p-bike")
    assert withdrawn > placed
    # What never streams (the decline reason, the local arrival time) and a
    # rewrite of the same state stamp nothing.
    conn.execute(
        "UPDATE bazaar_bids SET withdrawn=1, rejection_reason='x',"
        " created_at='2026-11-12T09:00:00+00:00'"
    )
    assert _post_seq(conn, "p-bike") == withdrawn
    conn.execute("DELETE FROM bazaar_bids")
    assert _post_seq(conn, "p-bike") > withdrawn


def test_deleting_the_parent_with_its_children_does_not_fail(conn):
    conn.execute(
        "INSERT INTO space_schedule_responses(slot_id, user_id, availability)"
        " VALUES('s-1','u-b','yes')"
    )
    conn.execute(
        "INSERT INTO bazaar_bids(id, listing_post_id, bidder_user_id, amount)"
        " VALUES('b-1','p-bike','u-b',40)"
    )
    conn.execute("DELETE FROM space_posts WHERE id IN ('p-date','p-bike')")
    assert (
        conn.execute("SELECT COUNT(*) FROM space_schedule_responses").fetchone()[0] == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM bazaar_bids").fetchone()[0] == 0
