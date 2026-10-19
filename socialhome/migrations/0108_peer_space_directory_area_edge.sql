-- 0108 — the paired-household directory keeps the host's "unsure" answer.
-- Follows 0107 (pairing share_home) — merge after it.
--
-- A local community's host tells each paired household whether the home it
-- shares lies inside the area (``in_area``, 0105). Answered exactly, that
-- verdict lets a household that moves its shared home step by step find the
-- rim and so the centre — somebody's street. Near the rim the host now says
-- neither yes nor no: ``in_area: null`` with ``area_edge: true`` in its
-- ``SPACE_DIRECTORY_SYNC`` snapshot (v_59, unreleased, redefined in place).
-- Discover and the About page show "Near the edge — ask to join and the
-- host will check" for it, which "unknown" (``in_area`` NULL: the host does
-- not know our home) cannot say.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the directory and the verdict:
--       ``PeerDirectoryService`` (sender, one snapshot per peer),
--       ``PeerDirectoryHandler._on_snapshot`` +
--       ``parse_local_community_wire`` (receiver),
--       ``SqlitePeerSpaceDirectoryRepo.replace_snapshot`` / ``_row`` (the
--       only SQL on the table), ``GET /api/peer_spaces`` (the only reader),
--       unpair cleanup (``clear_instance``).
--   (2) Alternatives rejected: a third ``in_area`` value — its CHECK (0105)
--       allows only 0 / 1, and widening it means rebuilding the table; NULL
--       alone — it already means "the host does not know our home", a
--       different next step ("share your home"); deriving "edge" on the
--       receiver from our own sharing state — a host that has not yet seen
--       our home would read as "edge"; not storing it — the directory is a
--       cache the SPA reads, refilled only on the host's next snapshot.
--   (3) Smallest change: one ``ADD COLUMN`` with a constant default, no
--       backfill, no rewrite, no index. Its CHECK keeps it unambiguous: set
--       only on a local community's row whose ``in_area`` is NULL.

ALTER TABLE peer_space_directory ADD COLUMN area_edge INTEGER NOT NULL DEFAULT 0
    CHECK (
        area_edge IN (0, 1)
        AND (area_edge = 0 OR (in_area IS NULL AND radius_km IS NOT NULL))
    );
