"""Service-level tests for the highlights pillar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.errors import InvalidMediaRefError
from socialhome.domain.events import (
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameViewed,
)
from socialhome.domain.highlight import HighlightAudience, HighlightFrameType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.highlight_repo import SqliteHighlightRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.highlight_service import (
    MAX_FRAMES_PER_HIGHLIGHT,
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
    cutoff_30d = datetime.now(timezone.utc) + timedelta(days=29)
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


async def test_get_with_frames_and_get_frame(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    highlight, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    pair = await service.get_with_frames(highlight.id)
    assert pair is not None
    s, frames = pair
    assert s.id == highlight.id and len(frames) == 1
    f = await service.get_frame(frame.id)
    assert f is not None and f.id == frame.id
    # Missing ids return None / empty.
    assert await service.get_with_frames("missing") is None
    assert await service.get_frame("missing") is None


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
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
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
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
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
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
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
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
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
