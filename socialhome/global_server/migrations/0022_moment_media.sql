-- GFS 0022 — encrypted public-moment media (v_63). Follows 0021 (moment key
-- mailbox).
--
-- A public moment's photo or video never reached a GFS follower: only its
-- ``api/media/<name>`` reference travelled, which names a file on the
-- author's household. Now the author's household encrypts the file once
-- (``socialhome/moment_public_media.py`` — a key derived from the moment's
-- item key, padded to a size bucket) and uploads the ciphertext once per
-- GFS; followers (and, later, a share link's viewer) fetch it here. This
-- server can't open it.
--
-- CLAUDE.md "audit before a migration":
-- 1. Audited every store that holds bytes for an author here:
--    ``gfs_user_pictures`` (one avatar per user, replaced in place, public
--    plaintext — the directory surface), the §D2b ``gfs_envelope_queue``
--    (small sealed envelopes per recipient household, deleted on delivery)
--    and ``gfs_moment_key_mailbox`` (one sealed key per author × household).
--    None holds a content-addressed blob that many households fetch.
-- 2. Alternatives rejected: streaming the file live from the author's
--    household per follower (N × size from a home uplink, and only while
--    it is online); putting the bytes in the moment envelope (every moment
--    frame would carry up to 8 MB through the WebSocket fan-out, also to
--    households that never look); the picture table (one row per user,
--    public plaintext).
-- 3. Smallest change: one table of metadata (the bytes are files under the
--    media dir, named by their SHA-256), cascading with the registration;
--    rows and files go when the moment's life ends.
CREATE TABLE IF NOT EXISTS gfs_moment_media (
    -- sha256 of the blob, lowercase hex — the blob's address.
    blob_id         TEXT PRIMARY KEY,
    author_user_id  TEXT NOT NULL
        REFERENCES gfs_user_registrations(user_id) ON DELETE CASCADE,
    -- Ciphertext bytes (a padded bucket + nonce + tag).
    size            INTEGER NOT NULL CHECK(size > 0),
    -- Unix seconds; never beyond a moment's longest life (+ slack).
    expires_at      INTEGER NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_gfs_moment_media_author
    ON gfs_moment_media(author_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_gfs_moment_media_expires
    ON gfs_moment_media(expires_at);
