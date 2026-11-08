-- GFS 0028 — binary relay media frames waiting for an offline household
-- (v_87, ``POST /gfs/envelope`` with a ``\x00SHR`` body). Follows 0027.
--
-- Its own table, not rows in ``gfs_envelope_queue``: that queue keeps sealed
-- JSON in a TEXT column whose ``frame_type`` CHECK allows only 'envelope' /
-- 'relay' (0014) — a third kind would mean rebuilding the table. A separate
-- table also keeps the budgets apart by construction: media bytes are never
-- counted against, and can never evict or crowd out, a household's control
-- envelopes. ``body`` is the opaque frame exactly as posted (the sealed
-- media and its recipient id; no sender exists on the wire). Swept by the
-- maintenance loop at ``expires_at``.
CREATE TABLE IF NOT EXISTS gfs_media_queue (
    id          INTEGER PRIMARY KEY,
    to_instance TEXT    NOT NULL,
    body        BLOB    NOT NULL,
    size_bytes  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gfs_media_queue_to
    ON gfs_media_queue(to_instance, expires_at);
