"""Tests for socialhome.services.moment_service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.errors import InvalidMediaRefError
from socialhome.domain.moment import Moment
from socialhome.federation.owner_bound_id import (
    MOMENT_KIND,
    OwnerBinding,
    check_owner_bound_id,
)
from socialhome.domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.moment_service import (
    MomentNotFoundError,
    MomentRateLimitError,
    MomentService,
)
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    moment_repo = SqliteMomentRepo(db)
    moment_svc = MomentService(
        moment_repo,
        user_repo,
        bus,
        own_instance_id=iid,
    )

    class S:
        pass

    s = S()
    s.db = db
    s.bus = bus
    s.user_svc = user_svc
    s.moment_repo = moment_repo
    s.moment_svc = moment_svc
    s.iid = iid

    async def provision(name):
        return await user_svc.provision(username=name, display_name=name.title())

    s.provision = provision
    yield s
    await db.shutdown()


# ── Validation ────────────────────────────────────────────────────────────


async def test_create_text_moment_persists_and_publishes(stack):
    a = await stack.provision("alice")
    captured: list[MomentCreated] = []
    stack.bus.subscribe(MomentCreated, captured.append)
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="hello")
    assert m.author_user_id == a.user_id
    assert m.content == "hello"
    assert m.origin_instance_id == stack.iid
    assert len(captured) == 1


async def test_a_moment_id_commits_to_its_author(stack):
    """v_36: the id is owner-bound to the author (no space), so no other
    household can announce it first as a moment of its own user."""
    a = await stack.provision("alice")
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="hi")
    assert (
        check_owner_bound_id(MOMENT_KIND, m.id, space_id="", owner_user_id=a.user_id)
        is OwnerBinding.VALID
    )
    assert (
        check_owner_bound_id(MOMENT_KIND, m.id, space_id="", owner_user_id="u-x")
        is OwnerBinding.MISMATCH
    )


async def test_delete_and_expiry_remove_media_file(stack, tmp_dir):
    """A moment's backing file is unlinked on delete and on retention expiry."""
    media_dir = tmp_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    svc = MomentService(
        stack.moment_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id=stack.iid,
        media_dir=media_dir,
        media_refs=SqliteMediaReferenceRepo(stack.db),
    )
    a = await stack.provision("alice")

    # delete path
    (media_dir / "a.webm").write_bytes(b"x")
    m = await svc.create_moment(
        author_user_id=a.user_id,
        content="",
        media_url="api/media/a.webm",
    )
    assert (media_dir / "a.webm").exists()
    await svc.delete_moment(m.id, actor_user_id=a.user_id)
    assert not (media_dir / "a.webm").exists()

    # expiry path — insert an already-expired row with media, then prune
    (media_dir / "b.webm").write_bytes(b"y")
    expired = Moment(
        id="m-exp",
        author_user_id=a.user_id,
        content="old",
        media_url="api/media/b.webm",
        media_type="video",
        duration_ms=1000,
        parent_moment_id=None,
        origin_instance_id=stack.iid,
        created_at=(datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),
        expires_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    )
    await stack.moment_repo.save(expired)
    assert (media_dir / "b.webm").exists()
    await svc.expire_due()
    assert not (media_dir / "b.webm").exists()


async def test_empty_text_and_no_media_rejected(stack):
    a = await stack.provision("alice")
    with pytest.raises(ValueError):
        await stack.moment_svc.create_moment(author_user_id=a.user_id, content="")


async def test_content_over_1000_chars_rejected(stack):
    a = await stack.provision("alice")
    with pytest.raises(ValueError):
        await stack.moment_svc.create_moment(
            author_user_id=a.user_id, content="x" * 1001
        )


async def test_video_over_15s_rejected(stack):
    a = await stack.provision("alice")
    with pytest.raises(ValueError):
        await stack.moment_svc.create_moment(
            author_user_id=a.user_id,
            content="",
            media_url="/api/media/clip.webm",
            media_type="video",
            duration_ms=20_000,
        )


async def test_video_at_max_15s_accepted(stack):
    a = await stack.provision("alice")
    m = await stack.moment_svc.create_moment(
        author_user_id=a.user_id,
        content="",
        media_url="/api/media/clip.webm",
        media_type="video",
        duration_ms=15_000,
    )
    assert m.duration_ms == 15_000


async def test_video_without_duration_rejected(stack):
    a = await stack.provision("alice")
    with pytest.raises(ValueError):
        await stack.moment_svc.create_moment(
            author_user_id=a.user_id,
            content="",
            media_url="/api/media/clip.webm",
            media_type="video",
        )


# ── Rate limit ────────────────────────────────────────────────────────────


async def test_top_level_rate_limit_kicks_in_within_15min(stack):
    a = await stack.provision("alice")
    await stack.moment_svc.create_moment(author_user_id=a.user_id, content="one")
    with pytest.raises(MomentRateLimitError):
        await stack.moment_svc.create_moment(author_user_id=a.user_id, content="two")


async def test_replies_skip_rate_limit(stack):
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    parent = await stack.moment_svc.create_moment(
        author_user_id=a.user_id, content="root"
    )
    # alice already has one top-level; replying again is fine.
    reply1 = await stack.moment_svc.create_moment(
        author_user_id=a.user_id,
        content="self reply",
        parent_moment_id=parent.id,
    )
    assert reply1.parent_moment_id == parent.id
    # bob can reply too without their own rate-limit firing.
    await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="me too",
        parent_moment_id=parent.id,
    )


async def test_reply_to_reply_attaches_to_root(stack):
    """Threads stay flat — replies-to-replies use the original parent."""
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    root = await stack.moment_svc.create_moment(
        author_user_id=a.user_id, content="root"
    )
    r1 = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="r1",
        parent_moment_id=root.id,
    )
    r2 = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="r2",
        parent_moment_id=r1.id,
    )
    # r2 was attached to root, not r1.
    assert r2.parent_moment_id == root.id


# ── Reactions ─────────────────────────────────────────────────────────────


async def test_react_persists_and_publishes(stack):
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="hi")
    captured: list[MomentReactionChanged] = []
    stack.bus.subscribe(MomentReactionChanged, captured.append)
    await stack.moment_svc.react(m.id, reactor_user_id=b.user_id, emoji="🔥")
    rs = await stack.moment_repo.list_reactions(m.id)
    assert [r.emoji for r in rs] == ["🔥"]
    assert len(captured) == 1 and captured[0].emoji == "🔥"
    # Clear → emoji=None on the published event.
    await stack.moment_svc.clear_reaction(m.id, reactor_user_id=b.user_id)
    assert await stack.moment_repo.list_reactions(m.id) == []
    assert captured[-1].emoji is None


# ── Delete + permissions ──────────────────────────────────────────────────


async def test_only_author_or_admin_can_delete(stack):
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="mine")
    with pytest.raises(PermissionError):
        await stack.moment_svc.delete_moment(m.id, actor_user_id=b.user_id)
    captured: list[MomentDeleted] = []
    stack.bus.subscribe(MomentDeleted, captured.append)
    await stack.moment_svc.delete_moment(m.id, actor_user_id=a.user_id)
    assert await stack.moment_repo.get(m.id) is None
    assert len(captured) == 1


async def test_admin_can_delete_someone_elses_moment(stack):
    a = await stack.provision("alice")
    moderator = await stack.provision("moderator")
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="x")
    await stack.moment_svc.delete_moment(
        m.id,
        actor_user_id=moderator.user_id,
        actor_is_admin=True,
    )
    assert await stack.moment_repo.get(m.id) is None


# ── Reads + retention scheduler ──────────────────────────────────────────


async def test_get_unknown_moment_raises(stack):
    with pytest.raises(MomentNotFoundError):
        await stack.moment_svc.get_moment("ghost")


async def test_react_with_empty_emoji_rejected(stack):
    a = await stack.provision("alice")
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="hi")
    with pytest.raises(ValueError):
        await stack.moment_svc.react(m.id, reactor_user_id=a.user_id, emoji="")


async def test_invalid_media_type_rejected(stack):
    a = await stack.provision("alice")
    with pytest.raises(ValueError):
        await stack.moment_svc.create_moment(
            author_user_id=a.user_id,
            content="hi",
            media_url="/api/media/x.bin",
            media_type="audio",
        )


async def test_image_rejects_duration_silently(stack):
    """``duration_ms`` only applies to videos — silently dropped otherwise."""
    a = await stack.provision("alice")
    m = await stack.moment_svc.create_moment(
        author_user_id=a.user_id,
        content="x",
        media_url="/api/media/x.webp",
        media_type="image",
        duration_ms=3000,
    )
    assert m.duration_ms is None


async def test_reply_to_unknown_parent_raises(stack):
    a = await stack.provision("alice")
    with pytest.raises(MomentNotFoundError):
        await stack.moment_svc.create_moment(
            author_user_id=a.user_id,
            content="orphan",
            parent_moment_id="ghost",
        )


async def test_clear_reaction_on_unknown_moment_raises(stack):
    a = await stack.provision("alice")
    with pytest.raises(MomentNotFoundError):
        await stack.moment_svc.clear_reaction("ghost", reactor_user_id=a.user_id)


async def test_list_replies_returns_chronological(stack):
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    root = await stack.moment_svc.create_moment(
        author_user_id=a.user_id, content="root"
    )
    r1 = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="r1",
        parent_moment_id=root.id,
    )
    r2 = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="r2",
        parent_moment_id=root.id,
    )
    replies = await stack.moment_svc.list_replies(root.id)
    assert [m.id for m in replies] == [r1.id, r2.id]


async def test_delete_unknown_moment_raises(stack):
    a = await stack.provision("alice")
    with pytest.raises(MomentNotFoundError):
        await stack.moment_svc.delete_moment("ghost", actor_user_id=a.user_id)


async def test_attach_instance_id_late_binding(stack):
    """Service constructed without an instance id, then bound later."""
    bus = stack.bus
    user_repo = stack.user_svc._repo
    moment_repo = stack.moment_repo
    svc = MomentService(moment_repo, user_repo, bus)
    svc.attach_instance_id("self-late")
    a = await stack.provision("zoe")
    m = await svc.create_moment(author_user_id=a.user_id, content="late")
    assert m.origin_instance_id == "self-late"


async def test_expire_due_drops_expired(stack):
    """Insert a row directly with a past expires_at and verify the
    scheduler hook prunes it without affecting fresh rows."""
    fresh = await stack.moment_svc.create_moment(
        author_user_id=(await stack.provision("alice")).user_id,
        content="fresh",
    )
    # Hand-write an expired row.
    expired_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    await stack.db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id, "
        "created_at, expires_at) VALUES(?,?,?,?,?,?)",
        ("m-stale", "u-stale", "old", stack.iid, expired_at, expired_at),
    )
    pruned = await stack.moment_svc.expire_due()
    assert pruned == 1
    assert await stack.moment_repo.get("m-stale") is None
    assert await stack.moment_repo.get(fresh.id) is not None


# ── Local media only (docs/principles.md "No third-party fetches") ───────


@pytest.mark.parametrize(
    "bad_url",
    ["https://example.invalid/img.jpg", "//evil.example/x.webp", "/media/x.webp"],
)
async def test_moment_with_non_local_media_is_refused(stack, bad_url):
    """A moment's media must be a local upload: a third-party URL leaks
    every viewer's IP and receivers drop it, so it would never federate."""
    a = await stack.provision("alice")
    captured: list[MomentCreated] = []
    stack.bus.subscribe(MomentCreated, captured.append)
    with pytest.raises(InvalidMediaRefError) as ei:
        await stack.moment_svc.create_moment(
            author_user_id=a.user_id,
            content="look",
            media_url=bad_url,
            media_type="image",
        )
    assert (ei.value.status, ei.value.code) == (422, "INVALID_MEDIA_URL")
    assert captured == []


async def test_moment_keeps_local_media_ref_verbatim(stack):
    a = await stack.provision("alice")
    m = await stack.moment_svc.create_moment(
        author_user_id=a.user_id,
        content="",
        media_url="api/media/pic.webp",
        media_type="image",
    )
    assert m.media_url == "api/media/pic.webp"


async def test_moment_empty_media_url_counts_as_no_media(stack):
    """An empty ``media_url`` is "no media", not a bad reference."""
    a = await stack.provision("alice")
    m = await stack.moment_svc.create_moment(
        author_user_id=a.user_id, content="text only", media_url=""
    )
    assert m.media_url is None
    with pytest.raises(ValueError):
        await stack.moment_svc.create_moment(
            author_user_id=a.user_id, content="", media_url=""
        )
