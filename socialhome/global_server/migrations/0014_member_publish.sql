-- 0014 — trusted-mode member publish (v_49): the newest proven content epoch
-- per space, and offline delivery of member-published items.
--
-- ``POST /gfs/member-publish`` lets a space member household publish its own
-- item with a space-authority-signed writer cert instead of a host signature
-- (``socialhome/global_server/member_publish.py``). Two things need a home:
--
--   * the newest CONTENT epoch this server has seen proven, so it relays only
--     certs of that epoch (plus the previous one for a short grace) and a
--     household removed at the last rotation stops being relayed;
--   * the items themselves while a subscriber is offline — the point of the
--     feature is that posting works while the host is offline, so "the
--     subscriber re-syncs from the host later" is exactly the assumption that
--     no longer holds.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every path that touches this data. Epochs: the GFS sees a
--       content epoch only as the plaintext, authority-signed ``epoch`` of a
--       ``space_post_public`` relay (``GfsFederationService.publish_event``);
--       subscriber key handoffs carry theirs inside the sealed blob, and the
--       ``authority_cert`` epoch (0013) is the AUTHORITY key's wall-clock
--       epoch, a different counter. Nothing stores a content epoch today.
--       Offline delivery: ``publish_event`` / ``_fan_out`` push over WS and
--       fall back to an HTTPS inbox POST that is structurally guaranteed to
--       fail (see ``_deliver_one``); the only GFS store of relayed bytes is
--       ``gfs_envelope_queue`` (0011), drained in order on hello.
--   (2) Alternatives considered and rejected. Epoch in memory only: a seed
--       holder sends its epoch notice ONCE per rotation, so a restart would
--       forget it and reopen the window for a removed writer until somebody
--       else happens to publish. Epoch on ``GlobalSpace``'s existing columns:
--       none holds it (``authority_cert`` is per authority key, not per
--       content epoch). A new table for the epoch: three nullable columns on
--       the row they describe are smaller and die with it. For the queue, a
--       second table was rejected in favour of reusing ``gfs_envelope_queue``
--       — same drain-on-hello, same TTL sweep, same per-recipient caps — with
--       a discriminator so the drain knows which frame to rebuild; and storing
--       the discriminator inside the JSON blob was rejected because that
--       column is opaque by contract and a DB-level CHECK beats a convention.
--   (3) Smallest possible change: four additive ``ADD COLUMN``s. The three
--       epoch columns are NULL-defaulted (NULL = no epoch learned yet; no
--       backfill). ``frame_type`` defaults to ``'envelope'``, which is what
--       every existing row is, so no row is rewritten.
--
-- What the GFS learns from the epoch columns is what it already saw on
-- authority-signed relays: that the space's content key rotated, and when.
-- They are never served on the public directory (``GlobalSpace`` does not
-- carry them) and are cleared whenever the authority key is re-pinned.
ALTER TABLE global_spaces ADD COLUMN content_epoch INTEGER;
ALTER TABLE global_spaces ADD COLUMN content_epoch_prev INTEGER;
ALTER TABLE global_spaces ADD COLUMN content_epoch_seen_at INTEGER;

-- ``'envelope'``: a sealed §D2b blob, drained as ``{type: "envelope",
-- sealed}``. ``'relay'``: a member-published space item, ``sealed_json``
-- holding the identity-free fan-out frame, drained as ``{type: "relay",
-- **frame}``. Each kind is capped per recipient on its own, so a busy space
-- can never crowd a household's invite / link-federation envelopes out.
ALTER TABLE gfs_envelope_queue ADD COLUMN frame_type TEXT NOT NULL
    DEFAULT 'envelope' CHECK (frame_type IN ('envelope', 'relay'));
