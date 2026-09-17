"""Repair the household admin's synthetic ``uid-<username>`` user id.

Every local ``users.user_id`` is meant to be
``derive_user_id(instance_public_key, derivation_input)`` (§4.1.3) — that is
precisely what a remote household recomputes to self-certify an author:
``derive_user_id(author_pk, identity_anchor or username) == author_user_id``
(:mod:`socialhome.services.space_public_author`). Two first-boot paths minted
a literal ``f"uid-{username}"`` instead, so the household's **admin** — the
very first user of every standalone / ha install — carried an id that no peer
could ever verify. Their posts reached the public-space relay and were dropped
by every subscriber with ``author verification failed``. The fix at the source
is in ``platform/standalone/adapter.py`` + ``routes/setup.py``, which now both
go through ``identity_bootstrap.derive_local_user_id`` (the one minting rule,
shared with ``platform/haos/bootstrap.py``); this migration repairs the
already-deployed rows.

Rewriting the id is not a one-table UPDATE. ~20 columns carry an explicit
``REFERENCES users(user_id)`` FK, and ~90 more carry a bare user-id string
with no FK at all (``space_posts.author``, ``space_members.user_id``,
``tasks.created_by``, ``highlights.author_user_id``, …) — deliberately, since
those columns may hold a *remote* user's id. Rewriting only the FK columns
would leave the admin's entire content history pointing at an id that no
longer exists. So the sweep is driven off the live schema: every column of
every real table (bar :data:`_SKIP_TABLES`), rewriting only cells whose
**entire value** equals the synthetic id. That value shape
(``uid-<username>``) only ever existed as a local user id — base32 derived ids
contain no ``-`` — so an exact-value match cannot collide with another *id*,
and nothing rots when a new table lands. It could in principle match a
free-text cell whose entire content is the literal string ``uid-<username>``
(a post body of exactly that, say); the sweep would rewrite it. That is
accepted rather than guarded: enumerating "content" columns would rot the same
way a hard-coded table list does, and the cost of the collision (one string
rewritten to this household's own admin id) is far smaller than the cost of
missing a real reference.

Safety properties, in the spirit of 0046:

* **Never a crash on a half-provisioned DB.** No ``instance_identity`` row →
  clean no-op (we cannot derive without the key, and guessing is worse than
  waiting for the next boot).
* **Atomic across parent + children.** ``AsyncDatabase`` sets
  ``PRAGMA foreign_keys=ON`` before migrations run, and unlike
  ``foreign_keys`` the ``defer_foreign_keys`` pragma *does* take effect inside
  a transaction — it defers FK checks to COMMIT, which is what lets the parent
  and its ~20 FK children be updated in any order. The pragma is asserted, not
  assumed.
* **Self-verifying.** Row counts must survive, no cell may still hold the old
  id, the new id must not already be taken, and ``PRAGMA foreign_key_check``
  must come back empty — any failure rolls the whole thing back.
* **Idempotent.** A second run finds no ``uid-%`` rows and returns immediately.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import sqlite3

log = logging.getLogger(__name__)

#: The synthetic shape the two buggy call sites produced. A derived id is
#: lowercase base32 (``[a-z2-7]{32}``) and can never contain ``-``, so this
#: pattern cannot match a correctly-minted id.
_SYNTHETIC_LIKE = "uid-%"

#: Tables the value sweep must not touch.
#:
#: ``remote_users`` is the cache of OTHER households' identities. Because the
#: synthetic id was derived from nothing, two households that both ran the
#: headless default username (``admin``) produced the *same* string —
#: ``uid-admin`` — so a match there may well be a peer's admin, not ours.
#: Leaving it alone costs the repair nothing (our own user never has a
#: ``remote_users`` row) and the peer's cached row re-syncs from their profile
#: broadcast once they upgrade.
#:
#: ``schema_version`` is the runner's own bookkeeping.
_SKIP_TABLES: frozenset[str] = frozenset({"remote_users", "schema_version"})


def _derive_user_id(instance_public_key: bytes, username: str) -> str:
    """Inlined copy of :func:`socialhome.crypto.derive_user_id` (§4.1.3).

    Migrations are loaded from a file path by the runner and must keep
    producing the SAME bytes forever, even if the application-level helper is
    one day re-based onto a new suite. Importing it would silently re-map
    already-repaired installs; a frozen copy cannot.
    """
    payload = instance_public_key + b"\x00" + username.encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return base64.b32encode(digest[:20]).decode("ascii").lower().rstrip("=")


def _instance_public_key(conn: sqlite3.Connection) -> bytes | None:
    """The household's Ed25519 identity public key, or ``None`` if unbooted."""
    try:
        row = conn.execute(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'",
        ).fetchone()
    except sqlite3.OperationalError:  # pragma: no cover — table always exists
        return None
    if row is None or not row[0]:
        return None
    try:
        pk = bytes.fromhex(row[0])
    except ValueError:
        return None
    return pk if len(pk) == 32 else None


def _user_id_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every ``(table, column)`` that could hold a local user id.

    Read from the live schema rather than a hard-coded list: a hard-coded one
    silently rots the moment a new table lands, leaving that table pointing at
    the dead id. Virtual tables (the FTS5 ``search_index``) and their shadow
    tables are skipped — FTS holds no user id and must be written through its
    own interface.
    """
    skip = set(_SKIP_TABLES)
    tables: list[str] = []
    virtual: list[str] = []
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master"
        " WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        if (sql or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
            virtual.append(name)
        else:
            tables.append(name)
    shadow = tuple(f"{v}_" for v in virtual)
    columns: list[tuple[str, str]] = []
    for table in tables:
        if table in skip or table.startswith(shadow):
            continue
        for row in conn.execute(f'PRAGMA table_xinfo("{table}")').fetchall():
            # row = (cid, name, type, notnull, dflt, pk, hidden); hidden != 0
            # marks a generated / hidden column, which cannot be UPDATEd.
            if row[6]:
                continue
            columns.append((table, row[1]))
    return columns


def _fk_user_id_columns(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """The subset carrying an explicit ``REFERENCES users(user_id)``.

    Used only as a self-check: every one of them MUST be inside the sweep, or
    the update would leave a dangling FK and COMMIT would fail.
    """
    found: set[tuple[str, str]] = set()
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
            # fk = (id, seq, table, from, to, on_update, on_delete, match)
            if fk[2] == "users" and (fk[4] or "user_id") == "user_id":
                found.add((table, fk[3]))
    return found


def migrate(conn: sqlite3.Connection) -> None:
    broken = conn.execute(
        "SELECT username, user_id FROM users WHERE user_id LIKE ?",
        (_SYNTHETIC_LIKE,),
    ).fetchall()
    if not broken:
        return

    pk = _instance_public_key(conn)
    if pk is None:
        # Never booted far enough to have an identity — there is nothing to
        # derive from. Deliberately not an error: the next boot mints the
        # identity, and a fresh install has no content to orphan anyway.
        log.warning(
            "0049: %d synthetic user id(s) found but instance_identity is "
            "absent — skipping the repair (no key to derive from)",
            len(broken),
        )
        return

    renames: list[tuple[str, str, str]] = []  # (username, old_id, new_id)
    for username, old_id in broken:
        new_id = _derive_user_id(pk, username)
        taken = conn.execute(
            "SELECT username FROM users WHERE user_id=? AND username<>?",
            (new_id, username),
        ).fetchone()
        if taken is not None:
            raise RuntimeError(
                f"0049: refusing to repair {username!r} — the derived id is "
                f"already held by {taken[0]!r}"
            )
        renames.append((username, old_id, new_id))

    users_before = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    columns = _user_id_columns(conn)
    missing_fk = _fk_user_id_columns(conn) - set(columns)
    if missing_fk:
        raise RuntimeError(
            f"0049: FK column(s) {sorted(missing_fk)} are not in the rewrite "
            f"sweep — updating users.user_id would orphan them"
        )

    # The runner wraps each migration in ``with conn``, which opens no
    # transaction on the autocommit connection AsyncDatabase uses. Without an
    # explicit one, ``defer_foreign_keys`` resets at the first statement's
    # implicit commit and the parent UPDATE fails immediately.
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("PRAGMA defer_foreign_keys=ON")
        if int(conn.execute("PRAGMA defer_foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError(
                "0049: PRAGMA defer_foreign_keys=ON did not take effect — "
                "rewriting users.user_id would break its ~20 child FKs mid-way"
            )

        for username, old_id, new_id in renames:
            for table, column in columns:
                conn.execute(
                    f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?',
                    (new_id, old_id),
                )
            conn.execute(
                "UPDATE users SET identity_anchor=? WHERE username=?",
                (username, username),
            )

        # ── Self-verification, before anything is made durable ──────────
        for _username, old_id, _new_id in renames:
            for table, column in columns:
                left = conn.execute(
                    f'SELECT count(*) FROM "{table}" WHERE "{column}"=?',
                    (old_id,),
                ).fetchone()[0]
                if left:
                    raise RuntimeError(
                        f"0049: {left} row(s) in {table}.{column} still hold "
                        f"the retired id {old_id!r}"
                    )
        users_after = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        if users_after != users_before:
            raise RuntimeError(
                f"0049: users row count changed during the rewrite "
                f"({users_before} -> {users_after}); rolling back"
            )
        for username, _old_id, new_id in renames:
            row = conn.execute(
                "SELECT user_id FROM users WHERE username=?", (username,)
            ).fetchone()
            if row is None or row[0] != new_id:
                raise RuntimeError(
                    f"0049: {username!r} did not end up with its derived id"
                )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"0049: rewrite left {len(violations)} foreign-key "
                f"violation(s): {violations[:5]}"
            )
    except Exception:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
    if own_tx:
        conn.execute("COMMIT")
    log.info(
        "0049: repaired %d synthetic user id(s): %s",
        len(renames),
        ", ".join(f"{u} {o} -> {n}" for u, o, n in renames),
    )
