-- 0108 — no longer changes anything (kept so the numbering stays contiguous).
-- Follows 0107 (pairing share_home).
--
-- Its first, never-released revision added ``peer_space_directory.area_edge``
-- — the host's "unsure" answer near a local community's rim, part of a
-- per-household verdict the host computed from the home a paired household
-- shared with it. That verdict is gone (owner decision 2026-10-19): a join
-- now carries the joiner's home rounded to ~1 km and the host decides then,
-- so the directory carries only the radius. A development database that ran
-- the first revision has the column; 0110 drops it there.

SELECT 1;
