"""Tests for NotificationService i18n integration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import socialhome.i18n as i18n_pkg
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    HighlightFrameReactionChanged,
    MomentCreated,
    MomentReactionChanged,
    PostCreated,
    SpaceAdminSeedsRetiredAfterRestore,
    TaskAssigned,
    UserFollowed,
)
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.task import Task, TaskStatus
from socialhome.i18n import Catalog
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import (
    _PUSH_SAFE_TITLES,
    NotificationService,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    # Two users, one with locale='de'.
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, locale) VALUES('alice', 'a-id', 'Alice', 'en')",
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, locale) VALUES('bob', 'b-id', 'Bob', 'de')",
    )

    bus = EventBus()
    notif_repo = SqliteNotificationRepo(db)
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db)

    cat = Catalog()
    cat.load_locale(
        "en",
        {
            "notification.post.created": "{author} posted",
            "notification.task.assigned": "Assigned: {title}",
            "notification.space.post.created": "{author} posted in {space_name}",
        },
    )
    cat.load_locale(
        "de",
        {
            "notification.post.created": "{author} hat gepostet",
            "notification.task.assigned": "Zugewiesen: {title}",
            "notification.space.post.created": "{author} im Raum {space_name}",
        },
    )

    svc = NotificationService(notif_repo, user_repo, space_repo, bus, i18n=cat)
    svc.wire()
    yield bus, notif_repo, db
    await db.shutdown()


def _post():
    return Post(
        id="p1",
        author="a-id",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


# ─── Per-recipient locale ───────────────────────────────────────────────


async def test_post_created_uses_recipient_locale(env):
    bus, repo, _ = env
    await bus.publish(PostCreated(post=_post()))
    bob = await repo.list("b-id")
    assert bob
    assert "gepostet" in bob[0].title


async def test_post_created_alice_gets_english(env):
    bus, repo, _ = env
    # Alice is the author — she shouldn't get her own notification.
    # Make Bob the author instead.
    await repo._db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES('p2', 'b-id', 'text', 'hi')",
    )
    p2 = Post(
        id="p2",
        author="b-id",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )
    await bus.publish(PostCreated(post=p2))
    alice = await repo.list("a-id")
    assert alice
    assert "Bob posted" in alice[0].title


async def test_task_assigned_uses_recipient_locale(env):
    bus, repo, _ = env
    now = datetime(2026, 4, 15, tzinfo=UTC)
    task = Task(
        id="t1",
        list_id="l1",
        title="Buy milk",
        status=TaskStatus.TODO,
        position=0,
        created_by="a-id",
        created_at=now,
        updated_at=now,
    )
    await bus.publish(TaskAssigned(task=task, assigned_to="b-id"))
    bob = await repo.list("b-id")
    assert bob
    assert "Zugewiesen" in bob[0].title


# ─── Shipped catalogs ───────────────────────────────────────────────────


async def test_post_restore_admin_notice_is_translated_for_the_owner(tmp_dir):
    """v_44 F5: the post-restore "review space admins" notice goes through
    the shipped catalog — every locale has it, and the owner reads it in
    their own language."""
    shipped = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
    for locale in ("en", "de", "es", "fr", "nl"):
        msg = shipped.gettext("notification.space.admins_reconfirm", locale=locale)
        assert msg != "notification.space.admins_reconfirm", locale
    db = AsyncDatabase(tmp_dir / "n.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, locale)"
            " VALUES('bob', 'b-id', 'Bob', 'de')",
        )
        bus = EventBus()
        spaces = SqliteSpaceRepo(db)
        await spaces.save(
            Space(
                id="sp1",
                name="Crew",
                owner_instance_id="iid",
                owner_username="bob",
                identity_public_key="00" * 32,
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.PRIVATE,
                join_mode=JoinMode.INVITE_ONLY,
            )
        )
        await spaces.save_member(
            SpaceMember(
                space_id="sp1",
                user_id="b-id",
                role=SpaceRole.OWNER.value,
                joined_at="2026-01-01 00:00:00",
            )
        )
        repo = SqliteNotificationRepo(db)
        NotificationService(repo, SqliteUserRepo(db), spaces, bus, i18n=shipped).wire()
        await bus.publish(SpaceAdminSeedsRetiredAfterRestore(space_id="sp1"))
        notes = [
            n for n in await repo.list("b-id") if n.type == "space_admins_reconfirm"
        ]
        assert [n.title for n in notes] == [
            shipped.gettext("notification.space.admins_reconfirm", locale="de")
        ]
        assert "Backup" in notes[0].title
    finally:
        await db.shutdown()


# ─── Momentum: named, in the recipient's language ───────────────────────


async def test_moment_notifications_name_the_person_and_are_translated(tmp_dir):
    """A reaction, reply or follow names the person — by the name this
    household holds for them, never their raw user id — in the
    recipient's language (shipped catalogs, every locale)."""
    shipped = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
    keys = (
        "notification.moment.reacted",
        "notification.moment.replied",
        "notification.user.followed",
        "notification.highlight.reacted",
        "notification.someone",
    )
    for locale in ("en", "de", "es", "fr", "nl"):
        for key in keys:
            assert shipped.gettext(key, locale=locale) != key, (locale, key)
    db = AsyncDatabase(tmp_dir / "m.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, locale)"
            " VALUES('bob', 'b-id', 'Bob', 'de'), ('anna', 'a-id', 'Anna', 'en')",
        )
        bus = EventBus()
        repo = SqliteNotificationRepo(db)
        NotificationService(
            repo, SqliteUserRepo(db), SqliteSpaceRepo(db), bus, i18n=shipped
        ).wire()
        # Anna (named) reacts to Bob's moment; someone nobody here knows
        # replies and follows him.
        await bus.publish(
            MomentReactionChanged(
                moment_id="m-1",
                reactor_user_id="a-id",
                author_user_id="b-id",
                emoji="🔥",
            )
        )
        await bus.publish(
            MomentCreated(
                moment_id="m-2",
                author_user_id="ru-unknown-7f3a",
                content="hey",
                media_url=None,
                media_type=None,
                duration_ms=None,
                parent_moment_id="m-1",
                parent_author_user_id="b-id",
                origin_instance_id="peer",
                expires_at="2026-12-01T00:00:00+00:00",
            )
        )
        await bus.publish(
            UserFollowed(follower_user_id="ru-unknown-7f3a", followed_user_id="b-id")
        )
        await bus.publish(
            HighlightFrameReactionChanged(
                highlight_id="h-1",
                frame_id="f-1",
                reactor_user_id="a-id",
                author_user_id="b-id",
                emoji="❤️",
            )
        )
        titles = {n.type: n.title for n in await repo.list("b-id")}
        assert titles["highlight_reacted"] == shipped.gettext(
            "notification.highlight.reacted", locale="de", name="Anna", emoji="❤️"
        )
        someone = shipped.gettext("notification.someone", locale="de")
        assert titles["moment_reacted"] == shipped.gettext(
            "notification.moment.reacted", locale="de", name="Anna", emoji="🔥"
        )
        assert titles["moment_replied"] == shipped.gettext(
            "notification.moment.replied", locale="de", name=someone
        )
        assert titles["user_followed"] == shipped.gettext(
            "notification.user.followed", locale="de", name=someone
        )
        assert not any("ru-unknown" in t for t in titles.values())
    finally:
        await db.shutdown()


# ─── Content-free push titles (§25.3) ───────────────────────────────────


class _PushCapture:
    def __init__(self) -> None:
        self.titles: list[str] = []

    async def push_to_user(self, user_id, payload) -> int:
        self.titles.append(payload.title)
        return 1


async def test_content_free_push_titles_are_translated(tmp_dir):
    """Every generic push title ships in every locale, and the push for a
    task assigned to a German speaker reads German, without the task title."""
    shipped = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
    for locale in ("en", "de", "es", "fr", "nl"):
        for key, _fallback in _PUSH_SAFE_TITLES.values():
            assert shipped.gettext(key, locale=locale) != key, (locale, key)
    db = AsyncDatabase(tmp_dir / "p.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, locale)"
            " VALUES('bob', 'b-id', 'Bob', 'de')",
        )
        bus = EventBus()
        svc = NotificationService(
            SqliteNotificationRepo(db),
            SqliteUserRepo(db),
            SqliteSpaceRepo(db),
            bus,
            i18n=shipped,
        )
        push = _PushCapture()
        svc.attach_push_service(push)
        svc.wire()
        now = datetime.now(UTC)
        await bus.publish(
            TaskAssigned(
                task=Task(
                    id="t1",
                    list_id="l1",
                    title="Steuererklärung",
                    status=TaskStatus.TODO,
                    position=0,
                    created_by="a-id",
                    created_at=now,
                    updated_at=now,
                    assignees=("b-id",),
                ),
                assigned_to="b-id",
            )
        )
        assert push.titles == ["Dir wurde eine Aufgabe zugewiesen"]
    finally:
        await db.shutdown()
