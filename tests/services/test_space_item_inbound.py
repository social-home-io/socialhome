"""Tests for :class:`SpaceItemInbound` — applying member-published comments,
comment edits / deletes, post deletes and reactions (v_49).

The caller (:class:`SpacePublicInbound`) has already verified the writer
cert, the scope, the user binding and the author signature. These tests pin
what the applier adds: author-only edits and deletes, owner-bound ids, the
roster check on member households, and ordering independence (a delete or
edit that arrives before its create, edits and reactions out of order).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    PostDeleted,
    PostReactionChanged,
)
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.space_item import AuthorityRemoval
from socialhome.federation.owner_bound_id import (
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    mint_owner_bound_id,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_item_author import build_signed_item_inner
from socialhome.services.space_item_inbound import SpaceItemInbound

SPACE = "sp-1"
KP = generate_identity_keypair()
AUTHOR = derive_user_id(KP.public_key, "bob")
ORIGIN = derive_instance_id(KP.public_key)
OTHER_KP = generate_identity_keypair()
OTHER = derive_user_id(OTHER_KP.public_key, "eve")
T0 = datetime.now(timezone.utc) - timedelta(minutes=5)


def _ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


class _Seats:
    """A stand-in for ``SpaceAuthorship.item_seat_admits`` /
    ``item_access_admits`` recording what it was asked."""

    def __init__(self, admits: bool = True) -> None:
        self.admits = admits
        self.calls: list[tuple[str, dict]] = []

    async def item_seat_admits(self, **kw):
        self.calls.append(("seat", kw))
        return self.admits

    async def item_access_admits(self, **kw):
        self.calls.append(("access", kw))
        return self.admits


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager.from_data_dir(tmp_dir))
    await spaces.save(
        Space(
            id=SPACE,
            name="S",
            owner_instance_id="host.home",
            owner_username="o",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=True),
            space_type=SpaceType.PUBLIC,
            join_mode=JoinMode.OPEN,
        )
    )
    posts = SqliteSpacePostRepo(db)
    bus = EventBus()
    events: list = []
    for cls in (
        CommentAdded,
        CommentUpdated,
        CommentDeleted,
        PostDeleted,
        PostReactionChanged,
    ):
        bus.subscribe(cls, events.append)
    applier = SpaceItemInbound(bus=bus, space_repo=spaces, space_post_repo=posts)
    post_id = mint_owner_bound_id(SPACE_POST_KIND, space_id=SPACE, owner_user_id=AUTHOR)
    await posts.save(
        SPACE,
        Post(
            id=post_id,
            author=AUTHOR,
            type=PostType.TEXT,
            created_at=T0,
            content="the post",
        ),
    )
    yield {
        "db": db,
        "spaces": spaces,
        "posts": posts,
        "applier": applier,
        "events": events,
        "post_id": post_id,
    }
    await db.shutdown()


def _cid(author: str = AUTHOR) -> str:
    return mint_owner_bound_id(SPACE_COMMENT_KIND, space_id=SPACE, owner_user_id=author)


def _inner(item_type: str, target: str, post_id: str, *, ts: float = 0, **kw) -> dict:
    return build_signed_item_inner(
        item_type=item_type,
        item_target=target,
        space_id=SPACE,
        post_id=post_id,
        author_user_id=AUTHOR,
        author_username="bob",
        author_pk=KP.public_key,
        author_identity_seed=KP.private_key,
        origin_instance_id=ORIGIN,
        ts=_ts(ts),
        **kw,
    )


def _comment(env, cid: str, *, content="hi", ts: float = 0, **kw) -> dict:
    return _inner(
        "comment",
        cid,
        env["post_id"],
        ts=ts,
        comment_type="text",
        content=content,
        created_at=_ts(ts),
        **kw,
    )


async def _apply(
    env,
    item_type: str,
    inner: dict,
    *,
    cert_scope="write",
    member=False,
    authorship=None,
) -> bool:
    space = await env["spaces"].get(SPACE)
    return await env["applier"].apply(
        space=space,
        item_type=item_type,
        inner=inner,
        origin_instance_id=ORIGIN,
        cert_scope=cert_scope,
        member_household=member,
        authorship=authorship,
    )


# ── comment ────────────────────────────────────────────────────────────────


async def test_a_comment_is_persisted_counted_and_announced(env):
    cid = _cid()
    assert await _apply(env, "comment", _comment(env, cid))
    got = await env["posts"].get_comment(cid)
    assert (got.author, got.content, got.post_id) == (AUTHOR, "hi", env["post_id"])
    _sid, post = await env["posts"].get(env["post_id"])
    assert post.comment_count == 1
    added = [e for e in env["events"] if isinstance(e, CommentAdded)]
    assert added[0].origin_instance_id == ORIGIN


async def test_a_duplicate_comment_is_dropped(env):
    cid = _cid()
    inner = _comment(env, cid)
    assert await _apply(env, "comment", inner)
    assert not await _apply(env, "comment", inner)
    _sid, post = await env["posts"].get(env["post_id"])
    assert post.comment_count == 1


@pytest.mark.parametrize("case", ["legacy", "someone_else"])
async def test_a_comment_needs_an_id_bound_to_its_author(env, case):
    cid = "c" * 32 if case == "legacy" else _cid(OTHER)
    assert not await _apply(env, "comment", _comment(env, cid))
    assert await env["posts"].get_comment(cid) is None


async def test_a_comment_on_an_unknown_or_deleted_post_is_dropped(env):
    inner = _inner(
        "comment", _cid(), "nope", comment_type="text", content="x", created_at=_ts(0)
    )
    assert not await _apply(env, "comment", inner)
    await env["posts"].soft_delete(env["post_id"], space_id=SPACE)
    assert not await _apply(env, "comment", _comment(env, _cid()))


@pytest.mark.parametrize(
    "kw",
    [
        {"comment_type": "video"},
        {"comment_type": "text", "content": "   "},
        {"comment_type": "image", "content": None},
    ],
)
async def test_a_malformed_comment_is_dropped(env, kw):
    inner = _inner("comment", _cid(), env["post_id"], created_at=_ts(0), **kw)
    assert not await _apply(env, "comment", inner)


async def test_a_member_household_checks_the_comment_seat(env):
    seats = _Seats(admits=False)
    cid = _cid()
    assert not await _apply(
        env, "comment", _comment(env, cid), member=True, authorship=seats
    )
    assert seats.calls == [
        (
            "seat",
            {
                "origin_instance_id": ORIGIN,
                "space_id": SPACE,
                "author_user_id": AUTHOR,
                # Followers may comment only while the space allows it.
                "subscriber_ok": False,
            },
        )
    ]
    assert await env["posts"].get_comment(cid) is None


async def test_a_follower_household_relies_on_the_cert(env):
    seats = _Seats(admits=False)
    assert await _apply(
        env, "comment", _comment(env, _cid()), member=False, authorship=seats
    )
    assert seats.calls == []


# ── comment_edit ─────────────────────────────────────────────────────────


async def _seed_comment(env, cid: str, author: str = AUTHOR) -> None:
    await env["posts"].add_comment(
        Comment(
            id=cid,
            post_id=env["post_id"],
            author=author,
            type=CommentType.TEXT,
            created_at=T0,
            content="original",
        ),
        space_id=SPACE,
    )


def _edit(env, cid: str, content: str, ts: float) -> dict:
    return _inner(
        "comment_edit",
        cid,
        env["post_id"],
        ts=ts,
        comment_type="text",
        content=content,
        created_at=_ts(0),
    )


async def test_the_author_edits_their_comment(env):
    cid = _cid()
    await _seed_comment(env, cid)
    assert await _apply(env, "comment_edit", _edit(env, cid, "edited", 5))
    assert (await env["posts"].get_comment(cid)).content == "edited"
    assert any(isinstance(e, CommentUpdated) for e in env["events"])


async def test_someone_elses_comment_cannot_be_edited(env):
    cid = _cid(OTHER)
    await _seed_comment(env, cid, author=OTHER)
    assert not await _apply(env, "comment_edit", _edit(env, cid, "hijack", 5))
    assert (await env["posts"].get_comment(cid)).content == "original"


async def test_out_of_order_comment_edits_keep_the_newest(env):
    cid = _cid()
    await _seed_comment(env, cid)
    assert await _apply(env, "comment_edit", _edit(env, cid, "newer", 10))
    assert not await _apply(env, "comment_edit", _edit(env, cid, "older", 5))
    assert (await env["posts"].get_comment(cid)).content == "newer"


async def test_an_edit_before_its_create_is_the_create_at_its_newest_content(env):
    cid = _cid()
    assert await _apply(env, "comment_edit", _edit(env, cid, "edited", 10))
    # The original create arrives later: it is a duplicate now.
    assert not await _apply(env, "comment", _comment(env, cid, content="original"))
    got = await env["posts"].get_comment(cid)
    assert got.content == "edited"
    assert got.edited_at is not None
    _sid, post = await env["posts"].get(env["post_id"])
    assert post.comment_count == 1


async def test_an_edit_never_resurrects_a_deleted_comment(env):
    cid = _cid()
    await _seed_comment(env, cid)
    await env["posts"].soft_delete_comment(cid, space_id=SPACE)
    assert not await _apply(env, "comment_edit", _edit(env, cid, "back", 10))
    assert (await env["posts"].get_comment(cid)).deleted


async def test_an_edit_with_a_far_future_stamp_is_refused(env):
    """A stamp far ahead would win every later edit: refused."""
    cid = _cid()
    await _seed_comment(env, cid)
    future = (datetime.now(timezone.utc) + timedelta(days=1) - T0).total_seconds()
    assert not await _apply(env, "comment_edit", _edit(env, cid, "forever", future))
    assert (await env["posts"].get_comment(cid)).content == "original"


# ── comment_delete ───────────────────────────────────────────────────────


def _cdel(env, cid: str, ts: float = 20) -> dict:
    return _inner("comment_delete", cid, env["post_id"], ts=ts)


async def test_the_author_deletes_their_comment(env):
    cid = _cid()
    await _seed_comment(env, cid)
    await env["posts"].increment_comment_count(env["post_id"], space_id=SPACE)
    assert await _apply(env, "comment_delete", _cdel(env, cid))
    assert (await env["posts"].get_comment(cid)).deleted
    _sid, post = await env["posts"].get(env["post_id"])
    assert post.comment_count == 0
    assert any(isinstance(e, CommentDeleted) for e in env["events"])


async def test_someone_elses_comment_cannot_be_deleted(env):
    cid = _cid(OTHER)
    await _seed_comment(env, cid, author=OTHER)
    assert not await _apply(env, "comment_delete", _cdel(env, cid))
    assert not (await env["posts"].get_comment(cid)).deleted


async def test_a_comment_delete_before_its_create_leaves_a_tombstone(env):
    cid = _cid()
    assert await _apply(env, "comment_delete", _cdel(env, cid))
    # The create arrives afterwards — on this path or any other (the
    # federated create refuses an id it already holds) — and is not
    # resurrected.
    assert not await _apply(env, "comment", _comment(env, cid))
    got = await env["posts"].get_comment(cid)
    assert got.deleted and got.content is None
    _sid, post = await env["posts"].get(env["post_id"])
    assert post.comment_count == 0


async def test_an_early_delete_for_someone_elses_id_leaves_nothing(env):
    """No tombstone unless the id proves the deleter is its author — else a
    household could pre-empt another member's comment."""
    cid = _cid(OTHER)
    assert not await _apply(env, "comment_delete", _cdel(env, cid))
    assert await env["posts"].get_comment(cid) is None


# ── post_delete ──────────────────────────────────────────────────────────


def _pdel(post_id: str, ts: float = 30) -> dict:
    return _inner("post_delete", post_id, post_id, ts=ts)


async def test_the_author_deletes_their_post(env):
    assert await _apply(env, "post_delete", _pdel(env["post_id"]))
    _sid, post = await env["posts"].get(env["post_id"])
    assert post.deleted and post.content is None
    deleted = [e for e in env["events"] if isinstance(e, PostDeleted)]
    assert deleted[0].origin_instance_id == ORIGIN


async def test_someone_elses_post_cannot_be_deleted(env):
    pid = mint_owner_bound_id(SPACE_POST_KIND, space_id=SPACE, owner_user_id=OTHER)
    await env["posts"].save(
        SPACE,
        Post(id=pid, author=OTHER, type=PostType.TEXT, created_at=T0, content="x"),
    )
    assert not await _apply(env, "post_delete", _pdel(pid))
    assert not (await env["posts"].get(pid))[1].deleted


async def test_a_post_delete_before_its_create_leaves_a_tombstone(env):
    pid = mint_owner_bound_id(SPACE_POST_KIND, space_id=SPACE, owner_user_id=AUTHOR)
    assert await _apply(env, "post_delete", _pdel(pid))
    got = await env["posts"].get(pid)
    assert got is not None and got[1].deleted
    assert got[1].author == AUTHOR


async def test_an_early_post_delete_needs_an_id_bound_to_its_author(env):
    for pid in (
        "p" * 32,
        mint_owner_bound_id(SPACE_POST_KIND, space_id=SPACE, owner_user_id=OTHER),
    ):
        assert not await _apply(env, "post_delete", _pdel(pid))
        assert await env["posts"].get(pid) is None


async def test_a_member_household_checks_the_posts_access_for_a_delete(env):
    seats = _Seats(admits=False)
    assert not await _apply(
        env, "post_delete", _pdel(env["post_id"]), member=True, authorship=seats
    )
    assert seats.calls[0][0] == "access"
    assert not (await env["posts"].get(env["post_id"]))[1].deleted


async def test_a_delete_with_a_mismatched_target_is_dropped(env):
    inner = _inner("post_delete", env["post_id"], "another-post", ts=1)
    assert not await _apply(env, "post_delete", inner)


# ── reactions ────────────────────────────────────────────────────────────


def _react(env, kind: str, emoji: str, ts: float) -> dict:
    return _inner(kind, env["post_id"], env["post_id"], ts=ts, emoji=emoji)


async def _reactions(env) -> dict:
    return dict((await env["posts"].get(env["post_id"]))[1].reactions)


async def test_a_reaction_is_added_and_removed(env):
    assert await _apply(env, "reaction_add", _react(env, "reaction_add", "👍", 1))
    assert AUTHOR in (await _reactions(env))["👍"]
    changed = [e for e in env["events"] if isinstance(e, PostReactionChanged)]
    assert (changed[0].space_id, changed[0].reactor_user_id, changed[0].added) == (
        SPACE,
        AUTHOR,
        True,
    )
    assert await _apply(env, "reaction_remove", _react(env, "reaction_remove", "👍", 2))
    assert "👍" not in await _reactions(env)


async def test_a_stale_reaction_add_after_its_remove_is_ignored(env):
    """A duplicate add from a second connection server, landing after the
    remove, must not bring the reaction back."""
    add = _react(env, "reaction_add", "👍", 1)
    assert await _apply(env, "reaction_add", add)
    assert await _apply(env, "reaction_remove", _react(env, "reaction_remove", "👍", 2))
    assert not await _apply(env, "reaction_add", add)
    assert "👍" not in await _reactions(env)


@pytest.mark.parametrize("emoji", ["", " 👍", "x" * 40])
async def test_a_malformed_emoji_is_dropped(env, emoji):
    assert not await _apply(env, "reaction_add", _react(env, "reaction_add", emoji, 1))


async def test_a_follower_needs_a_write_cert_unless_follower_reactions_are_on(env):
    inner = _react(env, "reaction_add", "👍", 1)
    assert not await _apply(env, "reaction_add", inner, cert_scope="comment")
    space = await env["spaces"].get(SPACE)
    await env["spaces"].save(
        space.__class__(
            **{
                **{f: getattr(space, f) for f in space.__dataclass_fields__},
                "features": SpaceFeatures(
                    allow_subscribers=True, allow_subscriber_react=True
                ),
            }
        )
    )
    assert await _apply(env, "reaction_add", inner, cert_scope="comment")


async def test_a_member_household_checks_the_reactor_seat(env):
    seats = _Seats(admits=False)
    inner = _react(env, "reaction_add", "👍", 1)
    assert not await _apply(
        env,
        "reaction_add",
        inner,
        cert_scope="comment",
        member=True,
        authorship=seats,
    )
    assert seats.calls[0][1]["subscriber_ok"] is False


async def test_a_reaction_on_a_deleted_post_is_dropped(env):
    await env["posts"].soft_delete(env["post_id"], space_id=SPACE)
    assert not await _apply(env, "reaction_add", _react(env, "reaction_add", "👍", 1))


async def test_an_unknown_item_type_is_dropped(env):
    assert not await _apply(env, "poll", _pdel(env["post_id"]))


# ── Cross-space and edge cases ───────────────────────────────────────────


async def _other_space_post(env) -> str:
    await env["spaces"].save(
        Space(
            id="sp-2",
            name="Other",
            owner_instance_id="host.home",
            owner_username="o",
            identity_public_key="11" * 32,
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=True),
            space_type=SpaceType.PUBLIC,
            join_mode=JoinMode.OPEN,
        )
    )
    pid = mint_owner_bound_id(SPACE_POST_KIND, space_id="sp-2", owner_user_id=AUTHOR)
    await env["posts"].save(
        "sp-2",
        Post(id=pid, author=AUTHOR, type=PostType.TEXT, created_at=T0, content="x"),
    )
    return pid


async def test_items_never_reach_into_another_space(env):
    pid = await _other_space_post(env)
    cid = _cid()
    await env["posts"].add_comment(
        Comment(
            id=cid,
            post_id=pid,
            author=AUTHOR,
            type=CommentType.TEXT,
            created_at=T0,
            content="elsewhere",
        ),
        space_id="sp-2",
    )
    for item_type, inner in (
        ("comment_edit", _edit(env, cid, "hijack", 5)),
        ("comment_delete", _cdel(env, cid)),
        ("post_delete", _pdel(pid)),
        ("reaction_add", _inner("reaction_add", pid, pid, ts=1, emoji="👍")),
    ):
        assert not await _apply(env, item_type, inner)
    assert (await env["posts"].get_comment(cid)).content == "elsewhere"
    assert not (await env["posts"].get(pid))[1].deleted


async def test_an_early_comment_delete_on_an_unknown_post_is_dropped(env):
    cid = _cid()
    inner = _inner("comment_delete", cid, "unknown-post", ts=1)
    assert not await _apply(env, "comment_delete", inner)
    assert await env["posts"].get_comment(cid) is None


async def test_an_image_comment_is_kept_and_a_bad_parent_refused(env):
    cid = _cid()
    image = _inner(
        "comment",
        cid,
        env["post_id"],
        comment_type="image",
        media_url="api/media/x.webp",
        created_at=_ts(0),
    )
    assert await _apply(env, "comment", image)
    orphan = _inner(
        "comment",
        _cid(),
        env["post_id"],
        comment_type="text",
        content="reply",
        parent_id="no-such-comment",
        created_at=_ts(0),
    )
    assert not await _apply(env, "comment", orphan)


async def test_an_item_without_a_target_is_dropped(env):
    inner = _inner("comment_delete", "x", env["post_id"], ts=1)
    inner["item_target"] = ""
    assert not await _apply(env, "comment_delete", inner)


# ── Review repros I1 / I2: reaction order survives restarts and other paths


async def test_a_restart_does_not_let_a_queued_old_add_resurrect_a_reaction(env):
    add = _react(env, "reaction_add", "👍", 1)
    assert await _apply(env, "reaction_add", add)
    assert await _apply(env, "reaction_remove", _react(env, "reaction_remove", "👍", 2))
    # A process restart: a fresh applier over the same database.
    env["applier"] = SpaceItemInbound(
        bus=EventBus(), space_repo=env["spaces"], space_post_repo=env["posts"]
    )
    # Another connection server's (or the offline queue's) copy of the add.
    assert not await _apply(env, "reaction_add", add)
    assert "👍" not in await _reactions(env)


async def test_a_reaction_changed_on_another_path_is_not_undone_by_a_late_copy(env):
    """A reaction added and removed on any other path (a local write here)
    is stamped too: a slower relayed copy of an older add stays out."""
    await env["posts"].add_reaction(env["post_id"], "👍", AUTHOR, space_id=SPACE)
    await env["posts"].remove_reaction(env["post_id"], "👍", AUTHOR, space_id=SPACE)
    assert not await _apply(env, "reaction_add", _react(env, "reaction_add", "👍", 1))
    assert "👍" not in await _reactions(env)


# ─── Authority removals (host relay) ────────────────────────────────────


def _removal(target: str, item_id: str, post_id: str, author: str = AUTHOR):
    return AuthorityRemoval(
        space_id=SPACE,
        target=target,
        item_id=item_id,
        post_id=post_id,
        author_user_id=author,
    )


async def _space(env):
    return await env["spaces"].get(SPACE)


async def test_an_authority_removal_is_not_author_bound(env):
    """The space authority may remove anyone's post, as a moderator may."""
    space = await _space(env)
    removal = _removal("post", env["post_id"], env["post_id"], author="")
    assert await env["applier"].apply_authority_removal(space=space, removal=removal)
    _sid, row = await env["posts"].get(env["post_id"])
    assert row.deleted
    (event,) = [e for e in env["events"] if isinstance(e, PostDeleted)]
    assert event.origin_instance_id == "host.home"
    assert event.author_user_id == AUTHOR
    # Final: a duplicate changes nothing.
    assert not await env["applier"].apply_authority_removal(
        space=space, removal=removal
    )


async def test_an_authority_removal_of_another_spaces_post_is_refused(env):
    other = dataclasses.replace(await _space(env), id="sp-other")
    await env["spaces"].save(other)
    removal = AuthorityRemoval(
        space_id="sp-other",
        target="post",
        item_id=env["post_id"],
        post_id=env["post_id"],
    )
    assert not await env["applier"].apply_authority_removal(
        space=other, removal=removal
    )
    _sid, row = await env["posts"].get(env["post_id"])
    assert not row.deleted


async def test_an_authority_comment_removal_soft_deletes_and_counts_down(env):
    cid = _cid()
    await _apply(env, "comment", _comment(env, cid))
    space = await _space(env)
    assert await env["applier"].apply_authority_removal(
        space=space, removal=_removal("comment", cid, env["post_id"])
    )
    assert (await env["posts"].get_comment(cid)).deleted
    _sid, post = await env["posts"].get(env["post_id"])
    assert post.comment_count == 0
    # Already deleted → nothing more.
    assert not await env["applier"].apply_authority_removal(
        space=space, removal=_removal("comment", cid, env["post_id"])
    )


async def test_an_authority_comment_removal_naming_another_post_is_refused(env):
    cid = _cid()
    await _apply(env, "comment", _comment(env, cid))
    other_post = mint_owner_bound_id(
        SPACE_POST_KIND, space_id=SPACE, owner_user_id=AUTHOR
    )
    await env["posts"].save(
        SPACE,
        Post(id=other_post, author=AUTHOR, type=PostType.TEXT, created_at=T0),
    )
    assert not await env["applier"].apply_authority_removal(
        space=await _space(env), removal=_removal("comment", cid, other_post)
    )
    assert not (await env["posts"].get_comment(cid)).deleted


async def test_an_authority_tombstone_needs_an_id_bound_here(env):
    space = await _space(env)
    bound = mint_owner_bound_id(SPACE_POST_KIND, space_id=SPACE, owner_user_id=AUTHOR)
    assert await env["applier"].apply_authority_removal(
        space=space, removal=_removal("post", bound, bound)
    )
    _sid, row = await env["posts"].get(bound)
    assert row.deleted and row.author == AUTHOR
    elsewhere = mint_owner_bound_id(
        SPACE_POST_KIND, space_id="sp-other", owner_user_id=AUTHOR
    )
    assert not await env["applier"].apply_authority_removal(
        space=space, removal=_removal("post", elsewhere, elsewhere)
    )
    assert await env["posts"].get(elsewhere) is None
