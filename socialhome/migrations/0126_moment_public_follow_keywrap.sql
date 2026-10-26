-- 0126 — a public-moment follow remembers the author household's key-wrap
-- key and its KEM suite (v_68, reactions reach authors you follow through a GFS; owner
-- decision 9, sign-off 1, 2026-10-25). Follows 0125.
--
-- A reaction to a public moment that came through a GFS is sealed to the
-- author household's static X25519 key-wrap key, which the author's
-- household now names inside every sealed create body
-- (``author_keywrap_pk`` + ``author_keywrap_suite``, under the envelope
-- signature). A reaction can come days later and after a restart, so the key
-- has to be on disk — with its suite, like every crypto wire shape
-- (CLAUDE.md "Crypto wire shapes carry a ``*_suite`` tag").
--
--   followed_keywrap_pk     hex key (64 chars for X25519) — NULL until a
--                           verified envelope of the author carried one (an
--                           older author household ships none; its moments
--                           keep their reactions at home, as before).
--   followed_keywrap_suite  the KEM suite the author named the key under
--                           (``author_keywrap_suite``, today ``x25519``);
--                           NULL exactly when the key is. The follower
--                           seals a reaction only under a suite it
--                           supports — never under an assumed default — so a
--                           later PQ-hybrid key (``x25519+mlkem768``, longer
--                           hex, room left by the key's CHECK) can never be
--                           misread as an X25519 key (#1179 review).
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the data. The key arrives only in
--       ``MomentPublicInbound`` (a create whose signature verified against
--       ``followed_instance_pk`` and that opened under the author's key);
--       it is read only by ``MomentReactionGfs`` (follower side of
--       ``MomentReactionRouter``). The follow table's writers —
--       ``SqliteMomentPublicFollowRepo.upsert`` (its ``ON CONFLICT`` update
--       names its columns, so a re-follow keeps the key), ``set_profile``,
--       ``delete`` (unfollow, protected account, GFS unpair cascade) — and
--       the 0124 moment-save triggers on follow deletes are unaffected; the
--       backup / export / recovery dumps copy the table whole. The moment
--       rows themselves (``moments``) are untouched: GFS rows keep no route.
--   (2) Alternatives rejected:
--       * ``moments.reaction_route`` (0125) — its CHECK admits the column on
--         ``received_via = 'household'`` rows only, and SQLite can't alter a
--         CHECK: admitting GFS rows means rebuilding ``moments`` (the
--         largest moment table, with its triggers, indexes and FKs) — a
--         destructive migration for a value that is per author household,
--         not per moment;
--       * a new ``moments`` column — one copy of the same 32 bytes per
--         moment, plus a tombstone trigger, where the follow row already
--         caches the very same household's identity key
--         (``followed_instance_pk``) and profile (0112);
--       * ``remote_instances.remote_keywrap_pk`` — paired households only;
--         a household met through a GFS never is one;
--       * asking the GFS for the key when reacting — a round trip per
--         reaction and a GFS-served value (bound only by a signature the
--         follower would then have to check), where the author's own
--         signed envelope already brings it;
--       * memory — a reaction can come days later, across a restart.
--       * deriving the suite from the key's length — the very "unlabelled
--         crypto material" CLAUDE.md forbids; a suite tag costs a few bytes.
--   (3) Smallest change: two NULL-defaulted ``ADD COLUMN``s, no backfill
--       (the author's next moment fills them), no index (read with the row
--       ``followers_of`` already selects). The CHECKs keep the key a
--       lowercase hex key of a sane length (64 for X25519; room for a PQ
--       hybrid key, ``docs/crypto.md`` Phase 2), the suite a short tag, and
--       the two set or unset together. The receiver checks the suite
--       against its supported set; the CHECK does not pin the list, so a new
--       suite needs no migration.
ALTER TABLE moment_public_follows ADD COLUMN followed_keywrap_pk TEXT NULL
    CHECK (
        followed_keywrap_pk IS NULL
        OR (
            length(followed_keywrap_pk) BETWEEN 64 AND 4096
            AND length(followed_keywrap_pk) % 2 = 0
            AND followed_keywrap_pk NOT GLOB '*[^0-9a-f]*'
        )
    );
ALTER TABLE moment_public_follows ADD COLUMN followed_keywrap_suite TEXT NULL
    CHECK (
        (followed_keywrap_suite IS NULL) = (followed_keywrap_pk IS NULL)
        AND (
            followed_keywrap_suite IS NULL
            OR length(followed_keywrap_suite) BETWEEN 1 AND 32
        )
    );
