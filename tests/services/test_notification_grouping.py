"""Notifications know who, what and where (0129, D7).

Covers :class:`NotificationService`'s grouping: one row per (recipient,
type, thing) that re-opens on new activity with counts and people, a fresh
row after a quiet week, the badge counting rows not events, clearing by
object, and rows going with the thing (deleted, left, dissolved).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    CommentAdded,
    CommentDeleted,
    HighlightRemoved,
    MomentDeleted,
    NotificationCreated,
    NotificationReadChanged,
    PostDeleted,
    RemoteSpaceMemberBanned,
    SpaceConfigChanged,
    SpaceMemberLeft,
    TaskDeleted,
)
from socialhome.domain.notification import (
    NotificationActor,
    NotificationObject,
    NotificationSubject,
)
from socialhome.domain.post import Comment, CommentType
from socialhome.domain.space import SpaceConfigEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.notification_repo import (
    SqliteNotificationRepo,
    new_notification,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import notification_service as ns_module
from socialhome.services.notification_service import NotificationService
from socialhome.services.user_service import UserService


@dataclass
class _Spaces:
    """The service needs no space reads for the paths covered here."""

    names: dict[str, str] = field(default_factory=dict)

    async def get(self, space_id: str):
        return None


class _Push:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def push_to_user(self, user_id, payload) -> None:
        self.sent.append((user_id, payload))


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (
            derive_instance_id(kp.public_key),
            kp.private_key.hex(),
            kp.public_key.hex(),
            "aa" * 32,
        ),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    notifs = SqliteNotificationRepo(db, max_per_user=50)
    svc = NotificationService(notifs, users, _Spaces(), bus)
    push = _Push()
    svc.attach_push_service(push)
    svc.wire()
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    class Env:
        pass

    e = Env()
    e.db, e.bus, e.svc, e.notifs, e.push = db, bus, svc, notifs, push
    e.read_changed = []
    e.created = []

    async def _rc(ev: NotificationReadChanged) -> None:
        e.read_changed.append(ev)

    async def _nc(ev: NotificationCreated) -> None:
        e.created.append(ev)

    bus.subscribe(NotificationReadChanged, _rc)
    bus.subscribe(NotificationCreated, _nc)

    async def user(name: str):
        return await user_svc.provision(username=name, display_name=name.title())

    e.user = user
    yield e
    await db.shutdown()


def _comment(author, cid: str, post_id: str = "p1") -> CommentAdded:
    return CommentAdded(
        post_id=post_id,
        comment=Comment(
            id=cid,
            post_id=post_id,
            author=author.user_id,
            type=CommentType.TEXT,
            created_at=datetime.now(UTC),
            content="hi",
        ),
    )


async def _rows(env, user, type: str = "comment_added"):
    return [n for n in await env.notifs.list(user.user_id, limit=50) if n.type == type]


async def _save(env, user, key: str, *, type: str = "x", space_id=None, actor=None):
    """A row about *key* through the gate, as a handler would write it."""
    return await env.svc._save_notif(
        new_notification(user_id=user.user_id, type=type, title=type, link_url="/x"),
        about=NotificationSubject(key, space_id),
        actor_user_id=actor,
    )


# ── 1. Every row says what it is about ────────────────────────────────


def test_every_save_notif_call_names_what_it_is_about():
    """``about`` is a required keyword, and every call in the service passes
    it explicitly — a new type can't land without an object."""
    assert (
        inspect.signature(NotificationService._save_notif).parameters["about"].default
        is inspect.Parameter.empty
    )
    tree = ast.parse(textwrap.dedent(inspect.getsource(ns_module)))
    missing = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_save_notif"
        and not any(k.arg == "about" for k in node.keywords)
    ]
    assert missing == []


async def test_a_row_carries_its_object_actor_and_counts(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    await env.bus.publish(_comment(ben, "c1"))
    [row] = await _rows(env, anna)
    assert row.object_key == "post:p1"
    assert row.space_id is None
    assert row.params.actors == (NotificationActor(ben.user_id, "Ben"),)
    assert (row.params.actor_count, row.params.new, row.params.total) == (1, 1, 1)
    [created] = [e for e in env.created if e.user_id == anna.user_id]
    assert (created.object_key, created.params) == ("post:p1", row.params)
    assert not (created.bumped or created.reopened)
    # D6: someone else's comment on a post that isn't yours is in-app only.
    assert created.silent


# ── 2. One row per thing, re-opened on new activity ───────────────────


async def test_more_activity_on_an_unread_row_bumps_it(env):
    """Two comments on one post: one row, two new, both people named, the
    badge still one — and no push (D6: not a reply to Anna)."""
    anna, ben, cleo = (
        await env.user("anna"),
        await env.user("ben"),
        await env.user("cleo"),
    )
    await env.bus.publish(_comment(ben, "c1"))
    await env.bus.publish(_comment(cleo, "c2"))
    [row] = await _rows(env, anna)
    assert row.link_url == "/post/p1?comment=c2"  # the newest activity
    assert [a.name for a in row.params.actors] == ["Cleo", "Ben"]
    assert (row.params.new, row.params.total) == (2, 2)
    assert await env.notifs.count_unread(anna.user_id) == 1
    second = [e for e in env.created if e.user_id == anna.user_id][1]
    assert (second.bumped, second.reopened, second.silent) == (True, False, True)
    assert second.notification_id == row.id
    assert [u for u, _ in env.push.sent].count(anna.user_id) == 0
    assert not env.read_changed  # the count did not move


async def test_activity_on_a_read_row_reopens_it_and_counts_once(env):
    anna, ben, cleo = (
        await env.user("anna"),
        await env.user("ben"),
        await env.user("cleo"),
    )
    await env.bus.publish(_comment(ben, "c1"))
    await env.bus.publish(_comment(cleo, "c2"))
    [row] = await _rows(env, anna)
    await env.notifs.mark_read(row.id, anna.user_id)
    env.created.clear()
    await env.bus.publish(_comment(ben, "c3"))
    [row] = await _rows(env, anna)
    assert not row.is_read
    # Only what is new since the read is named; the total keeps growing.
    assert [a.name for a in row.params.actors] == ["Ben"]
    assert (row.params.new, row.params.total) == (1, 3)
    assert await env.notifs.count_unread(anna.user_id) == 1
    [ev] = [e for e in env.created if e.user_id == anna.user_id]
    assert (ev.bumped, ev.reopened, ev.silent) == (True, True, True)
    # Every device hears the exact count once (the frame says "bumped", so
    # a client never adds one on top).
    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [
        (anna.user_id, 1)
    ]


async def test_a_quiet_week_starts_a_fresh_row(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    await env.bus.publish(_comment(ben, "c1"))
    [old] = await _rows(env, anna)
    stale = (datetime.now(UTC) - timedelta(days=7, minutes=1)).isoformat()
    await env.notifs.update_group(replace(old, created_at=stale))
    await env.bus.publish(_comment(ben, "c2"))
    rows = await _rows(env, anna)
    assert len(rows) == 2
    assert rows[0].id != old.id and rows[0].params.total == 1


async def test_a_row_inside_the_week_still_regroups(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    await env.bus.publish(_comment(ben, "c1"))
    [old] = await _rows(env, anna)
    six_days = (datetime.now(UTC) - timedelta(days=6)).isoformat()
    await env.notifs.update_group(replace(old, created_at=six_days))
    await env.bus.publish(_comment(ben, "c2"))
    [row] = await _rows(env, anna)
    assert row.id == old.id and row.params.total == 2


async def test_different_things_and_types_keep_their_own_rows(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    await env.bus.publish(_comment(ben, "c1", post_id="p1"))
    await env.bus.publish(_comment(ben, "c2", post_id="p2"))
    await _save(env, anna, "post:p1", type="post_created", actor=ben.user_id)
    assert {r.object_key for r in await _rows(env, anna)} == {"post:p1", "post:p2"}
    assert await env.notifs.count_unread(anna.user_id) == 3


async def test_a_chat_burst_is_silent_while_unread(env):
    """The DM collapse keeps working on top of the general grouping: no
    second push, no second Home Assistant event, the badge as is."""
    anna, ben = await env.user("anna"), await env.user("ben")
    for _ in range(3):
        await _save(env, anna, "dm:c1", type="dm_message", actor=ben.user_id)
    [row] = await _rows(env, anna, "dm_message")
    assert row.params.new == 3
    assert [(e.bumped, e.silent) for e in env.created] == [
        (False, False),
        (True, True),
        (True, True),
    ]
    assert len(env.push.sent) == 1


async def test_a_notice_nobody_caused_names_nobody(env):
    anna = await env.user("anna")
    await _save(env, anna, "task:t1", type="task_deadline")
    await _save(env, anna, "task:t1", type="task_deadline")
    [row] = await _rows(env, anna, "task_deadline")
    assert row.params.actors == () and row.params.actor_count == 0
    assert row.params.new == 2


async def test_an_unknown_actor_is_kept_without_a_name(env):
    anna = await env.user("anna")
    await _save(env, anna, "moment:m1", type="moment_reacted", actor="u-far")
    [row] = await _rows(env, anna, "moment_reacted")
    assert row.params.actors == (NotificationActor("u-far", None),)


# ── 3. Clearing by object ─────────────────────────────────────────────


async def test_mark_read_by_objects_reads_every_type_of_that_thing(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    await env.bus.publish(_comment(ben, "c1"))
    await _save(env, anna, "post:p1", type="post_created", actor=ben.user_id)
    await _save(env, anna, "post:p2", type="post_created", actor=ben.user_id)
    env.read_changed.clear()
    assert await env.svc.mark_read_by_objects(anna.user_id, ["post:p1"]) == 2
    assert await env.notifs.count_unread(anna.user_id) == 1
    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [
        (anna.user_id, 1)
    ]
    # Nothing left to read: no frame.
    assert await env.svc.mark_read_by_objects(anna.user_id, ["post:p1"]) == 0
    assert len(env.read_changed) == 1


# ── 4. The thing is gone: so are its rows ─────────────────────────────


@pytest.mark.parametrize(
    ("key", "event"),
    [
        ("post:p1", PostDeleted(post_id="p1")),
        ("listing:p1", PostDeleted(post_id="p1")),
        ("space_post:p1", PostDeleted(post_id="p1", space_id="s1")),
        (
            "space_comment:c1",
            CommentDeleted(post_id="p1", comment_id="c1", space_id="s1"),
        ),
        ("task:t1", TaskDeleted(task_id="t1", list_id="l1")),
        (
            "moment:m1",
            MomentDeleted(moment_id="m1", author_user_id="u", origin_instance_id="i"),
        ),
        (
            "highlight:h1",
            HighlightRemoved(
                highlight_id="h1", author_user_id="u", audience_kind="all", audience=()
            ),
        ),
    ],
    ids=["post", "listing", "space-post", "space-comment", "task", "moment", "hl"],
)
async def test_deleting_the_thing_deletes_its_rows(env, key, event):
    anna, ben = await env.user("anna"), await env.user("ben")
    await _save(env, anna, key, type="something", actor=ben.user_id)
    await _save(env, ben, key, type="something", actor=anna.user_id)
    other = await _save(env, anna, "post:p9", type="something", actor=ben.user_id)
    env.read_changed.clear()
    await env.bus.publish(event)
    assert [n.id for n in await env.notifs.list(anna.user_id, limit=50)] == [other.id]
    assert await env.notifs.list(ben.user_id, limit=50) == []
    assert sorted((e.user_id, e.unread_count) for e in env.read_changed) == sorted(
        [(anna.user_id, 1), (ben.user_id, 0)]
    )


async def test_a_decision_about_your_own_post_outlives_the_post(env):
    anna = await env.user("anna")
    kept = await _save(env, anna, "space_post:p1", type="post_moderated", space_id="s1")
    await env.bus.publish(PostDeleted(post_id="p1", space_id="s1"))
    assert [n.id for n in await env.notifs.list(anna.user_id, limit=50)] == [kept.id]


async def test_deleting_a_household_comment_keeps_the_post_row(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    await env.bus.publish(_comment(ben, "c1"))
    await env.bus.publish(CommentDeleted(post_id="p1", comment_id="c1"))
    assert len(await _rows(env, anna)) == 1


@pytest.mark.parametrize(
    "event",
    [
        SpaceMemberLeft(space_id="s1", user_id="__anna__"),
        RemoteSpaceMemberBanned(space_id="s1", user_id="__anna__"),
    ],
    ids=["left", "banned"],
)
async def test_leaving_a_space_drops_your_rows_of_it(env, event):
    anna, ben = await env.user("anna"), await env.user("ben")
    await _save(env, anna, "space_post:p1", space_id="s1", actor=ben.user_id)
    ended = await _save(env, anna, "space:s1", type="space_dissolved", space_id="s1")
    elsewhere = await _save(env, anna, "space_post:p2", space_id="s2")
    theirs = await _save(env, ben, "space_post:p1", space_id="s1")
    env.read_changed.clear()
    await env.bus.publish(replace(event, user_id=anna.user_id))
    left = {n.id for n in await env.notifs.list(anna.user_id, limit=50)}
    assert left == {ended.id, elsewhere.id}
    assert [n.id for n in await env.notifs.list(ben.user_id, limit=50)] == [theirs.id]
    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [
        (anna.user_id, 2)
    ]


async def test_a_space_dissolved_here_drops_its_rows_for_everyone(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    await _save(env, anna, "space_post:p1", space_id="s1")
    await _save(env, ben, "space:s1", type="space_member_joined", space_id="s1")
    await env.bus.publish(
        SpaceConfigChanged(
            space_id="s1",
            event_type=SpaceConfigEventType.DISSOLVED.value,
            payload={},
            sequence=1,
        )
    )
    assert await env.notifs.list(anna.user_id, limit=50) == []
    assert await env.notifs.list(ben.user_id, limit=50) == []


async def test_another_config_change_keeps_the_rows(env):
    anna = await env.user("anna")
    await _save(env, anna, "space_post:p1", space_id="s1")
    await env.bus.publish(
        SpaceConfigChanged(
            space_id="s1",
            event_type=SpaceConfigEventType.RENAME.value,
            payload={},
            sequence=1,
        )
    )
    assert len(await env.notifs.list(anna.user_id, limit=50)) == 1


def test_object_kinds_are_distinct():
    assert len({k.value for k in NotificationObject}) == len(NotificationObject)
