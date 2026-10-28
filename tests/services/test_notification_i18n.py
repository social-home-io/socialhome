"""Tests for NotificationService i18n integration."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

import orjson
import pytest

import socialhome.i18n as i18n_pkg
import socialhome.infrastructure.calendar_reminder_scheduler as calendar_reminder_scheduler_module
import socialhome.services.notification_service as notification_service_module
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    CommentAdded,
    DmMessageCreated,
    HighlightFrameReactionChanged,
    MomentCreated,
    MomentReactionChanged,
    PostCreated,
    RemoteSpaceDissolved,
    RemoteSpaceInviteAccepted,
    SpaceAdminSeedsRetiredAfterRestore,
    SpaceLocationFeatureEnabled,
    SpacePostModerated,
    TaskAssigned,
    UserFollowed,
)
from socialhome.domain.post import Comment, CommentType, Post, PostType
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
    _PUSH_AS_IS,
    _PUSH_GENERIC,
    _PUSH_TITLES,
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


async def test_language_picked_in_the_app_wins(env):
    """The SPA saves the language a person picks as ``preferences.locale``
    (``PATCH /api/me``) — nothing writes ``users.locale``. A row written
    for someone who picked German must read German (it rendered in English
    in the German UI: #1217's screenshots)."""
    bus, repo, db = env
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, preferences_json)"
        " VALUES('carl', 'c-id', 'Carl', ?)",
        (orjson.dumps({"locale": "de"}).decode(),),
    )
    # Alice's column says English; her latest pick in the app is German.
    await db.enqueue(
        "UPDATE users SET preferences_json=? WHERE username='alice'",
        (orjson.dumps({"locale": "de"}).decode(),),
    )
    p2 = Post(
        id="p2",
        author="b-id",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )
    await bus.publish(PostCreated(post=p2))
    assert (await repo.list("c-id"))[0].title == "Bob hat gepostet"
    assert (await repo.list("a-id"))[0].title == "Bob hat gepostet"


@pytest.mark.parametrize("prefs", ["{}", "not json", '{"locale": 7}', "[]"])
def test_locale_falls_back_to_the_column(prefs):
    user = SimpleNamespace(locale="nl", preferences_json=prefs)
    assert NotificationService._locale(user) == "nl"
    assert NotificationService._locale(None) is None


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
    """Every who-and-where push title ships in every locale, and the push
    for a task assigned to a German speaker reads German — who assigned it,
    without the task title."""
    shipped = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
    variants = [v for vs in _PUSH_TITLES.values() for v in vs] + [_PUSH_GENERIC]
    for locale in ("en", "de", "es", "fr", "nl"):
        for key, _fallback in variants:
            assert shipped.gettext(key, locale=locale) != key, (locale, key)
    db = AsyncDatabase(tmp_dir / "p.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, locale)"
            " VALUES('bob', 'b-id', 'Bob', 'de')",
        )
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, locale)"
            " VALUES('alice', 'a-id', 'Alice', 'en')",
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
        assert push.titles == ["Alice hat dir eine Aufgabe zugewiesen"]
    finally:
        await db.shutdown()


async def test_invite_answers_name_who_and_where_in_the_inviters_language(tmp_dir):
    """An invite answer says who answered and which space — in every locale,
    and in the inviter's own language."""
    shipped = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
    for locale in ("en", "de", "es", "fr", "nl"):
        for key in (
            "notification.space.remote_invite.accepted",
            "notification.space.remote_invite.declined",
        ):
            msg = shipped.gettext(key, locale=locale, name="Zoe", space_name="Crew")
            assert "Zoe" in msg and "Crew" in msg, (locale, key)
    db = AsyncDatabase(tmp_dir / "inv.db", batch_timeout_ms=10)
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
        await bus.publish(
            RemoteSpaceInviteAccepted(
                space_id="sp1",
                instance_id="peer",
                invitee_user_id="u-zoe",
                inviter_user_id="b-id",
                invitee_display_name="Zoe",
            )
        )
        notes = await repo.list("b-id")
        assert [n.title for n in notes] == [
            "Zoe hat deine Einladung zu Crew angenommen"
        ]
    finally:
        await db.shutdown()


# ─── Every title speaks the recipient's language ───────────────────────

_MESSAGES = Path(i18n_pkg.__file__).parent / "messages"
_LOCALES = ("en", "de", "es", "fr", "nl")
_SOURCES = (
    Path(notification_service_module.__file__),
    Path(calendar_reminder_scheduler_module.__file__),
)
#: Keys the service builds at runtime (``f"notification.{…}.{x}"``):
#: their constant prefix → every value it is completed with.
_DYNAMIC_KEYS = {
    "notification.space.feature.": ("pages", "tasks", "stickies", "calendar"),
    "notification.space.moderation.": ("approved", "rejected", "expired"),
}
_KEY = re.compile(r"^notification\.[a-z_]+(\.[a-z_]+)*$")
_PUSH_TITLES_ALL = [v for vs in _PUSH_TITLES.values() for v in vs] + [_PUSH_GENERIC]


def _catalogs() -> dict[str, dict[str, str]]:
    return {
        loc: orjson.loads((_MESSAGES / f"{loc}.json").read_bytes()) for loc in _LOCALES
    }


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in Formatter().parse(text) if f}


def _notification_keys() -> set[str]:
    """Every catalog key the notification code can look up."""
    keys: set[str] = set()
    for source in _SOURCES:
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _KEY.match(node.value):
                    keys.add(node.value)
            elif isinstance(node, ast.JoinedStr):
                head = node.values[0]
                if (
                    isinstance(head, ast.Constant)
                    and isinstance(head.value, str)
                    and head.value.startswith("notification.")
                ):
                    assert head.value in _DYNAMIC_KEYS, (
                        f"new runtime-built key {head.value}…: list its values "
                        "in _DYNAMIC_KEYS"
                    )
                    keys.update(head.value + v for v in _DYNAMIC_KEYS[head.value])
    return keys


def test_every_notification_key_ships_in_every_locale():
    """Each key the notification service uses exists in all five catalogs,
    with the same placeholders as English — none falls back to English."""
    catalogs = _catalogs()
    keys = _notification_keys()
    assert len(keys) > 60  # the walk found the service's keys
    missing = [
        (loc, k) for k in sorted(keys) for loc in _LOCALES if k not in catalogs[loc]
    ]
    assert missing == []
    mismatched = [
        (loc, k)
        for k in sorted(keys)
        for loc in _LOCALES
        if _fields(catalogs[loc][k]) != _fields(catalogs["en"][k])
    ]
    assert mismatched == []


def test_every_fallback_matches_its_english_catalog_entry():
    """A ``_t(key, fallback=…)`` fallback has the same placeholders as the
    English catalog entry, so a row reads right before or without the
    catalog."""
    catalogs = _catalogs()
    tree = ast.parse(_SOURCES[0].read_text(encoding="utf-8"))
    pairs: list[tuple[str, str]] = list(_PUSH_TITLES_ALL)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_t"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            for kw in node.keywords:
                if kw.arg == "fallback" and isinstance(kw.value, ast.Constant):
                    pairs.append((node.args[0].value, kw.value.value))
    assert len(pairs) > 40
    bad = [(k, f) for k, f in pairs if _fields(f) != _fields(catalogs["en"].get(k, ""))]
    assert bad == []


def _has_literal_text(node: ast.AST) -> bool:
    """Whether *node* builds text from an English literal — an f-string or
    a non-empty string constant — outside a catalog lookup."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"_t", "_someone", "_content_free_title"}
    ):
        return False
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return True
    return any(_has_literal_text(child) for child in ast.iter_child_nodes(node))


def test_no_title_is_built_from_a_literal_english_string():
    """A stored title or body never comes from an English literal: every
    one goes through the catalog in the recipient's language. (Regression:
    "{name} messaged you", "commented on a post", "Your post was
    moderated", "“{space}” was dissolved", "Location sharing turned on…",
    the household calendar's "{summary} — in N min".)"""
    offenders: list[str] = []
    for source in _SOURCES:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            values: list[ast.AST] = []
            if (
                isinstance(node, ast.keyword)
                and node.arg in {"title", "body"}
                or isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id in {"title", "body"}
                    for t in node.targets
                )
            ):
                values.append(node.value)
            offenders += [
                f"{source.name}:{v.lineno}" for v in values if _has_literal_text(v)
            ]
    assert offenders == []


def test_the_literal_title_check_catches_a_literal():
    """The check above does fire on the shapes it bans."""
    for code in (
        'new_notification(title=f"{name} messaged you")',
        'new_notification(title="Your post was moderated")',
    ):
        call = ast.parse(code).body[0].value
        assert _has_literal_text(call.keywords[0].value), code
    assert _has_literal_text(ast.parse('name + " joined"').body[0].value)
    assert not _has_literal_text(
        ast.parse('self._t("k", locale=loc, fallback="{a} posted", a=n)').body[0].value
    )


def _created_types() -> set[str]:
    """Every notification type the service writes."""
    tree = ast.parse(_SOURCES[0].read_text(encoding="utf-8"))
    types: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in {
            "type",
            "ntype",
            "generic_type",
        }:
            if isinstance(node.value, ast.Constant) and node.value.value:
                types.add(node.value.value)
            elif isinstance(node.value, ast.JoinedStr):
                head = node.value.values[0]
                assert isinstance(head, ast.Constant)
                assert head.value == "bazaar_listing_", head.value
                types |= {"bazaar_listing_sold", "bazaar_listing_expired"}
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Tuple)
            and isinstance(node.targets[0].elts[0], ast.Name)
            and node.targets[0].elts[0].id == "ntype"
            and isinstance(node.value, ast.Tuple)
            and isinstance(node.value.elts[0], ast.Constant)
        ):
            types.add(node.value.elts[0].value)
    return types


def test_every_type_decides_what_its_push_says():
    """D11: each type the service creates either pushes its bell title (it
    names only people, spaces and chats) or has a who-and-where push title
    — never both, never neither."""
    created = _created_types()
    assert {"dm_message", "space_mention", "space_here", "moment_reacted"} <= created
    assert not (_PUSH_AS_IS & _PUSH_TITLES.keys())
    assert created - _PUSH_AS_IS - _PUSH_TITLES.keys() == set()


# ─── The formerly English-only titles, in German ───────────────────────


@pytest.fixture
async def de_env(tmp_dir):
    """Bob (de) and Alice (en) in the space "Crew"; the shipped catalog."""
    db = AsyncDatabase(tmp_dir / "de.db", batch_timeout_ms=10)
    await db.startup()
    for row in (("bob", "b-id", "Bob", "de"), ("alice", "a-id", "Alice", "en")):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, locale)"
            " VALUES(?, ?, ?, ?)",
            row,
        )
    spaces = SqliteSpaceRepo(db)
    await spaces.save(
        Space(
            id="sp1",
            name="Crew",
            owner_instance_id="iid",
            owner_username="alice",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    for uid, role in (("a-id", SpaceRole.OWNER), ("b-id", SpaceRole.MEMBER)):
        await spaces.save_member(
            SpaceMember(
                space_id="sp1",
                user_id=uid,
                role=role.value,
                joined_at="2026-01-01 00:00:00",
            )
        )
    bus = EventBus()
    repo = SqliteNotificationRepo(db)
    users = SqliteUserRepo(db)
    svc = NotificationService(
        repo, users, spaces, bus, i18n=Catalog.from_directory(_MESSAGES)
    )
    svc.wire()
    yield SimpleNamespace(bus=bus, repo=repo, svc=svc, users=users)
    await db.shutdown()


async def _bob_titles(env) -> list[str]:
    return [n.title for n in await env.repo.list("b-id")]


async def test_a_household_comment_reads_german(de_env):
    await de_env.bus.publish(
        CommentAdded(
            post_id="p1",
            comment=Comment(
                id="c1",
                post_id="p1",
                author="a-id",
                type=CommentType.TEXT,
                created_at=datetime(2026, 4, 15, tzinfo=UTC),
                content="secret",
            ),
        )
    )
    assert await _bob_titles(de_env) == ["Alice hat einen Beitrag kommentiert"]


@pytest.mark.parametrize(
    ("message_type", "expected"),
    [
        ("text", "Alice hat dir geschrieben"),
        ("location", "Alice hat einen Ort geteilt"),
    ],
)
async def test_a_chat_message_reads_german(de_env, message_type, expected):
    """D3: a chat message makes no Notifications row; its push reads German."""
    push = _PushCapture()
    de_env.svc.attach_push_service(push)
    await de_env.bus.publish(
        DmMessageCreated(
            conversation_id="c1",
            message_id="m1",
            sender_user_id="a-id",
            sender_display_name="Alice",
            recipient_user_ids=("b-id",),
            content="secret",
            message_type=message_type,
        )
    )
    assert await _bob_titles(de_env) == []
    assert push.titles == [expected]


async def test_space_notices_read_german(de_env):
    await de_env.bus.publish(
        SpacePostModerated(
            space_id="sp1",
            post=Post(
                id="p1",
                author="b-id",
                type=PostType.TEXT,
                content="secret",
                created_at=datetime(2026, 4, 15, tzinfo=UTC),
            ),
            moderated_by="a-id",
        )
    )
    await de_env.bus.publish(
        SpaceLocationFeatureEnabled(
            space_id="sp1", space_name="Crew", actor_user_id="a-id"
        )
    )
    assert sorted(await _bob_titles(de_env)) == [
        "Dein Beitrag in Crew wurde moderiert",
        "Standortfreigabe in Crew eingeschaltet",
    ]


async def test_a_dissolved_space_reads_german(de_env):
    await de_env.bus.publish(RemoteSpaceDissolved(space_id="sp1"))
    notes = await de_env.repo.list("b-id")
    assert [(n.title, n.body) for n in notes] == [
        (
            "„Crew“ wurde aufgelöst",
            (
                "Der Eigentümer hat den Space aufgelöst. Deine Kopie ist jetzt ein "
                "schreibgeschütztes Archiv."
            ),
        )
    ]


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(7, "Elternabend – in 7 Min."), (0, "Elternabend – jetzt")],
)
async def test_an_upcoming_event_reads_german(de_env, minutes, expected):
    bob = await de_env.users.get_by_user_id("b-id")
    await de_env.svc.notify_upcoming_event(
        bob, event_id="e1", summary="Elternabend", minutes=minutes
    )
    notes = await de_env.repo.list("b-id")
    assert [(n.type, n.title, n.link_url) for n in notes] == [
        ("calendar_reminder", expected, "/calendar?event=e1")
    ]
