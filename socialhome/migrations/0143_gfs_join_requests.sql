-- 0143 — "Ask to join" a global space through the GFS (v_84,
-- ``socialhome/federation/gfs_join_request.py``). Three NULL-defaulted
-- columns, nothing rewritten, no backfill:
--
-- * ``public_space_cache.join_contact`` — the host's contact keys a GFS
--   directory listing carried (identity + key-wrap key, signed by the
--   SPACE AUTHORITY key), as the applicant verified it, plus the authority
--   key it pinned on first sight and the GFS it pinned it from (JSON). A
--   listing that later shows another authority key for the space is
--   refused (``conflict``), so no GFS can swap the keys a request is sealed
--   to without breaking the pinned signature.
-- * ``space_join_requests.gfs_relay`` — for a request that crossed the GFS:
--   on the HOST, the applicant household's identity + key-wrap keys, the
--   GFS it came through and the names it sent ("not verified"), so the
--   admins' answer — maybe days later, after a restart — goes back sealed;
--   on the APPLICANT, the approval (single-use token + the host keys + its
--   expiry) until the redeem seated the person, so a failed redeem is
--   retried on the next start / reconnect instead of being lost.
-- * ``space_invite_tokens.bound_instance_id`` / ``bound_user_id`` — the
--   token an approval mints is redeemable by the approved household and
--   person only (an invite LINK stays a bearer token: both NULL).
--
-- CLAUDE.md "audit before a migration":
--
-- 1. Audited: the paired request answers over the pairing (no keys
--    needed); the §D2b redeem seats a ``space_session`` row only after a
--    token proved membership; ``remote_applicant_pk`` is the person's key;
--    ``public_space_cache`` keeps only the directory's display fields;
--    ``space_invite_tokens`` has no notion of who may redeem.
-- 2. Rejected: a ``space_session`` row at request time (opens the relay's
--    inbound allow-list to a non-member); memory for the approval or the
--    host's answer contact (lost on restart — the reviewed bug); asking the
--    GFS for keys by id (a "who wants what" record on the GFS); a separate
--    pin table (the cache row is the one place a listing lives).
-- 3. Smallest: four nullable TEXT columns, no index (each is read through
--    its table's primary key).
ALTER TABLE public_space_cache ADD COLUMN join_contact TEXT;
ALTER TABLE space_join_requests ADD COLUMN gfs_relay TEXT;
ALTER TABLE space_invite_tokens ADD COLUMN bound_instance_id TEXT;
ALTER TABLE space_invite_tokens ADD COLUMN bound_user_id TEXT;
