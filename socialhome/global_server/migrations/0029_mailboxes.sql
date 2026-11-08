-- GFS 0029 — anonymous per-space mailboxes (GFS redesign PR 1). Follows 0028.
--
-- A household reached through this server gets one mailbox per (household,
-- space, generation): an address derived from a mailbox signing key
-- (``mailbox_id = base32(SHA-256("sh-mbx-v1" ‖ sign_pk))[:32]``), registered
-- anonymously with proof of possession (``POST /gfs/mailbox/register``) and
-- claimed on its own anonymous WebSocket (``GET /gfs/mailbox/ws``). Envelopes
-- and binary media frames addressed to it wait in the EXISTING
-- ``gfs_envelope_queue`` / ``gfs_media_queue`` rows keyed by the mailbox id —
-- it has the instance-id shape on purpose.
--
-- What this table holds is only what the server needs to authorize a claim:
-- the pinned key (trust on first use) and two timestamps for the idle
-- sweep. NO household identity, NO client address, NO space id — the server
-- cannot tell whose mailbox it is or which mailboxes belong together.
--
-- Migration audit (mandatory 3 points, CLAUDE.md):
--
--   (1) Audited every code path that already touches this data.
--       ``client_instances`` (instance ids + identity / key-wrap keys of
--       registered households) is the only key table a relay recipient is
--       checked against today (``GfsEnvelopeRelay.accept``). The two queues
--       (0011/0014, 0028) hold opaque frames keyed by a 32-char recipient id
--       with no FK, so they take a mailbox id as is. ``gfs_moment_key_mailbox``
--       (0021) is a per-(author, follower HOUSEHOLD) store of sealed keys —
--       identity-bound by design. ``gfs_channels`` / channel seats (0016) are
--       per-space and bind seats to registered instances. Nothing stores a
--       recipient address that is not an instance.
--
--   (2) Non-migration alternatives considered and REJECTED:
--       * a row in ``client_instances`` per mailbox — that table is the
--         household directory (display name, identity key, key-wrap key,
--         status, relay-seen); a mailbox has none of those and must never
--         be listed, counted or treated as a household;
--       * a ``gfs_channels``-style per-space seat — it would tell the server
--         which space a mailbox belongs to, exactly what the redesign hides;
--       * keeping registrations in memory — a restart, or another cluster
--         node taking the claim socket, would lose the pinned key and let
--         anyone re-register (TOFU needs the pin to survive) or refuse the
--         owner's claim; the cluster nodes share this DB for that reason;
--       * deriving the key from the id at claim time — impossible (the id is
--         a hash of the key); the claimant would have to send the key, and
--         then anyone could "claim" by sending their own key for an id that
--         is merely a hash of it, i.e. the id would bind nothing.
--
--   (3) Smallest possible change: ONE additive CREATE TABLE with its
--       primary key and one index for the idle sweep. No existing table is
--       altered, no row rewritten, no backfill. The queues keep their shape;
--       retention stays in code (``mailbox_idle_days`` swept by
--       ``GfsMaintenanceScheduler``).
--
-- Value domain:
--   mailbox_id     32 lowercase base32 chars, recomputed by the server from
--                  ``sign_pk`` before anything is stored.
--   sign_pk        the 32-byte Ed25519 mailbox public key (pinned on first
--                  use; a claim must be signed by it).
--   created_at     unix seconds of the first registration.
--   last_claim_at  unix seconds of the latest successful claim, NULL until
--                  the first one. The idle sweep drops a mailbox whose
--                  COALESCE(last_claim_at, created_at) is older than
--                  ``mailbox_idle_days``.

CREATE TABLE IF NOT EXISTS gfs_mailboxes (
    mailbox_id    TEXT    PRIMARY KEY,
    sign_pk       BLOB    NOT NULL CHECK (length(sign_pk) = 32),
    created_at    INTEGER NOT NULL,
    last_claim_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gfs_mailboxes_idle
    ON gfs_mailboxes(COALESCE(last_claim_at, created_at));
