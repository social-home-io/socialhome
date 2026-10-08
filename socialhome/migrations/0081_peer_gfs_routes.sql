-- 0081 — GFS relay as a fallback transport for paired households.
--
-- Two paired households (QR / §11 auto-pair, ``source = 'manual'``) talk
-- over the WebRTC DataChannel, else the HTTPS inbox. When NEITHER side has
-- an address the other can reach, the RTC signalling has nowhere to go and
-- the pair is silent. The connection-server envelope relay
-- (``POST /gfs/envelope``) already carries sealed §24.11 envelopes for
-- households seated from an invite link; this migration lets a paired
-- household use it too, as a last-resort tier, once it opted in.
--
--   * ``remote_instances.gfs_relay`` — OUR opt-in for relaying to / from
--     this peer. ``0`` (the default, every existing row) means a relayed
--     envelope from this peer is refused by the inbound pipeline and
--     nothing is ever sent to it through a connection server.
--   * ``peer_gfs_routes`` — the connection servers through which this peer
--     was confirmed reachable. Each household stores only ITS OWN
--     ``gfs_connections.id`` per route — never a peer's server id, URL or
--     inbox id, and nothing here ever goes on a wire. ``confirmed_at`` is
--     when the route was first proven, ``last_ack_at`` when it last was
--     (route expiry reads it). Timestamps are tz-aware UTC ISO 8601, the
--     shape the repository writes.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data. ``remote_instances``
--       is written by the pairing coordinator, the §11 auto-pair
--       coordinator and the §D2b invite seat, all through
--       ``SqliteFederationRepo.save_instance`` (one upsert) plus targeted
--       single-column setters; it is read by the transport facade
--       (``FederationTransport.send``), the outbox redelivery
--       (``app._redeliver_envelope``) and the inbound pipeline
--       (``make_lookup_instance*`` → ``make_check_peer_class``). The relay
--       transport (``GfsRelayTransport``) reads ``relay_via`` and
--       ``remote_keywrap_pk``. ``gfs_connections`` rows are deleted when
--       the household unpairs a connection server; ``gfs_space_publications``
--       (0001) already cascades from it the same way.
--   (2) Non-migration alternatives considered and rejected:
--       * Reuse ``relay_via`` — on a ``manual`` row it already stores the
--         §11 auto-pair introducer's instance id (read by the auto-pair
--         flow and the admin diagnostics), and it is single-valued while
--         routes are N:M (a pair may share several connection servers).
--       * A JSON list column on ``remote_instances`` — cannot cascade when
--         the household removes a connection server, so a dead server id
--         would linger in every peer row until something swept it; the
--         FK below makes removal exact, and expiry is a plain indexed
--         DELETE.
--       * Derive the opt-in from "has routes" — routes are discovered,
--         the opt-in is a decision; a household that opted out must refuse
--         relayed envelopes even when a route was once confirmed.
--   (3) Smallest possible change. Additive only: one ``ADD COLUMN`` with a
--       ``NOT NULL DEFAULT 0`` (no backfill — every existing pair stays
--       exactly as it is, relay off) and one new table with the index the
--       ``gfs_connections`` cascade scan needs (``gfs_connection_id`` is
--       the PK's second column, so the PK cannot serve it — the same
--       reasoning as ``idx_gfs_space_publications_gfs`` in 0001). No
--       existing row is changed.
ALTER TABLE remote_instances ADD COLUMN gfs_relay INTEGER NOT NULL DEFAULT 0
    CHECK (gfs_relay IN (0, 1));

CREATE TABLE IF NOT EXISTS peer_gfs_routes (
    instance_id       TEXT NOT NULL
                      REFERENCES remote_instances(id) ON DELETE CASCADE,
    gfs_connection_id TEXT NOT NULL
                      REFERENCES gfs_connections(id) ON DELETE CASCADE,
    confirmed_at      TEXT NOT NULL,
    last_ack_at       TEXT NOT NULL,
    PRIMARY KEY (instance_id, gfs_connection_id)
);
CREATE INDEX IF NOT EXISTS idx_peer_gfs_routes_gfs
    ON peer_gfs_routes(gfs_connection_id);
