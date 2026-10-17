"""``tests.db.connections`` closes what a test opened."""

import sqlite3

import pytest

from tests.db import connections


def test_close_all_closes_every_connection_opened():
    a = connections.connect(":memory:")
    b = connections.connect(":memory:", isolation_level=None)
    connections.close_all()
    for conn in (a, b):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    connections.close_all()  # idempotent
