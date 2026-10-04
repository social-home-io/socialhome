"""Release-blocker protocol tests: member-published comments, reactions and
own edits / deletes over the GFS relay (v_49, PR 3).

Marked ``@pytest.mark.security``.

Drives the REAL :class:`SpacePublicInbound` over real SQLite repos with real
writer certs, as a follower household receives ``space_item`` frames. Pins
the per-type authorization matrix — the relay is never more permissive than
the host path:

* a comment needs a ``comment`` (or ``write``) cert naming its author;
* a post edit or delete needs ``write`` — the post's own right — and only
  the post's author may make it;
* a comment edit or delete: only the comment's author;
* a reaction needs ``write`` at a follower (it cannot tell a comment-only
  member from a follower) unless the space lets followers react;
* a stale epoch, a forged author, a re-wrap to another type, a binding that
  does not name the author: refused for every type.

And ordering independence (hard requirement 5): a delete that overtakes its
create leaves a tombstone neither this relay nor the host relay resurrects;
an edit that overtakes its create is the create at its newest content; an
older edit never lands over a newer one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.owner_bound_id import (
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    mint_owner_bound_id,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_item_author import (
    build_signed_item_inner,
    item_signing_bytes,
)
from socialhome.services.space_public_author import (
    author_signing_bytes,
    build_signed_author_inner,
)
from socialhome.services.space_public_inbound import SpacePublicInbound
from socialhome.services.space_writer_cert_service import SpaceWriterCertService
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

pytestmark = pytest.mark.security

SPACE = "sp-items"


class _Author:
    def __init__(self, username: str) -> None:
        self.kp = generate_identity_keypair()
        self.username = username
        self.user_id = derive_user_id(self.kp.public_key, username)
        self.origin = derive_instance_id(self.kp.public_key)


BOB = _Author("bob")
EVE = _Author("eve")


def _now(seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
async def env(tmp_dir):
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
    inbound = SpacePublicInbound(
        bus=EventBus(), space_repo=spaces, space_crypto=crypto, space_post_repo=posts
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
    # Bob's post and comment, already held here.
    post_id = mint_owner_bound_id(
        SPACE_POST_KIND, space_id=SPACE, owner_user_id=BOB.user_id
    )
    await posts.save(
        SPACE,
        Post(
            id=post_id,
            author=BOB.user_id,
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="bob's post",
        ),
    )
    comment_id = mint_owner_bound_id(
        SPACE_COMMENT_KIND, space_id=SPACE, owner_user_id=BOB.user_id
    )
    await posts.add_comment(
        Comment(
            id=comment_id,
            post_id=post_id,
            author=BOB.user_id,
            type=CommentType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="bob's comment",
        ),
        space_id=SPACE,
    )
    yield {
        "db": db,
        "spaces": spaces,
        "crypto": crypto,
        "posts": posts,
        "inbound": inbound,
        "space_kp": space_kp,
        "post_id": post_id,
        "comment_id": comment_id,
    }
    await db.shutdown()


async def _cert(env, signer: _Author, scope: str, *, users=None, epoch=None) -> dict:
    epoch = epoch if epoch is not None else await env["crypto"].get_current_epoch(SPACE)
    cert = sign_writer_cert(
        space_seed=env["space_kp"].private_key,
        space_id=SPACE,
        epoch=epoch,
        instance_pk=signer.kp.public_key,
        scope=scope,
    )
    return bind_writer_users(
        cert,
        space_seed=env["space_kp"].private_key,
        user_ids=users if users is not None else [signer.user_id],
    ).to_wire()


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
        ts=kw.pop("ts", _now(-1)),
        **kw,
    )


def _post_inner(
    signer: _Author, post_id: str, item_type: str, *, content: str, **kw
) -> dict:
    return build_signed_author_inner(
        post=Post(
            id=post_id,
            author=signer.user_id,
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content=content,
        ),
        space_id=SPACE,
        author_username=signer.username,
        author_pk=signer.kp.public_key,
        author_identity_seed=signer.kp.private_key,
        origin_instance_id=signer.origin,
        item_type=item_type,
        item_target=post_id,
        **kw,
    )


async def _send(env, item_type: str, inner: dict, cert: dict, *, epoch=None):
    inner = {**inner, "writer_cert": cert}
    sealed_epoch, ct = await env["crypto"].encrypt(
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
            "epoch": sealed_epoch if epoch is None else epoch,
            "writer_cert": v1,
            "payload": ct,
        }
    )


# ── The item each matrix row sends, and how to tell it landed ─────────────


def _build(env, item_type: str, signer: _Author) -> dict:
    pid, cid = env["post_id"], env["comment_id"]
    if item_type == "comment":
        new_cid = mint_owner_bound_id(
            SPACE_COMMENT_KIND, space_id=SPACE, owner_user_id=signer.user_id
        )
        env["new_comment_id"] = new_cid
        return _generic(
            signer, "comment", new_cid, pid, comment_type="text", content="new"
        )
    if item_type == "comment_edit":
        return _generic(
            signer, "comment_edit", cid, pid, comment_type="text", content="edited"
        )
    if item_type == "comment_delete":
        return _generic(signer, "comment_delete", cid, pid)
    if item_type == "post_edit":
        return _post_inner(
            signer, pid, "post_edit", content="edited", edited_at=_now(-1)
        )
    if item_type == "post_delete":
        return _generic(signer, "post_delete", pid, pid)
    return _generic(signer, item_type, pid, pid, emoji="👍")


async def _landed(env, item_type: str) -> bool:
    posts = env["posts"]
    post = (await posts.get(env["post_id"]))[1]
    comment = await posts.get_comment(env["comment_id"])
    if item_type == "comment":
        return await posts.get_comment(env["new_comment_id"]) is not None
    if item_type == "comment_edit":
        return comment.content == "edited"
    if item_type == "comment_delete":
        return comment.deleted
    if item_type == "post_edit":
        return post.content == "edited"
    if item_type == "post_delete":
        return post.deleted
    return "👍" in post.reactions


MATRIX = [
    # (item type, cert scope, signer, lands?)
    ("comment", "comment", BOB, True),
    ("comment", "write", BOB, True),
    ("comment", "comment", EVE, True),  # anyone with a comment right
    ("comment_edit", "comment", BOB, True),
    ("comment_edit", "write", EVE, False),  # not the author
    ("comment_delete", "comment", BOB, True),
    ("comment_delete", "write", EVE, False),  # not the author
    ("post_edit", "write", BOB, True),
    ("post_edit", "comment", BOB, False),  # needs the post's own right
    ("post_edit", "write", EVE, False),  # not the author
    ("post_delete", "write", BOB, True),
    ("post_delete", "comment", BOB, False),
    ("post_delete", "write", EVE, False),
    ("reaction_add", "write", EVE, True),
    # A follower can't tell a comment-only member from a follower.
    ("reaction_add", "comment", EVE, False),
]


@pytest.mark.parametrize(("item_type", "scope", "signer", "lands"), MATRIX)
async def test_the_authorization_matrix(env, item_type, scope, signer, lands):
    await _send(
        env, item_type, _build(env, item_type, signer), await _cert(env, signer, scope)
    )
    assert await _landed(env, item_type) is lands


async def test_reactions_need_no_write_cert_where_followers_may_react(env):
    space = await env["spaces"].get(SPACE)
    await env["spaces"].save(
        Space(
            **{
                **{f: getattr(space, f) for f in space.__dataclass_fields__},
                "features": SpaceFeatures(
                    allow_subscribers=True, allow_subscriber_react=True
                ),
            }
        )
    )
    await _send(
        env,
        "reaction_add",
        _build(env, "reaction_add", EVE),
        await _cert(env, EVE, "comment"),
    )
    assert await _landed(env, "reaction_add")


TYPES = (
    "comment",
    "comment_edit",
    "comment_delete",
    "post_edit",
    "post_delete",
    "reaction_add",
)


@pytest.mark.parametrize("item_type", TYPES)
async def test_a_stale_epoch_is_refused_for_every_type(env, item_type):
    old_epoch = await env["crypto"].get_current_epoch(SPACE)
    cert = await _cert(env, BOB, "write", epoch=old_epoch)
    inner = {**_build(env, item_type, BOB), "writer_cert": cert}
    _e, ct = await env["crypto"].encrypt(
        SPACE, json.dumps({"item_type": item_type, "inner": inner}).encode()
    )
    await env["crypto"].rotate_epoch(SPACE)
    await env["crypto"].rotate_epoch(SPACE)
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
            "epoch": old_epoch,
            "writer_cert": v1,
            "payload": ct,
        }
    )
    assert not await _landed(env, item_type)


@pytest.mark.parametrize("item_type", TYPES)
async def test_a_forged_author_is_refused_for_every_type(env, item_type):
    """Eve signs, claiming to be Bob (Bob's user id and key), with Bob's
    household cert: the author signature does not verify."""
    inner = dict(_build(env, item_type, BOB))
    sig_bytes = (
        author_signing_bytes(inner)
        if item_type == "post_edit"
        else item_signing_bytes(inner)
    )
    inner["author_sig"] = b64url_encode(sign_ed25519(EVE.kp.private_key, sig_bytes))
    await _send(env, item_type, inner, await _cert(env, BOB, "write"))
    assert not await _landed(env, item_type)


@pytest.mark.parametrize(
    ("signed_as", "wrapped_as"),
    [
        ("comment_edit", "comment_delete"),
        ("comment_delete", "post_delete"),
        ("reaction_add", "reaction_remove"),
        ("post_edit", "post"),
    ],
)
async def test_a_re_wrap_to_another_type_is_refused(env, signed_as, wrapped_as):
    inner = _build(env, signed_as, BOB)
    await _send(env, wrapped_as, inner, await _cert(env, BOB, "write"))
    assert not await _landed(env, signed_as)
    assert not (await env["posts"].get(env["post_id"]))[1].deleted


@pytest.mark.parametrize("item_type", TYPES)
async def test_a_binding_not_naming_the_author_is_refused(env, item_type):
    cert = await _cert(env, BOB, "write", users=["someone-else"])
    await _send(env, item_type, _build(env, item_type, BOB), cert)
    assert not await _landed(env, item_type)


# ── Ordering independence ──────────────────────────────────────────────────


async def _host_relay(env, inner: dict) -> None:
    """The host's ``space_post_public`` copy of a post."""
    epoch, ct = await env["crypto"].encrypt(SPACE, json.dumps(inner).encode())
    envelope = {"space_id": SPACE, "epoch": epoch, "encrypted_payload": ct}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=SPACE,
            payload=strip_authority_sig_fields(envelope),
            space_seed=env["space_kp"].private_key,
        )
    )
    await env["inbound"].handle(
        {
            "type": "relay",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": envelope,
        }
    )


async def test_a_post_delete_before_its_create_is_never_resurrected(env):
    pid = mint_owner_bound_id(
        SPACE_POST_KIND, space_id=SPACE, owner_user_id=BOB.user_id
    )
    cert = await _cert(env, BOB, "write")
    await _send(env, "post_delete", _generic(BOB, "post_delete", pid, pid), cert)
    # The create, member-published and host-relayed, arrives afterwards.
    await _send(env, "post", _post_inner(BOB, pid, "post", content="late"), cert)
    host_copy = build_signed_author_inner(
        post=Post(
            id=pid,
            author=BOB.user_id,
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="late",
        ),
        space_id=SPACE,
        author_username=BOB.username,
        author_pk=BOB.kp.public_key,
        author_identity_seed=BOB.kp.private_key,
        origin_instance_id=BOB.origin,
    )
    await _host_relay(env, host_copy)
    got = await env["posts"].get(pid)
    assert got is not None and got[1].deleted and got[1].content is None


async def test_a_comment_delete_before_its_create_is_never_resurrected(env):
    cid = mint_owner_bound_id(
        SPACE_COMMENT_KIND, space_id=SPACE, owner_user_id=BOB.user_id
    )
    cert = await _cert(env, BOB, "comment")
    pid = env["post_id"]
    await _send(env, "comment_delete", _generic(BOB, "comment_delete", cid, pid), cert)
    await _send(
        env,
        "comment",
        _generic(BOB, "comment", cid, pid, comment_type="text", content="late"),
        cert,
    )
    got = await env["posts"].get_comment(cid)
    assert got.deleted and got.content is None


async def test_an_edit_before_its_create_and_out_of_order_edits(env):
    pid = mint_owner_bound_id(
        SPACE_POST_KIND, space_id=SPACE, owner_user_id=BOB.user_id
    )
    cert = await _cert(env, BOB, "write")
    newer = _post_inner(BOB, pid, "post_edit", content="v3", edited_at=_now(-1))
    older = _post_inner(BOB, pid, "post_edit", content="v2", edited_at=_now(-20))
    await _send(env, "post_edit", newer, cert)
    await _send(env, "post_edit", older, cert)
    await _send(env, "post", _post_inner(BOB, pid, "post", content="v1"), cert)
    assert (await env["posts"].get(pid))[1].content == "v3"
