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

Whole-cell matching cannot see an id *inside* a JSON document, so the handful
of columns that store user ids as JSON are repaired explicitly — see
:data:`_JSON_ID_SITES`. That list is hand-maintained and therefore the one
part of this migration that CAN rot; it must be reviewed whenever a new
JSON-of-user-ids column lands.

Safety properties, in the spirit of 0046:

* **Never a crash on a half-provisioned DB.** No ``instance_identity`` row →
  clean no-op (we cannot derive without the key, and guessing is worse than
  waiting for the next boot).
* **Never a crash on a collision either.** If the derived id is *already*
  present anywhere in the sweep, the repair is skipped with an operator-facing
  WARNING naming the table and column. Rewriting into it could trip a UNIQUE
  constraint mid-sweep, and an ``IntegrityError`` out of a migration fails
  ``AsyncDatabase.startup`` — i.e. a household that cannot boot at all, on
  every attempt. A household that still cannot federate is strictly better,
  and the skip is a no-op a later run retries.
* **Atomic across parent + children.** ``AsyncDatabase`` sets
  ``PRAGMA foreign_keys=ON`` before migrations run, and unlike
  ``foreign_keys`` the ``defer_foreign_keys`` pragma *does* take effect inside
  a transaction — it defers FK checks to COMMIT, which is what lets the parent
  and its ~20 FK children be updated in any order. The pragma is asserted, not
  assumed.
* **Verified, with a known scope.** Before COMMIT we assert that the ``users``
  row count is unchanged, that every repaired username now reads back its
  derived id, and that ``PRAGMA foreign_key_check`` is empty — any failure
  rolls the whole thing back. Note what that does NOT prove: the FK check only
  covers columns with a declared FK (~20 of ~1100), the row-count check only
  covers ``users``, and none of the three says anything about the FK-less
  columns or the JSON sites. They are guards against a *broken rewrite*, not
  evidence of a *complete* one. (An earlier revision also re-SELECTed every
  swept column for the retired id; it was dropped — re-reading the rows an
  UPDATE just wrote can only fail if the UPDATE itself was blocked, which
  would already have raised, and it doubled the runtime with ~1100 extra full
  table scans.)
* **Idempotent.** A second run finds no ``uid-%`` rows and returns immediately.

One deliberate deviation from the runner's contract: unlike a ``.sql``
migration, whose statements share the transaction the runner's version stamp
is written in, this module opens and COMMITs its own ``BEGIN IMMEDIATE`` (it
has to — see ``defer_foreign_keys`` above), so the repair lands *before*
``INSERT INTO schema_version``. A crash in that window leaves the repair
applied and unstamped; the next boot re-runs it, finds no ``uid-%`` rows and
returns immediately. The escape is safe precisely because the migration is
idempotent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
from typing import Any

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


#: Columns the value sweep must not touch even though they live in a swept
#: table. All three are *usernames*, not ids: a local user may legitimately be
#: called ``uid-<adminname>``, and rewriting their username to the admin's
#: derived id would rename them, break ``derive_user_id(pk, anchor) ==
#: user_id`` for them forever — inflicting the very bug this migration repairs
#: — and cascade through ``ON UPDATE CASCADE`` (0042) into every username FK.
#: ``users.identity_anchor`` is the derivation *input*, never an id.
#: Every column with a declared FK onto ``users(username)`` /
#: ``platform_users(username)`` is excluded alongside them (read off the live
#: schema by :func:`_username_columns`) — excluding only the parents would let
#: the sweep rewrite a child and orphan it.
_EXCLUDED_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("users", "username"),
        ("users", "identity_anchor"),
        ("platform_users", "username"),
    }
)

#: Columns that store user ids *inside* a JSON document, where the whole-cell
#: sweep is blind. Hand-maintained, and therefore the one part of this
#: migration that can rot: **review this list whenever a new column storing
#: user ids as JSON lands.** Each entry is repaired by parsing the cell and
#: replacing string *values* that equal the retired id exactly, at any depth —
#: which covers both shapes in use (a bare array of ids, and a nested object
#: such as ``preferences_json`` → ``highlights.default_audience.ids``) without
#: this list also having to encode a path per site. A cell that is not valid
#: JSON is left untouched rather than crashing the boot.
#:
#: Deliberately NOT here: ``federation_outbox.payload_json``. It stores the
#: fully-built, Ed25519-**signed** envelope whose user ids live inside
#: ``encrypted_payload`` — unreachable without the session key, and any
#: rewrite of the surrounding JSON would invalidate the signature so the peer
#: drops the event. Queued entries carrying the retired id simply expire
#: (§4.4.7); the content is re-sent from the repaired rows.
_JSON_ID_SITES: tuple[tuple[str, str], ...] = (
    ("tasks", "assignees_json"),
    ("space_tasks", "assignees_json"),
    ("call_sessions", "participant_user_ids"),
    ("highlights", "audience_json"),  # only when audience_kind='users'
    ("users", "preferences_json"),  # highlights.default_audience.ids
    ("calendar_events", "attendees_json"),
    ("space_calendar_events", "attendees_json"),
    ("feed_posts", "reactions"),  # {emoji: [user_id, ...]}
    ("space_posts", "reactions"),
    ("conversation_messages", "reply_to_highlight_frame_snapshot"),
    ("space_admin_proposals", "params_json"),
    ("space_admin_proposals", "host_view_json"),
    ("app_pending_sessions", "payload_json"),
    ("app_kv", "value_json"),
)


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


def _instance_public_key(conn: sqlite3.Connection) -> tuple[bytes | None, str]:
    """The household's Ed25519 identity public key and why it is missing.

    Returns ``(key, reason)`` where ``reason`` is ``"ok"``, ``"absent"`` (no
    identity minted yet — a normal half-provisioned DB) or ``"malformed"``
    (a row exists but the key is not 32 hex-encoded bytes). The two are very
    different operator situations — "not booted yet" resolves itself on the
    next boot, a corrupt identity key never does — so they must not share a
    log line.
    """
    try:
        row = conn.execute(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'",
        ).fetchone()
    except sqlite3.OperationalError:  # pragma: no cover — table always exists
        return None, "absent"
    if row is None or not row[0]:
        return None, "absent"
    try:
        pk = bytes.fromhex(row[0])
    except ValueError:
        return None, "malformed"
    if len(pk) != 32:
        return None, "malformed"
    return pk, "ok"


def _username_columns(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """:data:`_EXCLUDED_COLUMNS` plus every FK child of a username column."""
    excluded = set(_EXCLUDED_COLUMNS)
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
            # fk = (id, seq, table, from, to, on_update, on_delete, match)
            if (fk[2], fk[4]) in (
                ("users", "username"),
                ("platform_users", "username"),
            ):
                excluded.add((table, fk[3]))
    return excluded


def _replace_in_json(node: Any, old_id: str, new_id: str) -> tuple[Any, bool]:
    """Rewrite every string equal to ``old_id`` anywhere in a parsed document.

    Values only — a dict *key* equal to the id is left alone, since no shape in
    :data:`_JSON_ID_SITES` keys by user id and rewriting one could collide with
    a sibling key.
    """
    if isinstance(node, str):
        return (new_id, True) if node == old_id else (node, False)
    if isinstance(node, list):
        out = []
        changed = False
        for item in node:
            value, hit = _replace_in_json(item, old_id, new_id)
            out.append(value)
            changed = changed or hit
        return out, changed
    if isinstance(node, dict):
        result = {}
        changed = False
        for key, item in node.items():
            value, hit = _replace_in_json(item, old_id, new_id)
            result[key] = value
            changed = changed or hit
        return result, changed
    return node, False


def _repair_json_sites(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Rewrite the retired id inside the JSON columns of :data:`_JSON_ID_SITES`.

    Only rows whose text contains the id are parsed, so the cost is one
    indexless scan per site and a parse for the handful of hits. A cell that
    does not parse is skipped with a WARNING: a malformed blob is already
    broken, and crashing the boot over one is the worse failure.
    """
    for table, column in _JSON_ID_SITES:
        try:
            rows = conn.execute(
                f'SELECT rowid, "{column}" FROM "{table}"'
                f" WHERE \"{column}\" LIKE '%' || ? || '%'",
                (old_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # The table/column was dropped by a later schema change, or this
            # DB predates it. Not fatal — the sweep is best-effort here.
            log.warning("0049: skipping JSON site %s.%s: %s", table, column, exc)
            continue
        for rowid, raw in rows:
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                log.warning(
                    "0049: %s.%s rowid=%s is not valid JSON — left as-is",
                    table,
                    column,
                    rowid,
                )
                continue
            repaired, changed = _replace_in_json(parsed, old_id, new_id)
            if not changed:
                continue
            conn.execute(
                f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?',
                (json.dumps(repaired, separators=(",", ":")), rowid),
            )


def _user_id_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every ``(table, column)`` that could hold a local user id.

    Read from the live schema rather than a hard-coded list: a hard-coded one
    silently rots the moment a new table lands, leaving that table pointing at
    the dead id. Virtual tables (the FTS5 ``search_index``) and their shadow
    tables are skipped — FTS holds no user id and must be written through its
    own interface. Username columns (:func:`_username_columns`) are skipped
    too: they hold names, not ids.
    """
    skip = set(_SKIP_TABLES)
    excluded = _username_columns(conn)
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
            if (table, row[1]) in excluded:
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

    pk, reason = _instance_public_key(conn)
    if pk is None:
        if reason == "malformed":
            # A row exists but the key is unusable. Unlike "absent" this does
            # NOT resolve itself on the next boot — surface it as its own line.
            log.error(
                "0049: %d synthetic user id(s) found but the stored "
                "instance_identity public key is malformed (not 32 hex bytes) "
                "— skipping the repair; the household's identity needs "
                "operator attention",
                len(broken),
            )
        else:
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
        if not username:
            # ``derive_user_id`` refuses an empty username, so a repaired id
            # here could never be reproduced by the app helper. ``uid-%`` also
            # matches the bare string ``uid-``; leave such a row alone.
            log.warning("0049: skipping user_id %r — empty username", old_id)
            continue
        renames.append((username, old_id, _derive_user_id(pk, username)))
    if not renames:
        return

    users_before = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    columns = _user_id_columns(conn)

    # ── Pre-flight: is the derived id already in use anywhere? ───────────
    # Rewriting into an occupied id can trip a UNIQUE / PK constraint mid-
    # sweep (``users.user_id``, ``space_members(space_id, user_id)``, …), and
    # an IntegrityError out of a migration fails ``AsyncDatabase.startup``:
    # the household stops booting entirely, on every attempt. Skipping leaves
    # it bootable but unrepaired, which a later run can retry.
    for username, _old_id, new_id in renames:
        for table, column in columns:
            hit = conn.execute(
                f'SELECT count(*) FROM "{table}" WHERE "{column}"=?',
                (new_id,),
            ).fetchone()[0]
            if hit:
                log.error(
                    "0049: refusing to repair %r — its derived id %s is "
                    "already present in %s.%s (%d row(s)); skipping the whole "
                    "repair so the household still boots. Resolve the "
                    "duplicate and restart to retry.",
                    username,
                    new_id,
                    table,
                    column,
                    hit,
                )
                return
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
            _repair_json_sites(conn, old_id, new_id)
            conn.execute(
                "UPDATE users SET identity_anchor=? WHERE username=?",
                (username, username),
            )

        # ── Self-verification, before anything is made durable ──────────
        # Scope, stated honestly: these three catch a *broken* rewrite (a
        # blocked UPDATE, a lost row, a dangling FK). None of them proves the
        # rewrite was *complete* — foreign_key_check only sees the ~20
        # declared FKs, the count only sees ``users``, and neither looks at
        # the FK-less columns or the JSON sites at all.
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
