-- GFS 0033 — the space-keyed relay's state goes (v_96, subscriber seats P7).
-- Follows 0032.
--
-- Since v_96 a space's content reaches a GFS only through its opaque
-- channel (``/gfs/channels/*``) — apart from the sealed household-to-
-- household envelope relay, which never names a space: ``POST /gfs/publish``, ``POST
-- /gfs/subscribe``, ``GET /gfs/spaces/{id}/subscribers``, ``POST
-- /gfs/spaces/{id}/key_request``, ``POST /gfs/member-publish-anon`` and
-- ``POST /gfs/spaces/{id}/epoch`` are gone, and so is the ``space`` scope of
-- the space media cache (owner decision Q5). This drops what only they kept.
--
-- Audit (CLAUDE.md "Before adding a SQL migration"):
--
-- 1. Code paths that touched this data, all removed in the same change:
--    ``space_subscribers`` (0001; ``reads_space_item`` from 0023) —
--    ``repositories`` ``add_subscriber`` / ``remove_subscriber`` /
--    ``purge_subscribers`` / ``list_subscribers`` / ``get_subscriber`` /
--    ``list_subscribed_spaces`` / ``set_seat_accepts`` /
--    ``count_subscribers_without_*`` / ``list_recently_seen_subscribers`` /
--    ``list_subscribers_with_keys``; ``federation`` (subscribe, publish fan-
--    out, owner notices, the 5b-c reconcile read, the publish-time purge);
--    ``cluster_relay`` (the ``space`` audience); ``cluster`` (forwarded
--    ``new_subscriber`` notices); ``member_publish``; ``routes/ws`` (the
--    hello's key-repair pass). Space-keyed queue rows
--    (``gfs_envelope_queue.frame_type = 'relay'`` with a ``space_id`` and an
--    ``event_type`` of the four space-keyed kinds) were written by the
--    targeted ``/gfs/publish`` and the member-publish fan-out only; channel
--    frames carry ``channel_id`` and no ``space_id``, moment frames neither.
--    ``scope_kind = 'space'`` rows of ``gfs_space_media`` /
--    ``gfs_space_media_demand`` / ``gfs_space_media_uploads`` were written
--    only by uploads and ``wanted`` requests the codec now refuses
--    (``domain/gfs_space_media.MEDIA_SCOPES`` = ``{"channel"}``).
-- 2. Non-migration alternative rejected: leaving them inert would keep an
--    identity-bound follow graph by ``space_id`` at rest on every GFS (who
--    follows which space) — the very data v_96 removes — plus queued frames
--    no household takes and blobs no reader can address. ``global_spaces``
--    ``subscriber_count`` and the member-publish columns (0014 / 0015) are
--    NOT dropped: they hold no identity, and a column drop rewrites the
--    table; they stay inert.
-- 3. Smallest change: one DROP TABLE (its two indexes go with it) and
--    targeted DELETEs. The media triggers (0025) keep the totals and the
--    per-scope / per-owner byte counters right; the files of the deleted
--    rows are orphans the maintenance sweep removes (``sweep_orphans``).
-- Rolling upgrade (cluster nodes share one database): the first upgraded
-- node drops the table while an older node may still serve the legacy
-- routes. Households upgrade first (v_96 sends nothing space-keyed), but an
-- older node still reads the table on a legacy household's
-- ``/gfs/subscribe`` / ``/gfs/publish``, on a listing publish with
-- ``allow_subscribers: false`` (its subscriber purge — a 500 after the
-- listing row was upserted) and in its fail-soft hello key-repair pass —
-- so stop every old alloc before the first new one starts. (An old node
-- writing meanwhile leaves space-keyed queue / media rows that expire by
-- TTL; households drop them.) Accepted: keeping the table for one more release would keep the
-- follow graph at rest that this migration exists to remove.
--
-- ``json_extract`` raises on malformed JSON and SQLite does not promise to
-- evaluate ``json_valid`` first in an AND, so every extract sits behind a
-- CASE (a non-JSON row is kept, never an error).
DELETE FROM gfs_envelope_queue
 WHERE frame_type = 'relay'
   AND CASE WHEN json_valid(sealed_json)
            THEN json_extract(sealed_json, '$.space_id') END IS NOT NULL
   AND CASE WHEN json_valid(sealed_json)
            THEN json_extract(sealed_json, '$.event_type') END IN (
       'space_post_public',
       'space_subscriber_key_handoff',
       'space_author_profiles_bundle',
       'space_item'
   );

DROP TABLE IF EXISTS space_subscribers;

DELETE FROM gfs_space_media WHERE scope_kind = 'space';
DELETE FROM gfs_space_media_demand WHERE scope_kind = 'space';
DELETE FROM gfs_space_media_uploads WHERE scope_kind = 'space';

-- Belt and braces: the 0025 delete trigger removes a counter row once its
-- bytes reach zero; any drift must not leave a space id or a household id
-- (the ``household`` fairness owner of a space blob) at rest.
DELETE FROM gfs_space_media_scope_bytes WHERE scope_kind = 'space';
DELETE FROM gfs_space_media_owner_bytes WHERE owner_kind = 'household';

-- ``space_instance_bans(space_id, instance_id)`` (0001) has had no reader or
-- writer on the GFS (``global_server/*.py``) for many releases — an
-- identity-bound table keyed by space id, the shape this migration removes.
-- (The household's own ``space_instance_bans`` is another database.)
DROP TABLE IF EXISTS space_instance_bans;
