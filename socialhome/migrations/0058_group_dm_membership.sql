-- §23.47 cross-household group conversations (proto v_37).
--
-- A group conversation's member list is owned by the household that
-- created it (the "authority" — named by the conversation id itself, an
-- owner-bound id, so no column records it). The authority ships the whole
-- member list as a versioned ``DM_GROUP_ROSTER`` snapshot to every member
-- household; a receiver applies a snapshot only when its version is newer
-- than the one it holds, so reordered or replayed rosters can never roll
-- the membership back. That monotonic version has to survive a restart,
-- and no existing column carries it.
--
-- A member household may never have paired with another member household
-- (both only know the authority). It then holds no ``remote_users`` row
-- for that household's people, so the seat itself has to say who sits in
-- it: ``user_id`` binds a message's ``sender_user_id`` to the seat (and so
-- to the household that must sign it), ``display_name`` is what the
-- thread shows for them. Both come from the authority's roster; both stay
-- NULL for every 1:1 seat and every seat written before this migration,
-- where ``remote_users`` keeps answering as before.
--
-- Additive and defaulted: every existing conversation starts at version
-- 0, every existing seat keeps its current meaning.
ALTER TABLE conversations ADD COLUMN membership_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE conversation_remote_members ADD COLUMN user_id TEXT;
ALTER TABLE conversation_remote_members ADD COLUMN display_name TEXT;
