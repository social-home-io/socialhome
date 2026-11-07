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
        (
            "INSERT INTO post_comments(id, post_id, author, media_url) "
            "VALUES('c1', 'fp0', 'u1', 'api/media/fcomment.webp')"
        ),
        (
            "INSERT INTO space_post_comments(id, post_id, author, media_url) "
            "VALUES('c2', 'sp0', 'u1', 'api/media/scomment.webp')"
        ),
        (
            "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode, "
            "title, end_time, currency, image_urls_json) VALUES('sp0', 'sp1', 'u1', "
            "'fixed', 'T', '2099-01-01', 'USD', '[\"api/media/bazaar.webp\"]')"
        ),
        (
            "INSERT INTO calendar_events(id, calendar_id, summary, start_dt, end_dt, "
            "created_by, cover_url) VALUES('e1', 'cal1', 'S', '2026-01-01T00:00:00+00:00', "
            "'2026-01-02T00:00:00+00:00', 'u1', '/api/media/calcover.webp')"
        ),
        (
            "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, "
            "end_dt, created_by, cover_url) VALUES('e2', 'sp1', 'S', '2026-01-01T00:00:00+00:00', "
            "'2026-01-02T00:00:00+00:00', 'u1', '/api/media/spcalcover.webp')"
        ),
        (
            "INSERT INTO pages(id, title, created_by, cover_image_url) "
            "VALUES('pg1', 'P', 'u1', 'api/media/pagecover.webp')"
        ),
        (
            "INSERT INTO space_pages(id, space_id, title, created_by, cover_image_url) "
            "VALUES('pg2', 'sp1', 'P', 'u1', 'api/media/spagecover.webp')"
        ),
        (
            "INSERT INTO page_edit_history(id, page_id, title, content, edited_by, "
            "version, cover_image_url) VALUES('h1', 'pg1', 'P', '', 'u1', 1, "
            "'api/media/oldcover.webp')"
        ),
        (
            "INSERT INTO task_attachments(id, task_id, uploaded_by, url, filename, "
            "mime, size_bytes) VALUES('a1', 't1', 'u1', 'api/media/attach.pdf', "
            "'report.pdf', 'application/pdf', 1)"
        ),
        (
            "INSERT INTO post_drafts(id, username, context, media_url) "
            "VALUES('d1', 'u1', 'feed', 'api/media/draft.webp')"
        ),
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


async def test_pending_moderation_payloads_keep_their_media(db):
    """A queued post's images are referenced only by its queue payload until
    it is approved (up to 7 days) — the orphan sweep must keep them; once the
    item is decided the payload no longer counts."""
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'S', 'i', 'o', 'aa')"
    )
    payload = (
        '{"entity": "post", "image_urls": ["api/media/pend1.webp",'
        ' "/api/media/pend2.webp?exp=1&sig=x"],'
        ' "attachments": {"bazaar": {"image_urls": ["api/media/listing.webp"]}}}'
    )
    for item_id, status in (("m1", "pending"), ("m2", "rejected")):
        await db.enqueue(
            "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
            " submitted_by, payload_json, expires_at, status)"
            " VALUES(?, 'sp', 'posts', 'create', 'u', ?, '2099-01-01', ?)",
            (
                item_id,
                payload.replace("pend", "pend" if status == "pending" else "gone"),
                status,
            ),
        )
    repo = SqliteMediaReferenceRepo(db)
    names = await repo.referenced_basenames()
    assert {"pend1.webp", "pend2.webp", "listing.webp"} <= names
    assert "gone1.webp" not in names
    assert await repo.is_referenced("pend2.webp")
    assert await repo.is_referenced("listing.webp")
    assert not await repo.is_referenced("gone1.webp")
    assert not await repo.is_referenced("pend")


# ─── may_reference: who may name a file in a write ───────────────────────


async def _people(db):
    for uid, name in (("u1", "ann"), ("u2", "tom"), ("u3", "lea")):
        await db.enqueue(
            "INSERT INTO users(user_id, username, display_name) VALUES(?, ?, ?)",
            (uid, name, name),
        )


async def test_an_unreferenced_file_is_free_to_name(db):
    await _people(db)
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.may_reference("fresh.webp", "u3")
    assert not await repo.may_reference("", "u3")


async def test_a_private_album_file_only_by_its_uploader(db):
    await _people(db)
    await db.enqueue(
        "INSERT INTO gallery_albums(id, name, owner_user_id, audience)"
        " VALUES('al', 'Gifts', 'u1', 'chosen')"
    )
    await db.enqueue(
        "INSERT INTO gallery_album_audience(album_id, user_id) VALUES('al', 'u2')"
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height)"
        " VALUES('gi', 'al', 'u1', 'photo', 'p.webp', 'pt.webp', 1, 1)"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.may_reference("p.webp", "u1")
    assert not await repo.may_reference("p.webp", "u2")  # chosen, not uploader
    assert not await repo.may_reference("pt.webp", "u3")  # the thumbnail too


async def test_a_household_or_space_album_file(db):
    await _people(db)
    await db.enqueue(
        "INSERT INTO gallery_albums(id, name, owner_user_id) VALUES('al', 'A', 'u1')"
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height)"
        " VALUES('gi', 'al', 'u1', 'photo', 'h.webp', 'ht.webp', 1, 1)"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.may_reference("h.webp", "u3")


async def test_a_chat_attachment_only_by_its_members(db):
    await _people(db)
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('c1', 'dm')")
    for name in ("ann", "tom"):
        await db.enqueue(
            "INSERT INTO conversation_members(conversation_id, username) VALUES('c1', ?)",
            (name,),
        )
    await db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
        " media_url) VALUES('m1', 'c1', 'u1', 'api/media/dm.webp')"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.may_reference("dm.webp", "u2")
    assert not await repo.may_reference("dm.webp", "u3")


async def test_a_file_seen_through_any_row_may_be_named(db):
    """A DM attachment also posted to the feed: anyone at home may."""
    await _people(db)
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('c1', 'dm')")
    await db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
        " media_url) VALUES('m1', 'c1', 'u1', 'api/media/both.webp')"
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, image_urls_json)"
        " VALUES('fp', 'u1', 'image', '[\"api/media/both.webp\"]')"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.may_reference("both.webp", "u3")


async def test_own_moment_highlight_draft_and_pending_item(db):
    await _people(db)
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, origin_instance_id, expires_at,"
        " media_url) VALUES('mo', 'u1', 'self', '2099-01-01T00:00:00+00:00',"
        " 'api/media/mom.webp')"
    )
    await db.enqueue(
        "INSERT INTO highlights(id, author_user_id, highlight_date, expires_at)"
        " VALUES('h1', 'u1', '2026-01-01', '2099-01-01T00:00:00+00:00')"
    )
    await db.enqueue(
        "INSERT INTO highlight_frames(id, highlight_id, sequence, frame_type,"
        " media_url) VALUES('hf', 'h1', 1, 'image', 'api/media/hl.webp')"
    )
    repo = SqliteMediaReferenceRepo(db)
    for name in ("mom.webp", "hl.webp"):
        assert await repo.may_reference(name, "u1")
        assert not await repo.may_reference(name, "u3")


async def _queue_item(
    db,
    item_id: str,
    status: str,
    name: str,
    by: str = "u1",
    expires: str = "2099-01-01T00:00:00+00:00",
):
    await db.enqueue(
        "INSERT OR IGNORE INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'S', 'i', 'ann', ?)",
        ("ab" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
        " submitted_by, payload_json, expires_at, status)"
        " VALUES(?, 'sp', 'calendar', 'create', ?, ?, ?, ?)",
        (item_id, by, f'{{"cover_url": "api/media/{name}"}}', expires, status),
    )


@pytest.mark.parametrize("status", ["pending", "approved"])
async def test_a_submitter_names_their_own_held_file_while_it_is_released(db, status):
    """Regression: the approve claims the item (``approved``) BEFORE the
    apply re-checks its files for the submitter, and only a *pending*
    item counted as theirs — so every reviewed calendar cover / page cover
    whose file nothing else names yet was refused at release."""
    await _people(db)
    await _queue_item(db, "q1", status, "held.webp")
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.may_reference("held.webp", "u1")
    assert not await repo.may_reference("held.webp", "u3")


@pytest.mark.parametrize("status", ["rejected", "expired"])
async def test_a_decided_items_files_are_nobodys(db, status):
    await _people(db)
    await _queue_item(db, "q1", status, "gone.webp")
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.may_reference("gone.webp", "u1")


@pytest.mark.parametrize("status", ["pending", "approved"])
async def test_an_own_item_never_opens_a_file_another_row_closes(db, status):
    """A queue item is not a key of its own: a file that another space's
    post shows stays that space's seats' — whatever the submitter's own
    item names (a remote submission is never checked on its way in)."""
    await _people(db)
    await _queue_item(db, "q1", status, "other.webp")
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp2', 'T', 'i', 'ann', ?)",
        ("cd" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp2', 'u2', 'owner')"
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url)"
        " VALUES('p2', 'sp2', 'u2', 'image', 'api/media/other.webp')"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.may_reference("other.webp", "u1")
    assert await repo.may_reference("other.webp", "u2")


_SD = "sd-" + "a" * 40 + ".webp"  # a space file as a household delivered it


@pytest.mark.parametrize("status", ["pending", "approved"])
async def test_a_remote_submitter_never_opens_an_orphan_file(db, status):
    """A remote submission's files always take the submitter household's
    ``sd-`` names here (``SpaceMediaNamer``). A remote item naming any other
    file nothing references — an orphan, or a fresh upload of ours not
    posted yet — must not hand it over; its own delivered copy still
    releases. A local submitter's own fresh upload is theirs."""
    await _people(db)
    await _queue_item(db, "q1", status, "orphan.webp", by="u-remote")
    await _queue_item(db, "q2", status, _SD, by="u-remote")
    await _queue_item(db, "q3", status, "fresh.webp", by="u1")
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.may_reference("orphan.webp", "u-remote")
    assert await repo.may_reference(_SD, "u-remote")
    assert await repo.may_reference("fresh.webp", "u1")


@pytest.mark.parametrize("theirs", ["pending", "approved", "rejected"])
async def test_an_own_item_never_piggybacks_on_someone_elses(db, theirs):
    """Naming a file in one's own item does not open it while another
    submitter's item names it too."""
    await _people(db)
    await _queue_item(db, "q-victim", theirs, "victim.webp", by="u1")
    await _queue_item(db, "q-attacker", "pending", "victim.webp", by="u3")
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.may_reference("victim.webp", "u3")


async def test_an_approved_item_past_its_window_is_settled(db):
    """An approved item can resume only inside its review window; after it
    the item no longer counts — for its submitter or the sweep."""
    await _people(db)
    await _queue_item(
        db, "q1", "approved", "late.webp", expires="2000-01-01T00:00:00+00:00"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.is_referenced("late.webp")
    assert "late.webp" not in await repo.referenced_basenames()


async def test_an_approved_items_files_survive_the_sweep_until_it_lands(db):
    """The approve claims the item before its apply: an apply that stopped
    part-way (resumable while the window is open) keeps its cover through
    an orphan sweep — the queue item is still its only reference."""
    await _people(db)
    await _queue_item(db, "q1", "approved", "resume.webp")
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.is_referenced("resume.webp")
    assert "resume.webp" in await repo.referenced_basenames()


async def test_household_files_are_never_a_remote_members(db):
    """A remote space member (no ``users`` row here) reaches this check
    only through a reviewed item it submitted — a household feed photo, a
    personal event cover or a household album's file is never theirs to
    name, even with their own item naming it."""
    await _people(db)
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, image_urls_json)"
        " VALUES('fp', 'u1', 'image', '[\"api/media/home.webp\"]')"
    )
    await db.enqueue(
        "INSERT INTO gallery_albums(id, name, owner_user_id, audience)"
        " VALUES('al', 'Home', 'u1', 'everyone')"
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height) VALUES('gi', 'al', 'u1', 'photo',"
        " 'album.webp', 'album_t.webp', 1, 1)"
    )
    for name in ("home.webp", "album.webp"):
        await _queue_item(db, f"q-{name}", "approved", name, by="u-remote")
    repo = SqliteMediaReferenceRepo(db)
    for name in ("home.webp", "album.webp"):
        assert await repo.may_reference(name, "u3"), name
        assert not await repo.may_reference(name, "u-remote"), name


async def test_a_space_photo_only_for_its_seats(db):
    await _people(db)
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'S', 'i', 'ann', ?)",
        ("ab" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp', 'u2', 'member')"
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url)"
        " VALUES('sp1', 'sp', 'u2', 'image', 'api/media/sp.webp')"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.may_reference("sp.webp", "u2")
    assert not await repo.may_reference("sp.webp", "u3")


# ─── confined_to_space: may a space's media be sealed for its GFS readers ─


async def _two_spaces(db):
    await _seed_parents(db)
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key) VALUES('sp2', 'T', 'self', 'u1', 'k')",
    )


async def test_a_file_of_this_spaces_post_is_confined_to_it(db):
    """#1309 review D: a file is sealed for a space's GFS readers only when
    every row that references it is that space's content."""
    await _two_spaces(db)
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, image_urls_json)"
        " VALUES('p1', 'sp1', 'u1', 'image', ?)",
        ('["api/media/s1.webp"]',),
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.confined_to_space("s1.webp", "sp1")
    assert not await repo.confined_to_space("s1.webp", "sp2")
    # Unreferenced: not a space file at all.
    assert not await repo.confined_to_space("nobody.webp", "sp1")
    assert not await repo.confined_to_space("", "sp1")


async def test_a_file_also_named_outside_the_space_is_not_confined(db):
    """A member naming a household post's, another space's or a DM's file
    in a post of S must not get it sealed under S's key."""
    await _two_spaces(db)
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url)"
        " VALUES('p1', 'sp1', 'u1', 'image', 'api/media/shared.webp')"
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, media_url)"
        " VALUES('f1', 'u1', 'image', 'api/media/shared.webp')"
    )
    await db.enqueue(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, end_dt,"
        " created_by, cover_url) VALUES('e2', 'sp2', 'S', '2026-01-01T00:00:00+00:00',"
        " '2026-01-02T00:00:00+00:00', 'u1', 'api/media/other.webp')"
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url)"
        " VALUES('p2', 'sp1', 'u1', 'image', 'api/media/other.webp')"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.confined_to_space("shared.webp", "sp1")
    assert not await repo.confined_to_space("other.webp", "sp1")


async def test_gallery_files_follow_their_album(db):
    await _two_spaces(db)
    await db.enqueue(
        "INSERT INTO gallery_albums(id, name, owner_user_id, space_id)"
        " VALUES('sa', 'S', 'u1', 'sp1')"
    )
    await db.enqueue(
        "INSERT INTO gallery_albums(id, name, owner_user_id, audience)"
        " VALUES('pa', 'Private', 'u1', 'chosen')"
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height)"
        " VALUES('g1', 'sa', 'u1', 'photo', 'g.webp', 'gt.webp', 1, 1)"
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height)"
        " VALUES('g2', 'pa', 'u1', 'photo', 'priv.webp', 'privt.webp', 1, 1)"
    )
    # A member names the private album's file in a post of the space.
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url)"
        " VALUES('p3', 'sp1', 'u1', 'image', 'api/media/priv.webp')"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.confined_to_space("g.webp", "sp1")
    assert await repo.confined_to_space("gt.webp", "sp1")
    assert not await repo.confined_to_space("priv.webp", "sp1")


async def test_a_link_card_thumbnail_shared_with_other_posts_is_confined(db):
    """#1309 re-review 3 (members equal): a link-preview thumbnail is cached
    per URL, so the same file backs every post that linked the page — in
    the feed and in other spaces. It is public web content the household
    fetched itself; those references don't stop it reaching this space's
    link-joined members and followers."""
    await _two_spaces(db)
    card = '{"url": "https://example.org", "thumbnail_url": "api/media/card.webp"}'
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, link_preview_json)"
        " VALUES('p1', 'sp1', 'u1', 'text', ?)",
        (card,),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, link_preview_json)"
        " VALUES('p2', 'sp2', 'u1', 'text', ?)",
        (card,),
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, link_preview_json)"
        " VALUES('f1', 'u1', 'text', ?)",
        (card,),
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.confined_to_space("card.webp", "sp1")
    assert await repo.confined_to_space("card.webp", "sp2")


async def test_a_card_thumbnail_is_not_confined_when_named_as_other_media(db):
    """Only link-preview references are exempt: the same file named as a
    household post's picture still keeps it out."""
    await _two_spaces(db)
    card = '{"url": "https://example.org", "thumbnail_url": "api/media/card.webp"}'
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, link_preview_json)"
        " VALUES('p1', 'sp1', 'u1', 'text', ?)",
        (card,),
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, media_url)"
        " VALUES('f1', 'u1', 'image', 'api/media/card.webp')"
    )
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.confined_to_space("card.webp", "sp1")


async def test_a_file_only_other_spaces_cards_name_is_not_this_spaces(db):
    """Exempt references never make a file this space's: one only another
    space's card names is not confined to this space."""
    await _two_spaces(db)
    card = '{"url": "https://example.org", "thumbnail_url": "api/media/card.webp"}'
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, link_preview_json)"
        " VALUES('p2', 'sp2', 'u1', 'text', ?)",
        (card,),
    )
    repo = SqliteMediaReferenceRepo(db)
    assert not await repo.confined_to_space("card.webp", "sp1")


# ─── A page's body pictures (v_81) ──────────────────────────────────────

_BODY = (
    "![a](api/media/body.webp) ![b](/api/media/body2.webp?exp=1&sig=x) "
    "[link](https://elsewhere.example/api/media/notours.webp)"
)


async def _page_rows(db) -> None:
    await _seed_parents(db)
    await db.enqueue(
        "INSERT INTO space_pages(id, space_id, title, content, created_by)"
        " VALUES('pg', 'sp1', 'P', ?, 'u1')",
        (_BODY,),
    )
    await db.enqueue(
        "INSERT INTO pages(id, title, content, created_by)"
        " VALUES('hp', 'H', '![h](api/media/house.webp)', 'u1')"
    )
    await db.enqueue(
        "INSERT INTO page_edit_history(id, page_id, space_id, title, content,"
        " edited_by, version) VALUES('h1', 'pg', 'sp1', 'P',"
        " '![o](api/media/older.webp)', 'u1', 1)"
    )
    await db.enqueue(
        "INSERT INTO space_page_snapshots(page_id, space_id, snapshot_at, body,"
        " snapshot_by, side, conflict) VALUES('pg', 'sp1', '2026-01-01', ?,"
        " 'u1', 'theirs', 1)",
        ('{"content": "![s](api/media/side.webp)"}',),
    )


async def test_a_pages_body_pictures_are_never_swept(db):
    """Regression: only a page's cover counted as a reference, so the sweep
    deleted every picture a page body showed a day after it was posted —
    a space page, a household page, a version in its history, an open
    conflict side. A path inside another URL is not a file here."""
    await _page_rows(db)
    repo = SqliteMediaReferenceRepo(db)
    names = await repo.referenced_basenames()
    for name in ("body.webp", "body2.webp", "house.webp", "older.webp", "side.webp"):
        assert name in names, name
        assert await repo.is_referenced(name), name
    assert "notours.webp" not in names
    assert not await repo.is_referenced("notours.webp")


async def test_a_space_pages_picture_is_its_seats_and_confined_to_it(db):
    await _page_rows(db)
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role)"
        " VALUES('sp1', 'u-seat', 'member')"
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key) VALUES('sp2', 'T', 'self', 'u1', 'k')",
    )
    repo = SqliteMediaReferenceRepo(db)
    assert await repo.may_reference("body.webp", "u-seat")
    assert not await repo.may_reference("body.webp", "u-stranger")
    assert await repo.may_reference("house.webp", "u1")
    assert not await repo.may_reference("house.webp", "u-seat")
    assert await repo.confined_to_space("body2.webp", "sp1")
    assert await repo.confined_to_space("older.webp", "sp1")
    assert not await repo.confined_to_space("body.webp", "sp2")
    assert not await repo.confined_to_space("house.webp", "sp1")
    # A file another space's row names too is no longer this space's.
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url)"
        " VALUES('p2', 'sp2', 'u1', 'image', 'api/media/body.webp')"
    )
    assert not await repo.confined_to_space("body.webp", "sp1")
