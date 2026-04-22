"""Tests for NotificationService i18n integration."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    PostCreated,
    TaskAssigned,
)
from socialhome.domain.post import Post, PostType
from socialhome.domain.task import Task, TaskStatus
from socialhome.i18n import Catalog
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import NotificationService


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
        created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
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
    await repo._db.enqueue(  # noqa: SLF001
        "INSERT INTO feed_posts(id, author, type, content) VALUES('p2', 'b-id', 'text', 'hi')",
    )
    p2 = Post(
        id="p2",
        author="b-id",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )
    await bus.publish(PostCreated(post=p2))
    alice = await repo.list("a-id")
    assert alice
    assert "Bob posted" in alice[0].title


async def test_task_assigned_uses_recipient_locale(env):
    bus, repo, _ = env
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
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
