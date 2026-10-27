"""Service-level tests for the highlights pillar."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.domain.errors import InvalidMediaRefError
from socialhome.domain.events import (
    HighlightAudienceChanged,
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameViewed,
    HighlightRemoved,
)
from socialhome.domain.highlight import (
    HighlightAudience,
    HighlightAudienceEmptyError,
    HighlightAudienceMismatchError,
    HighlightAudiencePeerTooOldError,
    HighlightFrameType,
    HighlightNotTodayError,
)
from socialhome.domain.post import Post, PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.highlight_repo import SqliteHighlightRepo
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import highlight_service as highlight_service_module
from socialhome.services.highlight_service import (
    MAX_FRAMES_PER_HIGHLIGHT,
    NOT_FOUND_MESSAGE,
    HighlightForbiddenError,
    HighlightFrameLimitError,
    HighlightNotFoundError,
    HighlightService,
)


async def _seed_user(
    db, user_id: str, username: str, *, prefs_json: str | None = None
) -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state, preferences_json) "
        "VALUES(?, ?, ?, 'active', COALESCE(?, '{}'))",
        (user_id, username, username.title(), prefs_json),
    )


@pytest.fixture
async def svc(db):
    bus = EventBus()
    return HighlightService(SqliteHighlightRepo(db), SqliteUserRepo(db), bus), bus


async def test_first_frame_creates_highlight_and_publishes(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    captured: list[HighlightFrameAdded] = []
    bus.subscribe(HighlightFrameAdded, lambda e: captured.append(e))

    highlight, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/a.webp",
        caption_text="hello",
    )
    assert highlight.author_user_id == "u1"
    assert frame.sequence == 1
    assert highlight.audience_kind is HighlightAudience.ALL_PAIRED
    assert len(captured) == 1
    assert captured[0].is_first_frame is True


async def test_delete_highlight_removes_frame_files(db, tmp_dir):
    """Deleting a highlight unlinks every frame's media file (cascade)."""
    media_dir = tmp_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    service = HighlightService(
        SqliteHighlightRepo(db),
        SqliteUserRepo(db),
        EventBus(),
        media_dir=media_dir,
        media_refs=SqliteMediaReferenceRepo(db),
    )
    await _seed_user(db, "u1", "pascal")
    for name in ("h1.webp", "h2.webp"):
        (media_dir / name).write_bytes(b"x")
    highlight, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="api/media/h1.webp",
    )
    await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="api/media/h2.webp",
    )
    assert (media_dir / "h1.webp").exists() and (media_dir / "h2.webp").exists()
    await service.delete_highlight(highlight_id=highlight.id, actor_user_id="u1")
    assert not (media_dir / "h1.webp").exists()
    assert not (media_dir / "h2.webp").exists()


async def test_second_frame_appends_to_same_highlight(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    s1, f1 = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    s2, f2 = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/b.webp",
    )
    assert s1.id == s2.id
    assert f1.sequence == 1 and f2.sequence == 2


async def test_frame_cap_raises(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    for _ in range(MAX_FRAMES_PER_HIGHLIGHT):
        await service.create_or_append_frame(
            author_user_id="u1",
            frame_type=HighlightFrameType.IMAGE,
            media_url="/api/media/x.webp",
        )
    with pytest.raises(HighlightFrameLimitError):
        await service.create_or_append_frame(
            author_user_id="u1",
            frame_type=HighlightFrameType.IMAGE,
            media_url="/api/media/y.webp",
        )


async def test_view_publishes_event(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    captured: list[HighlightFrameViewed] = []
    bus.subscribe(HighlightFrameViewed, lambda e: captured.append(e))
    await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u2")
    assert len(captured) == 1
    assert captured[0].viewer_user_id == "u2"


async def test_only_the_first_view_of_a_frame_is_announced(db, svc):
    """A viewer replays a highlight a few times; the author's household
    needs one receipt per (frame, viewer). Before, every re-view went out
    again (a WS frame here, a HIGHLIGHT_FRAME_VIEWED envelope to a remote
    author's household); now only the first does."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    captured: list[HighlightFrameViewed] = []
    bus.subscribe(HighlightFrameViewed, lambda e: captured.append(e))
    for _ in range(5):
        await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u2")
    assert len(captured) == 1


async def test_author_view_is_noop(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    captured: list[HighlightFrameViewed] = []
    bus.subscribe(HighlightFrameViewed, lambda e: captured.append(e))
    await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u1")
    assert captured == []  # author's own views don't accumulate


async def test_retention_uses_author_preferences(db, svc):
    service, _ = svc
    await _seed_user(
        db,
        "u1",
        "pascal",
        prefs_json='{"highlights": {"retention_days": 1}}',
    )
    highlight, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    # Author's retention is 1 day so expires_at is ~24h out — much less
    # than the 30-day default.
    assert highlight.expires_at is not None
    expires = datetime.fromisoformat(highlight.expires_at)
    cutoff_30d = datetime.now(UTC) + timedelta(days=29)
    assert expires < cutoff_30d


async def test_react_then_clear(db, svc):
    """Reaction set + clear publish events; clear removes the row."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    cleared = []
    bus.subscribe(HighlightFrameReactionChanged, lambda e: cleared.append(e))
    await service.react_to_frame(
        frame_id=frame.id,
        reactor_user_id="u2",
        emoji="🔥",
    )
    await service.clear_reaction(frame_id=frame.id, reactor_user_id="u2")
    # Two events: set then clear (emoji=None on the second).
    assert len(cleared) == 2
    assert cleared[1].emoji is None


async def test_delete_frame_requires_author(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    with pytest.raises(HighlightForbiddenError):
        await service.delete_frame(frame_id=frame.id, actor_user_id="u2")
    # Author can delete it.
    await service.delete_frame(frame_id=frame.id, actor_user_id="u1")


async def test_delete_highlight_requires_author(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    highlight, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    with pytest.raises(HighlightForbiddenError):
        await service.delete_highlight(highlight_id=highlight.id, actor_user_id="u2")
    await service.delete_highlight(highlight_id=highlight.id, actor_user_id="u1")


async def test_react_or_view_unknown_frame_raises(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    with pytest.raises(HighlightNotFoundError):
        await service.mark_frame_viewed(frame_id="nope", viewer_user_id="u1")
    with pytest.raises(HighlightNotFoundError):
        await service.react_to_frame(frame_id="nope", reactor_user_id="u1", emoji="🔥")
    with pytest.raises(HighlightNotFoundError):
        await service.clear_reaction(frame_id="nope", reactor_user_id="u1")


async def test_explicit_audience_kind_overrides_prefs(db, svc):
    """Caller-supplied audience overrides the author's default."""
    service, _ = svc
    await _seed_user(
        db,
        "u1",
        "pascal",
        prefs_json='{"highlights": {"default_audience": {"kind": "all_paired"}}}',
    )
    highlight, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
        audience_kind=HighlightAudience.HOUSEHOLDS,
        audience=("inst-b",),
    )
    assert highlight.audience_kind is HighlightAudience.HOUSEHOLDS
    assert highlight.audience == ("inst-b",)


async def test_your_own_highlight_is_never_unseen(db, svc):
    """Authors' views are never recorded (``mark_frame_viewed``), so
    counting their own frames as unseen lit a glowing ring and a
    "You 5" badge on their own highlight forever."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "anna")
    for name in ("a", "b", "c"):
        highlight, frame = await service.create_or_append_frame(
            author_user_id="u1",
            frame_type=HighlightFrameType.IMAGE,
            media_url=f"/api/media/{name}.webp",
        )
    await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u1")
    (own,) = await service.list_visible("u1")
    assert own["highlight"].id == highlight.id
    assert own["unseen_count"] == 0
    # Anyone else still sees what they haven't watched.
    await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u2")
    (theirs,) = await service.list_visible("u2")
    assert theirs["unseen_count"] == 2


async def test_only_the_author_learns_how_many_people_watched(db, svc):
    """``seen_by`` (people, not frame views) is the author's status line;
    a viewer's row carries ``None`` — how many others watched is not
    theirs to know."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "anna")
    await _seed_user(db, "u3", "maria")
    for name in ("a", "b"):
        _, frame = await service.create_or_append_frame(
            author_user_id="u1",
            frame_type=HighlightFrameType.IMAGE,
            media_url=f"/api/media/{name}.webp",
        )
    (own,) = await service.list_visible("u1")
    assert own["seen_by"] == 0
    await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u2")
    await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u3")
    (own,) = await service.list_visible("u1")
    assert own["seen_by"] == 2
    (theirs,) = await service.list_visible("u2")
    assert theirs["seen_by"] is None


# ── A day's audience is never silently widened ────────────────────────────


async def _first_frame(service, kind, audience=()):
    return await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/first.webp",
        audience_kind=kind,
        audience=audience,
    )


async def test_a_narrower_frame_never_joins_a_wider_highlight(db, svc):
    """Today's highlight is for everyone; a frame meant only for Anna is
    refused instead of being shown to everyone."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await _first_frame(service, HighlightAudience.ALL_PAIRED)
    captured: list[HighlightFrameAdded] = []
    bus.subscribe(HighlightFrameAdded, lambda e: captured.append(e))
    with pytest.raises(HighlightAudienceMismatchError) as exc:
        await service.create_or_append_frame(
            author_user_id="u1",
            frame_type=HighlightFrameType.IMAGE,
            media_url="/api/media/private.webp",
            audience_kind=HighlightAudience.USERS,
            audience=("u-anna",),
        )
    assert exc.value.status == 409
    assert exc.value.code == "HIGHLIGHT_AUDIENCE_MISMATCH"
    assert exc.value.params["audience_kind"] == "all_paired"
    # Nothing written, nothing published.
    assert len(await service._highlights.list_frames(highlight.id)) == 1
    assert captured == []


async def test_a_wider_frame_never_joins_a_narrower_highlight(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _first_frame(service, HighlightAudience.USERS, ("u-anna",))
    with pytest.raises(HighlightAudienceMismatchError):
        await _first_frame(service, HighlightAudience.ALL_PAIRED)
    with pytest.raises(HighlightAudienceMismatchError):
        await _first_frame(service, HighlightAudience.USERS, ("u-anna", "u-lena"))
    with pytest.raises(HighlightAudienceMismatchError):
        await _first_frame(service, HighlightAudience.HOUSEHOLDS, ("u-anna",))


async def test_the_same_audience_appends(db, svc):
    """Same kind and the same ids in any order append to today's highlight."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    first, _ = await _first_frame(service, HighlightAudience.USERS, ("a", "b"))
    second, frame = await _first_frame(service, HighlightAudience.USERS, ("b", "a"))
    assert second.id == first.id and frame.sequence == 2


async def test_a_frame_without_an_audience_joins_todays_highlight(db, svc):
    """No audience asked for: the frame goes to today's people, whatever
    the default in Settings says."""
    service, _ = svc
    await _seed_user(
        db,
        "u1",
        "pascal",
        prefs_json='{"highlights": {"default_audience": {"kind": "all_paired"}}}',
    )
    first, _ = await _first_frame(service, HighlightAudience.USERS, ("u-anna",))
    second, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/next.webp",
    )
    assert second.id == first.id and frame.sequence == 2
    assert second.audience_kind is HighlightAudience.USERS


async def test_get_for_viewer_and_get_frame(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    highlight, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    s, frames = await service.get_for_viewer(highlight.id, "u1")
    assert s.id == highlight.id and len(frames) == 1
    f = await service.get_frame(frame.id)
    assert f is not None and f.id == frame.id
    with pytest.raises(HighlightNotFoundError, match=NOT_FOUND_MESSAGE):
        await service.get_for_viewer("missing", "u1")
    assert await service.get_frame("missing") is None


async def _users_only(service, audience: tuple[str, ...] = ()):
    return await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
        audience_kind=HighlightAudience.USERS,
        audience=audience,
    )


async def test_a_non_reader_gets_not_found_and_a_reader_forbidden(db, svc):
    """Someone who may not read the highlight learns nothing — the
    unknown-id error; a reader who is not the author gets the 403."""
    service, _ = svc
    for uid, name in (("u1", "pascal"), ("u2", "maria"), ("u3", "lena")):
        await _seed_user(db, uid, name)
    highlight, frame = await _users_only(service, ("u2",))
    for actor in ("u2", "u3"):
        expected = HighlightForbiddenError if actor == "u2" else HighlightNotFoundError
        with pytest.raises(expected):
            await service.delete_highlight(
                highlight_id=highlight.id, actor_user_id=actor
            )
        with pytest.raises(expected):
            await service.delete_frame(frame_id=frame.id, actor_user_id=actor)
        with pytest.raises(expected):
            await service.share_to_feed(
                highlight_id=highlight.id,
                actor_user_id=actor,
                scope="household",
                space_id=None,
                note=None,
                feed_service=None,
                space_service=None,
            )
    with pytest.raises(HighlightNotFoundError, match=NOT_FOUND_MESSAGE):
        await service.get_for_viewer(highlight.id, "u3")


def _share_post(post_id: str, highlight_id: str, *, author: str = "u1", **kw) -> Post:
    return Post(
        id=post_id,
        author=author,
        type=PostType.HIGHLIGHT_SHARE,
        created_at=datetime.now(UTC),
        linked_highlight_id=highlight_id,
        **kw,
    )


class _SpacePosts:
    def __init__(self, posts: dict[str, tuple[str, Post]]) -> None:
        self._posts = posts

    async def get(self, post_id: str):
        return self._posts.get(post_id)


class _Spaces:
    """The three reads the share path makes of the space repo."""

    def __init__(self, spaces: dict, seats: set, banned: set = frozenset()) -> None:
        self._spaces, self._seats, self._banned = spaces, seats, banned

    async def get(self, space_id: str):
        return self._spaces.get(space_id)

    async def get_member(self, space_id: str, user_id: str):
        return object() if (space_id, user_id) in self._seats else None

    async def is_banned(self, space_id: str, user_id: str) -> bool:
        return (space_id, user_id) in self._banned


class _FeedPosts:
    def __init__(self, posts: dict[str, Post]) -> None:
        self._posts = posts

    async def get(self, post_id: str):
        return self._posts.get(post_id)


async def test_space_share_path_needs_a_live_seat_in_a_live_space(db):
    for uid, name in (("u1", "pascal"), ("u2", "maria")):
        await _seed_user(db, uid, name)
    space_posts: dict[str, tuple[str, Post]] = {}
    spaces = _Spaces(
        {
            "sp-live": SimpleNamespace(dissolved=False),
            "sp-gone": SimpleNamespace(dissolved=True),
            "sp-ban": SimpleNamespace(dissolved=False),
        },
        seats={("sp-live", "u2"), ("sp-gone", "u2"), ("sp-ban", "u2")},
        banned={("sp-ban", "u2")},
    )
    service = HighlightService(
        SqliteHighlightRepo(db),
        SqliteUserRepo(db),
        EventBus(),
        post_repo=_FeedPosts({}),
        space_post_repo=_SpacePosts(space_posts),
        space_repo=spaces,
    )
    highlight, frame = await _users_only(service)
    for space_id in ("sp-live", "sp-gone", "sp-ban", "sp-missing"):
        space_posts[f"p-{space_id}"] = (
            space_id,
            _share_post(f"p-{space_id}", highlight.id),
        )
    got, _ = await service.get_for_viewer(highlight.id, "u2", via_post_id="p-sp-live")
    assert got.id == highlight.id
    for post_id in ("p-sp-gone", "p-sp-ban", "p-sp-missing", "p-nowhere"):
        with pytest.raises(HighlightNotFoundError, match=NOT_FOUND_MESSAGE):
            await service.get_for_viewer(highlight.id, "u2", via_post_id=post_id)
    # The frame actions follow the same share.
    await service.mark_frame_viewed(
        frame_id=frame.id, viewer_user_id="u2", via_post_id="p-sp-live"
    )
    await service.react_to_frame(
        frame_id=frame.id, reactor_user_id="u2", emoji="🔥", via_post_id="p-sp-live"
    )
    await service.clear_reaction(
        frame_id=frame.id, reactor_user_id="u2", via_post_id="p-sp-live"
    )


async def test_household_share_path_refuses_unshareable_posts(db):
    """Deleted, wrong-type, other-highlight and other-author posts open nothing."""
    for uid, name in (("u1", "pascal"), ("u2", "maria")):
        await _seed_user(db, uid, name)
    posts: dict[str, Post] = {}
    service = HighlightService(
        SqliteHighlightRepo(db),
        SqliteUserRepo(db),
        EventBus(),
        post_repo=_FeedPosts(posts),
    )
    highlight, _ = await _users_only(service)
    posts["ok"] = _share_post("ok", highlight.id)
    posts["deleted"] = _share_post("deleted", highlight.id, deleted=True)
    posts["other-author"] = _share_post("other-author", highlight.id, author="u2")
    posts["other-highlight"] = _share_post("other-highlight", "h-else")
    posts["wrong-type"] = Post(
        id="wrong-type",
        author="u1",
        type=PostType.TEXT,
        created_at=datetime.now(UTC),
        linked_highlight_id=highlight.id,
    )
    got, _ = await service.get_for_viewer(highlight.id, "u2", via_post_id="ok")
    assert got.id == highlight.id
    for post_id in ("deleted", "other-author", "other-highlight", "wrong-type"):
        with pytest.raises(HighlightNotFoundError):
            await service.get_for_viewer(highlight.id, "u2", via_post_id=post_id)
    # A share never opens an unknown highlight.
    with pytest.raises(HighlightNotFoundError):
        await service.get_for_viewer("missing", "u2", via_post_id="ok")


async def test_unwired_share_path_opens_nothing(db, svc):
    service, _ = svc
    for uid, name in (("u1", "pascal"), ("u2", "maria")):
        await _seed_user(db, uid, name)
    highlight, _ = await _users_only(service)
    with pytest.raises(HighlightNotFoundError):
        await service.get_for_viewer(highlight.id, "u2", via_post_id="any-post")


async def test_share_to_feed_household(db, svc):
    """share_to_feed routes household scope through feed_service.create_post."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )

    class FakeFeedService:
        def __init__(self):
            self.calls = []

        async def create_post(self, **kwargs):
            self.calls.append(kwargs)
            return type("P", (), {"id": "post-1"})()

    feed = FakeFeedService()
    post = await service.share_to_feed(
        highlight_id=highlight.id,
        actor_user_id="u1",
        scope="household",
        space_id=None,
        note="hello",
        feed_service=feed,
        space_service=None,
    )
    assert post.id == "post-1"
    assert feed.calls[0]["type"] == "highlight_share"
    assert feed.calls[0]["linked_highlight_id"] == highlight.id


async def test_share_to_feed_rejects_non_author(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    highlight, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    with pytest.raises(HighlightForbiddenError):
        await service.share_to_feed(
            highlight_id=highlight.id,
            actor_user_id="u2",
            scope="household",
            space_id=None,
            note=None,
            feed_service=None,
            space_service=None,
        )


async def test_share_to_feed_unknown_scope(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    with pytest.raises(ValueError):
        await service.share_to_feed(
            highlight_id=highlight.id,
            actor_user_id="u1",
            scope="bogus",
            space_id=None,
            note=None,
            feed_service=None,
            space_service=None,
        )


async def test_dm_reply_to_frame_calls_dm_service_with_snapshot(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
        caption_text="from the trip",
        caption_emoji="🏖",
    )

    class FakeDmService:
        def __init__(self):
            self.kwargs = None

        async def send_message(self, conversation_id, **kw):
            self.kwargs = {"conversation_id": conversation_id, **kw}
            return type("M", (), {"id": "msg-1"})()

    dm = FakeDmService()
    msg = await service.dm_reply_to_frame(
        frame_id=frame.id,
        sender_user_id="u2",
        conversation_id="conv-1",
        content="lol nice",
        dm_service=dm,
    )
    assert msg.id == "msg-1"
    assert dm.kwargs["sender_username"] == "maria"
    assert dm.kwargs["reply_to_highlight_frame_id"] == frame.id
    snapshot_json = dm.kwargs["reply_to_highlight_frame_snapshot"]
    assert "x.webp" in snapshot_json
    assert "🏖" in snapshot_json


async def test_expire_due_drops_expired(db, svc):
    """``expire_due`` runs prune_expired and reports counts."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    repo = service._highlights
    # Inject one stale row by hand so prune_expired finds work.
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-01-01",
        expires_at=past,
    )
    expired, over_max = await service.expire_due()
    assert expired == 1
    assert over_max == 0  # max_count default (100) is well above one row


async def test_expire_due_runs_over_max_pass_per_author(db, svc):
    """``expire_due`` walks every author with live rows so per-author
    ``max_count`` retention prunes too — covers the per-author lookup +
    prefs parse + prune_over_max call inside :meth:`expire_due`."""
    service, _ = svc
    # Seed a user with the lowest-allowed ``max_count`` (10 — clamped by
    # ``parse_highlights_preferences``) so 11 rows trigger one prune.
    await _seed_user(db, "u1", "pascal", prefs_json='{"highlights": {"max_count": 10}}')
    repo = service._highlights
    future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    for i in range(11):
        await repo.find_or_create_today(
            author_user_id="u1",
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
            highlight_date=f"2026-01-{i + 1:02d}",
            expires_at=future,
        )
    expired, over_max = await service.expire_due()
    assert expired == 0
    assert over_max == 1


async def test_expire_due_removes_expired_frame_files(db, tmp_dir):
    """Retention expiry unlinks the frame media of expired highlights."""
    media_dir = tmp_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    service = HighlightService(
        SqliteHighlightRepo(db),
        SqliteUserRepo(db),
        EventBus(),
        media_dir=media_dir,
        media_refs=SqliteMediaReferenceRepo(db),
    )
    await _seed_user(db, "u1", "pascal")
    (media_dir / "e.webp").write_bytes(b"x")
    h, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="api/media/e.webp",
    )
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await db.enqueue("UPDATE highlights SET expires_at=? WHERE id=?", (past, h.id))
    assert (media_dir / "e.webp").exists()
    expired, _ = await service.expire_due()
    assert expired == 1
    assert not (media_dir / "e.webp").exists()


async def test_expire_due_removes_over_max_frame_files(db, tmp_dir):
    """The per-author over-max prune unlinks the dropped frames' media."""
    media_dir = tmp_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    service = HighlightService(
        SqliteHighlightRepo(db),
        SqliteUserRepo(db),
        EventBus(),
        media_dir=media_dir,
        media_refs=SqliteMediaReferenceRepo(db),
    )
    # max_count is clamped to a floor of 10, so 11 rows trigger one prune.
    await _seed_user(db, "u1", "pascal", prefs_json='{"highlights": {"max_count": 10}}')
    repo = service._highlights
    future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    (media_dir / "old.webp").write_bytes(b"x")
    # Oldest date sorts last under DESC ordering → it's the one dropped.
    oldest = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-01-01",
        expires_at=future,
    )
    await repo.append_frame(
        highlight_id=oldest.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="api/media/old.webp",
    )
    for i in range(2, 12):  # 10 newer highlights
        await repo.find_or_create_today(
            author_user_id="u1",
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
            highlight_date=f"2026-02-{i:02d}",
            expires_at=future,
        )
    assert (media_dir / "old.webp").exists()
    _, over_max = await service.expire_due()
    assert over_max == 1
    assert not (media_dir / "old.webp").exists()


async def test_create_or_append_frame_unknown_author_raises_lookup(db, svc):
    """The author existence check must raise ``LookupError`` when the
    user row is missing — covers the early-return branch."""
    service, _ = svc
    with pytest.raises(LookupError):
        await service.create_or_append_frame(
            author_user_id="ghost",
            frame_type=HighlightFrameType.IMAGE,
            media_url="/api/media/x.webp",
        )


# ── Local media only (docs/principles.md "No third-party fetches") ───────


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://example.invalid/img.jpg",
        "//evil.example/x.webp",
        "javascript:alert(1)",
        "api/media/../secret",
        "/media/x.webp",
    ],
)
async def test_frame_with_non_local_media_is_refused(db, svc, bad_url):
    """A frame must point at a local upload: a third-party URL would be
    fetched by every viewer's browser (IP leak) and every receiving
    household drops it, so the highlight would never federate."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    captured: list[HighlightFrameAdded] = []
    bus.subscribe(HighlightFrameAdded, lambda e: captured.append(e))
    with pytest.raises(InvalidMediaRefError) as ei:
        await service.create_or_append_frame(
            author_user_id="u1",
            frame_type=HighlightFrameType.IMAGE,
            media_url=bad_url,
        )
    assert (ei.value.status, ei.value.code) == (422, "INVALID_MEDIA_URL")
    assert captured == []
    # No empty highlight row is left behind either.
    assert await SqliteHighlightRepo(db).list_authored("u1") == []


async def test_frame_keeps_local_media_ref_verbatim(db, svc):
    """The stored value is the one the client sent — receivers keep the
    federated copy verbatim too, so both sides hold the same string."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    assert frame.media_url == "/api/media/a.webp"


class _Clock(datetime):
    """``datetime.now`` the test moves across UTC midnight."""

    current = datetime(2026, 10, 25, 23, 59, 50, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current


async def test_a_frame_naming_todays_audience_after_midnight_keeps_it(
    db, svc, monkeypatch
):
    """The composer shows "Today's highlight is for Anna" and sends that
    audience with every frame; when the frame lands after UTC midnight the
    new day's highlight is for Anna too — never the Settings default."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")  # Settings default: all_paired
    monkeypatch.setattr(highlight_service_module, "datetime", _Clock)
    _Clock.current = datetime(2026, 10, 25, 23, 59, 50, tzinfo=UTC)
    first, _ = await _first_frame(service, HighlightAudience.USERS, ("u-anna",))
    _Clock.current = datetime(2026, 10, 26, 0, 0, 5, tzinfo=UTC)
    second, frame = await _first_frame(service, HighlightAudience.USERS, ("u-anna",))
    assert second.id != first.id and second.highlight_date == "2026-10-26"
    assert second.audience_kind is HighlightAudience.USERS
    assert second.audience == ("u-anna",)
    assert frame.sequence == 1


async def test_concurrent_first_frames_never_fail_with_a_500(db, svc):
    """Two first frames of the day racing on UNIQUE(author, date): the loser
    reads the winner's highlight — appends with the same audience, a clean
    409 with another — instead of an IntegrityError."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "anna")
    results = await asyncio.gather(
        _first_frame(service, HighlightAudience.ALL_PAIRED),
        _first_frame(service, HighlightAudience.USERS, ("u-anna",)),
        return_exceptions=True,
    )
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds == ["HighlightAudienceMismatchError", "tuple"], results
    same = await asyncio.gather(
        service.create_or_append_frame(
            author_user_id="u2",
            frame_type=HighlightFrameType.IMAGE,
            media_url="/api/media/x.webp",
            audience_kind=HighlightAudience.ALL_PAIRED,
        ),
        service.create_or_append_frame(
            author_user_id="u2",
            frame_type=HighlightFrameType.IMAGE,
            media_url="/api/media/y.webp",
            audience_kind=HighlightAudience.ALL_PAIRED,
        ),
        return_exceptions=True,
    )
    assert all(isinstance(r, tuple) for r in same), same
    assert same[0][0].id == same[1][0].id


# ─── Change for all of today (owner decision 1) ─────────────────────────


async def _audience_changes(bus) -> list:
    captured: list[HighlightAudienceChanged] = []
    bus.subscribe(HighlightAudienceChanged, lambda e: captured.append(e))
    return captured


async def test_the_author_changes_todays_audience_for_every_frame(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    highlight, first = await _first_frame(service, HighlightAudience.ALL_PAIRED)
    _, second = await _first_frame(service, HighlightAudience.ALL_PAIRED)
    captured = await _audience_changes(bus)

    changed = await service.change_audience(
        highlight_id=highlight.id,
        actor_user_id="u1",
        audience_kind=HighlightAudience.USERS,
        audience=("u-anna",),
    )
    assert changed.audience_kind is HighlightAudience.USERS
    assert changed.audience == ("u-anna",)
    stored = await service._highlights.get_highlight(highlight.id)
    assert stored.audience == ("u-anna",)
    [event] = captured
    assert (event.old_audience_kind, event.old_audience) == ("all_paired", ())
    assert (event.audience_kind, event.audience) == ("users", ("u-anna",))
    assert [f.id for f in event.frames] == [first.id, second.id]
    assert event.highlight_date == highlight.highlight_date
    assert event.expires_at == highlight.expires_at
    # Later frames name the new audience and join today's highlight.
    again, frame = await _first_frame(service, HighlightAudience.USERS, ("u-anna",))
    assert again.id == highlight.id and frame.sequence == 3


async def test_all_paired_drops_picked_ids(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await _first_frame(service, HighlightAudience.USERS, ("u-anna",))
    changed = await service.change_audience(
        highlight_id=highlight.id,
        actor_user_id="u1",
        audience_kind="all_paired",
        audience=("u-anna",),
    )
    assert changed.audience_kind is HighlightAudience.ALL_PAIRED
    assert changed.audience == ()


async def test_the_same_audience_changes_nothing(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await _first_frame(service, HighlightAudience.USERS, ("a", "b"))
    captured = await _audience_changes(bus)
    same = await service.change_audience(
        highlight_id=highlight.id,
        actor_user_id="u1",
        audience_kind=HighlightAudience.USERS,
        audience=("b", "a"),
    )
    assert same.audience == ("a", "b")
    assert captured == []


async def test_nobody_picked_is_refused(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await _first_frame(service, HighlightAudience.ALL_PAIRED)
    captured = await _audience_changes(bus)
    with pytest.raises(HighlightAudienceEmptyError) as exc:
        await service.change_audience(
            highlight_id=highlight.id,
            actor_user_id="u1",
            audience_kind=HighlightAudience.USERS,
            audience=(),
        )
    assert exc.value.status == 422
    assert captured == []
    # "Your household only" is households with none listed — allowed.
    own = await service.change_audience(
        highlight_id=highlight.id,
        actor_user_id="u1",
        audience_kind=HighlightAudience.HOUSEHOLDS,
        audience=(),
    )
    assert own.audience_kind is HighlightAudience.HOUSEHOLDS


async def test_only_the_author_changes_the_audience(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "anna")
    await _seed_user(db, "u3", "lena")
    highlight, _ = await _first_frame(service, HighlightAudience.USERS, ("u2",))
    captured = await _audience_changes(bus)
    # Anna can read it: she is told no. Lena can't: the unknown-id 404.
    with pytest.raises(HighlightForbiddenError):
        await service.change_audience(
            highlight_id=highlight.id,
            actor_user_id="u2",
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
        )
    with pytest.raises(HighlightNotFoundError):
        await service.change_audience(
            highlight_id=highlight.id,
            actor_user_id="u3",
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
        )
    stored = await service._highlights.get_highlight(highlight.id)
    assert stored.audience == ("u2",)
    assert captured == []


async def test_an_earlier_days_highlight_keeps_its_audience(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await _first_frame(service, HighlightAudience.USERS, ("u-anna",))
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    await db.enqueue(
        "UPDATE highlights SET highlight_date=? WHERE id=?", (yesterday, highlight.id)
    )
    captured = await _audience_changes(bus)
    with pytest.raises(HighlightNotTodayError) as exc:
        await service.change_audience(
            highlight_id=highlight.id,
            actor_user_id="u1",
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
        )
    assert exc.value.status == 409
    assert exc.value.code == "HIGHLIGHT_NOT_TODAY"
    assert captured == []


async def test_a_change_the_gate_refuses_stores_and_sends_nothing(db, svc):
    """v_70: a narrowing that would take people away from a household too
    old to keep the removal is refused before anything is written."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await _first_frame(service, HighlightAudience.ALL_PAIRED)
    captured = await _audience_changes(bus)
    asked: list[tuple] = []

    async def _gate(old_kind, old, kind, new) -> bool:
        asked.append((old_kind, old, kind, new))
        return False

    service.attach_audience_gate(_gate)
    with pytest.raises(HighlightAudiencePeerTooOldError) as exc:
        await service.change_audience(
            highlight_id=highlight.id,
            actor_user_id="u1",
            audience_kind=HighlightAudience.HOUSEHOLDS,
            audience=("peer-b",),
        )
    assert exc.value.status == 409
    assert asked == [("all_paired", (), "households", ("peer-b",))]
    stored = await service._highlights.get_highlight(highlight.id)
    assert stored.audience_kind is HighlightAudience.ALL_PAIRED
    assert captured == []


async def test_a_frame_and_a_change_never_interleave(db, svc):
    """Appending a frame and changing the audience serialize per author: the
    frame's event names the audience it is stored under."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await _first_frame(service, HighlightAudience.ALL_PAIRED)
    order: list[str] = []
    gate_open = asyncio.Event()

    async def _slow_gate(*_a) -> bool:
        order.append("change: gate")
        await gate_open.wait()
        return True

    service.attach_audience_gate(_slow_gate)
    frames: list[HighlightFrameAdded] = []
    bus.subscribe(HighlightFrameAdded, lambda e: frames.append(e))
    change = asyncio.create_task(
        service.change_audience(
            highlight_id=highlight.id,
            actor_user_id="u1",
            audience_kind=HighlightAudience.HOUSEHOLDS,
            audience=("peer-b",),
        )
    )
    await asyncio.sleep(0)
    append = asyncio.create_task(
        _first_frame(service, HighlightAudience.HOUSEHOLDS, ("peer-b",))
    )
    await asyncio.sleep(0.01)
    assert frames == []  # the frame waits for the change
    gate_open.set()
    await change
    await append
    [event] = frames
    assert (event.audience_kind, event.audience) == ("households", ("peer-b",))


async def test_a_second_frame_does_not_wait_for_a_slow_publish(db, svc):
    """F15: the first frame's event is still being handled (federation is
    trying a household that doesn't answer). The author's next frame is
    stored and returns at once, and its event follows the first one."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    release = asyncio.Event()
    seen: list[int] = []

    async def _slow_household(e: HighlightFrameAdded) -> None:
        if e.sequence == 1:
            await release.wait()
        seen.append(e.sequence)

    bus.subscribe(HighlightFrameAdded, _slow_household)
    first = asyncio.create_task(_first_frame(service, HighlightAudience.ALL_PAIRED))
    await asyncio.sleep(0.01)
    assert not first.done()  # a lone frame still returns once announced
    _, second = await asyncio.wait_for(
        _first_frame(service, HighlightAudience.ALL_PAIRED), timeout=1
    )
    assert second.sequence == 2
    assert seen == []
    release.set()
    await first
    await service.flush_published()
    assert seen == [1, 2]


async def test_a_change_and_a_delete_do_not_wait_and_keep_their_order(db, svc):
    """An audience change and a delete behind a slow frame publish return
    at once; the events still go out frame → change → removal."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    highlight, _ = await _first_frame(service, HighlightAudience.ALL_PAIRED)
    release = asyncio.Event()
    order: list[str] = []

    async def _slow_frame(e: HighlightFrameAdded) -> None:
        await release.wait()
        order.append(f"frame {e.sequence}")

    bus.subscribe(HighlightFrameAdded, _slow_frame)
    bus.subscribe(
        HighlightAudienceChanged, lambda e: order.append(f"change {e.audience_kind}")
    )
    bus.subscribe(HighlightRemoved, lambda e: order.append("removed"))
    second = asyncio.create_task(_first_frame(service, HighlightAudience.ALL_PAIRED))
    await asyncio.sleep(0.01)
    await asyncio.wait_for(
        service.change_audience(
            highlight_id=highlight.id,
            actor_user_id="u1",
            audience_kind=HighlightAudience.HOUSEHOLDS,
            audience=("peer-b",),
        ),
        timeout=1,
    )
    await asyncio.wait_for(
        service.delete_highlight(highlight_id=highlight.id, actor_user_id="u1"),
        timeout=1,
    )
    assert order == []
    release.set()
    await second
    await service.flush_published()
    assert order == ["frame 2", "change households", "removed"]


async def test_stop_publishes_what_is_queued(db, svc):
    """Shutdown sends the events already decided before the app goes."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    release = asyncio.Event()
    seen: list[int] = []

    async def _slow(e: HighlightFrameAdded) -> None:
        if e.sequence == 1:
            await release.wait()
        seen.append(e.sequence)

    bus.subscribe(HighlightFrameAdded, _slow)
    first = asyncio.create_task(_first_frame(service, HighlightAudience.ALL_PAIRED))
    await asyncio.sleep(0.01)
    await _first_frame(service, HighlightAudience.ALL_PAIRED)
    asyncio.get_running_loop().call_later(0.02, release.set)
    await service.stop(timeout=5)
    await first
    assert seen == [1, 2]
