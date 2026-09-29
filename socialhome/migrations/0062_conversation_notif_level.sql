-- §23.42 per-member notification level for a group conversation — local only.
--
-- In a busy group chat a member can choose to be rung only when a message
-- @-mentions them. ``all`` (default) keeps today's behaviour: every message
-- rings. ``mentions`` rings only for a message that mentions them (a
-- ``dm_mention`` bell). The #755 mute (``muted_until``) stays a separate,
-- time-boxed column and wins over either level while it lasts; when it runs
-- out the member's level applies again.
--
-- Like the mute, the level belongs to one (conversation, local user) pair,
-- which is exactly a ``conversation_members`` row: it cascades away with the
-- conversation and the user, and ``add_member``'s upsert / the group roster
-- apply leave it alone. Never federated.
--
-- Additive: one column with a constant default (metadata-only in SQLite —
-- no table rewrite, no backfill) and a CHECK so no other value can land.
ALTER TABLE conversation_members
    ADD COLUMN notif_level TEXT NOT NULL DEFAULT 'all'
    CHECK (notif_level IN ('all', 'mentions'));
