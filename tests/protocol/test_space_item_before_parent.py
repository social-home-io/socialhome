"""Release-blocker protocol tests: an item that overtakes its post is held,
not lost.

Marked ``@pytest.mark.security``.

A space's items travel several paths at once — direct federation, the
host's relay of a member's write, ``SPACE_ROUTED``, the v_49 member publish
over one or more GFSs, §25.6 sync — so a comment (a reaction, an edit, a
delete, a reply) routinely arrives before the post (or parent comment) it
references. Refusing it lost it on that household for good once the post
arrived live (the demo's "SPACE_COMMENT_CREATED … post … is not in space —
refusing" on a host that had just come back online).

The rule pinned here (:class:`~socialhome.federation.pending_parent_buffer.
PendingParentBuffer`):

* an item whose parent is **absent** is held, and replayed when the parent
  lands — through every check again (the §24.11 post-decrypt gates and the
  handler's authorship rules; for a ``space_item`` the decrypt, writer
  cert, author signature and roster checks);
* an item for a parent that never lands is dropped when its entry expires;
* an item for a **deleted** (tombstoned) post is refused, never held;
* the buffer stays bounded under a flood from one household;
* a write the sender may not make is never held at all.

Both doors run for real: the GFS member publish (:class:`SpacePublicInbound`
over real SQLite repos, real writer certs and real ciphertext) and the
direct path (the real app, :meth:`FederationService.replay_held` — the
production post-decrypt seam — over a real database).
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.owner_bound_id import (
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.pending_parent_buffer import PendingParentBuffer
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_item_author import build_signed_item_inner
from socialhome.services.space_public_author import build_signed_author_inner
from socialhome.services.space_public_inbound import SpacePublicInbound
from socialhome.services.space_writer_cert_service import SpaceWriterCertService
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

from .test_space_content_authorship import _SEED, AUTHOR, OTHER, SP, _config

pytestmark = pytest.mark.security

FET = FederationEventType
SPACE = "sp-late"


class _Author:
    def __init__(self, username: str) -> None:
        self.kp = generate_identity_keypair()
        self.username = username
        self.user_id = derive_user_id(self.kp.public_key, username)
        self.origin = derive_instance_id(self.kp.public_key)


BOB = _Author("bob")
EVE = _Author("eve")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _ts(seconds: float = -1) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ── The GFS member-publish door ───────────────────────────────────────────


@pytest.fixture
async def gfs(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    kek = KeyManager.from_data_dir(tmp_dir)
    spaces = SqliteSpaceRepo(db, key_manager=kek)
    keys = SqliteSpaceKeyRepo(db)
    crypto = SpaceContentEncryption(keys, kek)
    posts = SqliteSpacePostRepo(db)
    space_kp = generate_space_keypair()
    await spaces.save(
        Space(
            id=SPACE,
            name="S",
            owner_instance_id="host.home",
            owner_username="o",
            identity_public_key=space_kp.public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=True),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )
    )
    await crypto.initialise_for_space(SPACE)
    bus = EventBus()
    clock = _Clock()
    buffer = PendingParentBuffer(
        ttl_seconds=60, max_per_sender=16, max_entries=64, clock=clock
    )
    buffer.attach_bus(bus)
    inbound = SpacePublicInbound(
        bus=bus, space_repo=spaces, space_crypto=crypto, space_post_repo=posts
    )
    inbound.attach_identity(own_instance_id="follower.home")
    inbound.attach_writer_certs(
        SpaceWriterCertService(
            space_repo=spaces,
            remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
            space_key_repo=keys,
            own_instance_id="follower.home",
            own_identity_pk=generate_identity_keypair().public_key,
        )
    )
    inbound.attach_pending_parents(buffer)
    yield {
        "crypto": crypto,
        "posts": posts,
        "inbound": inbound,
        "space_kp": space_kp,
        "buffer": buffer,
        "clock": clock,
    }
    await db.shutdown()


async def _cert(env, signer: _Author, scope: str) -> dict:
    cert = sign_writer_cert(
        space_seed=env["space_kp"].private_key,
        space_id=SPACE,
        epoch=await env["crypto"].get_current_epoch(SPACE),
        instance_pk=signer.kp.public_key,
        scope=scope,
    )
    return bind_writer_users(
        cert, space_seed=env["space_kp"].private_key, user_ids=[signer.user_id]
    ).to_wire()


async def _send(env, signer: _Author, scope: str, item_type: str, inner: dict):
    cert = await _cert(env, signer, scope)
    inner = {**inner, "writer_cert": cert}
    epoch, ct = await env["crypto"].encrypt(
        SPACE, json.dumps({"item_type": item_type, "inner": inner}).encode()
    )
    v1 = {
        k: v
        for k, v in cert.items()
        if k not in ("writer_user_ids", "users_sig", "users_sig_suite")
    }
    await env["inbound"].handle(
        {
            "type": "relay",
            "space_id": SPACE,
            "event_type": "space_item",
            "epoch": epoch,
            "writer_cert": v1,
            "payload": ct,
        }
    )


def _post_id(author: _Author = BOB) -> str:
    return mint_owner_bound_id(
        SPACE_POST_KIND, space_id=SPACE, owner_user_id=author.user_id
    )


def _comment_id(author: _Author = EVE) -> str:
    return mint_owner_bound_id(
        SPACE_COMMENT_KIND, space_id=SPACE, owner_user_id=author.user_id
    )


async def _send_post(env, post_id: str, author: _Author = BOB) -> None:
    inner = build_signed_author_inner(
        post=Post(
            id=post_id,
            author=author.user_id,
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="the post",
        ),
        space_id=SPACE,
        author_username=author.username,
        author_pk=author.kp.public_key,
        author_identity_seed=author.kp.private_key,
        origin_instance_id=author.origin,
        item_type="post",
        item_target=post_id,
    )
    await _send(env, author, "write", "post", inner)


def _generic(signer: _Author, item_type: str, target: str, post_id: str, **kw):
    return build_signed_item_inner(
        item_type=item_type,
        item_target=target,
        space_id=SPACE,
        post_id=post_id,
        author_user_id=signer.user_id,
        author_username=signer.username,
        author_pk=signer.kp.public_key,
        author_identity_seed=signer.kp.private_key,
        origin_instance_id=signer.origin,
        ts=kw.pop("ts", _ts()),
        **kw,
    )


async def _send_comment(env, post_id: str, cid: str, *, signer=EVE, **kw) -> None:
    inner = _generic(
        signer, "comment", cid, post_id, comment_type="text", content="hi", **kw
    )
    await _send(env, signer, "comment", "comment", inner)


async def test_gfs_a_comment_before_its_post_lands_after_the_post(gfs):
    pid, cid = _post_id(), _comment_id()
    await _send_comment(gfs, pid, cid)
    assert await gfs["posts"].get_comment(cid) is None
    assert len(gfs["buffer"]) == 1
    await _send_post(gfs, pid)
    comment = await gfs["posts"].get_comment(cid)
    assert comment is not None and comment.post_id == pid
    assert (await gfs["posts"].get(pid))[1].comment_count == 1
    assert len(gfs["buffer"]) == 0


async def test_gfs_the_same_item_from_two_gfss_takes_one_slot_and_lands_once(gfs):
    pid, cid = _post_id(), _comment_id()
    inner = _generic(EVE, "comment", cid, pid, comment_type="text", content="hi")
    await _send(gfs, EVE, "comment", "comment", inner)
    await _send(gfs, EVE, "comment", "comment", inner)
    await _send_post(gfs, pid)
    assert await gfs["posts"].get_comment(cid) is not None
    assert (await gfs["posts"].get(pid))[1].comment_count == 1


async def test_gfs_a_reaction_and_a_reply_chain_before_the_post(gfs):
    pid = _post_id()
    parent, reply = _comment_id(), _comment_id()
    # The reply overtakes its parent, which overtakes the post.
    await _send_comment(gfs, pid, reply, parent_id=parent)
    await _send_comment(gfs, pid, parent)
    await _send(
        gfs,
        EVE,
        "write",
        "reaction_add",
        _generic(EVE, "reaction_add", pid, pid, emoji="👍"),
    )
    assert len(gfs["buffer"]) == 3
    await _send_post(gfs, pid)
    assert await gfs["posts"].get_comment(parent) is not None
    landed = await gfs["posts"].get_comment(reply)
    assert landed is not None and landed.parent_id == parent
    assert EVE.user_id in (await gfs["posts"].get(pid))[1].reactions["👍"]
    assert len(gfs["buffer"]) == 0


async def test_gfs_an_early_comment_delete_keeps_the_comment_gone(gfs):
    pid, cid = _post_id(), _comment_id()
    await _send(
        gfs, EVE, "comment", "comment_delete", _generic(EVE, "comment_delete", cid, pid)
    )
    await _send_post(gfs, pid)
    await _send_comment(gfs, pid, cid)
    comment = await gfs["posts"].get_comment(cid)
    assert comment is not None and comment.deleted


async def test_gfs_a_post_that_never_arrives_drops_the_comment(gfs):
    pid, cid = _post_id(), _comment_id()
    await _send_comment(gfs, pid, cid)
    assert len(gfs["buffer"]) == 1
    gfs["clock"].now += 61
    gfs["buffer"].purge()
    assert len(gfs["buffer"]) == 0
    # The post arriving after the TTL replays nothing.
    await _send_post(gfs, pid)
    assert await gfs["posts"].get_comment(cid) is None


async def test_gfs_a_comment_on_a_tombstoned_post_is_refused_not_held(gfs):
    pid, cid = _post_id(), _comment_id()
    # The author's delete overtook the create: the tombstone.
    await _send(
        gfs, BOB, "write", "post_delete", _generic(BOB, "post_delete", pid, pid)
    )
    assert (await gfs["posts"].get(pid))[1].deleted
    await _send_comment(gfs, pid, cid)
    assert len(gfs["buffer"]) == 0
    assert await gfs["posts"].get_comment(cid) is None


async def test_gfs_a_flood_from_one_household_stays_bounded(gfs):
    for _ in range(40):
        await _send_comment(gfs, _post_id(), _comment_id())
    assert len(gfs["buffer"]) == 16  # the per-household cap
    # Another household's item is still held — and lands with its post.
    pid, cid = _post_id(), _comment_id(BOB)
    await _send_comment(gfs, pid, cid, signer=BOB)
    assert len(gfs["buffer"]) == 17
    await _send_post(gfs, pid)
    assert await gfs["posts"].get_comment(cid) is not None


async def test_gfs_an_unauthorized_item_is_never_held(gfs):
    """A forged item never reaches the buffer: every check runs first."""
    pid, cid = _post_id(), _comment_id()
    inner = _generic(EVE, "comment", cid, pid, comment_type="text", content="hi")
    inner["content"] = "tampered"  # breaks the author signature
    await _send(gfs, EVE, "comment", "comment", inner)
    assert len(gfs["buffer"]) == 0


# ── The direct federation door (real app, production post-decrypt seam) ───

_msg_ids = itertools.count()


@pytest.fixture
async def direct(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    return app, db


async def _deliver(app, event_type, payload, *, sender) -> None:
    await app[federation_service_key].replay_held(
        FederationEvent(
            msg_id=f"m-{next(_msg_ids)}",
            event_type=event_type,
            from_instance=sender,
            to_instance="us",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=dict(payload),
            space_id=SP,
        ),
        reason="test",
    )


async def _comment_row(db, cid: str):
    return await db.fetchone(
        "SELECT post_id, parent_id, deleted, content FROM space_post_comments"
        " WHERE id=?",
        (cid,),
    )


def _post(pid: str, author: str = "u-a") -> dict:
    return {"id": pid, "author": author, "type": "text", "content": "x"}


def _comment(pid: str, cid: str, author: str = "u-a", **kw) -> dict:
    return {
        "post_id": pid,
        "comment_id": cid,
        "author": author,
        "type": "text",
        "content": "late",
        **kw,
    }


async def test_direct_a_comment_before_its_post_lands_after_the_post(direct):
    app, db = direct
    await _deliver(
        app, FET.SPACE_COMMENT_CREATED, _comment("p-late", "c-late"), sender=AUTHOR
    )
    assert await _comment_row(db, "c-late") is None
    await _deliver(app, FET.SPACE_POST_CREATED, _post("p-late"), sender=AUTHOR)
    row = await _comment_row(db, "c-late")
    assert row is not None and row["post_id"] == "p-late"
    count = await db.fetchone(
        "SELECT comment_count FROM space_posts WHERE id='p-late'", ()
    )
    assert count["comment_count"] == 1


async def test_direct_a_reply_before_its_parent_comment(direct):
    app, db = direct
    await _deliver(
        app,
        FET.SPACE_COMMENT_CREATED,
        _comment("post-a", "c-reply", parent_id="c-parent"),
        sender=AUTHOR,
    )
    assert await _comment_row(db, "c-reply") is None
    await _deliver(
        app, FET.SPACE_COMMENT_CREATED, _comment("post-a", "c-parent"), sender=AUTHOR
    )
    row = await _comment_row(db, "c-reply")
    assert row is not None and row["parent_id"] == "c-parent"


async def test_direct_edits_and_deletes_before_their_create(direct):
    app, db = direct
    # A post delete and a comment delete that overtook their creates.
    await _deliver(app, FET.SPACE_POST_DELETED, {"post_id": "p-gone"}, sender=AUTHOR)
    await _deliver(
        app,
        FET.SPACE_COMMENT_DELETED,
        {"comment_id": "c-gone", "post_id": "post-a"},
        sender=AUTHOR,
    )
    await _deliver(
        app,
        FET.SPACE_COMMENT_UPDATED,
        {"id": "c-edit", "content": "edited"},
        sender=AUTHOR,
    )
    await _deliver(app, FET.SPACE_POST_CREATED, _post("p-gone"), sender=AUTHOR)
    await _deliver(
        app, FET.SPACE_COMMENT_CREATED, _comment("post-a", "c-gone"), sender=AUTHOR
    )
    await _deliver(
        app, FET.SPACE_COMMENT_CREATED, _comment("post-a", "c-edit"), sender=AUTHOR
    )
    post = await db.fetchone("SELECT deleted FROM space_posts WHERE id='p-gone'", ())
    assert post["deleted"] == 1
    assert (await _comment_row(db, "c-gone"))["deleted"] == 1
    assert (await _comment_row(db, "c-edit"))["content"] == "edited"


async def test_direct_a_comment_on_a_tombstoned_post_is_refused(direct):
    app, db = direct
    await _deliver(
        app, FET.SPACE_COMMENT_CREATED, _comment("post-del", "c-on-del"), sender=AUTHOR
    )
    assert await _comment_row(db, "c-on-del") is None
    # Nothing waits: a later re-send of the post (refused — it stays
    # deleted) has nothing to release either.
    await _deliver(app, FET.SPACE_POST_CREATED, _post("post-del"), sender=AUTHOR)
    assert await _comment_row(db, "c-on-del") is None


@pytest.mark.parametrize("demoted", [False, True])
async def test_direct_the_replay_runs_every_gate_again(direct, demoted):
    """A comment held while its household could write is refused on replay
    once that household lost its right (here: demoted to a follower). The
    undemoted run is the positive control."""
    app, db = direct
    await _deliver(
        app,
        FET.SPACE_COMMENT_CREATED,
        _comment("p-late", "c-revoked", author="u-o"),
        sender=OTHER,
    )
    if demoted:
        await db.enqueue(
            "UPDATE space_remote_members SET role='subscriber' WHERE instance_id=?",
            (OTHER,),
        )
    await _deliver(app, FET.SPACE_POST_CREATED, _post("p-late"), sender=AUTHOR)
    assert (await _comment_row(db, "c-revoked") is None) is demoted


async def test_direct_a_comment_naming_someone_else_is_never_held(direct):
    """OTHER may not author as u-a: refused before the buffer, so the post
    landing replays nothing."""
    app, db = direct
    await _deliver(
        app, FET.SPACE_COMMENT_CREATED, _comment("p-late", "c-forged"), sender=OTHER
    )
    await _deliver(app, FET.SPACE_POST_CREATED, _post("p-late"), sender=AUTHOR)
    assert await _comment_row(db, "c-forged") is None
