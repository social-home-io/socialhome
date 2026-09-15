"""Migration 0048 — collapse case-forked shopping store rows.

``shopping_stores.name`` is a plain ``TEXT PRIMARY KEY`` (BINARY
collation), so ``'Migros'`` and ``'migros'`` happily coexist as two
catalogue rows. PR #570 added entry-time case canonicalisation in
``SqliteShoppingRepo`` but shipped no migration, so every install that
forked a store before that landed still carries the forked rows.

The forking is not cosmetic: the SPA renders one section per *catalogue*
row and matches items with an exact ``i.store === section.key`` compare,
so an item whose casing doesn't match a catalogue row renders in no
section at all — not even the "No store" bucket, which is keyed on
``!i.store``. Items pointing at a store with no catalogue row at all
("orphans") disappear the same way.

These tests pin the repair (which spelling wins, what happens to the
sort order, orphan adoption, what must stay untouched), the durable
``NOCASE`` unique guard, and the fact that the whole file is re-runnable
— a ``.sql`` migration is not atomic, so a part-way failure re-runs it
from the top on the next boot.
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
    / "0048_shopping_store_case_dedupe.sql"
)

#: The pre-0048 shape: both shopping tables verbatim from
#: 0001_initial.sql (nothing between 0001 and 0048 touches them).
_SCHEMA = """
CREATE TABLE shopping_list_items (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    completed     INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT,
    store         TEXT
);
CREATE INDEX idx_shopping_items_store
    ON shopping_list_items(store) WHERE store IS NOT NULL;

CREATE TABLE shopping_stores (
    name        TEXT PRIMARY KEY,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_shopping_stores_order ON shopping_stores(sort_order);
"""


def _apply(conn: sqlite3.Connection) -> None:
    conn.executescript(_MIG.read_text(encoding="utf-8"))


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(_SCHEMA)
    yield c
    c.close()


def _add_store(conn: sqlite3.Connection, name: str, sort_order: int) -> None:
    conn.execute(
        "INSERT INTO shopping_stores(name, sort_order) VALUES (?, ?)",
        (name, sort_order),
    )


def _add_items(conn: sqlite3.Connection, store: str | None, count: int) -> None:
    """``count`` items all pointing at ``store`` (may be NULL / blank)."""
    existing = conn.execute("SELECT count(*) FROM shopping_list_items").fetchone()[0]
    for n in range(count):
        conn.execute(
            "INSERT INTO shopping_list_items(id, text, created_by, store)"
            " VALUES (?, ?, 'u-alice', ?)",
            (f"it-{existing + n}", f"Milk {existing + n}", store),
        )


def _stores(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (r["name"], r["sort_order"])
        for r in conn.execute(
            "SELECT name, sort_order FROM shopping_stores ORDER BY sort_order, name"
        )
    ]


def _item_stores(conn: sqlite3.Connection) -> list[str | None]:
    return [
        r["store"]
        for r in conn.execute("SELECT store FROM shopping_list_items ORDER BY id")
    ]


def _seed_fork(conn: sqlite3.Connection) -> None:
    """The production shape: three spellings of one store.

    ``Migros`` is the popular spelling (9 items) AND the lowest
    ``sort_order``; ``migros`` has a single stray item; ``MIGROS`` is a
    catalogue-only ghost nobody ever used.
    """
    _add_store(conn, "Migros", 2)
    _add_store(conn, "migros", 5)
    _add_store(conn, "MIGROS", 7)
    _add_items(conn, "Migros", 9)
    _add_items(conn, "migros", 1)


def test_forked_catalogue_collapses_to_the_most_used_spelling(conn):
    """Three rows become one, named for the spelling the items use."""
    _seed_fork(conn)

    _apply(conn)

    assert _stores(conn) == [("Migros", 2)]


def test_every_item_is_repointed_at_the_surviving_spelling(conn):
    """The whole point: the SPA matches ``i.store === section.key``."""
    _seed_fork(conn)

    _apply(conn)

    assert _item_stores(conn) == ["Migros"] * 10


def test_most_used_spelling_wins_even_at_a_higher_sort_order(conn):
    """Item count beats catalogue order — the name users actually type."""
    _add_store(conn, "Coop", 1)
    _add_store(conn, "COOP", 8)
    _add_items(conn, "Coop", 1)
    _add_items(conn, "COOP", 5)

    _apply(conn)

    # The NAME comes from the popular row; the SORT ORDER is the group
    # minimum, so the store keeps its place in the trip order.
    assert _stores(conn) == [("COOP", 1)]
    assert set(_item_stores(conn)) == {"COOP"}


def test_item_count_tie_falls_back_to_the_lowest_sort_order(conn):
    """Equal usage: the row the household dragged highest wins."""
    _add_store(conn, "Denner", 3)
    _add_store(conn, "denner", 1)
    _add_items(conn, "Denner", 2)
    _add_items(conn, "denner", 2)

    _apply(conn)

    assert _stores(conn) == [("denner", 1)]
    assert set(_item_stores(conn)) == {"denner"}


def test_orphan_store_on_an_item_gets_a_catalogue_row(conn):
    """An item referencing a store with no catalogue row is invisible.

    Adopting it into the catalogue is what puts it back on screen.
    """
    _add_store(conn, "Migros", 0)
    _add_items(conn, "Migros", 1)
    _add_items(conn, "Lidl", 3)

    _apply(conn)

    assert _stores(conn) == [("Migros", 0), ("Lidl", 1)]
    assert sorted(_item_stores(conn)) == ["Lidl", "Lidl", "Lidl", "Migros"]


def test_orphan_adoption_merges_case_variants_too(conn):
    """Two orphan spellings are one store, adopted once."""
    _add_items(conn, "Lidl", 1)
    _add_items(conn, "LIDL", 4)

    _apply(conn)

    assert _stores(conn) == [("LIDL", 0)]
    assert set(_item_stores(conn)) == {"LIDL"}


def test_blank_stores_make_no_catalogue_row_and_normalise_to_null(conn):
    """Blank means "no store": no catalogue row, and a real ``NULL``.

    Whitespace is not left as-is. A whitespace string is TRUTHY in
    JavaScript, so the SPA's ``!i.store`` bucket would exclude such an
    item while no catalogue section matches it either — invisible in
    both directions, which is the bug this migration exists to fix.
    """
    _add_items(conn, None, 1)
    _add_items(conn, "   ", 1)

    _apply(conn)

    assert _stores(conn) == []
    assert _item_stores(conn) == [None, None]


def test_store_with_no_items_keeps_its_row_and_order(conn):
    """The catalogue deliberately outlives an empty shopping list."""
    _add_store(conn, "Bakery", 4)

    _apply(conn)

    assert _stores(conn) == [("Bakery", 4)]


def test_non_forked_catalogue_is_untouched(conn):
    """A clean install must come out byte-for-byte identical."""
    _add_store(conn, "Migros", 0)
    _add_store(conn, "Bakery", 1)
    _add_store(conn, "Pharmacy", 2)
    _add_items(conn, "Migros", 2)
    _add_items(conn, "Bakery", 1)
    before_stores = _stores(conn)
    before_items = _item_stores(conn)

    _apply(conn)

    assert _stores(conn) == before_stores
    assert _item_stores(conn) == before_items


def test_index_blocks_a_new_case_variant(conn):
    """The durable guard — the fork cannot come back."""
    _seed_fork(conn)

    _apply(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO shopping_stores(name, sort_order) VALUES('MIGROS', 99)"
        )


def test_index_still_allows_a_genuinely_different_store(conn):
    """NOCASE only folds case — distinct names still insert."""
    _seed_fork(conn)

    _apply(conn)

    conn.execute("INSERT INTO shopping_stores(name, sort_order) VALUES('Aldi', 99)")
    assert ("Aldi", 99) in _stores(conn)


def test_rerunning_the_migration_is_a_no_op(conn):
    """``executescript`` COMMITs first, so a .sql migration is NOT atomic.

    A part-way failure leaves ``schema_version`` un-bumped and the next
    boot runs the whole file again — every step has to be re-runnable.
    """
    _seed_fork(conn)
    _add_items(conn, "Lidl", 2)
    _add_items(conn, None, 1)

    _apply(conn)
    after_first = (_stores(conn), _item_stores(conn))
    _apply(conn)

    assert (_stores(conn), _item_stores(conn)) == after_first


def test_temp_mapping_tables_do_not_survive(conn):
    """The scratch tables are dropped — nothing leaks into the schema."""
    _seed_fork(conn)

    _apply(conn)

    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_temp_master"
            " UNION ALL SELECT name FROM sqlite_master"
        )
    }
    assert "shopping_store_case_map" not in names
    assert "shopping_store_candidates" not in names
    # ...and the real tables are of course still there.
    assert {"shopping_stores", "shopping_list_items"} <= names


def test_discovered_as_version_48():
    """The runner picks it up in order, with no duplicate-version clash."""
    versions = [m.version for m in discover_migrations()]
    assert 48 in versions
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))


def test_real_chain_dedupes_a_forked_catalogue(tmp_path):
    """0048 against the REAL schema, on the real upgrade path.

    ``_SCHEMA`` above is a fast model, not production — it cannot catch a
    column-name drift, and it never sees the pragma state the earlier
    migrations leave behind. This one seeds through ``run_migrations``
    itself. ``isolation_level=None`` mirrors ``AsyncDatabase._open``.
    """
    conn = sqlite3.connect(tmp_path / "real.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    for m in discover_migrations():
        if m.version > 47:
            break
        m.apply(conn)
        conn.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (m.version, m.description),
        )
    assert (
        conn.execute(
            "SELECT count(*) FROM sqlite_master"
            " WHERE name='ux_shopping_stores_name_nocase'"
        ).fetchone()[0]
        == 0
    ), "0048 must not have run yet"

    _add_store(conn, "Migros", 2)
    _add_store(conn, "migros", 5)
    _add_items(conn, "Migros", 3)
    _add_items(conn, "migros", 1)
    _add_items(conn, "Lidl", 1)

    run_migrations(conn)

    assert _stores(conn) == [("Migros", 2), ("Lidl", 6)]
    assert sorted(_item_stores(conn)) == [
        "Lidl",
        "Migros",
        "Migros",
        "Migros",
        "Migros",
    ]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO shopping_stores(name, sort_order) VALUES('MIGROS', 9)"
        )
    conn.close()


def _add_raw_store(conn: sqlite3.Connection, name: str | None, sort_order: int) -> None:
    """Insert a catalogue row bypassing every sanity check.

    ``shopping_stores.name`` is ``TEXT PRIMARY KEY`` on a rowid table,
    which in SQLite permits NULL — and ``BackupService._import_table``
    writes this table straight from restore JSON with no validation.
    """
    conn.execute(
        "INSERT INTO shopping_stores(name, sort_order) VALUES (?, ?)",
        (name, sort_order),
    )


def _junk_named_stores(conn: sqlite3.Connection) -> list[str | None]:
    """Catalogue rows whose name is NULL or whitespace-only."""
    return [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM shopping_stores"
            " WHERE name IS NULL"
            " OR trim(name, char(32)||char(9)||char(10)||char(13)||char(160)) = ''"
        )
    ]


def test_null_named_catalogue_row_is_purged_and_the_fork_still_collapses(conn):
    """A NULL-named row must not poison the de-dup (unattended boot loop).

    ``NULL = NULL`` is never true, so a NULL fold key produced a mapping
    row with a NULL ``canonical``; ``NOT IN`` against a set containing
    NULL then matched nothing and the real losers survived, the orphan
    INSERT added *another* NULL row, and the unique index blew up — every
    boot, forever, leaking one NULL row per attempt.
    """
    _add_raw_store(conn, None, 0)
    _add_store(conn, "Migros", 2)
    _add_store(conn, "migros", 5)
    _add_items(conn, "Migros", 2)
    _add_items(conn, "migros", 1)

    _apply(conn)

    assert _stores(conn) == [("Migros", 2)]
    assert _junk_named_stores(conn) == []
    assert _item_stores(conn) == ["Migros"] * 3
    assert (
        conn.execute(
            "SELECT count(*) FROM sqlite_master"
            " WHERE name='ux_shopping_stores_name_nocase'"
        ).fetchone()[0]
        == 1
    )

    # ...and a second run neither raises nor leaks another junk row.
    _apply(conn)

    assert _stores(conn) == [("Migros", 2)]
    assert _junk_named_stores(conn) == []


def test_blank_named_catalogue_row_is_purged(conn):
    """A ``''``-named row can't be rendered, matched, or deleted in the UI."""
    _add_raw_store(conn, "", 0)
    _add_store(conn, "Migros", 1)
    _add_items(conn, "Migros", 1)

    _apply(conn)

    assert _stores(conn) == [("Migros", 1)]
    assert _junk_named_stores(conn) == []


def test_whitespace_only_stores_never_become_catalogue_rows(conn):
    """SQLite's 1-arg ``trim()`` strips U+0020 ONLY.

    A tab-only or NBSP-only ``store`` therefore sailed past
    ``trim(store) <> ''`` and got adopted as a catalogue row that is
    permanently visible in the SPA and undeletable from the UI.
    """
    _add_items(conn, "\t", 1)
    _add_items(conn, "\xa0", 1)
    _add_items(conn, "   ", 1)
    _add_items(conn, None, 1)

    _apply(conn)

    assert _stores(conn) == []
    # ...and every one of them is a real NULL, so the SPA renders it
    # under "No store" instead of dropping it out of every section.
    assert _item_stores(conn) == [None, None, None, None]
    assert _junk_named_stores(conn) == []


def test_a_whitespace_store_does_not_survive_next_to_a_real_one(conn):
    """The NULL-ing must not disturb items with a genuine store."""
    _add_store(conn, "Migros", 0)
    _add_items(conn, "Migros", 2)
    _add_items(conn, "\t", 1)

    _apply(conn)

    assert _stores(conn) == [("Migros", 0)]
    assert sorted(_item_stores(conn), key=lambda v: (v is None, v)) == [
        "Migros",
        "Migros",
        None,
    ]


def test_group_min_can_move_a_store_forward_in_the_trip_order(conn):
    """The surviving row keeps the BEST place any spelling ever held.

    ``c`` and ``C`` are one store; ``A`` and ``B`` are unrelated ones
    sitting between them. Collapsing onto ``C`` therefore moves the
    merged row *forward* past ``A`` and ``B``, because the household did
    once drag a spelling of it to the top of the trip. That is intended
    — the group MIN is deliberate, not an accident of which row won.
    """
    _add_store(conn, "c", 0)
    _add_store(conn, "A", 3)
    _add_store(conn, "B", 4)
    _add_store(conn, "C", 5)
    _add_items(conn, "C", 5)

    _apply(conn)

    assert _stores(conn) == [("C", 0), ("A", 3), ("B", 4)]
    assert _item_stores(conn) == ["C"] * 5


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(
            lambda c: (_add_raw_store(c, None, 0), _add_store(c, "Migros", 2)),
            id="null-named-row",
        ),
        pytest.param(
            lambda c: (_add_raw_store(c, "", 0), _add_items(c, "Migros", 1)),
            id="blank-named-row",
        ),
        pytest.param(
            lambda c: (_add_raw_store(c, "\t", 0), _add_raw_store(c, "\xa0", 1)),
            id="whitespace-named-rows",
        ),
        pytest.param(
            lambda c: (_add_items(c, "\t", 1), _add_items(c, "\xa0", 2)),
            id="whitespace-only-item-stores",
        ),
        pytest.param(
            lambda c: (_add_raw_store(c, None, 0), _add_raw_store(c, "", 1)),
            id="null-and-blank-together",
        ),
    ],
)
def test_no_unnameable_catalogue_row_ever_survives(conn, seed):
    """Regression guard: the catalogue never keeps a row the UI can't show.

    A NULL / whitespace-only name renders as nothing, matches no item,
    and has no delete affordance — it is junk, and it is also what broke
    the de-dup, so it must be gone after the repair and stay gone across
    re-runs.
    """
    seed(conn)

    _apply(conn)
    assert _junk_named_stores(conn) == []

    _apply(conn)
    assert _junk_named_stores(conn) == []
