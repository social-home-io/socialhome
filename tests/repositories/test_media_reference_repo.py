"""Tests for the media-reference live-set query (orphan-sweep safety net)."""

import pytest

from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo

pytestmark = pytest.mark.asyncio


async def test_empty_db_returns_empty_set(db):
    """The query must run against every source table without error."""
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.referenced_basenames() == set()


async def test_collects_basenames_from_every_source(db):
    # Parents to satisfy FKs.
    await db.enqueue(
        "INSERT INTO users(user_id, display_name) VALUES('u1', 'U1')",
    )
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('c1', 'dm')")
    await db.enqueue("INSERT INTO gallery_albums(id, name) VALUES('al1', 'A')")
    await db.enqueue(
        "INSERT INTO highlights(id, author_user_id, highlight_date, expires_at) "
        "VALUES('h1', 'u1', '2026-01-01', '2099-01-01T00:00:00+00:00')",
    )

    # One media-bearing row per source (URLs in the stored api/media/ form).
    await db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id, "
        "media_url) VALUES('m1', 'c1', 'u1', 'api/media/dm.webp')",
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, media_url) "
        "VALUES('fp1', 'u1', 'image', '/api/media/feedsingle.webp?v=2')",
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, image_urls_json) "
        "VALUES('fp2', 'u1', 'image', "
        '\'["api/media/feed1.webp", "api/media/feed2.webp"]\')',
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, "
        "filename, thumbnail_filename, width, height) "
        "VALUES('gi1', 'al1', 'u1', 'photo', 'gal.webp', 'galt.webp', 1, 1)",
    )
    await db.enqueue(
        "INSERT INTO highlight_frames(id, highlight_id, sequence, frame_type, "
        "media_url) VALUES('hf1', 'h1', 1, 'image', 'api/media/hl.webp')",
    )
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, origin_instance_id, expires_at, "
        "media_url) VALUES('mo1', 'u1', 'self', '2099-01-01T00:00:00+00:00', "
        "'api/media/mom.webp')",
    )

    repo = SqliteMediaReferenceRepo(db)
    names = await repo.referenced_basenames()
    assert names == {
        "dm.webp",
        "feedsingle.webp",  # leading slash + ?query stripped
        "feed1.webp",
        "feed2.webp",
        "gal.webp",
        "galt.webp",
        "hl.webp",
        "mom.webp",
    }


async def _seed_parents(db) -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name) VALUES('u1', 'u1', 'U1')",
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key) VALUES('sp1', 'S', 'self', 'u1', 'k')",
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type) VALUES('fp0', 'u1', 'text')",
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type) "
        "VALUES('sp0', 'sp1', 'u1', 'text')",
    )
    await db.enqueue(
        "INSERT INTO calendars(id, name, owner_username) VALUES('cal1', 'C', 'u1')",
    )


async def test_collects_the_remaining_media_sources(db):
    """Comments, bazaar listings, calendar / page covers, task attachments
    and drafts reference media too — none of them may be swept."""
    await _seed_parents(db)
    stmts = [
        "INSERT INTO post_comments(id, post_id, author, media_url) "
        "VALUES('c1', 'fp0', 'u1', 'api/media/fcomment.webp')",
        "INSERT INTO space_post_comments(id, post_id, author, media_url) "
        "VALUES('c2', 'sp0', 'u1', 'api/media/scomment.webp')",
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode, "
        "title, end_time, currency, image_urls_json) VALUES('sp0', 'sp1', 'u1', "
        "'fixed', 'T', '2099-01-01', 'USD', '[\"api/media/bazaar.webp\"]')",
        "INSERT INTO calendar_events(id, calendar_id, summary, start_dt, end_dt, "
        "created_by, cover_url) VALUES('e1', 'cal1', 'S', '2026-01-01', "
        "'2026-01-02', 'u1', '/api/media/calcover.webp')",
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, "
        "end_dt, created_by, cover_url) VALUES('e2', 'sp1', 'S', '2026-01-01', "
        "'2026-01-02', 'u1', '/api/media/spcalcover.webp')",
        "INSERT INTO pages(id, title, created_by, cover_image_url) "
        "VALUES('pg1', 'P', 'u1', 'api/media/pagecover.webp')",
        "INSERT INTO space_pages(id, space_id, title, created_by, cover_image_url) "
        "VALUES('pg2', 'sp1', 'P', 'u1', 'api/media/spagecover.webp')",
        "INSERT INTO page_edit_history(id, page_id, title, content, edited_by, "
        "version, cover_image_url) VALUES('h1', 'pg1', 'P', '', 'u1', 1, "
        "'api/media/oldcover.webp')",
        "INSERT INTO task_attachments(id, task_id, uploaded_by, url, filename, "
        "mime, size_bytes) VALUES('a1', 't1', 'u1', 'api/media/attach.pdf', "
        "'report.pdf', 'application/pdf', 1)",
        "INSERT INTO post_drafts(id, username, context, media_url) "
        "VALUES('d1', 'u1', 'feed', 'api/media/draft.webp')",
    ]
    for sql in stmts:
        await db.enqueue(sql)
    repo = SqliteMediaReferenceRepo(db)
    expected = {
        "fcomment.webp",
        "scomment.webp",
        "bazaar.webp",
        "calcover.webp",
        "spcalcover.webp",
        "pagecover.webp",
        "spagecover.webp",
        "oldcover.webp",
        "attach.pdf",
        "draft.webp",
    }
    assert await repo.referenced_basenames() == expected
    for name in expected:
        assert await repo.is_referenced(name), name


async def test_is_referenced_matches_the_whole_file_name(db):
    await _seed_parents(db)
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url) "
        "VALUES('p1', 'sp1', 'u1', 'image', 'api/media/abc_1.webp')",
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, image_urls_json) "
        "VALUES('p2', 'sp1', 'u1', 'image', '[\"api/media/x.webp\"]')",
    )
    await db.enqueue("INSERT INTO gallery_albums(id, name) VALUES('al1', 'A')")
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, "
        "filename, thumbnail_filename, width, height) "
        "VALUES('gi1', 'al1', 'u1', 'photo', 'gal.webp', 'galt.webp', 1, 1)",
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.is_referenced("abc_1.webp")
    assert await repo.is_referenced("x.webp")
    assert await repo.is_referenced("galt.webp")
    # A substring, or a LIKE wildcard, is not a match.
    assert not await repo.is_referenced("c_1.webp")
    assert not await repo.is_referenced("abc%.webp")
    assert not await repo.is_referenced("abcx1.webp")
    assert not await repo.is_referenced("")


async def test_a_deleted_post_no_longer_references_its_images(db):
    """Soft delete clears ``media_url`` but keeps ``image_urls_json``; a
    deleted row must not pin the files forever."""
    await _seed_parents(db)
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, image_urls_json, "
        "deleted) VALUES('p1', 'sp1', 'u1', 'image', '[\"api/media/gone.webp\"]', 1)",
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, image_urls_json, deleted) "
        "VALUES('f1', 'u1', 'image', '[\"api/media/fgone.webp\"]', 1)",
    )
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.is_referenced("gone.webp")
    assert not await repo.is_referenced("fgone.webp")
    assert await repo.referenced_basenames() == set()


async def test_link_preview_images_are_referenced(db):
    """The re-encoded link-preview image a post carries is live media — the
    orphan sweep must not delete it, nor may deleting one of two posts that
    share it."""
    await _seed_parents(db)
    rows = [
        (
            "feed_posts",
            "f1",
            '{"url": "https://e.example/", "thumbnail_url": "api/media/lp_1.webp"}',
        ),
        (
            "space_posts",
            "p1",
            '{"url": "https://e.example/", "thumbnail_url": "api/media/lp_2.webp"}',
        ),
        # A preview without an image, and a corrupt value, reference nothing.
        ("space_posts", "p2", '{"url": "https://e.example/"}'),
        ("space_posts", "p3", "not json lp_2.webp"),
    ]
    for table, pid, value in rows:
        if table == "feed_posts":
            await db.enqueue(
                "INSERT INTO feed_posts(id, author, type, link_preview_json) "
                "VALUES(?, 'u1', 'text', ?)",
                (pid, value),
            )
        else:
            await db.enqueue(
                "INSERT INTO space_posts(id, space_id, author, type, "
                "link_preview_json) VALUES(?, 'sp1', 'u1', 'text', ?)",
                (pid, value),
            )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.referenced_basenames() == {"lp_1.webp", "lp_2.webp"}
    assert await repo.is_referenced("lp_1.webp")
    assert await repo.is_referenced("lp_2.webp")
    assert not await repo.is_referenced("lp_.webp")
