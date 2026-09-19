-- Store-and-forward queue for sealed household-to-household envelopes
-- relayed by ``POST /gfs/envelope`` (§D2b bootstrap redeem).
--
-- Two households introduced by an invite link never learn each other's
-- network address. The redeemer seals a self-authenticating body to the
-- issuer's published key-wrap key and hands the GFS an OPAQUE blob addressed
-- only by instance id; the GFS pushes it over that household's ``/gfs/ws``
-- socket. When the recipient is offline the blob has to wait somewhere —
-- that is this table, and nothing else about the relay is persisted.
--
-- The GFS stores only ciphertext it cannot open: ``sealed_json`` is the
-- verbatim ``{kem_suite, eph_pk, ciphertext}`` dict the sender produced. No
-- sender attribute is stored at all (there is none on the wire — the routing
-- envelope names the recipient and nothing else), so this table cannot
-- reconstruct who talked to whom.
--
-- Migration audit (mandatory 3 points, CLAUDE.md):
--
--   (1) Audited every code path that already touches this data. There is
--       none: nothing on the GFS stores anything per relayed message today.
--       ``GfsFederationService.publish_event`` fans a space event out and
--       keeps only an in-memory, per-node replay digest (5 min, no table);
--       ``gfs_highlight_publications`` / ``gfs_moment_*`` store routing
--       METADATA for published content, never the bytes; ``client_instances``
--       stores published public keys. ``ws_registry`` holds live sockets in
--       memory and drops a frame when no socket is registered. So this is the
--       first GFS-side store of relayed bytes — deliberately opaque, TTL'd
--       and capped, and correspondingly the first place the "should this even
--       be in the database" question has a yes.
--
--   (2) Non-migration alternative considered and REJECTED: drop the envelope
--       when the recipient has no live socket (what every other GFS push path
--       does today). That is correct for a space fan-out — the subscriber
--       re-syncs on reconnect from the authoritative household — but there is
--       no such authority here: the issuing household is the only holder of
--       the invite state, and the redeem is a one-shot request/reply. Dropping
--       makes an invite link fail whenever the issuer happens to be asleep,
--       which is most of the night. In-memory buffering was rejected too: a
--       GFS restart (or a second cluster node taking the socket) would lose
--       the blob with no way to notice, reintroducing the same failure with
--       worse diagnosability. Re-using an existing table was rejected because
--       none of them holds message bytes, and widening one that holds routing
--       metadata to also hold ciphertext would blur exactly the boundary this
--       relay is built to keep sharp.
--
--   (3) Smallest possible change. ONE additive CREATE TABLE plus its lookup
--       index. No column added to, renamed in, or removed from any existing
--       table; no backfill; no rewrite of a single existing row. Retention is
--       enforced in code (``ENVELOPE_QUEUE_TTL_SECONDS`` swept by
--       ``GfsMaintenanceScheduler``; ``ENVELOPE_QUEUE_MAX_PER_RECIPIENT``
--       and ``ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT`` enforced on insert,
--       tail-dropping the NEW envelope at either ceiling) rather than by a
--       trigger, so the policy stays visible next to the relay it bounds.
--
-- Value domain:
--   to_instance  recipient household instance id. The route validates the
--                exact identifier shape before anything is stored or
--                logged — 32 lowercase base32 characters, anchored
--                (``ENVELOPE_INSTANCE_ID_RE``), which is what
--                ``derive_instance_id`` produces. NEVER a sender id —
--                there is no sender field.
--   sealed_json  the verbatim sealed dict as JSON. Opaque; never parsed,
--                never logged.
--   created_at   unix seconds, insertion order for the in-order drain.
--   expires_at   unix seconds; rows at or past this are swept and never
--                delivered.

CREATE TABLE IF NOT EXISTS gfs_envelope_queue (
    id          INTEGER PRIMARY KEY,
    to_instance TEXT    NOT NULL,
    sealed_json TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);

-- The drain reads one recipient's rows in insertion order; the sweep reads
-- by expiry. The composite covers the first exactly and the second well
-- enough at this table's size (bounded by clients ×
-- ``ENVELOPE_QUEUE_MAX_PER_RECIPIENT`` rows, and by
-- ``ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT`` bytes, whichever binds first).
CREATE INDEX IF NOT EXISTS idx_gfs_envelope_queue_to
    ON gfs_envelope_queue(to_instance, created_at);
