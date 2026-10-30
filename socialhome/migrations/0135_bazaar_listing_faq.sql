-- 0135 — a listing's public questions and answers (the seller's FAQ, v_75).
--
-- The seller of a bazaar listing can publish up to 10 short questions with
-- their answers on the listing ("Questions and answers"), often taken from
-- an "Ask the seller" thread (0134) and reworded. Everyone who can see the
-- listing sees them; they reach every member household sealed inside
-- ``BAZAAR_LISTING_FAQ_UPDATED`` (one entry per event, a delta) and a late
-- joiner gets them with the listing over the §25.6 space sync.
--
--   listing_post_id  The listing (``bazaar_listings.post_id``). The FAQ
--                    goes with the listing (``ON DELETE CASCADE``): a
--                    deleted listing, a purged space or a household leaving
--                    the space drops its FAQ with it.
--   entry_id         32 hex, minted by the seller's household; the same on
--                    every household.
--   question         ≤ 200 characters, one line, plain text. Never names
--                    the person who asked. '' on a removed entry.
--   answer           ≤ 500 characters, plain text. '' on a removed entry.
--   position         Display order (ascending), set by the seller's
--                    household when the entry is added.
--   rev              The seller's household's revision of THIS listing's
--                    FAQ when the entry last changed (one counter per
--                    listing). A receiver applies a change only when its
--                    ``rev`` is higher than the one held, so a late or
--                    replayed envelope never undoes a newer edit.
--   removed          1 = the seller removed the entry: the row stays, text
--                    blanked, as a marker so a late edit of it can never
--                    bring it back. At most 20 markers per listing are kept
--                    (the oldest go first); at most 10 live entries.
--   updated_at       Local bookkeeping (when this household stored it).
--
-- The touch triggers re-stamp the wrapper post (0086), so an FAQ change
-- reaches a household by the incremental space sync like any change to the
-- listing itself.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches the listing's data.
--       ``bazaar_listings`` (0001) is written by ``SqliteBazaarRepo``
--       (``save_listing`` / ``insert_listing_once`` / ``mark_*``), the
--       inbound ``BAZAAR_LISTING_*`` handlers and the §25.6 ``bazaar``
--       receiver; read by the bazaar routes, the sync exporter, the
--       "Ask the seller" threads (0134), the data export
--       (``bazaar_listings`` by seller) and the backup. Its touch triggers
--       (0086) compare every column, so a new column there would need the
--       trigger rebuilt as well. No existing column holds free text but
--       ``title`` / ``description``, which are the listing's own words and
--       the wrapper post's caption.
--   (2) Non-migration alternatives considered and rejected:
--       * Append the questions to ``description`` — it is the wrapper
--         post's caption (moderated / edited as a post), has no entries to
--         edit or remove one by one, and an edit could not be told apart
--         from the seller's text; a receiver could not bound it per entry.
--       * A JSON column on ``bazaar_listings`` (``faq_json``) — one
--         read-modify-write blob per edit: two quick edits race, and a
--         receiver could not apply one entry's change newer-wins without
--         rewriting the whole list; it also needs the 0086 touch trigger
--         rebuilt (``DROP`` + ``CREATE``) to compare the new column.
--       * Keep it in the federation event only — every household must
--         hold the FAQ to show it, and a late joiner needs it from sync.
--       * Reuse the thread's messages (0134) — those are private between
--         one buyer and the seller; the FAQ is public and the seller's
--         own (reworded) words, never the asker's.
--   (3) Smallest possible change. One new table with no change to any
--       existing row or table, one index-free primary key (every read is
--       by listing: the key's prefix), and the three touch triggers that
--       keep the incremental sync honest. Nothing is backfilled.
CREATE TABLE IF NOT EXISTS bazaar_listing_faq (
    listing_post_id TEXT NOT NULL
                    REFERENCES bazaar_listings(post_id) ON DELETE CASCADE,
    entry_id        TEXT NOT NULL,
    question        TEXT NOT NULL DEFAULT '',
    answer          TEXT NOT NULL DEFAULT '',
    position        INTEGER NOT NULL DEFAULT 0,
    rev             INTEGER NOT NULL CHECK (rev > 0),
    removed         INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (listing_post_id, entry_id)
);

CREATE TRIGGER bazaar_listing_faq_touch_post_insert
AFTER INSERT ON bazaar_listing_faq
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.listing_post_id;
END;
CREATE TRIGGER bazaar_listing_faq_touch_post_update
AFTER UPDATE ON bazaar_listing_faq
WHEN (NEW.listing_post_id, NEW.entry_id, NEW.question, NEW.answer, NEW.position, NEW.rev, NEW.removed)
 IS NOT (OLD.listing_post_id, OLD.entry_id, OLD.question, OLD.answer, OLD.position, OLD.rev, OLD.removed)
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.listing_post_id;
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.listing_post_id AND OLD.listing_post_id IS NOT NEW.listing_post_id;
END;
CREATE TRIGGER bazaar_listing_faq_touch_post_delete
AFTER DELETE ON bazaar_listing_faq
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.listing_post_id;
END;
