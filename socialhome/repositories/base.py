"""Small helpers shared across repositories.

Kept narrow on purpose — anything more opinionated belongs in the service
layer. The goal here is only to collapse common row-to-domain boilerplate.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a :class:`sqlite3.Row` to a plain dict, or ``None``.

    Using this keeps the service layer free of sqlite-specific types; a
    service that received a ``Row`` could tiptoe into using its column index
    access and couple itself to the DB layer.

    Implemented as ``dict(zip(keys, row))`` rather than the more obvious
    ``{k: row[k] ...}`` so it doesn't go through ``Row.__getitem__`` for
    every column. The name-based path has surfaced an
    ``IndexError: tuple index out of range`` on at least one operator's
    long-lived dev DB; iterating the Row positionally and zipping with
    the column descriptions sidesteps that codepath entirely. The two
    forms are equivalent for healthy Row instances — same keys, same
    values, same order.
    """
    if row is None:
        return None
    return dict(zip(row.keys(), row))


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(zip(r.keys(), r)) for r in rows]


def dump_json(value: Any) -> str:
    """Canonical JSON serialiser used by repositories for TEXT-as-JSON columns.

    Uses ``sort_keys=True`` + compact separators so equality checks on the
    DB column (e.g. idempotent upserts) are reliable.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_json(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def bool_col(value: Any) -> bool:
    """Interpret a SQLite BOOLEAN-as-INTEGER column.

    SQLite has no native bool type; ``0``/``1`` is the convention. This
    helper tolerates the occasional ``True``/``False`` that slips in
    through a direct binding.
    """
    return bool(value)


def pick(
    mapping: Mapping[str, Any],
    keys: Iterable[str],
) -> dict[str, Any]:
    """Return ``{k: mapping[k]}`` for every ``k`` present in ``mapping``.

    Useful when mapping a wide DB row down to the columns that happen to be
    present on a particular update payload.
    """
    return {k: mapping[k] for k in keys if k in mapping}
