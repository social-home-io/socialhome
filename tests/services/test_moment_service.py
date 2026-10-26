"""Tests for socialhome.services.moment_service."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.errors import InvalidMediaRefError
from socialhome.domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.moment import (
    Moment,
    MomentOwnSaveError,
    MomentReactionInvalidError,
    MomentSaveUnavailableError,
)
from socialhome.federation.owner_bound_id import (
    MOMENT_KIND,
    OwnerBinding,
    check_owner_bound_id,
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

    # expiry path — an already-expired copy received from another
    # household, with media, then prune
    (media_dir / "b.webm").write_bytes(b"y")
    (media_dir / "c.webm").write_bytes(b"z")
    expired = Moment(
        id="m-exp",
        author_user_id="u-peer",
        content="old",
        media_url="api/media/b.webm",
        media_type="video",
        duration_ms=1000,
        parent_moment_id=None,
        origin_instance_id="peer",
        created_at=(datetime.now(UTC) - timedelta(days=9)).isoformat(),
        expires_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        received_via="household",
    )
    await stack.moment_repo.save(expired)
    # …and the author's own expired moment, kept in their archive.
    await stack.moment_repo.save(
        replace(
            expired,
            id="m-own",
            author_user_id=a.user_id,
            origin_instance_id=stack.iid,
            media_url="api/media/c.webm",
            received_via="self",
        )
    )
    assert (media_dir / "b.webm").exists()
    assert await svc.expire_due() == 1
    assert not (media_dir / "b.webm").exists()
    assert (media_dir / "c.webm").exists()
    assert (await stack.moment_repo.get("m-own")) is not None


def _archived(author_user_id: str, iid: str, id: str = "m-arch") -> Moment:
    """The author's own moment, past its expiry (kept in their archive)."""
    return Moment(
        id=id,
        author_user_id=author_user_id,
        content="last week",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id=iid,
        created_at=(datetime.now(UTC) - timedelta(days=9)).isoformat(),
        expires_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
    )


async def test_the_author_opens_an_archived_moment_others_cannot(stack):
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    await stack.moment_repo.save(_archived(a.user_id, stack.iid))
    got = await stack.moment_svc.get_for_viewer("m-arch", a.user_id)
    assert got.content == "last week"
    with pytest.raises(MomentNotFoundError):
        await stack.moment_svc.get_for_viewer("m-arch", b.user_id)


async def test_no_reply_to_an_archived_moment(stack):
    """A reply would fan out to households that no longer hold the
    moment — replies close when the moment ends."""
    a = await stack.provision("alice")
    await stack.moment_repo.save(_archived(a.user_id, stack.iid))
    with pytest.raises(ValueError, match="archive"):
        await stack.moment_svc.create_moment(
            author_user_id=a.user_id, content="me again", parent_moment_id="m-arch"
        )


async def test_the_author_deletes_an_archived_moment(stack):
    a = await stack.provision("alice")
    await stack.moment_repo.save(_archived(a.user_id, stack.iid))
    await stack.moment_svc.delete_moment("m-arch", actor_user_id=a.user_id)
    assert await stack.moment_repo.get("m-arch") is None
    # The tombstone keeps its past expiry, so the next sweep takes it.
    assert await stack.moment_svc.expire_due() == 1


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


# ── Replies never go public on their own ──────────────────────────────────


async def test_reply_to_household_moment_is_never_public(stack):
    """A reply can't publish what its parent keeps in the household: an
    ``is_public`` reply to a non-public moment is stored (and so
    published) as household-only."""
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    root = await stack.moment_svc.create_moment(
        author_user_id=a.user_id, content="household only"
    )
    reply = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="reply",
        parent_moment_id=root.id,
        is_public=True,
    )
    assert reply.is_public is False
    stored = await stack.moment_repo.get(reply.id)
    assert stored is not None and stored.is_public is False


async def test_reply_to_reply_follows_the_root_publicness(stack):
    """Threads are flat: the root decides, not the reply being answered."""
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    root = await stack.moment_svc.create_moment(
        author_user_id=a.user_id, content="household only"
    )
    r1 = await stack.moment_svc.create_moment(
        author_user_id=b.user_id, content="r1", parent_moment_id=root.id
    )
    r2 = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="r2",
        parent_moment_id=r1.id,
        is_public=True,
    )
    assert r2.is_public is False


async def test_reply_to_public_moment_may_be_public(stack):
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    root = await stack.moment_svc.create_moment(
        author_user_id=a.user_id, content="public", is_public=True
    )
    reply = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="reply",
        parent_moment_id=root.id,
        is_public=True,
    )
    assert reply.is_public is True


async def test_reply_to_a_remote_moment_marked_public_may_be_public(stack):
    """A public moment that reached us through pairing first is marked
    public once its GFS copy arrives (``mark_public``); a reply to it can
    then be public, like a reply to any public moment."""
    b = await stack.provision("bob")
    now = datetime.now(UTC)
    await stack.moment_repo.save(
        Moment(
            id="m-remote",
            author_user_id="u-noor",
            content="bees",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="inst-noor",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            received_via="household",
        )
    )
    first = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="before",
        parent_moment_id="m-remote",
        is_public=True,
    )
    assert first.is_public is False
    await stack.moment_repo.mark_public("m-remote")
    stack_reply = await stack.moment_svc.create_moment(
        author_user_id=b.user_id,
        content="after",
        parent_moment_id="m-remote",
        is_public=True,
    )
    assert stack_reply.is_public is True


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
    # v_66: a private moment's delete says so — no GFS hears of it.
    assert captured[0].is_public is False
    public = await stack.moment_svc.create_moment(
        author_user_id=b.user_id, content="public", is_public=True
    )
    await stack.moment_svc.delete_moment(public.id, actor_user_id=b.user_id)
    assert captured[-1].is_public is True


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
    with pytest.raises(MomentReactionInvalidError):
        await stack.moment_svc.react(m.id, reactor_user_id=a.user_id, emoji="")


@pytest.mark.parametrize("emoji", ["hello", "❤️❤️", "x" * 40, "🔥" * 9])
async def test_react_takes_one_emoji_of_at_most_32_bytes(stack, emoji):
    """The same bound every inbound path holds a reaction to (v_67)."""
    a = await stack.provision("alice")
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="hi")
    with pytest.raises(MomentReactionInvalidError):
        await stack.moment_svc.react(m.id, reactor_user_id=a.user_id, emoji=emoji)
    await stack.moment_svc.react(m.id, reactor_user_id=a.user_id, emoji="👩‍👩‍👧‍👦")
    assert [r.emoji for r in await stack.moment_svc.list_reactions(m.id)] == [
        "👩‍👩‍👧‍👦"
    ]


async def test_reaction_reach_defaults_to_author_until_wired(stack):
    a = await stack.provision("alice")
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="hi")
    assert await stack.moment_svc.reaction_reach([m]) == {m.id: "author"}

    class _Reach:
        async def reach_for(self, moments):
            return {x.id: "household" for x in moments}

    stack.moment_svc.attach_reaction_reach(_Reach())
    assert await stack.moment_svc.reaction_reach([m]) == {m.id: "household"}


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
    replies = await stack.moment_svc.list_replies(root.id, viewer_user_id=a.user_id)
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
    expired_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
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


# ── Follower retention (decision 6d) ─────────────────────────────────────


def _lifetime_days(m: Moment) -> float:
    created = datetime.fromisoformat(m.created_at)
    return (datetime.fromisoformat(m.expires_at) - created).total_seconds() / 86_400


async def test_a_new_moment_lives_seven_days_by_default(stack):
    a = await stack.provision("alice")
    captured: list[MomentCreated] = []
    stack.bus.subscribe(MomentCreated, captured.append)
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="hi")
    assert round(_lifetime_days(m), 3) == 7
    # The signed wire value is the stored one.
    assert captured[0].expires_at == m.expires_at


@pytest.mark.parametrize(("chosen", "days"), [(1, 1), (14, 14), (30, 30), (90, 30)])
async def test_the_author_chooses_how_long_followers_keep_it(stack, chosen, days):
    a = await stack.provision("alice")
    await stack.user_svc.patch_preferences(
        "alice", {"moments": {"follower_retention_days": chosen}}
    )
    m = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="hi")
    assert round(_lifetime_days(m), 3) == days


async def test_a_reply_never_outlives_its_thread(stack):
    """Bob keeps moments 30 days, Alice 2: Bob's reply to Alice ends with
    her moment, so it never stands alone once the root is gone."""
    a = await stack.provision("alice")
    b = await stack.provision("bob")
    await stack.user_svc.patch_preferences(
        "alice", {"moments": {"follower_retention_days": 2}}
    )
    await stack.user_svc.patch_preferences(
        "bob", {"moments": {"follower_retention_days": 30}}
    )
    root = await stack.moment_svc.create_moment(author_user_id=a.user_id, content="q")
    reply = await stack.moment_svc.create_moment(
        author_user_id=b.user_id, content="a", parent_moment_id=root.id
    )
    assert reply.expires_at == root.expires_at
    # A shorter choice than the root's stays the replier's.
    await stack.user_svc.patch_preferences(
        "bob", {"moments": {"follower_retention_days": 1}}
    )
    short = await stack.moment_svc.create_moment(
        author_user_id=b.user_id, content="b", parent_moment_id=root.id
    )
    assert round(_lifetime_days(short), 3) == 1


# ── Saves (decision 6b) ───────────────────────────────────────────────────


def _peer(iid: str, *, version: int, source=InstanceSource.MANUAL) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://{iid}/wh",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=source,
        proto_version=version,
    )


class _Gates:
    """Fake save gates: the paired households (``get_instance``) and which
    GFSs keep delete certificates."""

    def __init__(self, *, peers=(), gfss=()):
        self.peers = {p.id: p for p in peers}
        self.gfss = set(gfss)
        self.peer_calls: list[str] = []

    async def get_instance(self, instance_id: str):
        self.peer_calls.append(instance_id)
        return self.peers.get(instance_id)

    async def moment_delete_log_supported(self, gfs_id: str) -> bool:
        return gfs_id in self.gfss


def _svc(stack, gates: _Gates) -> MomentService:
    return MomentService(
        stack.moment_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        federation_repo=gates,  # type: ignore[arg-type]
        gfs_delete_log=gates,
    )


async def _received(
    stack, mid: str, *, author: str, via: str, origin="inst-b", gfs=None
):
    if gfs is not None:
        await stack.db.enqueue(
            "INSERT OR IGNORE INTO gfs_connections(id, gfs_instance_id,"
            " display_name, public_key, inbox_url, status, paired_at)"
            " VALUES(?, ?, ?, ?, 'https://gfs.example', 'active', datetime('now'))",
            (gfs, gfs, gfs, "aa" * 32),
        )
    now = datetime.now(UTC)
    m = Moment(
        id=mid,
        author_user_id=author,
        content="hi",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id=origin,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(days=7)).isoformat(),
        received_via=via,
        received_via_gfs_id=gfs,
    )
    await stack.moment_repo.save(m)
    return m


_V66 = FederationCapability.MIN_FOR_LATE_MOMENT_DELETES


async def test_save_needs_a_paired_household_that_keeps_deletes(stack):
    me = await stack.provision("me")
    await _received(stack, "m-new", author="u-b", via="household", origin="inst-new")
    await _received(stack, "m-old", author="u-b", via="household", origin="inst-old")
    await _received(stack, "m-fof", author="u-c", via="household", origin="inst-far")
    await _received(stack, "m-seat", author="u-d", via="household", origin="inst-seat")
    svc = _svc(
        stack,
        _Gates(
            peers=[
                _peer("inst-new", version=_V66),
                _peer("inst-old", version=_V66 - 1),
                # Only shares a space with us: it never sends us its moments'
                # deletes.
                _peer("inst-seat", version=_V66, source=InstanceSource.SPACE_SESSION),
            ]
        ),
    )
    await svc.save_moment("m-new", user_id=me.user_id)
    for mid in ("m-old", "m-fof", "m-seat"):
        with pytest.raises(MomentSaveUnavailableError):
            await svc.save_moment(mid, user_id=me.user_id)
    saved = await svc.list_saved(user_id=me.user_id)
    assert [s.moment.id for s in saved] == ["m-new"]


async def test_save_without_federation_is_never_offered_for_household_copies(stack):
    me = await stack.provision("me")
    await _received(stack, "m-1", author="u-b", via="household")
    with pytest.raises(MomentSaveUnavailableError):
        await stack.moment_svc.save_moment("m-1", user_id=me.user_id)


async def test_save_needs_a_gfs_that_keeps_delete_certificates(stack):
    me = await stack.provision("me")
    svc = _svc(stack, _Gates(gfss={"g-new"}))
    await _received(stack, "m-g-new", author="u-far", via="gfs", gfs="g-new")
    await _received(stack, "m-g-old", author="u-far", via="gfs", gfs="g-old")
    await svc.save_moment("m-g-new", user_id=me.user_id)
    with pytest.raises(MomentSaveUnavailableError):
        await svc.save_moment("m-g-old", user_id=me.user_id)
    # A GFS row without its server id can't be vouched for either.
    m = await _received(stack, "m-g-none", author="u-far", via="gfs")
    assert await svc.can_save(m, me.user_id) is False


async def test_own_moment_is_not_saved_a_housemates_is(stack):
    me = await stack.provision("me")
    mia = await stack.provision("mia")
    mine = await stack.moment_svc.create_moment(author_user_id=me.user_id, content="a")
    hers = await stack.moment_svc.create_moment(author_user_id=mia.user_id, content="b")
    with pytest.raises(MomentOwnSaveError):
        await stack.moment_svc.save_moment(mine.id, user_id=me.user_id)
    await stack.moment_svc.save_moment(hers.id, user_id=me.user_id)
    flags = await stack.moment_svc.save_flags([mine, hers], viewer_user_id=me.user_id)
    assert flags[mine.id] == (False, False)
    assert flags[hers.id] == (True, True)


async def test_save_of_an_unreadable_moment_is_the_unknown_id_404(stack):
    me = await stack.provision("me")
    await _received(stack, "m-1", author="u-b", via="household", origin="inst-b")
    svc = _svc(stack, _Gates(peers=[_peer("inst-b", version=_V66)]))
    await stack.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        (me.user_id, "u-b"),
    )
    with pytest.raises(MomentNotFoundError):
        await svc.save_moment("m-1", user_id=me.user_id)
    with pytest.raises(MomentNotFoundError):
        await svc.save_moment("m-nope", user_id=me.user_id)


async def test_a_protected_viewer_saves_within_the_protected_filters(stack):
    """Sign-off 5: a protected account saves like anyone, but never a
    GFS-delivered moment (it can't read one)."""
    kid = await stack.provision("kid")
    await stack.db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id=?", (kid.user_id,)
    )
    svc = _svc(stack, _Gates(peers=[_peer("inst-b", version=_V66)], gfss={"g1"}))
    await _received(stack, "m-paired", author="u-b", via="household", origin="inst-b")
    await _received(stack, "m-gfs", author="u-far", via="gfs", gfs="g1")
    await svc.save_moment("m-paired", user_id=kid.user_id)
    with pytest.raises(MomentNotFoundError):
        await svc.save_moment("m-gfs", user_id=kid.user_id)


async def test_a_protected_accounts_moment_can_be_saved(stack):
    """Sign-off 5: a child's moment is saved like any moment."""
    me = await stack.provision("me")
    kid = await stack.provision("kid")
    await stack.db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id=?", (kid.user_id,)
    )
    m = await stack.moment_svc.create_moment(author_user_id=kid.user_id, content="hi")
    await stack.moment_svc.save_moment(m.id, user_id=me.user_id)
    assert [
        s.moment.id for s in await stack.moment_svc.list_saved(user_id=me.user_id)
    ] == [m.id]


async def test_save_flags_cache_one_lookup_per_origin(stack):
    me = await stack.provision("me")
    gates = _Gates(peers=[_peer("inst-b", version=_V66)])
    svc = _svc(stack, gates)
    rows = [
        await _received(stack, f"m-{i}", author="u-b", via="household")
        for i in range(3)
    ]
    await svc.save_moment("m-1", user_id=me.user_id)
    gates.peer_calls.clear()
    flags = await svc.save_flags(rows, viewer_user_id=me.user_id)
    assert flags == {"m-0": (False, True), "m-1": (True, True), "m-2": (False, True)}
    assert gates.peer_calls == ["inst-b"]


async def test_unsave_is_idempotent_and_needs_no_read(stack):
    me = await stack.provision("me")
    await _received(stack, "m-1", author="u-b", via="household")
    svc = _svc(stack, _Gates(peers=[_peer("inst-b", version=_V66)]))
    await svc.save_moment("m-1", user_id=me.user_id)
    await svc.unsave_moment("m-1", user_id=me.user_id)
    await svc.unsave_moment("m-1", user_id=me.user_id)
    await svc.unsave_moment("m-unknown", user_id=me.user_id)
    assert await svc.list_saved(user_id=me.user_id) == []


async def test_a_saved_moment_outlives_expiry_until_the_author_deletes(stack):
    me = await stack.provision("me")
    await _received(stack, "m-1", author="u-b", via="household")
    gates = _Gates(peers=[_peer("inst-b", version=_V66)])
    svc = _svc(stack, gates)
    await svc.save_moment("m-1", user_id=me.user_id)
    await stack.db.enqueue(
        "UPDATE moments SET expires_at=? WHERE id='m-1'",
        ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
    )
    assert await svc.expire_due() == 0
    assert (await svc.get_for_viewer("m-1", me.user_id)).id == "m-1"
    # Already saved: saving again is fine, even if the household went quiet.
    gates.peers.clear()
    await svc.save_moment("m-1", user_id=me.user_id)
    await stack.moment_repo.tombstone(
        "m-1",
        author_user_id="u-b",
        origin_instance_id="inst-b",
        expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
    )
    assert await svc.list_saved(user_id=me.user_id) == []
    with pytest.raises(MomentNotFoundError):
        await svc.get_for_viewer("m-1", me.user_id)
