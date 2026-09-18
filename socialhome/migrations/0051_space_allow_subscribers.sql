-- Readability is its own switch: ``spaces.allow_subscribers``.
--
-- A space carries a ``space_type`` (private / household / public / global)
-- and a ``join_mode`` (invite_only / open / request). Neither says whether
-- STRANGERS may follow the space read-only, and until now the code inferred
-- that from ``join_mode`` — the wrong model. ``join_mode`` governs how
-- someone becomes a MEMBER who can post; readability is an explicit admin
-- opt-in, the same shape as the ``allow_subscriber_comment`` /
-- ``allow_subscriber_react`` flags it sits beside on ``SpaceFeatures``.
--
-- Two columns, same flag, two tables:
--
-- * ``spaces.allow_subscribers`` — the owner's switch for a space THIS
--   household hosts (and the mirrored value on a stub of a remote one). It
--   gates the three producer seams (``space_public_outbound``,
--   ``space_post_outbound``, ``space_subscriber_key_outbound``) and
--   ``SpaceService.subscribe_to_space``.
-- * ``public_space_cache.allow_subscribers`` — the same flag as reported by
--   a GFS directory poll, so ``GET /api/public_spaces`` can tell the browser
--   whether to offer Subscribe BEFORE any local row for the space exists.
--
-- FAIL-CLOSED DEFAULT, deliberately: every pre-existing public/global space
-- reads as ``allow_subscribers = 0`` and STOPS being publicly readable until
-- its owner opts in. That mirrors how ``allow_subscriber_comment`` already
-- defaults (0001_initial.sql:589) and how the sibling GFS column behaves; a
-- default of 1 would silently keep publishing content under a model the
-- owner never agreed to. The cache column self-heals on the next directory
-- poll (minutes); the GFS side self-heals on the owner's next WS reconnect
-- (``GfsConnectionService.heal_space_pins`` re-publishes every space).
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Every code path that already touches this data was read. ``spaces`` is
--    written by ``SqliteSpaceRepo.save`` (the single writer — config edits,
--    creation, and §D1b remote stubs all funnel through it) and read back
--    through ``SpaceFeatures.from_row``; the consumers of the old join-mode
--    gate are ``space_public_outbound`` (2 seams), ``space_post_outbound``
--    (the ``public_relay`` hint), ``space_subscriber_key_outbound`` (push +
--    reconcile) and ``SpaceService.subscribe_to_space``. ``public_space_cache``
--    has exactly one writer (``PublicSpaceDiscoveryService._parse_listings``
--    → ``SqlitePublicSpaceRepo.upsert``) and three readers
--    (``routes/public_spaces.py``, ``GfsSpaceMirrorService.was_gfs_listed``,
--    the maintenance purge) — the same set migration 0050 audited for
--    ``join_mode``. Nothing in either table encoded readership.
-- 2. Non-migration alternatives considered and rejected:
--    (a) keep deriving it from ``join_mode`` — that IS the bug: the product
--        model says the two dials are independent, and conflating them makes
--        ``invite_only`` + broadcast unexpressible;
--    (b) derive it from ``space_type`` — the tier says where a space may be
--        listed, not who may read it, and every global space would be
--        readable again;
--    (c) carry it only in the federation config event / the GFS publish body
--        without storing it — the value is consulted on unrelated later
--        requests (every post relay, every subscriber-key handoff, every
--        ``POST /api/spaces/{id}/subscribe``), so it must live with the row;
--    (d) reuse ``allow_subscriber_comment`` as a proxy — that flag means
--        "subscribers may comment", a strictly narrower statement, and
--        reusing it would force a space to grant comment rights in order to
--        be readable at all.
-- 3. Smallest possible change: two additive ``ADD COLUMN``s with a NOT NULL
--    default and the same ``CHECK(... IN (0,1))`` its siblings carry. No
--    backfill, no table rebuild, no index, no existing row rewritten — the
--    default supplies every historical row's value.

ALTER TABLE spaces
    ADD COLUMN allow_subscribers INTEGER NOT NULL DEFAULT 0
               CHECK(allow_subscribers IN (0,1));

ALTER TABLE public_space_cache
    ADD COLUMN allow_subscribers INTEGER NOT NULL DEFAULT 0
               CHECK(allow_subscribers IN (0,1));
