"""``receive_posts``: posts, comments, polls and their tombstones by §25.6 sync."""

from __future__ import annotations

from datetime import UTC

from socialhome.domain.events import SpaceCommentSynced
from tests.federation.sync.space.receiver_fakes import _here, _send


async def test_dispatch_keeps_a_hidden_anchor_post_out_of_the_joiners_feed(receiver):
    """A synced bazaar / calendar anchor lands with ``hidden_from_feed``.

    The provider now ships anchor posts (their listings reference them by
    FK); the joiner must store the flag with the row, or every unannounced
    listing would surface as a feed card on the household that joined.
    """
    r, _space_repo, post_repo = receiver
    await r._dispatch(
        "posts",
        "sp-1",
        [
            {
                "id": "p-anchor",
                "author": "u-1",
                "type": "text",
                "content": "listing card",
                "hidden_from_feed": True,
            },
            {"id": "p-shown", "author": "u-1", "type": "text", "content": "hi"},
        ],
        provider="peer-a",
    )
    by_id = {p.id: p for _sid, p in post_repo.saved}
    assert by_id["p-anchor"].hidden_from_feed is True
    assert by_id["p-shown"].hidden_from_feed is False


def test_synced_post_record_keeps_a_validated_link_preview():
    """A sync record round-trips the author-built card (exporter → receiver);
    the receiver re-validates it and keeps only a local image reference."""
    from datetime import datetime

    from socialhome.domain.link_preview import LinkPreview
    from socialhome.domain.post import Post, PostType
    from socialhome.federation.sync.space.exporters.posts import _post_to_dict
    from socialhome.federation.sync.space.receive_posts import _post_from_record

    card = LinkPreview(
        url="https://example.com/", title="T", thumbnail_url="api/media/lp.webp"
    )
    post = Post(
        id="p-sync",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        content="https://example.com/",
        link_preview=card,
    )
    record = _post_to_dict(post)
    assert record["link_preview"]["thumbnail_url"] == "api/media/lp.webp"
    back = _post_from_record(record)
    assert back is not None and back.link_preview == card
    record["link_preview"]["thumbnail_url"] = "https://tracker.example/x.png"
    assert _post_from_record(record).link_preview.thumbnail_url is None
    record["link_preview"] = {"url": "file:///etc/passwd", "title": "T"}
    assert _post_from_record(record).link_preview is None


async def test_posts(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-1",
                "author": "u-1",
                "type": "text",
                "content": "hi",
                "created_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.posts) == 1
    space_id, post = c.posts[0]
    assert space_id == "sp-1"
    assert post.id == "p-1"


async def test_a_post_deleted_here_is_never_resurrected_by_a_sync(setup):
    """v_49: a delete can overtake its create (a member-published delete
    leaves a soft-deleted row); a provider that missed the delete must not
    bring the post back."""
    from datetime import datetime

    from socialhome.domain.post import Post, PostType

    r, c, kp = setup
    c.held_posts["p-gone"] = Post(
        id="p-gone",
        author="u-1",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
        deleted=True,
    )
    record = {
        "author": "u-1",
        "type": "text",
        "content": "hi",
        "created_at": "2026-04-18T00:00:00+00:00",
    }
    await _send(r, kp, "posts", [{"id": "p-gone", **record}, {"id": "p-2", **record}])
    assert [post.id for _sid, post in c.posts] == ["p-2"]


async def test_posts_keep_their_image_urls(setup):
    """The exporter ships ``image_urls``; a joiner must keep them — the
    media bytes that follow are matched against them, and the feed renders
    from them. Strings only, capped at the feed maximum."""
    from socialhome.domain.post import FEED_POST_MAX_IMAGES

    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-img",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/a.webp", 3]
                + [f"api/media/{n}.webp" for n in range(9)],
            },
        ],
    )
    _sid, post = c.posts[0]
    assert post.image_urls[0] == _here("a.webp")
    assert all(isinstance(u, str) for u in post.image_urls)
    assert len(post.image_urls) == FEED_POST_MAX_IMAGES


async def test_comments(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "comments",
        [
            {
                "id": "c-1",
                "post_id": "p-1",
                "author": "u-1",
                "type": "text",
                "content": "nice",
                "created_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.comments) == 1
    assert c.comments[0].id == "c-1"


async def test_a_synced_comment_tells_the_parent_wait_buffer(setup, bus):
    """A stored comment publishes ``SpaceCommentSynced`` (and nothing that
    notifies), so replies held for it replay."""
    r, _c, kp = setup
    seen: list = []

    async def _on(event):
        seen.append(event)

    bus.subscribe(SpaceCommentSynced, _on)
    await _send(
        r,
        kp,
        "comments",
        [
            {
                "id": "c-s",
                "post_id": "p-1",
                "author": "u-1",
                "type": "text",
                "content": "x",
            }
        ],
    )
    assert [(e.comment.id, e.space_id) for e in seen] == [("c-s", "sp-1")]


async def test_an_older_providers_deleted_comment_record_is_not_stored(setup):
    """Before ``comments_deleted`` a provider streamed its deleted comments
    as plain records (content cleared); stored, one read as a live empty
    comment. Deletes travel as tombstones now — such a record is skipped."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "comments",
        [{"id": "c-gone", "post_id": "p-1", "author": "u-1", "deleted": True}],
    )
    assert c.comments == []


async def test_synced_media_references_keep_only_the_local_shape(setup):
    """A synced row's media references must look like a local upload
    (``api/media/<name>``); anything else is dropped, never stored."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-m",
                "author": "u-1",
                "type": "image",
                "media_url": "https://elsewhere.example/x.webp",
                "image_urls": [
                    "api/media/ok.webp",
                    "api/media/../escape",
                    "ok2.webp",
                    "/api/media/ok3.webp",
                ],
            },
        ],
    )
    await _send(
        r,
        kp,
        "comments",
        [
            {
                "id": "c-m",
                "post_id": "p-m",
                "author": "u-1",
                "type": "image",
                "media_url": "api/media/sub/dir.webp",
            },
        ],
    )
    await _send(
        r,
        kp,
        "bazaar",
        [
            {
                "post_id": "bzr-m",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "Chair",
                "image_urls": ["api/media/chair.webp", "file:///etc/passwd"],
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "price": 1,
            },
        ],
    )
    _sid, post = c.posts[0]
    assert post.media_url is None
    assert post.image_urls == (_here("ok.webp"), _here("ok3.webp"))
    assert c.comments[0].media_url is None
    assert c.bazaar_listings[0].image_urls == (_here("chair.webp"),)


async def test_polls_skips_persistence(setup):
    """v1: polls ride along with posts via Post.poll — standalone poll
    records just log."""
    r, c, kp = setup
    await _send(r, kp, "polls", [{"post_id": "p-1", "meta": {}, "options": []}])
    assert c.posts == []


async def test_post_missing_required_field_drops(setup):
    """A post record without id/author is skipped by the helper."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {"type": "text", "content": "orphan"},
        ],
    )
    assert c.posts == []


async def test_a_held_comment_mid_chunk_never_stops_the_rest(setup, tmp_dir):
    """Review repro I3: a comment the member relay already delivered, or a
    delete-before-create tombstone, used to make the plain INSERT raise and
    lose the rest of the chunk. Held rows stay as they are; the later
    records land."""
    from datetime import datetime

    from socialhome.db.database import AsyncDatabase
    from socialhome.domain.post import Comment, CommentType, Post, PostType
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo

    r, _c, kp = setup
    db = AsyncDatabase(tmp_dir / "sync.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES('sp-1','S','peer-a','o','ab')"
        )
        repo = SqliteSpacePostRepo(db)
        now = datetime.now(UTC)
        await repo.save(
            "sp-1",
            Post(id="p-1", author="u-1", type=PostType.TEXT, created_at=now),
        )
        await repo.add_comment(
            Comment(
                id="c-held",
                post_id="p-1",
                author="u-1",
                type=CommentType.TEXT,
                created_at=now,
                content="from the relay",
            ),
            space_id="sp-1",
        )
        await repo.add_comment(
            Comment(
                id="c-gone",
                post_id="p-1",
                author="u-1",
                type=CommentType.TEXT,
                created_at=now,
                deleted=True,
            ),
            space_id="sp-1",
        )
        r._space_post_repo = repo
        rec = {"post_id": "p-1", "author": "u-1", "type": "text"}
        await _send(
            r,
            kp,
            "comments",
            [
                {"id": "c-held", **rec, "content": "from sync"},
                {"id": "c-gone", **rec, "content": "resurrected"},
                {"id": "c-new", **rec, "content": "later record"},
            ],
        )
        assert (await repo.get_comment("c-held")).content == "from the relay"
        gone = await repo.get_comment("c-gone")
        assert gone.deleted and gone.content is None
        assert (await repo.get_comment("c-new")).content == "later record"
    finally:
        await db.shutdown()


async def test_a_sync_keeps_a_held_posts_reactions_and_comment_count(setup):
    """Review repro S1: re-sending a held post must not wipe the reactions
    (or comment count) held here — the stamps would then refuse the copy
    that could restore them."""
    from datetime import datetime

    from socialhome.domain.post import Post, PostType

    r, c, kp = setup
    c.held_posts["p-1"] = Post(
        id="p-1",
        author="u-1",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
        content="hi",
        reactions={"👍": frozenset({"u-2"})},
        comment_count=3,
    )
    record = {"id": "p-1", "author": "u-1", "type": "text", "content": "hi"}
    await _send(r, kp, "posts", [record])
    _sid, saved = c.posts[0]
    assert saved.reactions == {"👍": frozenset({"u-2"})}
    assert saved.comment_count == 3


async def test_a_new_post_from_a_sync_carries_its_reactions(setup):
    r, c, kp = setup
    record = {
        "id": "p-new",
        "author": "u-1",
        "type": "text",
        "reactions": {"👍": ["u-2", "u-3"], "bad": "x", "🎉": [4]},
    }
    await _send(r, kp, "posts", [record])
    _sid, saved = c.posts[0]
    assert saved.reactions == {"👍": frozenset({"u-2", "u-3"})}


async def test_relayed_reactions_survive_a_sync_end_to_end(setup, tmp_dir):
    """S1 on real SQLite: a relayed reaction, a sync re-sending the post, a
    duplicate copy of the add — the reaction is still there."""
    from datetime import datetime

    from socialhome.db.database import AsyncDatabase
    from socialhome.domain.post import Post, PostType
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo

    r, _c, kp = setup
    db = AsyncDatabase(tmp_dir / "sync.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES('sp-1','S','peer-a','o','ab')"
        )
        repo = SqliteSpacePostRepo(db)
        await repo.save(
            "sp-1",
            Post(
                id="p-1",
                author="u-1",
                type=PostType.TEXT,
                created_at=datetime.now(UTC),
                content="hi",
            ),
        )
        await repo.add_reaction(
            "p-1", "👍", "u-2", space_id="sp-1", stamp="2026-10-03 12:00:00"
        )
        r._space_post_repo = repo
        await _send(
            r,
            kp,
            "posts",
            [{"id": "p-1", "author": "u-1", "type": "text", "content": "hi"}],
        )
        assert "u-2" in (await repo.get("p-1"))[1].reactions["👍"]
    finally:
        await db.shutdown()
