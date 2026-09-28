-- §23.47 group conversations: a leave can't be undone by a stale roster.
--
-- A member household that leaves a group steps out locally and tells the
-- authority (``DM_GROUP_LEAVE``). A roster the authority built *before* it
-- saw that leave can still arrive afterwards, newer than anything held
-- here and still listing the leaver — and used to seat them again.
--
-- Two facts settle it, and neither is stored today:
--
-- * ``joined_version`` — the membership version at which a member was
--   last seated. The authority ships it per roster entry (``since``), so a
--   receiver can tell "still listed from before" from "added again".
--   Stamped on every seat (local and remote) when a snapshot seats it.
-- * ``left_version`` — on the leaving household, the version it held when
--   its user left. A roster entry for that user whose ``since`` is not
--   newer is stale: the user stays out and the leave is sent again.
--
-- The roster version itself (0058) can't carry either: it moves with every
-- change, and the leave is exactly the change the authority hasn't seen.
-- Additive and NULL-defaulted: existing seats ship no ``since`` (receivers
-- keep today's behaviour for them), existing left rows carry no marker.
ALTER TABLE conversation_members ADD COLUMN joined_version INTEGER;
ALTER TABLE conversation_members ADD COLUMN left_version INTEGER;
ALTER TABLE conversation_remote_members ADD COLUMN joined_version INTEGER;
