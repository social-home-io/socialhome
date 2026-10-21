-- 0116 — the share tokens this household minted for its public highlight
-- links.
--
-- A public highlight link is ``…/highlight/{instance}/{highlight}/{token}
-- #k={secret}``; the GFS mints the token and forwards it with each viewer's
-- offer / relay request. Until now the household derived the link secret
-- from whatever token the GFS forwarded, so revoking one link was enforced
-- by the GFS alone. Now the household streams only for a token it minted
-- itself and has not revoked.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the data: the token is returned by
--       the GFS in ``HighlightPublicationService.publish`` (and only there),
--       revoked in ``revoke_token`` and dropped with the publication in
--       ``unpublish`` / ``on_account_protected`` (``mark_unpublished``);
--       ``HighlightSignalingHandler`` reads it off each offer / relay. The
--       household kept none of it: ``highlights`` holds only
--       ``public_gfs_id`` / ``public_published_at`` (one publication, many
--       tokens).
--   (2) Alternatives rejected: asking the GFS whether a token is live (that
--       is the trust this removes); keeping tokens in memory (a restart
--       would kill every link); a JSON list column on ``highlights`` (no
--       constraint, no cascade, a read-modify-write on every publish);
--       having the household mint a MAC'd token itself (a GFS wire change,
--       and a stateless token still can't be revoked).
--   (3) Smallest change: one new table, no change to any existing row or
--       column, no backfill — links minted before this carry no ``#k``
--       secret and already open nothing.
CREATE TABLE IF NOT EXISTS highlight_public_tokens (
    -- SHA-256 (lowercase hex) of the share token: the bearer token itself is
    -- never kept at rest here.
    token_hash    TEXT PRIMARY KEY CHECK(length(token_hash) = 64),
    highlight_id  TEXT NOT NULL REFERENCES highlights(id) ON DELETE CASCADE,
    -- The GFS that minted it; a token is honoured only via that publication.
    gfs_id        TEXT NOT NULL REFERENCES gfs_connections(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_highlight_public_tokens_highlight
    ON highlight_public_tokens(highlight_id);
-- Covers the ON DELETE CASCADE child-scan when a GFS is unpaired.
CREATE INDEX IF NOT EXISTS idx_highlight_public_tokens_gfs
    ON highlight_public_tokens(gfs_id);
