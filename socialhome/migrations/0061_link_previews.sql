-- Link previews — the card a text post shows for its first web link.
--
-- The author's household fetches the page once when the post is created
-- (services/link_preview_service.py, behind the outbound_fetch SSRF guard)
-- and the preview then travels inside the post, so receiving households
-- never fetch the URL. The preview is part of the post the way its
-- location pin or file metadata is, so it is stored on the post row as a
-- small JSON object {url, title, description, site_name, thumbnail_url}
-- (thumbnail_url is a local ``api/media/<name>`` reference), exactly like
-- the existing ``location_json`` / ``file_meta_json`` columns.
--
-- Audit / alternatives considered:
--   * No existing column fits: ``file_meta_json`` is the FILE-post
--     attachment and ``location_json`` the LOCATION pin; overloading either
--     would change what every reader of those columns sees.
--   * Computing at read time is exactly what the feature must not do — it
--     would fetch the URL on every household that shows the post.
--   * A separate URL-keyed table would need its own lifecycle (delete with
--     the post, sync, backup) that the post row already has.
--   * The household on/off switch is one more boolean on the existing
--     household ``preferences`` row, next to the other feed toggles.
--
-- Smallest change: three additive columns. The two post columns are NULL
-- by default (every existing post simply has no preview); the preference
-- defaults ON, the owner's intended default. No backfill, no rewrite.
ALTER TABLE feed_posts ADD COLUMN link_preview_json TEXT;
ALTER TABLE space_posts ADD COLUMN link_preview_json TEXT;
ALTER TABLE preferences ADD COLUMN allow_link_preview INTEGER NOT NULL DEFAULT 1
    CHECK(allow_link_preview IN (0, 1));
