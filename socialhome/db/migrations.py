"""SQLite migration runner (§28.2).

Design choices for v1:

* **File-based migrations.** Each migration is a numbered ``.sql`` file under
  ``socialhome/migrations/``. File naming convention: ``NNNN_description.sql``
  (for example ``0001_initial.sql``). The integer ``NNNN`` is the version
  number and must be unique.
* **Python migrations are supported too.** A migration file may be named
  ``NNNN_description.py`` and must expose a ``migrate(conn)`` callable that
  receives the open :class:`sqlite3.Connection`. Use this when pure SQL
  cannot express the change idempotently (e.g. ``ALTER TABLE ... ADD
  COLUMN`` guarded by a ``PRAGMA table_info`` check).
* **Idempotent.** The runner stamps each applied version into
  ``schema_version``. Re-running is a no-op.
* **Transactional.** Each migration runs inside its own SQLite transaction.
  A failure rolls back only that migration; the previous state is preserved
  and the exception propagates so startup fails with a clear log line.

Schema evolution rules are the §28.4 invariants — never modify a shipped
migration; always use ``ADD COLUMN`` with a ``DEFAULT`` or nullable; never
rename or drop a column (use a new column + backfill).
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


#: Location of the ``NNNN_*.sql`` / ``NNNN_*.py`` migration files.
MIGRATIONS_DIR: Path = Path(__file__).resolve().parent.parent / "migrations"


# A Python migration module must expose a ``migrate(conn)`` callable.
PythonMigration = Callable[[sqlite3.Connection], None]


class MigrationError(Exception):
    """Raised when migration discovery or application fails."""


@dataclass(frozen=True)
class Migration:
    """A single discovered migration file."""

    version: int
    description: str
    path: Path
    is_python: bool

    def apply(self, conn: sqlite3.Connection) -> None:
        if self.is_python:
            self._run_python(conn)
        else:
            conn.executescript(self.path.read_text(encoding="utf-8"))

    def _run_python(self, conn: sqlite3.Connection) -> None:
        spec = importlib.util.spec_from_file_location(
            f"socialhome.migrations.m{self.version:04d}",
            self.path,
        )
        if spec is None or spec.loader is None:
            raise MigrationError(f"Cannot load migration {self.path.name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrate: PythonMigration | None = getattr(module, "migrate", None)
        if migrate is None:
            raise MigrationError(
                f"Python migration {self.path.name!s} does not define migrate(conn)"
            )
        migrate(conn)


_FILENAME_RE = re.compile(
    r"^(?P<version>\d{4})_(?P<description>[\w\-]+)\.(?P<ext>sql|py)$"
)


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Return the ordered list of migrations found in ``directory``.

    Raises :class:`MigrationError` if two files share a version number or if
    a filename does not match the ``NNNN_description.(sql|py)`` pattern.
    """
    base = Path(directory) if directory is not None else MIGRATIONS_DIR
    migrations: dict[int, Migration] = {}
    if not base.exists():
        return []
    for path in sorted(base.iterdir()):
        if path.name.startswith("_") or path.name.startswith("."):
            continue
        if path.is_dir():
            continue
        match = _FILENAME_RE.match(path.name)
        if match is None:
            # Accept README etc.; skip silently.
            if path.suffix in (".sql", ".py"):
                raise MigrationError(
                    f"Migration filename does not match NNNN_description.(sql|py): {path.name}"
                )
            continue
        version = int(match["version"])
        if version in migrations:
            raise MigrationError(
                f"Duplicate migration version {version}: "
                f"{migrations[version].path.name} and {path.name}"
            )
        migrations[version] = Migration(
            version=version,
            description=match["description"].replace("_", " "),
            path=path,
            is_python=match["ext"] == "py",
        )
    return [migrations[v] for v in sorted(migrations)]


def run_migrations(
    conn: sqlite3.Connection,
    *,
    directory: Path | None = None,
) -> list[Migration]:
    """Apply all pending migrations, in order, to the given connection.

    Returns the list of migrations that were actually applied in this call
    (i.e. whose version is greater than the highest already in
    ``schema_version``).
    """
    _ensure_schema_version_table(conn)
    current = _current_version(conn)

    to_apply = [m for m in discover_migrations(directory) if m.version > current]
    applied: list[Migration] = []

    for migration in to_apply:
        log.info(
            "Applying migration %04d: %s",
            migration.version,
            migration.description,
        )
        try:
            # ``with conn`` enters an implicit BEGIN, commits on success,
            # rolls back on exception.
            with conn:
                migration.apply(conn)
                conn.execute(
                    "INSERT INTO schema_version(version, description) VALUES (?,?)",
                    (migration.version, migration.description),
                )
        except Exception:
            log.exception(
                "Migration %04d FAILED — database not modified",
                migration.version,
            )
            raise
        applied.append(migration)
        log.info("Migration %04d complete", migration.version)

    return applied


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            description TEXT,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)
