-- Invite links carry the role the redeemer lands in, and remember where
-- they were published.
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Code paths that already touch this data were read first. WRITERS of
--    ``space_invite_tokens``: ``SpaceService.create_invite_token`` (the
--    SPA mint), ``SpaceService.invite_remote_user`` (§D1b, mints its own
--    5-minute single-use token) and the §D2 join-request approval — the
--    latter two go straight to ``SqliteSpaceRepo.create_invite_token``
--    and keep today's implicit ``member``. READERS: the atomic
--    ``consume_invite_token`` (local ``accept_invite_token``, the §D2
--    ``_consume_seat_and_build_ack`` and the §D2b bootstrap redeem that
--    shares it) plus the new admin list. Every one of them treated the
--    seat as "member" with no way to say otherwise, so the role had no
--    existing home to reuse.
--
-- 2. Non-migration alternatives considered and rejected:
--    * Encode the role in the token STRING (``admin:<hex>``). Rejected —
--      the token is a bearer credential the redeemer holds and replays;
--      the seat it grants must never be attacker-controlled input.
--    * Ship the role in the redeem request. Same objection: the
--      redeemer would be asking for their own privilege. The issuer's
--      stored row is the only party allowed to decide.
--    * A ``space_invite_token_publications`` join table for the GFS
--      fields. Rejected — a token is published to at most ONE connection
--      server (the blob names the relay that reaches the issuer, so a
--      second copy elsewhere would be a different blob and therefore a
--      different token). One row, one server: columns, not a table.
--    * Deriving the minted use count for the admin list ("3 of 10
--      left") from ``uses_remaining``. Rejected — it is not derivable:
--      the counter is decremented in place, so the total is gone the
--      moment the first person redeems.
--
-- 3. Minimality — five additive ``ADD COLUMN``s, no backfill, no row
--    rewrite, no index. ``role`` takes a NOT NULL DEFAULT so every
--    existing token keeps meaning exactly what it means today; the CHECK
--    mirrors the ``space_members`` on-disk authority minus ``owner``,
--    which is never mintable (ownership moves only through
--    ``transfer_ownership``). The three ``gfs_*`` columns and
--    ``uses_total`` are NULL for every token that exists when this
--    migration runs — a NULL total simply means "unknown", and the API
--    falls back to the remaining count rather than inventing one.
ALTER TABLE space_invite_tokens
    ADD COLUMN role TEXT NOT NULL DEFAULT 'member'
    CHECK(role IN ('member', 'subscriber', 'admin'));
-- Where the link's blob is parked, and the handle needed to take it down
-- again. All three are set together or not at all.
ALTER TABLE space_invite_tokens ADD COLUMN gfs_id TEXT;
ALTER TABLE space_invite_tokens ADD COLUMN gfs_token TEXT;
ALTER TABLE space_invite_tokens ADD COLUMN gfs_url TEXT;
-- How many uses the link was MINTED with. ``uses_remaining`` is
-- decremented in place, so without this the admin list can only say
-- "3 left", never "3 of 10 left".
ALTER TABLE space_invite_tokens ADD COLUMN uses_total INTEGER;
