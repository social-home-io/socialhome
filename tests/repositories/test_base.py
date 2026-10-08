"""Tests for socialhome.repositories.base — shared repository helpers."""

from __future__ import annotations

import json
import sqlite3


from socialhome.repositories.base import (
    bool_col,
    dump_json,
    load_json,
    pick,
    retention_window_sql,
    row_to_dict,
    rows_to_dicts,
    sync_page_cursor,
)


# ── row_to_dict ───────────────────────────────────────────────────────────


def test_row_to_dict_none():
    """row_to_dict(None) returns None."""
    assert row_to_dict(None) is None


def test_row_to_dict_sqlite_row():
    """row_to_dict converts a sqlite3.Row to a plain dict."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 1 AS a, 'hello' AS b").fetchone()
    result = row_to_dict(row)
    assert result == {"a": 1, "b": "hello"}
    conn.close()


# ── rows_to_dicts ─────────────────────────────────────────────────────────


def test_rows_to_dicts_empty():
    """rows_to_dicts returns an empty list for an empty iterable."""
    assert rows_to_dicts([]) == []


def test_rows_to_dicts_multiple():
    """rows_to_dicts converts multiple sqlite3.Row objects to a list of dicts."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (x INTEGER, y TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'a')")
    conn.execute("INSERT INTO t VALUES (2, 'b')")
    rows = conn.execute("SELECT * FROM t ORDER BY x").fetchall()
    result = rows_to_dicts(rows)
    assert result == [{"x": 1, "y": "a"}, {"x": 2, "y": "b"}]
    conn.close()


# ── dump_json ─────────────────────────────────────────────────────────────


def test_dump_json_basic():
    """dump_json produces compact, sorted-key JSON."""
    result = dump_json({"b": 2, "a": 1})
    parsed = json.loads(result)
    assert parsed == {"a": 1, "b": 2}
    # Compact separators — no spaces
    assert " " not in result


def test_dump_json_stable():
    """dump_json produces the same string for the same input."""
    val = {"z": [3, 1, 2], "a": "hello"}
    assert dump_json(val) == dump_json(val)


def test_dump_json_list():
    """dump_json handles list input."""
    result = dump_json([1, 2, 3])
    assert json.loads(result) == [1, 2, 3]


# ── load_json ─────────────────────────────────────────────────────────────


def test_load_json_valid():
    """load_json parses valid JSON strings."""
    assert load_json('["a","b"]', []) == ["a", "b"]


def test_load_json_none_returns_default():
    """load_json returns the default when the value is None."""
    assert load_json(None, []) == []


def test_load_json_empty_string_returns_default():
    """load_json returns the default when the value is an empty string."""
    assert load_json("", {}) == {}


def test_load_json_invalid_returns_default():
    """load_json returns the default when the JSON is malformed."""
    assert load_json("not valid json", "fallback") == "fallback"


# ── bool_col ──────────────────────────────────────────────────────────────


def test_bool_col_zero():
    """bool_col(0) returns False."""
    assert bool_col(0) is False


def test_bool_col_one():
    """bool_col(1) returns True."""
    assert bool_col(1) is True


def test_bool_col_true():
    """bool_col(True) returns True."""
    assert bool_col(True) is True


def test_bool_col_false():
    """bool_col(False) returns False."""
    assert bool_col(False) is False


def test_bool_col_none():
    """bool_col(None) returns False (falsy)."""
    assert bool_col(None) is False


# ── pick ──────────────────────────────────────────────────────────────────


def test_pick_selects_present_keys():
    """pick returns only the requested keys that exist in the mapping."""
    mapping = {"a": 1, "b": 2, "c": 3}
    result = pick(mapping, ["a", "c"])
    assert result == {"a": 1, "c": 3}


def test_pick_ignores_missing_keys():
    """pick silently ignores keys that are not in the mapping."""
    mapping = {"a": 1}
    result = pick(mapping, ["a", "z"])
    assert result == {"a": 1}


def test_pick_empty_keys():
    """pick returns an empty dict when no keys are requested."""
    assert pick({"a": 1}, []) == {}


def test_pick_empty_mapping():
    """pick returns an empty dict when the mapping is empty."""
    assert pick({}, ["a", "b"]) == {}


# ── §25.6 sync paging helpers ─────────────────────────────────────────────


def test_retention_window_sql_keep_forever_adds_nothing():
    assert retention_window_sql("created_at", None) == ("", ())


def test_retention_window_sql_cutoff_and_exempt_types():
    sql, params = retention_window_sql("p.created_at", "2026-01-01 00:00:00")
    assert sql == " AND datetime(p.created_at) >= datetime(?)"
    assert params == ("2026-01-01 00:00:00",)
    sql, params = retention_window_sql(
        "p.created_at",
        "2026-01-01 00:00:00",
        type_col="p.type",
        exempt_types=("poll", "poll", "event"),
    )
    assert "p.type IN (SELECT value FROM json_each(?))" in sql
    assert params == ("2026-01-01 00:00:00", '["event", "poll"]')
    # Exempt types without a type column add nothing beyond the cutoff.
    sql, _ = retention_window_sql("c", "x", exempt_types=("poll",))
    assert "json_each" not in sql


def test_retention_window_sql_runs_in_sqlite():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id TEXT, created_at TEXT, type TEXT)")
    conn.executemany(
        "INSERT INTO t VALUES(?,?,?)",
        [
            ("new-iso", "2026-06-01T10:00:00+00:00", "text"),
            ("new-naive", "2026-06-01 10:00:00", "text"),
            ("old", "2025-01-01T10:00:00+00:00", "text"),
            ("old-poll", "2025-01-01T10:00:00+00:00", "poll"),
        ],
    )
    sql, params = retention_window_sql(
        "created_at", "2026-01-01 00:00:00", type_col="type", exempt_types=("poll",)
    )
    got = {r[0] for r in conn.execute("SELECT id FROM t WHERE 1=1" + sql, params)}
    assert got == {"new-iso", "new-naive", "old-poll"}


def test_sync_page_cursor():
    rows = [{"sync_rowid": 3}, {"sync_rowid": 9}]
    assert sync_page_cursor(rows, 2) == 9
    assert sync_page_cursor(rows, 3) is None
    assert sync_page_cursor([], 2) is None
