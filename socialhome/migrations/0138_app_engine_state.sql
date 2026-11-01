-- 0138 — App engine state (group play PR 9, v_78). Follows 0137.
--
--   app_engine_state  One row per (table, app engine) this household runs:
--       ``state_json`` is one KEK-wrapped blob ``{"v": 1, "enc": "<nonce:ct>"}``
--       (KeyManager AES-GCM, AAD bound to the table id and the engine id).
--       For the gift draw (engine ``draw``): the organiser's settings, its
--       people's wish lists, this household's mixer key and person seeds
--       per round, the round's public data it checks proofs against, and
--       its people's results; on the hub also the relay state (keys,
--       onions, the signed chain, commitments, sealed cards — ciphertext
--       and signatures). ``updated_at`` UTC ISO 8601. Gone with its table
--       (FK ON DELETE CASCADE: the 7-day / 90-day prune, the space cascade).
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the data. ``app_group_sessions``
--       (0136, 0137): ``SqliteAppGroupRepo`` (upsert, ``save_newer``,
--       ``prune``, the lobby expiry, ``delete``), ``AppGroupService`` (hub,
--       mirrors, sweep, uninstall), ``SpaceTableService`` (cards, the space
--       cascade). The engine's state is read and written only by
--       ``SqliteAppEngineStateRepo`` from the engine itself; nothing else
--       reads the table. A table's delete (prune, space cascade, uninstall)
--       takes its engine rows with it.
--   (2) Non-migration alternatives, rejected:
--       * a ``draw_json`` column on ``app_group_sessions`` (the first cut) —
--         one engine per column; the next engine (a "deal" of secret
--         cards) would need another column, and a table may host several;
--       * the app's own ``store`` — plaintext, and readable by the app,
--         which must never see a result (decision D13);
--       * ``app_pending_sessions.payload_json`` — per person, not per
--         table, and the hub's relay state is no person's;
--       * ``seat_key_json`` — its shape is checked on every roster.
--   (3) Smallest change: one new table, additive, no backfill, keyed by its
--       primary key only (no extra index).

CREATE TABLE app_engine_state (
    session_id TEXT NOT NULL
        REFERENCES app_group_sessions(session_id) ON DELETE CASCADE,
    engine     TEXT NOT NULL CHECK (length(engine) BETWEEN 1 AND 32),
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, engine)
);
