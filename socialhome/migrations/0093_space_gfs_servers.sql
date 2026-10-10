-- 0093 — ``spaces.gfs_servers``: the GFSs a space is listed on, as the
-- OWNER household signed them. A JSON object
-- ``{servers: [{gfs_instance_id, inbox_url, public_key, gfs_key_suite,
-- display_name}, ...], issued_at, owner_pk, gfs_servers_sig_suite,
-- gfs_servers_sig}`` (``socialhome/domain/gfs_server.py``,
-- ``socialhome/gfs_server_list_sig.py``), kept byte-identical to what the
-- owner signed so any household can relay it. NULL = not known yet (an older
-- owner, or a space this household never saw a list for) — readers fall back
-- to today's behaviour; a signed empty ``servers`` = listed nowhere.
--
-- * On the OWNER household it is derived from the space's active
--   ``gfs_space_publications`` rows joined with ``gfs_connections``
--   (``GfsServerSelectionService.sync_snapshot``); those rows stay the truth.
--   Leaving the public tiers signs an empty list.
-- * On a MEMBER household it is the owner's signed list from ``space_meta``
--   (``SPACE_CONFIG_CHANGED``, the §D1b join snapshots, the rotation
--   bundle), taken only when it verifies against the owner household's
--   identity key (``derive_instance_id(owner_pk) == owner_instance_id``) and
--   its ``issued_at`` is newer than the stored one. A delegated admin can
--   relay the owner's list but cannot write, alter or replay one.
-- * Written only by ``set_gfs_servers`` — never by the ``save`` upsert.
--
-- A member needs it to reach every GFS listing the space — including GFSs its
-- own household never connected to — while the host is offline.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data.
--       * ``gfs_space_publications`` (0001, 0027): written only by
--         ``SqliteGfsConnectionRepo.publish_space`` / ``unpublish_space``,
--         reached only from ``GfsConnectionService.publish_space`` /
--         ``unpublish_space`` (and the ``*_to_all`` / ``*_from_listed`` /
--         ``republish_space`` loops over them) — plus
--         ``SqliteGfsConnectionRepo.delete``, which removes a connection's
--         rows directly on disconnect (FK ``gfs_connection_id →
--         gfs_connections(id) ON DELETE CASCADE`` as a backstop). It
--         exists only on the household that published, keyed by ITS OWN
--         connection ids — a member has no such rows.
--       * ``gfs_connections`` (0001): this household's own pairings, so a
--         member can describe only GFSs it is itself connected to.
--       * ``peer_gfs_routes`` (0081): per paired PEER, which of OUR
--         connections reach it — no space in it at all.
--       * ``spaces.gfs_channel_id`` / ``gfs_channel_pk`` /
--         ``gfs_channel_healed_at`` and ``space_keys.gfs_channel`` (0077):
--         the PRIVATE space's opaque channel and the member's grant. The
--         grant's ``gfs_ids`` (``GfsChannelGrant``) are bare
--         ``gfs_instance_id`` values — no URL, no key — and sit under the
--         authority ``binding_sig``; public / global spaces have no grant.
--       * ``space_metadata_for_federation`` / ``stub_space_from_metadata``
--         (space_service) and the inbound ``SPACE_CONFIG_CHANGED`` handler
--         (federation_inbound_service, with its owner-only pins for
--         ``gfs_publish_mode`` / ``private_gfs``) and the forwarded-edit
--         path (``SpaceService._run_admin_action`` /
--         ``_REMOTE_CONFIG_FIELDS``): the config carriage carries no GFS
--         list today — nothing on a member names the servers a space is on.
--   (2) Non-migration alternatives considered and rejected:
--       * Member-side ``gfs_space_publications`` rows — the FK needs a
--         ``gfs_connections`` row, which a member does not have for a GFS it
--         never connected to (and must not create: that is a pairing).
--       * A new table — one small list per space is exactly the row's grain;
--         a table adds joins and a second home for the same fact.
--       * Carrying it only in the channel grant — changes the grant's
--         ``binding_sig`` shape, and public / global spaces have no grant.
--       * Computing it at read time — the owner can (from its publication
--         rows), a member cannot: it holds none of those rows.
--       * Memory only — a member needs the list across restarts while the
--         host is offline, which is the whole point.
--   (3) Smallest possible change: one NULL-default ``ADD COLUMN`` with a
--       CHECK that keeps it a JSON object — the signature and ``issued_at``
--       live in the same value as the list they cover, not in extra
--       columns — metadata-only, no rewrite, no
--       backfill (the owner's fill happens in code, so members get it in a
--       sequence-raised config broadcast). Nothing existing is changed.

ALTER TABLE spaces ADD COLUMN gfs_servers TEXT
    CHECK (gfs_servers IS NULL
           OR (json_valid(gfs_servers) AND json_type(gfs_servers) = 'object'));

-- ``spaces.invited_by_instance`` (same migration, same change): on a MEMBER
-- household's stub, the delegated admin household that issued THIS
-- household's invite (a §D1b private invite or an invite-link redeem ACK)
-- when it is not the space's owner — recorded ONLY when the snapshot carried
-- the owner's signed statement (``spaces.owner_statement`` below) and the
-- admin's seed-holder proof. Owner decision 2026-10-10 ("trust the
-- inviting admin for sync"): while the space's ``delegated_admin_authority``
-- is ON and that household still holds a live ``admin`` seat, it is
-- host-equivalent here for CONTENT and admin actions (its full §25.6
-- catch-up stream incl. roster and bans, live bans, role changes, remote-member
-- removals, proposal mirrors, the signing-seed share, the host writer
-- exemption) — never for owner-only settings (the signed GFS list,
-- ``private_gfs``, ``gfs_publish_mode``, the authority cert /
-- ``host_identity_pk``, config, dissolve, age gate), which stay the owner's.
-- NULL = invited by the owner itself (or an owned space / older stub).
--
-- Audit for this column:
--   (1) Paths that touch the data: the two join paths
--       (``PrivateSpaceInviteHandler._on_invite``,
--       ``SpaceInviteTokenRedeemCoordinator._seat_local_after_ack``) write
--       it; ``socialhome/federation/host_equivalence.py`` reads it for every
--       host check listed above. The inviter is otherwise recorded only on
--       the invitation row (``space_invitations.remote_instance_id``: §D1b
--       invites only, not invite links, and a per-invitation row) and as a
--       ``space_instances`` row (every member household has one) whose
--       ``identity_pk`` (0078) is also written by host-side mesh claims
--       for ANY space we host — none of them says "this household seated
--       us into this space".
--   (2) Rejected: deriving it from ``space_instances.identity_pk`` (not
--       exclusive, see above), from the invitation rows (missing for invite
--       links, several per space), from the roster (an admin seat does not
--       say who invited us — any admin household would become a host),
--       memory-only (the trust must survive a restart while the owner is
--       offline, which is the case it exists for).
--   (3) One NULL-default TEXT column, no backfill: every existing stub keeps
--       today's behaviour (no inviting-admin trust).

ALTER TABLE spaces ADD COLUMN invited_by_instance TEXT;

-- ``spaces.invited_by_epoch`` (same change): the authority-key epoch the
-- inviting admin PROVED it holds the seed of (its inviter proof). The trust
-- holds only while the pinned epoch equals it: after a rotation it resumes
-- only on a fresh proof for the new epoch (an honest admin's §25.6 sentinel
-- carries one), which an admin revoked by that rotation cannot make. A moved
-- pin alone never resumes it. NULL when ``invited_by_instance`` is NULL —
-- the CHECK holds that direction (an epoch with no inviter is meaningless);
-- ``set_invited_by`` writes both together and NULLs the epoch with the
-- inviter, and ``set_invited_by_epoch`` writes only while an inviter is set.
--
-- Audit for this column:
--   (1) Paths that touch the data: ``set_invited_by`` (the join paths, and
--       the ``not_seated`` answer clearing the inviter), ``set_invited_by_epoch``
--       (``apply_inviter_reproof`` at a §25.6 sentinel), and
--       ``host_equivalence.is_trusted_inviter``, the only reader.
--   (2) Rejected: binding the trust to the pinned authority KEY instead
--       (``spaces.identity_public_key`` at seat time, or an inviter-proof
--       blob re-verified on every check) — the pin moves on a relayed cert
--       any sender may forward, so a key/epoch compare against the pin
--       alone cannot tell "the inviter proved this key" from "the pin moved
--       under it", and keeping the whole proof blob only to re-derive this
--       integer adds a JSON column for nothing; reusing ``authority_epoch``
--       on the inviter's seat row (``space_remote_members``) — that column
--       is the epoch a roster EVENT was verified under, rewritten by every
--       gossip upsert, not what the inviter proved.
--   (3) One NULL-default INTEGER column, no backfill (NULL = no trust,
--       today's behaviour for every existing stub).
ALTER TABLE spaces ADD COLUMN invited_by_epoch INTEGER
    CHECK (invited_by_epoch IS NULL OR invited_by_instance IS NOT NULL);

-- ``space_remote_members.removed_at`` (same migration): when a seat was
-- tombstoned (UTC, ``datetime('now')``). The owner's terminal ``removed``
-- sync reject carries the newest one, so a household re-invited by a
-- delegated admin after leaving — whose BEGIN reached the owner before the
-- re-join gossip — ignores a ``removed`` older than its own current join
-- instead of archiving. Audit: the tombstone row (0031) already exists for
-- exactly this lifetime; only its time was missing — no new table. NULL on
-- live seats and on tombstones written before 0093 (then ``removed`` is
-- honoured as before).
ALTER TABLE space_remote_members ADD COLUMN removed_at TEXT;

-- ``spaces.owner_statement`` (same migration): the owner household's signed
-- ownership statement for this space (``socialhome/space_owner_statement.py``)
-- as last verified here, a JSON object — kept so this household can relay it
-- in the snapshots it sends (an honest delegated admin's invite carries the
-- owner's statement). A joiner seats a stub under a claimed owner other than
-- the inviting household ONLY with a valid statement from that owner binding
-- the pinned authority key, plus the inviter's seed-holder proof; otherwise
-- it seats under the inviter as before 0093 (no ``invited_by_instance``).
-- NULL on owned spaces (the owner signs a fresh one into every config,
-- invite, rotation bundle and §25.6 sync sentinel it sends) and on stubs that
-- never got one. A statement counts for 30 days and only for the pinned key
-- and epoch. Audit: the v_44 authority cert is the nearest existing shape, but it
-- exists only from key epoch 1 (epoch 0 is TOFU, never certified, and the GFS
-- verifies certs with that rule), so it cannot name the owner of a space
-- that never rotated; nothing else on the row is signed by the owner. One
-- NULL-default column, written only by ``set_owner_statement``.

ALTER TABLE spaces ADD COLUMN owner_statement TEXT
    CHECK (owner_statement IS NULL
           OR (json_valid(owner_statement)
               AND json_type(owner_statement) = 'object'));

-- ``space_invitations.join_snapshot`` (same migration): on the INVITEE
-- household, the issuer's ``space_meta`` for a pending §D1b private invite,
-- as a JSON object ``{meta, host_identity_pk, received_at_ms,
-- owner_instance_id}`` (set back to NULL once the invitation is answered) — the space content key in
-- it KEK-wrapped (never plaintext at rest). Owner decision 2026-10-10: an
-- invitation never seats a stub until the user accepts it. Receipt stores
-- only this row; the accept seats the stub from THIS invitation's snapshot
-- (owner statement + inviter proof + cert checked then), so several pending
-- invitations for one space id from different households coexist and none
-- can block or alter another's. NULL on host-side rows, local invites, and
-- invitations received before 0093 (those were pre-seated as before, and
-- accept keeps that path).
--
-- Audit for this column:
--   (1) Paths that touch the data: ``PrivateSpaceInviteHandler._on_invite``
--       writes the row (``save_remote_invitation``);
--       ``SpaceService.accept_remote_invite`` reads it; decline only flips
--       the status. Before 0093 the snapshot went straight into
--       ``spaces`` / ``space_instances`` / ``space_remote_members`` /
--       ``space_keys`` / covers / icons at receipt — the pre-seated stub that
--       a stranger's invitation could plant ahead of the real owner's.
--   (2) Rejected: keeping the pre-seated stub and arbitrating between
--       claimants (the displacement rules this replaces — every rule left a
--       race between two pending invitations); a separate table (the
--       invitation row already is one-per-invitation, the snapshot's grain);
--       re-asking the issuer for the snapshot at accept time (the issuer may
--       be offline, and the accept would gain a round trip it does not have
--       today); memory only (an invitation waits for days and across
--       restarts).
--   (3) One NULL-default TEXT column with a JSON-object CHECK, no backfill.
ALTER TABLE space_invitations ADD COLUMN join_snapshot TEXT
    CHECK (join_snapshot IS NULL
           OR (json_valid(join_snapshot)
               AND json_type(join_snapshot) = 'object'));
