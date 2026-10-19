-- 0107 — pairing honours the household's home-sharing choice.
-- Follows 0106 (peer space directory drops the centre).
--
-- Home sharing with a paired household is a per-pair switch
-- (``remote_instances.share_home``, 0006), on by default for a new pair
-- (owner decision 2026-10-19). Until now the pairing code (QR) and the
-- scanner's ``PAIRING_PEER_ACCEPT`` carried this household's home location
-- without consulting any switch: at that point no pair row exists yet.
-- Two additive columns let every sender honour the choice instead:
--
-- * ``preferences.share_home_default`` — the household admin's "share our
--   home location with households we pair with" setting (default on). It
--   pre-sets the pairing dialog's switch and is the switch of a pair made
--   without one (the auto-pair trust relay).
-- * ``pending_pairings.share_home`` — the choice the code owner made when
--   the code was minted. The code owner's pair row is created only when the
--   scanner's peer-accept arrives, so the choice must outlive the request
--   that showed the code. ``0`` for a session minted before this migration
--   (fail-closed; sessions expire after 5 minutes anyway).
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that sends or stores our home location:
--       ``PairingCoordinator.initiate`` (QR), ``.accept`` (peer-accept body),
--       ``.handle_peer_accept`` / ``.confirm`` / ``.handle_peer_confirm``
--       (row creation + promotion, which keeps ``share_home``),
--       ``AutoPairCoordinator`` (provisional + approved rows),
--       ``FederationService._on_local_home_location_updated`` (fan-out,
--       already gated on ``share_home``), ``PeerHomeSharingService``
--       (toggle + one-shot envelope), ``InviteTokenRedeem`` (space-session
--       rows, ``share_home = 0``). Nothing else puts
--       ``instance_identity.home_lat`` / ``home_lon`` on a wire.
--   (2) Alternatives rejected: the ``instance_config`` key/value table for
--       the household default would need its own endpoint and settings
--       surface beside the household preferences every other household
--       switch lives in; the SPA re-sending the code owner's choice at SAS
--       confirm would lose it on a reload or for an API client and fall
--       back to sharing; a per-token ``instance_config`` key leaves orphans
--       when a session expires; ``intro_note`` already means something else.
--   (3) Smallest change: two ``ADD COLUMN`` with constant defaults — no
--       backfill, no rewrite, no new table or index.

ALTER TABLE preferences ADD COLUMN share_home_default INTEGER NOT NULL DEFAULT 1
    CHECK(share_home_default IN (0, 1));
ALTER TABLE pending_pairings ADD COLUMN share_home INTEGER NOT NULL DEFAULT 0
    CHECK(share_home IN (0, 1));
