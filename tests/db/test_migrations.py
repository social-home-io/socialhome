"""Tests for socialhome.db.migrations — discover_migrations and run_migrations."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import (
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
