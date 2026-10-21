-- 0118 — public moments are encrypted from the GFS: moment content keys.
-- Follows 0117 (hide profile from followers).
--
-- A public moment used to cross the GFS in plaintext (signed, held in GFS
-- memory during fan-out). From v_62 it is encrypted under its author's
-- moment key: one random root per author and GENERATION, ratcheted forward
-- once a day (``moment_public_seal.ratchet``), each moment sealed under a
-- key derived from the current step and its id. The author's household
-- mints a generation (is_own = 1) and seals the current step key to each
-- follower household's key-wrap key; a follower household keeps the step
-- keys it was delivered (is_own = 0), advancing them in place.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every store a moment key could live in. ``space_keys``
--       (the per-space content keys, KEK-sealed) has ``space_id
--       REFERENCES spaces(id)`` — an author is not a space, and a fake
--       space row would make an author look like a space everywhere spaces
--       are listed, synced, exported or federated. ``moment_public_
--       registrations`` is per (user, GFS) and is deleted on deregister, so
--       a generation counter there would restart and repeat. ``moment_public_
--       follows`` is per (local follower, author, GFS) — a household with
--       two local followers would hold the key twice, and a follow deleted
--       and re-made would lose it. ``instance_config`` is a flat
--       key/value store with no per-row KEK binding. Readers of the new
--       table: ``MomentPublicKeyOutbound`` (author: mint, ratchet, seal to
--       followers) and ``MomentPublicKeyInbound`` (follower: import, open).
--       Backup / export / recovery kit never copy it.
--   (2) Alternatives rejected: deriving every root from the identity seed
--       (still needs a stored generation per author, and a follower still
--       has to store what it was given); keeping keys in memory (a restart
--       would leave a follower unable to read anything until the author's
--       next delivery, and an author would re-key every follower on each
--       restart); no key at all with per-follower sealing of every moment
--       (N sealed copies per moment through the GFS instead of one).
--   (3) Smallest change: one new table, no change to any existing row or
--       column. One row per (author, role, generation) — the step key is
--       advanced in place, so an old step is gone once the ratchet moves
--       on. Keys are KEK-sealed with (role, author, generation) as
--       associated data, so a sealed key can't be moved onto another row.
CREATE TABLE IF NOT EXISTS moment_public_keys (
    author_user_id  TEXT NOT NULL,
    -- 1 = this household's own author (minted here); 0 = delivered by a
    -- followed author's household. Kept apart so a delivered key can never
    -- become the key a local author encrypts with.
    is_own          INTEGER NOT NULL CHECK(is_own IN (0, 1)),
    -- Unix seconds when the generation was minted (``max(now, previous +
    -- 1)``): never repeats, even after the author deregisters everywhere.
    key_gen         INTEGER NOT NULL,
    -- The ratchet step ``key_sealed`` is at (one step a day).
    key_step        INTEGER NOT NULL DEFAULT 0 CHECK(key_step >= 0),
    -- KEK-encrypted 32-byte step key (``KeyManager.encrypt``).
    key_sealed      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- Own keys only: a follower household left / was blocked / a
    -- registration ended since this generation was minted. The next publish
    -- (at most once per 6 h) starts a new generation — lazy rotation.
    rotate_due      INTEGER NOT NULL DEFAULT 0 CHECK(rotate_due IN (0, 1)),
    PRIMARY KEY (author_user_id, is_own, key_gen)
);
