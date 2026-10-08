"""Release-blocker protocol tests: a missed post / comment delete heals by
§25.6 sync, and a space syncs its retention window — never a fixed count.

Marked ``@pytest.mark.security``.

Three real households, each a full app over its own SQLite file:

* **H** — holds the host's data; its streams are dispatched as the
  space's host (``HOST``);
* **C** — a member household (``MEMBER``, its user ``u-cora``) that was
  offline while H deleted a post and a comment;
* **D** — a household joining the space later, which catches up from H
  and from C.

Two more households exist only as seats: ``OTHER`` (a plain member) and
``MOD`` (a moderator household).

* A post and a comment deleted while C was offline are deleted on C by H's
  ``posts_deleted`` / ``comments_deleted`` stream; C, once caught up, never
  streams them to D again; a stale copy C spread before cannot come back
  after D heard of the delete.
* A forged tombstone — a household that neither wrote the row nor holds
  content authority — is refused; the author's household and a moderator
  household pass the live ``SPACE_POST_DELETED`` rule.
* A space with ``retention_days = 7`` streams only the last 7 days (its
  tombstones too); a space without retention streams a post 1 200 posts
  back, page by page in bounded chunks.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import orjson
import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    event_bus_key,
    space_sync_receiver_key,
    space_sync_service_key,
)
from socialhome.config import Config
from socialhome.crypto import generate_identity_keypair
from socialhome.domain.events import CommentDeleted, PostDeleted
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.owner_bound_id import (
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.sync.space.exporter import (
    CHUNK_SIZE_BUDGET_BYTES,
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
    ChunkBuilder,
)

pytestmark = pytest.mark.security

SPACE = "sp-posts"
S2 = "sp-other-space"
HOST = "the-host"
MEMBER = "the-member"
OTHER = "other-member"
MOD = "mod-house"
AUTHOR = "u-anna"  # the host's user
CORA = "u-cora"  # C's user
OLIVER = "u-oliver"  # a plain member on OTHER
MOLLY = "u-molly"  # a moderator on MOD
SEATS = (
    (HOST, AUTHOR, "member"),
    (MEMBER, CORA, "member"),
    (OTHER, OLIVER, "member"),
    (MOD, MOLLY, "moderator"),
)
_NOW = datetime.now(timezone.utc)


def _config(tmp_path, name: str) -> Config:
    d = tmp_path / name
    d.mkdir()
    return Config(
        data_dir=str(d),
        db_path=str(d / "sh.db"),
        media_path=str(d / "media"),
        apps_path=str(d / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=1,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://t.example"})},
        ),
    )


async def _seed(db, *, local: str | None = None) -> None:
    """The space hosted by ``HOST``, every household seated; ``local`` is
    the one user who lives on this household."""
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (SPACE, SPACE, HOST, "anna", "00" * 32),
    )
    for instance, user, role in SEATS:
        if user == local:
            await db.enqueue(
                "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
                (user, user, user),
            )
            await db.enqueue(
                "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
                (SPACE, user, role),
            )
            continue
        await db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
            (SPACE, instance),
        )
        await db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES(?,?,?,?)",
            (SPACE, instance, user, role),
        )
        # Every peer advertises v_42+: it names the actor of each write.
        await db.enqueue(
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status, source, proto_version, capabilities_seen_at)"
            " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', 42, ?)",
            (
                instance,
                instance,
                "ab" * 32,
                f"https://{instance}/inbox/x",
                f"{instance}_local",
                _NOW.isoformat(),
            ),
        )


@pytest.fixture
async def houses(aiohttp_client, tmp_path):
    h = create_app(_config(tmp_path, "h"))
    c = create_app(_config(tmp_path, "c"))
    d = create_app(_config(tmp_path, "d"))
    for app in (h, c, d):
        await aiohttp_client(app)
    await _seed(h[db_key])
    await _seed(c[db_key], local=CORA)
    await _seed(d[db_key])
    return h, c, d


def _repo(app):
    return app[space_sync_service_key]._space_post_repo


def _pid(owner: str, space: str = SPACE) -> str:
    return mint_owner_bound_id(SPACE_POST_KIND, space_id=space, owner_user_id=owner)


def _cid(owner: str) -> str:
    return mint_owner_bound_id(SPACE_COMMENT_KIND, space_id=SPACE, owner_user_id=owner)


def _post(pid: str, author: str, *, days: float = 0.0, **kw) -> Post:
    return Post(
        id=pid,
        author=author,
        type=kw.pop("type", PostType.TEXT),
        created_at=_NOW - timedelta(days=days),
        content=f"words of {author}",
        **kw,
    )


def _comment(cid: str, pid: str, author: str, *, days: float = 0.0) -> Comment:
    return Comment(
        id=cid,
        post_id=pid,
        author=author,
        type=CommentType.TEXT,
        created_at=_NOW - timedelta(days=days),
        content=f"a reply by {author}",
    )


async def _hold(apps, *posts: Post, comments: tuple[Comment, ...] = ()) -> None:
    for app in apps:
        for post in posts:
            assert await _repo(app).save(SPACE, post) is not None
        for comment in comments:
            assert await _repo(app).add_comment(comment, space_id=SPACE)
            await _repo(app).increment_comment_count(comment.post_id, space_id=SPACE)


async def _sync(provider_app, to_app, *, provider: str, only=None) -> None:
    """Stream every resource of ``provider_app``'s space to ``to_app``'s
    receiver as ``provider``, in wire order (chunking + crypto aside)."""
    exporters = provider_app[space_sync_service_key]._exporters
    receiver = to_app[space_sync_receiver_key]
    for resource in RESOURCE_ORDER:
        if only is not None and resource not in only:
            continue
        exporter = exporters.get(resource)
        if exporter is None:
            continue
        records = await exporter.list_records(SPACE)
        await receiver._dispatch(resource, SPACE, records, provider=provider)


async def _dispatch(app, resource: str, records: list[dict], *, provider: str):
    await app[space_sync_receiver_key]._dispatch(
        resource, SPACE, records, provider=provider
    )


async def _post_row(app, pid: str) -> dict | None:
    row = await app[db_key].fetchone(
        "SELECT deleted, content, comment_count FROM space_posts WHERE id=?", (pid,)
    )
    return dict(row) if row is not None else None


async def _comment_row(app, cid: str) -> dict | None:
    row = await app[db_key].fetchone(
        "SELECT deleted, content FROM space_post_comments WHERE id=?", (cid,)
    )
    return dict(row) if row is not None else None


async def _live(app, pid: str) -> bool:
    row = await _post_row(app, pid)
    return row is not None and not row["deleted"]


async def _comment_live(app, cid: str) -> bool:
    row = await _comment_row(app, cid)
    return row is not None and not row["deleted"]


async def _host_deletes(h, *, post: str | None = None, comment: Comment | None = None):
    """H deletes (a moderator removal of somebody's post, a comment)."""
    if post is not None:
        assert await _repo(h).soft_delete(post, space_id=SPACE, moderated_by=AUTHOR)
    if comment is not None:
        assert await _repo(h).soft_delete_comment(comment.id, space_id=SPACE)
        await _repo(h).decrement_comment_count(comment.post_id, space_id=SPACE)


async def _scenario(h, c):
    """C's post P and H's post Q (with a comment of each user) held by H
    and C; H deletes P and AUTHOR's comment while C is offline."""
    p = _post(_pid(CORA), CORA, days=1)
    q = _post(_pid(AUTHOR), AUTHOR, days=1)
    k_gone = _comment(_cid(AUTHOR), q.id, AUTHOR, days=0.5)
    k_kept = _comment(_cid(CORA), q.id, CORA, days=0.5)
    await _hold((h, c), p, q, comments=(k_gone, k_kept))
    await _host_deletes(h, post=p.id, comment=k_gone)
    return p, q, k_gone, k_kept


# ── A missed delete heals ────────────────────────────────────────────────


async def test_a_post_and_comment_deleted_while_offline_are_deleted_on_catch_up(
    houses,
):
    h, c, _d = houses
    events: list = []
    c[event_bus_key].subscribe(PostDeleted, events.append)
    c[event_bus_key].subscribe(CommentDeleted, events.append)
    p, q, k_gone, k_kept = await _scenario(h, c)
    assert await _live(c, p.id) and await _comment_live(c, k_gone.id)

    await _sync(h, c, provider=HOST)

    row = await _post_row(c, p.id)
    assert row is not None and row["deleted"] and row["content"] is None
    gone = await _comment_row(c, k_gone.id)
    assert gone is not None and gone["deleted"] and gone["content"] is None
    assert await _live(c, q.id) and await _comment_live(c, k_kept.id)
    assert (await _post_row(c, q.id))["comment_count"] == 1
    # Published as the live deletes are — from the provider (no re-broadcast).
    assert {(type(e).__name__, e.origin_instance_id) for e in events} == {
        ("PostDeleted", HOST),
        ("CommentDeleted", HOST),
    }
    # A second stream is a no-op (nothing published twice, count unchanged).
    await _sync(h, c, provider=HOST)
    assert len(events) == 2
    assert (await _post_row(c, q.id))["comment_count"] == 1


async def test_a_caught_up_household_never_re_exports_them_to_a_joiner(houses):
    h, c, d = houses
    p, _q, k_gone, k_kept = await _scenario(h, c)
    await _sync(h, c, provider=HOST)
    exporters = c[space_sync_service_key]._exporters
    posts = {r["id"] for r in await exporters["posts"].list_records(SPACE)}
    comments = {r["id"] for r in await exporters["comments"].list_records(SPACE)}
    assert p.id not in posts and k_gone.id not in comments and k_kept.id in comments
    # C's stream to the joiner: the deleted post never lands, no content
    # rides the tombstone resources.
    gone = await exporters["posts_deleted"].list_records(SPACE)
    assert [r["id"] for r in gone] == [p.id]
    assert all(set(r) == {"id", "post_id", "author", "created_at"} for r in gone)
    await _sync(c, d, provider=MEMBER)
    assert not await _live(d, p.id)


async def test_a_stale_copy_spread_to_a_joiner_heals_and_cannot_return(houses):
    h, c, d = houses
    p, *_ = await _scenario(h, c)
    # C, still stale, streams its own user's post to D — D takes it.
    await _sync(c, d, provider=MEMBER, only={"posts"})
    assert await _live(d, p.id)
    # The host's stream tells D; C's stale stream cannot bring it back.
    await _sync(h, d, provider=HOST)
    assert not await _live(d, p.id)
    await _sync(c, d, provider=MEMBER, only={"posts"})
    assert not await _live(d, p.id)


async def test_a_hosts_tombstone_stubs_an_id_never_held(houses):
    h, c, d = houses
    p, *_ = await _scenario(h, c)
    # D never saw P: the host's tombstone leaves the soft-deleted row …
    await _sync(h, d, provider=HOST, only={"posts_deleted"})
    row = await _post_row(d, p.id)
    assert row is not None and row["deleted"]
    # … so C's stale stream of it afterwards cannot create it.
    await _sync(c, d, provider=MEMBER, only={"posts"})
    assert not await _live(d, p.id)


async def test_a_hosts_comment_tombstone_stubs_on_a_held_post_only(houses):
    h, _c, d = houses
    q = _post(_pid(AUTHOR), AUTHOR)
    await _hold((d,), q)
    k = _comment(_cid(CORA), q.id, CORA)
    stub = {"id": k.id, "post_id": q.id, "author": CORA}
    await _dispatch(d, "comments_deleted", [stub], provider=HOST)
    row = await _comment_row(d, k.id)
    assert row is not None and row["deleted"]
    # A late create of the comment stays out.
    await _dispatch(
        d,
        "comments",
        [{"id": k.id, "post_id": q.id, "author": CORA, "content": "back"}],
        provider=HOST,
    )
    assert not await _comment_live(d, k.id)
    # A tombstone on a post not held here records nothing.
    other = _cid(CORA)
    await _dispatch(
        d,
        "comments_deleted",
        [{"id": other, "post_id": "p-not-here", "author": CORA}],
        provider=HOST,
    )
    assert await _comment_row(d, other) is None


async def test_a_member_households_tombstone_never_stubs(houses):
    _h, c, _d = houses
    pid = _pid(CORA)
    await _dispatch(c, "posts_deleted", [{"id": pid, "author": CORA}], provider=MEMBER)
    assert await _post_row(c, pid) is None


async def test_a_tombstone_for_an_id_not_bound_here_records_nothing(houses):
    _h, _c, d = houses
    squat = _pid(CORA, space=S2)  # another space's id
    unbound = "legacy-post-id"
    await _dispatch(
        d,
        "posts_deleted",
        [{"id": squat, "author": CORA}, {"id": unbound, "author": CORA}],
        provider=HOST,
    )
    assert await _post_row(d, squat) is None
    assert await _post_row(d, unbound) is None


# ── Who may stream a delete ─────────────────────────────────────────────


async def test_a_forged_tombstone_is_refused(houses):
    h, c, _d = houses
    q = _post(_pid(AUTHOR), AUTHOR)
    k = _comment(_cid(CORA), q.id, CORA)
    await _hold((h, c), q, comments=(k,))
    # OTHER: neither the author's household nor a moderator household.
    await _dispatch(
        c, "posts_deleted", [{"id": q.id, "author": AUTHOR}], provider=OTHER
    )
    await _dispatch(
        c,
        "comments_deleted",
        [{"id": k.id, "post_id": q.id, "author": CORA}],
        provider=OTHER,
    )
    assert await _live(c, q.id) and await _comment_live(c, k.id)
    # Lying about the author changes nothing: the held row's author counts.
    await _dispatch(
        c, "posts_deleted", [{"id": q.id, "author": OLIVER}], provider=OTHER
    )
    assert await _live(c, q.id)
    # An unseated household is refused too.
    await _dispatch(
        c, "posts_deleted", [{"id": q.id, "author": AUTHOR}], provider="nobody"
    )
    assert await _live(c, q.id)


async def test_the_authors_household_may_stream_its_own_delete(houses):
    h, c, _d = houses
    p = _post(_pid(CORA), CORA)
    k = _comment(_cid(CORA), p.id, CORA)
    await _hold((h, c), p, comments=(k,))
    await _repo(c).soft_delete_comment(k.id, space_id=SPACE)
    await _repo(c).soft_delete(p.id, space_id=SPACE)
    # H (receiving as a member would) hears C's tombstones.
    await _sync(c, h, provider=MEMBER, only={"posts_deleted", "comments_deleted"})
    assert not await _live(h, p.id) and not await _comment_live(h, k.id)


async def test_a_moderator_household_may_stream_a_delete_in_an_open_space(houses):
    h, c, _d = houses
    q = _post(_pid(AUTHOR), AUTHOR)
    k = _comment(_cid(AUTHOR), q.id, AUTHOR)
    await _hold((h, c), q, comments=(k,))
    await _dispatch(
        c,
        "comments_deleted",
        [{"id": k.id, "post_id": q.id, "author": AUTHOR}],
        provider=MOD,
    )
    assert not await _comment_live(c, k.id)
    await _dispatch(c, "posts_deleted", [{"id": q.id, "author": AUTHOR}], provider=MOD)
    assert not await _live(c, q.id)


async def test_a_restricted_posts_level_still_takes_the_authors_own_delete(houses):
    h, c, _d = houses
    await c[db_key].enqueue(
        "UPDATE spaces SET posts_access='moderated' WHERE id=?", (SPACE,)
    )
    q = _post(_pid(AUTHOR), AUTHOR)
    p = _post(_pid(OLIVER), OLIVER)
    await _hold((c,), q, p)
    # A moderator household's tombstone names no deleter: under review it
    # is refused (the host's own stream carries that delete) …
    await _dispatch(c, "posts_deleted", [{"id": q.id, "author": AUTHOR}], provider=MOD)
    assert await _live(c, q.id)
    # … while the author's own household removes its own post.
    await _dispatch(
        c, "posts_deleted", [{"id": p.id, "author": OLIVER}], provider=OTHER
    )
    assert not await _live(c, p.id)
    # The host's stream is taken whole.
    await _dispatch(c, "posts_deleted", [{"id": q.id, "author": AUTHOR}], provider=HOST)
    assert not await _live(c, q.id)


async def test_tombstones_land_in_a_space_archived_here(houses):
    h, c, _d = houses
    p, q, *_ = await _scenario(h, c)
    await c[db_key].enqueue(
        "UPDATE spaces SET archived=1, archived_reason='removed' WHERE id=?",
        (SPACE,),
    )
    late = _post(_pid(AUTHOR), AUTHOR)
    await _hold((h,), late)
    await _sync(h, c, provider=HOST)
    assert not await _live(c, p.id)  # the removal landed …
    assert await _post_row(c, late.id) is None  # … new content did not
    assert {"posts_deleted", "comments_deleted"} <= REMOVAL_RESOURCES


# ── The retention window, no fixed cap ──────────────────────────────────


async def test_a_space_with_retention_syncs_only_its_window(houses):
    h, _c, d = houses
    await h[db_key].enqueue("UPDATE spaces SET retention_days=7 WHERE id=?", (SPACE,))
    new = _post(_pid(AUTHOR), AUTHOR, days=1)
    old = _post(_pid(AUTHOR), AUTHOR, days=10)
    old_poll = _post(_pid(AUTHOR), AUTHOR, days=10, type=PostType.POLL)
    new_gone = _post(_pid(CORA), CORA, days=2)
    old_gone = _post(_pid(CORA), CORA, days=12)
    await _hold(
        (h,),
        new,
        old,
        old_poll,
        new_gone,
        old_gone,
        comments=(
            _comment(_cid(CORA), new.id, CORA, days=0.5),
            _comment(_cid(CORA), old.id, CORA, days=9),
        ),
    )
    await _host_deletes(h, post=new_gone.id)
    await _host_deletes(h, post=old_gone.id)
    await _sync(h, d, provider=HOST)
    held = {
        r["id"]
        for r in await d[db_key].fetchall(
            "SELECT id FROM space_posts WHERE space_id=? AND deleted=0", (SPACE,)
        )
    }
    assert held == {new.id}
    stubs = {
        r["id"]
        for r in await d[db_key].fetchall(
            "SELECT id FROM space_posts WHERE space_id=? AND deleted=1", (SPACE,)
        )
    }
    assert stubs == {new_gone.id}
    comments = await d[db_key].fetchall("SELECT post_id FROM space_post_comments")
    assert [r["post_id"] for r in comments] == [new.id]
    # A post type the space exempts from retention streams at any age.
    await h[db_key].enqueue(
        "UPDATE spaces SET retention_exempt_json='[\"poll\"]' WHERE id=?", (SPACE,)
    )
    await _sync(h, d, provider=HOST)
    assert await _live(d, old_poll.id) and not await _live(d, old.id)


async def _bulk_posts(app, n: int) -> None:
    """``n`` posts by AUTHOR, the oldest a year back, in one statement."""
    await app[db_key].enqueue(
        "WITH RECURSIVE s(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM s WHERE i < ?)"
        " INSERT INTO space_posts(id, space_id, author, type, content, created_at)"
        " SELECT printf('bulk-%05d', i), ?, ?, 'text', 'post ' || i,"
        " strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', printf('-%d hours', ? - i))"
        " FROM s",
        (n - 1, SPACE, AUTHOR, n * 7),
    )


class _PlainCrypto:
    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")


async def test_without_retention_a_post_1200_back_still_syncs(houses):
    h, _c, d = houses
    await _bulk_posts(h, 1200)
    exporter = h[space_sync_service_key]._exporters["posts"]
    # Streamed in bounded chunks over bounded pages — every post once.
    builder = ChunkBuilder(
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_PlainCrypto(),  # type: ignore[arg-type]
    )
    seen: list[str] = []
    seq = 0
    async for chunk in builder.build_chunks(
        exporter=exporter, space_id=SPACE, sync_id="s-1", sig_suite="ed25519"
    ):
        assert len(orjson.dumps(chunk)) <= CHUNK_SIZE_BUDGET_BYTES
        assert chunk["seq_start"] == seq
        seq = chunk["seq_end"]
        payload = base64.urlsafe_b64decode(chunk["encrypted_payload"])
        seen.extend(r["id"] for r in orjson.loads(payload)["records"])
    assert len(seen) == len(set(seen)) == 1200 == seq
    assert "bulk-00000" in seen  # the oldest, 1 200 posts back
    # And the joiner stores all of them.
    await _sync(h, d, provider=HOST, only={"posts"})
    row = await d[db_key].fetchone(
        "SELECT COUNT(*) AS n FROM space_posts WHERE space_id=? AND deleted=0",
        (SPACE,),
    )
    assert row["n"] == 1200
    assert await _live(d, "bulk-00000")
