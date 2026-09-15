-- 0048_shopping_store_case_dedupe.sql
--
-- Collapse case-forked household shopping-store rows, adopt orphaned
-- store names into the catalogue, then install a DB-level guard so the
-- fork cannot come back.
--
-- THE BUG
--
-- ``shopping_stores.name`` is a plain ``TEXT PRIMARY KEY``
-- (0001_initial.sql), i.e. BINARY collation, so ``'Migros'`` and
-- ``'migros'`` coexist as two independent catalogue rows.
-- ``shopping_list_items.store`` is free text with no FK, so an item can
-- also point at a spelling the catalogue has never seen at all.
--
-- This is not cosmetic. The SPA's grouped shopping view renders one
-- section per *catalogue* row and matches items with an exact
-- ``i.store === section.key`` compare, so an item whose casing doesn't
-- match a catalogue row renders in NO section — and it isn't in the
-- trailing "No store" bucket either, because that bucket is keyed on
-- ``!i.store``. The item is simply invisible. Same outcome for an item
-- whose ``store`` has no catalogue row at all ("orphans").
--
-- PR #570 added entry-time canonicalisation in the repo
-- (``SqliteShoppingRepo._canonical_store_name`` folds a new name onto an
-- existing catalogue row via ``COLLATE NOCASE``), but shipped NO
-- migration — so every install that forked a store before #570 landed
-- still carries the forked rows and the invisible items today.
--
-- WHAT THIS DOES, IN ORDER
--
--   0. Purge catalogue rows with a NULL or whitespace-only name — junk
--      that is unrenderable, unmatchable and undeletable, and that used
--      to poison every set-membership test below (see the next section).
--   1. Build a TEMP mapping keyed by ``lower(name)`` over the UNION of
--      ``shopping_stores.name`` and every non-blank
--      ``shopping_list_items.store`` value. Per ``lower()`` group:
--
--        * canonical name = the winner by
--          ``(item_count DESC, sort_order ASC, rowid ASC)`` — the
--          spelling the most items already reference (the one the
--          household actually types), ties broken by the lowest
--          catalogue ``sort_order`` and then ``rowid`` so the choice is
--          deterministic on every install. A name that appears only on
--          items has no catalogue row and therefore no ``sort_order`` /
--          ``rowid``; it is COALESCEd to a maximal sentinel so it sorts
--          LAST among ties (a catalogue row the household has curated
--          beats a bare string) while still winning outright on
--          ``item_count``. The sentinel is needed because SQLite sorts
--          NULLs FIRST under ``ASC``, which would have made an unknown
--          spelling beat a real one.
--        * group ``sort_order`` = the MIN over the group's catalogue
--          rows, so the surviving store keeps the best place the
--          household ever dragged any of its spellings to. A group with
--          no catalogue row at all (pure orphan) falls back to one past
--          the current global max — the same "append past the end" rule
--          ``SqliteShoppingRepo.touch_store`` uses for a brand-new
--          store. Several orphan groups can land on that same number;
--          ``sort_order`` is not unique and ``list_stores`` breaks ties
--          on ``name``, so that is well-defined, just arbitrary.
--
--   2. Rewrite ``shopping_list_items.store`` to the canonical spelling
--      for every row whose current value differs from it. This is what
--      makes the invisible items render again.
--   2b. NULL out an item whose ``store`` is whitespace-only. It is
--      "no store" in every sense, but a whitespace string is TRUTHY in
--      JavaScript, so the SPA's ``!i.store`` bucket excluded it while
--      steps 0-1 refused to mint it a catalogue row — invisible in both
--      directions. NULL is what puts it back under "No store".
--   3. Delete the losing ``shopping_stores`` rows; INSERT a catalogue
--      row for any canonical name that had none (orphan adoption); set
--      every surviving row's ``sort_order`` to its group minimum.
--   4. Create the ``NOCASE`` unique index — the durable guard.
--   5. Drop the temp tables.
--
-- The de-dup MUST precede the index: a unique index built over
-- still-duplicated rows fails, and the add-on then boot-loops on every
-- affected install.
--
-- UNNAMEABLE CATALOGUE ROWS ARE JUNK — AND THEY POISONED THE DE-DUP
--
-- ``shopping_stores.name`` is ``TEXT PRIMARY KEY`` on a **rowid** table,
-- and SQLite's long-standing quirk is that such a primary key still
-- PERMITS NULL (only ``WITHOUT ROWID`` tables enforce NOT NULL). Nothing
-- in ``SqliteShoppingRepo`` can write one, but
-- ``BackupService._import_table`` does an unvalidated
-- ``INSERT OR IGNORE INTO shopping_stores(name, sort_order, created_at)``
-- straight from restore JSON — and this table only just became
-- exportable — so a restored, legacy, or third-party-written database
-- can absolutely carry one. A whitespace-only name arrives the same way.
--
-- Such a row is JUNK by construction: it renders as nothing in the SPA's
-- store list, no item can ever match it (``i.store === section.key``
-- against an empty/NULL key), and the UI offers no way to delete it. So
-- step 0 below purges it outright. NOTE the distinction, it matters:
-- an ITEM with ``store IS NULL`` is the legitimate "No store" bucket and
-- is left completely alone — this purge is about *catalogue rows*, never
-- about items.
--
-- Left in place, a NULL-named row did not merely linger, it broke the
-- repair and boot-looped the add-on:
--
--   * its ``fold_key`` is NULL, and ``WHERE w.fold_key = c.fold_key`` is
--     ``NULL = NULL`` → unknown → the winner subquery returns no row, so
--     that mapping row's ``canonical`` came out NULL;
--   * ``DELETE ... WHERE name NOT IN (SELECT canonical ...)`` against a
--     set containing NULL is NEVER true for any row (three-valued logic),
--     so the genuine ``Migros``/``migros`` losers survived;
--   * the orphan-adoption ``... WHERE s.name = m.canonical`` compared
--     against NULL → never true → it INSERTed a *second* NULL-named row;
--   * ``CREATE UNIQUE INDEX`` then failed over the still-duplicated rows,
--     ``schema_version`` stayed un-bumped, startup failed, and the next
--     boot repeated the whole thing — leaking one more NULL row each time.
--
-- Hence two rules that a future reader MUST NOT "simplify" away:
--
--   1. **``NOT EXISTS``, never ``NOT IN``**, wherever a name is tested
--      for set membership. ``NOT IN`` is a trap the moment a NULL can
--      reach either side; ``NOT EXISTS`` is row-wise and degrades to the
--      right answer instead of to "no rows at all".
--   2. **The candidate/mapping build itself excludes NULL and blank
--      names**, so a NULL ``canonical`` can never be produced in the
--      first place. Belt and braces: the purge and the filters are
--      independent defences.
--
-- WHAT COUNTS AS BLANK
--
-- SQLite's one-argument ``trim(x)`` strips U+0020 SPACE **only** — a
-- tab-only or NBSP-only store sails straight through ``trim(store) <> ''``
-- and gets adopted as a permanently-visible, UI-undeletable catalogue
-- row. Every blankness test in this file therefore passes an explicit
-- character set: ``trim(x, char(32)||char(9)||char(10)||char(13)||char(160))``
-- — SPACE, TAB, LF, CR, NBSP. That set deliberately tracks the write
-- path: ``_clean_store`` in ``socialhome/services/shopping_service.py``
-- uses Python's ``str.strip()`` and collapses a whitespace-only value to
-- ``None``, so the migration and the live code agree on what "no store"
-- means. (``str.strip()`` folds the full Unicode whitespace set, which
-- is strictly wider; these five are the characters that actually reach a
-- store field. If an exotic separator ever shows up on disk, widen the
-- set here — do NOT fall back to the 1-argument ``trim()``.)
--
-- ASCII-ONLY FOLDING, ON BOTH SIDES
--
-- SQLite's built-in ``lower()`` and its ``NOCASE`` collation are both
-- ASCII-only: neither folds ``"Müller"`` onto ``"MÜLLER"``. That is a
-- real limitation, but the repair and the guard agree on it EXACTLY —
-- they use the same fold — so this migration can never produce a pair
-- its own index would then reject, and a ``NOCASE``-distinct pair the
-- index tolerates is a pair the repair deliberately left apart. The
-- entry-time fold in ``_canonical_store_name`` is the same ``NOCASE``,
-- so all three layers draw the line in the same place.
--
-- NOT ATOMIC — EVERY STEP IS RE-RUNNABLE
--
-- ``run_migrations`` wraps each migration in ``with conn:``, but
-- ``conn.executescript`` issues an implicit COMMIT before it runs, so a
-- ``.sql`` migration is NOT atomic. Every step here is therefore
-- idempotent: the temp tables are ``DROP TABLE IF EXISTS``-ed up front
-- and rebuilt from scratch, the item rewrite only touches rows that
-- still differ, the catalogue delete/insert/update are all convergent,
-- and the guard is ``CREATE UNIQUE INDEX IF NOT EXISTS``. A failure
-- part-way leaves ``schema_version`` un-bumped, so the next boot runs
-- the whole file again and converges on a correct state: identical
-- items, identical catalogue membership, and the guard in place.
-- Re-running on an already-repaired database is a no-op.
--
-- One honest caveat, since it would be easy to over-claim here: a
-- *pure-orphan* group (a name that existed only on items) has no
-- catalogue ``sort_order`` to inherit and falls back to
-- ``MAX(sort_order) + 1``. If a crash lands between the catalogue DELETE
-- and the orphan INSERT, the re-run recomputes that fallback over a
-- catalogue that has since shrunk, so such a store can end up at a
-- different position than an uninterrupted run would have given it. Its
-- position is documented above as arbitrary either way, and every other
-- abort point converges exactly — the repair is correct, just not
-- byte-identical in that one cell.
--
-- AUDIT (CLAUDE.md "Before adding a SQL migration, audit the code path")
--
-- 1. Every writer of ``shopping_list_items.store`` / ``shopping_stores``
--    was read — all of them live in
--    ``socialhome/repositories/shopping_repo.py``:
--
--      * ``add`` / ``update_item`` — both route a non-empty ``store``
--        through ``_canonical_store_name`` (NOCASE lookup against the
--        catalogue) and then ``touch_store``, so since #570 they can no
--        longer fork a name. They are the reason entry-time
--        canonicalisation is NOT enough on its own: they repair nothing
--        already on disk.
--      * ``touch_store`` — ``INSERT ... ON CONFLICT(name) DO NOTHING``.
--        The conflict target is the BINARY primary key, so before this
--        migration a differently-cased name inserted a second row; after
--        it, the new ``NOCASE`` index makes that INSERT a no-op instead
--        (the ON CONFLICT clause matches any uniqueness violation on the
--        row), which is exactly the desired behaviour.
--      * ``rename_store`` / ``delete_store`` — both still match
--        case-SENSITIVELY (``WHERE name = ?`` / ``WHERE store = ?``) and
--        are therefore live fork paths; they are fixed alongside this
--        migration in the same PR.
--      * ``reorder_stores`` — writes only ``sort_order``, by exact name
--        taken from ``list_stores``; it cannot create a name.
--
--    No other surface touches these tables: the shopping list is a
--    LOCAL-HOUSEHOLD feature and is never federated (see the
--    "§23.120 — local household only, no federation" block in
--    ``socialhome/domain/events.py``; nothing under
--    ``socialhome/federation/`` references shopping at all). So unlike
--    0047, there is no remote-side copy of this data and no
--    federation-level repair to document as a limitation — the local
--    repair is the whole repair.
--
-- 2. A non-migration alternative was considered and rejected:
--    entry-time canonicalisation alone. That is precisely what #570
--    shipped, and it is why this is still broken in production. It
--    prevents new forks on the two paths it covers, but it repairs
--    nothing already written — a household that forked "Migros" in 2025
--    keeps its invisible items forever, because no code path ever
--    revisits an item that is not being edited — and it installs no
--    durable guard, so any future writer (a restored backup, an import,
--    a new code path, the still-case-sensitive rename/delete) re-creates
--    the fork. The invariant belongs where every writer meets it.
--
-- 3. This is the smallest change that holds. No column is added, no
--    table is rebuilt. Only provably-redundant catalogue rows are
--    removed — a losing row differs from the survivor by casing alone,
--    and its one piece of durable state (``sort_order``) is preserved by
--    taking the group MIN rather than the survivor's own value. Items
--    are re-pointed at a spelling that still exists rather than being
--    cleared. Orphan adoption is additive. The guard is an additive
--    INDEX, reversible by dropping it.

-- ── 0. Purge unnameable catalogue rows ────────────────────────────────

-- A ``TEXT PRIMARY KEY`` on a rowid table permits NULL in SQLite, and
-- ``BackupService._import_table`` writes this table unvalidated from
-- restore JSON. A NULL- or whitespace-only-named catalogue row cannot be
-- rendered, cannot be matched by any item, and cannot be deleted through
-- the UI — it is junk, and it is what used to break the de-dup below and
-- boot-loop the add-on. It goes first, before anything reads the table.
--
-- This step is about CATALOGUE ROWS ONLY. An *item* with ``store IS NULL``
-- is the legitimate "No store" bucket and is never touched by this file.
-- (An item whose ``store`` is whitespace-only is a different case: step 2b
-- normalises it TO NULL, which is what makes it visible again.)
--
-- ``trim(x, <set>)`` because the 1-argument ``trim()`` strips U+0020
-- only; the set is SPACE, TAB, LF, CR, NBSP (see header).
DELETE FROM shopping_stores
 WHERE name IS NULL
    OR trim(name, char(32)||char(9)||char(10)||char(13)||char(160)) = '';

-- ── 1. The mapping ─────────────────────────────────────────────────────

-- Every distinct spelling in play, from BOTH sources. ``UNION`` dedupes
-- exact matches, so a name that is both a catalogue row and an item's
-- store appears once. Blank / whitespace-only item values are "no
-- store" and are excluded here, so they are never adopted into the
-- catalogue and never rewritten below — with ``trim(x, <set>)``, because
-- the 1-argument ``trim()`` would let a tab-only or NBSP-only value
-- through. The catalogue side is filtered too: step 0 already removed
-- those rows, but keeping the guard here means no NULL ``fold_key`` and
-- hence no NULL ``canonical`` can EVER be produced, whatever else runs
-- against this table.
DROP TABLE IF EXISTS temp.shopping_store_candidates;
CREATE TEMP TABLE shopping_store_candidates AS
SELECT
    n.name AS name,
    lower(n.name) AS fold_key,
    (
        SELECT count(*) FROM shopping_list_items i WHERE i.store = n.name
    ) AS item_count,
    s.sort_order AS sort_order,
    s.rowid AS rid
  FROM (
        SELECT name FROM shopping_stores
         WHERE name IS NOT NULL
           AND trim(name, char(32)||char(9)||char(10)||char(13)||char(160)) <> ''
        UNION
        SELECT store FROM shopping_list_items
         WHERE store IS NOT NULL
           AND trim(store, char(32)||char(9)||char(10)||char(13)||char(160)) <> ''
  ) n
  LEFT JOIN shopping_stores s ON s.name = n.name;

-- One row per fold key: the winning spelling + the order the survivor
-- inherits. The COALESCE sentinels push an items-only spelling to the
-- back of a tie (SQLite sorts NULL first under ASC, which would
-- otherwise let it jump the queue).
DROP TABLE IF EXISTS temp.shopping_store_case_map;
CREATE TEMP TABLE shopping_store_case_map AS
SELECT
    c.fold_key AS fold_key,
    (
        SELECT w.name
          FROM shopping_store_candidates w
         WHERE w.fold_key = c.fold_key
         ORDER BY w.item_count DESC,
                  COALESCE(w.sort_order, 2147483647) ASC,
                  COALESCE(w.rid, 9223372036854775807) ASC
         LIMIT 1
    ) AS canonical,
    COALESCE(
        MIN(c.sort_order),
        (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM shopping_stores)
    ) AS sort_order
  FROM shopping_store_candidates c
 GROUP BY c.fold_key;

-- ── 2. Re-point the items ──────────────────────────────────────────────

UPDATE shopping_list_items
   SET store = (
        SELECT m.canonical FROM shopping_store_case_map m
         WHERE m.fold_key = lower(shopping_list_items.store)
   )
 WHERE store IS NOT NULL
   AND trim(store, char(32)||char(9)||char(10)||char(13)||char(160)) <> ''
   AND store <> (
        SELECT m.canonical FROM shopping_store_case_map m
         WHERE m.fold_key = lower(shopping_list_items.store)
   );

-- ── 2b. A blank-ish item store becomes a real NULL ─────────────────────

-- An item carrying ``'\t'``, ``'\u00a0'`` or ``'   '`` is "no store"
-- in every sense that matters, but it is NOT NULL — and that distinction
-- is exactly what makes the item invisible, which is the bug this whole
-- file exists to fix. The SPA's grouped view buckets unassigned items
-- with a truthiness test (``!i.store`` in ``ShoppingPage.tsx``), and a
-- whitespace string is TRUTHY in JavaScript. So such an item is excluded
-- from the "No store" section, while steps 0-1 deliberately refused to
-- mint it a catalogue row to match against — leaving it in no section at
-- all.
--
-- Normalising it to NULL is what actually puts the item back on screen,
-- under "No store". It also converges the column on the single
-- representation the write path already produces: ``_clean_store``
-- (``services/shopping_service.py``) collapses a whitespace-only name to
-- ``None`` via Python's ``str.strip()``, whose fold is the set used here.
--
-- Same character set as everywhere else in this file. Items with a
-- genuine store name are untouched; items already NULL are untouched.
UPDATE shopping_list_items
   SET store = NULL
 WHERE store IS NOT NULL
   AND trim(store, char(32)||char(9)||char(10)||char(13)||char(160)) = '';

-- ── 3. Repair the catalogue ────────────────────────────────────────────

-- Losers go. (Every nameable catalogue row is a candidate, so its fold
-- key has a mapping row; a name absent from ``canonical`` is therefore a
-- row some other spelling won.)
--
-- ``NOT EXISTS``, NOT ``NOT IN``: ``NOT IN`` against a set that contains
-- a single NULL is never true for ANY row, so one junk row used to make
-- this statement silently delete nothing and take the whole migration
-- down with it. Do not "simplify" this back.
DELETE FROM shopping_stores
 WHERE NOT EXISTS (
        SELECT 1 FROM shopping_store_case_map m
         WHERE m.canonical = shopping_stores.name
 );

-- Orphan adoption: a canonical name that only ever existed on items now
-- gets the catalogue row that makes its items render.
--
-- ``m.canonical IS NOT NULL`` is belt-and-braces against the C1 failure
-- mode: a NULL canonical made ``s.name = m.canonical`` unknown for every
-- existing row, so ``NOT EXISTS`` held and this INSERT minted a fresh
-- NULL-named row on every boot. The mapping can no longer produce one —
-- this guard makes that impossible to regress silently.
INSERT INTO shopping_stores(name, sort_order)
SELECT m.canonical, m.sort_order
  FROM shopping_store_case_map m
 WHERE m.canonical IS NOT NULL
   AND NOT EXISTS (
        SELECT 1 FROM shopping_stores s WHERE s.name = m.canonical
 );

-- Survivors inherit the group's best position.
UPDATE shopping_stores
   SET sort_order = (
        SELECT m.sort_order FROM shopping_store_case_map m
         WHERE m.canonical = shopping_stores.name
   )
 WHERE EXISTS (
        SELECT 1 FROM shopping_store_case_map m
         WHERE m.canonical = shopping_stores.name
 );

-- ── 4. The guard ───────────────────────────────────────────────────────

CREATE UNIQUE INDEX IF NOT EXISTS ux_shopping_stores_name_nocase
    ON shopping_stores(name COLLATE NOCASE);

-- ── 5. Scratch tables go ───────────────────────────────────────────────

DROP TABLE temp.shopping_store_case_map;
DROP TABLE temp.shopping_store_candidates;
