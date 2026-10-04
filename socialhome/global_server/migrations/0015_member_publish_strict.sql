-- 0015 — strict-mode member publish (v_50): the space owner's publish mode
-- and the pinned writer group keys.
--
-- ``POST /gfs/member-publish-anon`` lets a member household publish without
-- naming itself: it signs with the space's per-epoch writer GROUP key, which
-- the space authority pins here with a ``writer_key_cert``
-- (``socialhome/global_server/member_publish.py``). And for a space whose
-- owner chose strict mode, this server must REFUSE identified publishes. Two
-- facts need a home: which mode the owner chose, and the writer public keys
-- of the current and previous content epoch.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every path that touches this data. The mode is the owner's
--       space setting (``SpaceFeatures.gfs_publish_mode``); the GFS sees it
--       only on the owner's household-signed epoch notice — owner-only (a
--       delegated admin's notice is authority-signed and cannot carry it),
--       never served publicly, and already sent on every rotation, every
--       reconnect and now on every mode change.
--       The writer key is a new statement: nothing on this server stores
--       one. ``global_spaces.content_epoch*`` (0014) holds the epoch tiers the
--       pins are bound to; ``identity_public_key`` / ``authority_cert`` (0004,
--       0013) the authority key that signs them.
--   (2) Alternatives considered and rejected. Keeping it in memory: an owner
--       sends its notice once per rotation and on reconnect, so a restart
--       would reopen identified publishing into a strict space (and refuse
--       every anonymous publish) until the owner reconnects. A new table for
--       the keys: at most two keys per space (current + previous, for the
--       same 600 s grace the epoch tiers give), so columns on the row they
--       describe are smaller and die with it, like 0014's. Putting the mode
--       on the owner-signed listing (``GlobalSpace``): it would be served on
--       the public directory for nobody's benefit, and the listing columns are
--       rewritten on every republish. Deriving the
--       mode from "a writer key is pinned": a key is pinned in every v_50
--       space, so presence says nothing about the owner's choice.
--   (3) Smallest possible change: additive ``ADD COLUMN``s. The mode defaults
--       to ``'trusted'`` — exactly today's behaviour for every existing row,
--       so nothing is rewritten — and a CHECK pins its two values. Every key
--       column is NULL-defaulted (NULL = no writer key pinned yet).
--
-- The writer-key columns are cleared with the epoch columns whenever the
-- space authority key is re-pinned (a key cert signed by the old authority
-- stops meaning anything). The mode is NOT cleared: it is the owner's
-- household-signed statement, not an authority one. Neither is served on the
-- public directory.

-- The owner's choice, and the (unix seconds) ``ts`` of the owner notice that
-- set it — a notice older than the stored one never moves the mode back.
ALTER TABLE global_spaces ADD COLUMN member_publish_mode TEXT NOT NULL
    DEFAULT 'trusted' CHECK (member_publish_mode IN ('trusted', 'strict'));
ALTER TABLE global_spaces ADD COLUMN member_publish_mode_at INTEGER;

-- The pinned writer public key (b64url Ed25519) of the newest content epoch
-- a writer key was pinned for, and of the one before it.
ALTER TABLE global_spaces ADD COLUMN writer_key_epoch INTEGER;
ALTER TABLE global_spaces ADD COLUMN writer_key_pk TEXT;
ALTER TABLE global_spaces ADD COLUMN writer_key_prev_epoch INTEGER;
ALTER TABLE global_spaces ADD COLUMN writer_key_prev_pk TEXT;
