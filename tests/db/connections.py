"""``sqlite3`` connections a migration test opens, closed at its teardown.

The migration tests drive the raw runner on plain ``sqlite3`` connections,
often several per test (an upgrade copy, a HEAD-schema copy). Opening them
through :func:`connect` hands them to ``tests/db/conftest.py``, which closes
every one after the test — so a helper that returns a connection can't leak
it (an unclosed ``sqlite3.Connection`` is a ``ResourceWarning``, which the
suite treats as an error).
"""

from __future__ import annotations

import sqlite3
from typing import Any

_OPEN: list[sqlite3.Connection] = []


def connect(database: Any, **kwargs: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(database, **kwargs)
    _OPEN.append(conn)
    return conn


def close_all() -> None:
    while _OPEN:
        _OPEN.pop().close()
