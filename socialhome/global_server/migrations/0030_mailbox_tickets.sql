-- GFS 0030 — anonymous registration tickets for mailboxes. Follows 0029.
--
-- ``POST /gfs/mailbox/register`` now spends one Privacy Pass token (RFC
-- 9576/9577/9578, publicly verifiable, blind RSA RFC 9474). A household
-- registered here fetches a daily batch over its signed channel
-- (``POST /gfs/mailbox/tickets``); the server blind-signs and never sees
-- the token it later redeems.
--
--   gfs_ticket_issuer_keys — the issuer RSA key per epoch, shared by every
--       node of a cluster (a token issued on node A is redeemed on node B).
--   gfs_ticket_issuance    — ONE row per household: the UTC day and how many
--       tokens it took that day. Nothing per token, no blinded message, so
--       nothing ties an issuance to a redemption.
--   gfs_spent_tickets      — the nonce of every redeemed token until its key
--       stops being accepted (double-spend guard). Nothing about who spent it.
--
-- Migration audit (mandatory 3 points, CLAUDE.md).
--
--   (1) Audited every code path that already touches this data. Mailbox
--       registration (``GfsMailboxService.register``) was limited by an
--       in-memory per-address sketch and a server-wide daily count read
--       from ``gfs_mailboxes.created_at`` (``SqliteGfsMailboxRepo.register``);
--       that cap is the denial-of-service this replaces and is dropped from
--       code (the index ``idx_gfs_mailboxes_created`` stays, unused but
--       harmless — dropping it is a rewrite for no gain). No table holds a
--       per-household budget or an issuer key; the cluster shares only the
--       identity seed (optional — approved-key clusters have none) and this
--       database.
--
--   (2) Non-migration alternatives considered and REJECTED:
--       * issuer key in TOML / derived from the identity seed: an
--         approved-key cluster has no shared secret, deterministic RSA key
--         generation from a seed is not a standard building block, and a
--         TOML key would never rotate;
--       * spent nonces in memory (like the register nonce): a token spent on
--         node A could be spent again on node B and again after a restart;
--       * counting issuance in memory: per node, so a household would get
--         N × nodes a day, and a restart would reset it.
--
--   (3) Smallest possible change: three new tables, no existing row or
--       column touched. Every table is bounded — two issuer keys live, one
--       row per registered household, spent nonces only for the two live
--       keys — and the maintenance loop prunes the rest.

CREATE TABLE IF NOT EXISTS gfs_ticket_issuer_keys (
    epoch       INTEGER PRIMARY KEY,
    key_id      BLOB    NOT NULL UNIQUE CHECK (length(key_id) = 32),
    private_key BLOB    NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gfs_ticket_issuance (
    instance_id TEXT    PRIMARY KEY,
    day         INTEGER NOT NULL,
    issued      INTEGER NOT NULL CHECK (issued >= 0)
);

CREATE TABLE IF NOT EXISTS gfs_spent_tickets (
    nonce      BLOB    PRIMARY KEY CHECK (length(nonce) = 32),
    expires_at INTEGER NOT NULL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_gfs_spent_tickets_expiry
    ON gfs_spent_tickets(expires_at);
