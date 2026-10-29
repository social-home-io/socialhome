"""Tests for socialhome.repositories.notification_repo."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.notification import (
    NotificationActor,
    NotificationHandled,
    NotificationParams,
)
from socialhome.domain.notification_kinds import SpaceNotifPref
from socialhome.repositories._spec import Spec
from socialhome.repositories.notification_repo import (
    SqliteNotificationRepo,
    new_notification,
)


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a notification repo over a real SQLite database."""
    from socialhome.crypto import derive_instance_id, generate_identity_keypair
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.notif_repo = SqliteNotificationRepo(db, max_per_user=10)
    yield e
    await db.shutdown()


async def test_notification_cap(env):
    """Notifications are capped at max_per_user (10 in this env)."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("notif_user", "uid-notif-user", "NotifUser"),
    )
    uid = "uid-notif-user"

    for i in range(15):
        await env.notif_repo.save(
            new_notification(
                user_id=uid,
                type="test",
                title=f"Notification {i}",
            )
        )

    notes = await env.notif_repo.list(uid, limit=50)
    assert len(notes) == 10


async def test_notification_mark_read(env):
    """mark_read flags one notification read without affecting others."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("notif_user2", "uid-notif2", "NotifUser2"),
    )
    uid = "uid-notif2"
    n1 = new_notification(user_id=uid, type="x", title="N1")
    n2 = new_notification(user_id=uid, type="x", title="N2")
    await env.notif_repo.save(n1)
    await env.notif_repo.save(n2)
    await env.notif_repo.mark_read(n1.id, uid)
    assert (await env.notif_repo.get(n1.id)).is_read
    assert not (await env.notif_repo.get(n2.id)).is_read
    assert await env.notif_repo.count_unread(uid) == 1


async def test_notification_delete_old(env):
    """delete_old removes notifications past the threshold."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("old_user", "uid-old", "OldUser"),
    )
    uid = "uid-old"
    n = new_notification(user_id=uid, type="x", title="Old")
    await env.notif_repo.save(n)
    await env.db.enqueue(
        "UPDATE notifications SET created_at='2000-01-01T00:00:00Z' WHERE id=?",
        (n.id,),
    )
    purged = await env.notif_repo.delete_old(older_than_days=30)
    assert purged >= 1


# ── Per-space notification preferences ─────────────────────────────────────


async def test_get_space_notif_level_is_none_until_chosen(env):
    """D5: no row means "never chose" — the service picks the default by
    space type and seat, so the repo must not invent ``all``."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("u1", "uid-u1", "U1"),
    )
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-n", "N", "iid", "u1", "aabb" * 16),
    )
    level = await env.notif_repo.get_space_notif_level(
        user_id="uid-u1", space_id="sp-n"
    )
    assert level is None


async def test_set_and_get_space_notif_level(env):
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("u2", "uid-u2", "U2"),
    )
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-n2", "N2", "iid", "u2", "aabb" * 16),
    )
    await env.notif_repo.set_space_notif_level(
        user_id="uid-u2", space_id="sp-n2", level="muted"
    )
    assert (
        await env.notif_repo.get_space_notif_level(user_id="uid-u2", space_id="sp-n2")
        == "muted"
    )
    # Upsert flips to mentions.
    await env.notif_repo.set_space_notif_level(
        user_id="uid-u2", space_id="sp-n2", level="mentions"
    )
    assert (
        await env.notif_repo.get_space_notif_level(user_id="uid-u2", space_id="sp-n2")
        == "mentions"
    )


async def _space_seat(env, uid: str, sid: str) -> None:
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (uid, uid, uid),
    )
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (sid, sid, "iid", uid, "aabb" * 16),
    )


async def test_a_timed_mute_reads_as_muted_then_the_level_before(env):
    """D9 (0130): while ``muted_until`` runs the space is muted; after it
    the stored level — the one from before — applies again."""
    await _space_seat(env, "uid-t", "sp-t")
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await env.notif_repo.set_space_notif_level(
        user_id="uid-t", space_id="sp-t", level="mentions", muted_until=soon
    )
    assert (
        await env.notif_repo.get_space_notif_level(user_id="uid-t", space_id="sp-t")
        == "muted"
    )
    pref = await env.notif_repo.get_space_notif_pref(user_id="uid-t", space_id="sp-t")
    assert pref == SpaceNotifPref("mentions", soon)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await env.notif_repo.set_space_notif_level(
        user_id="uid-t", space_id="sp-t", level="mentions", muted_until=past
    )
    assert (
        await env.notif_repo.get_space_notif_level(user_id="uid-t", space_id="sp-t")
        == "mentions"
    )
    # Picking a level without a time ends the timed mute.
    await env.notif_repo.set_space_notif_level(
        user_id="uid-t", space_id="sp-t", level="all"
    )
    assert await env.notif_repo.get_space_notif_pref(
        user_id="uid-t", space_id="sp-t"
    ) == SpaceNotifPref("all", None)
    assert await env.notif_repo.list_space_notif_prefs("uid-t") == [
        ("sp-t", SpaceNotifPref("all", None))
    ]
    assert await env.notif_repo.list_space_notif_prefs("nobody") == []


# ── Retention keeps work that is still waiting ────────────────────────


async def _user(env, uid: str = "uid-r") -> str:
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (uid, uid, uid),
    )
    return uid


async def _save(env, uid, *, type="test", stamp: str, read: bool = False):
    note = new_notification(user_id=uid, type=type, title=f"{type} {stamp}")
    note = replace(note, created_at=stamp)
    await env.notif_repo.save(note)
    if read:
        await env.notif_repo.mark_read(note.id, uid)
    return note


async def test_cap_prune_drops_read_rows_before_unread_ones(env):
    """The cap used to drop the oldest rows, unread included: a burst of
    news silently erased things the user never saw while older read rows
    stayed."""
    uid = await _user(env)
    unread_old = await _save(env, uid, stamp="2026-01-01T00:00:00+00:00")
    for i in range(9):
        await _save(env, uid, stamp=f"2026-02-0{i + 1}T00:00:00+00:00", read=True)
    # 10 rows (the cap); one more lands.
    await _save(env, uid, stamp="2026-03-01T00:00:00+00:00")
    ids = {n.id for n in await env.notif_repo.list(uid, limit=50)}
    assert len(ids) == 10
    assert unread_old.id in ids  # the oldest *read* row went instead


async def test_cap_prune_never_drops_an_unread_action_item(env):
    """A pending join request must not vanish under a flood of unread news."""
    uid = await _user(env)
    request = await _save(
        env, uid, type="space_join_requested", stamp="2026-01-01T00:00:00+00:00"
    )
    for i in range(15):
        await _save(env, uid, stamp=f"2026-02-{i + 10}T00:00:00+00:00")
    notes = await env.notif_repo.list(uid, limit=50)
    assert len(notes) == 10
    assert request.id in {n.id for n in notes}


async def test_cap_prune_drops_a_read_action_item_like_any_row(env):
    uid = await _user(env)
    handled = await _save(
        env,
        uid,
        type="moderation_pending",
        stamp="2026-01-01T00:00:00+00:00",
        read=True,
    )
    for i in range(10):
        await _save(env, uid, stamp=f"2026-02-{i + 10}T00:00:00+00:00")
    assert handled.id not in {n.id for n in await env.notif_repo.list(uid, limit=50)}


async def test_delete_old_keeps_unread_action_items(env):
    """The 90-day purge spares a request nobody has handled yet."""
    uid = await _user(env)
    old = "2020-01-01T00:00:00+00:00"
    waiting = await _save(env, uid, type="space_report", stamp=old)
    handled = await _save(env, uid, type="space_report", stamp=old, read=True)
    news = await _save(env, uid, stamp=old)
    purged = await env.notif_repo.delete_old(older_than_days=90)
    left = {n.id for n in await env.notif_repo.list(uid, limit=50)}
    assert purged == 2
    assert waiting.id in left
    assert handled.id not in left and news.id not in left


async def test_list_unread_and_mark_read_ids_touch_only_the_owner(env):
    uid = await _user(env)
    other = await _user(env, "uid-o")
    mine = await _save(env, uid, stamp="2026-01-01T00:00:00+00:00")
    read = await _save(env, uid, stamp="2026-01-02T00:00:00+00:00", read=True)
    theirs = await _save(env, other, stamp="2026-01-03T00:00:00+00:00")
    assert [n.id for n in await env.notif_repo.list_unread(uid)] == [mine.id]
    assert await env.notif_repo.mark_read_ids(uid, [mine.id, read.id, theirs.id]) == 1
    assert await env.notif_repo.count_unread(uid) == 0
    assert await env.notif_repo.count_unread(other) == 1
    assert await env.notif_repo.mark_read_ids(uid, []) == 0


async def test_list_unread_only_pages_through_unread_rows(env):
    """The page's Unread tab: only rows not read yet, newest first, and the
    ``before`` cursor still applies."""
    uid = await _user(env)
    old = await _save(env, uid, stamp="2026-01-01T00:00:00+00:00")
    await _save(env, uid, stamp="2026-01-02T00:00:00+00:00", read=True)
    new = await _save(env, uid, stamp="2026-01-03T00:00:00+00:00")
    rows = await env.notif_repo.list(uid, unread_only=True)
    assert [n.id for n in rows] == [new.id, old.id]
    older = await env.notif_repo.list(
        uid, unread_only=True, before="2026-01-03T00:00:00+00:00"
    )
    assert [n.id for n in older] == [old.id]


async def _action(env, uid, key, *, type="space_join_requested", params=True):
    note = replace(
        new_notification(user_id=uid, type=type, title="t"),
        object_key=key,
        params=NotificationParams() if params else None,
    )
    await env.notif_repo.save(note)
    return note


async def test_resolve_object_marks_every_recipients_row_handled(env):
    a = await _user(env, "uid-a")
    b = await _user(env, "uid-b")
    c = await _user(env, "uid-c")
    na = await _action(env, a, "join_request:r1")
    nb = await _action(env, b, "join_request:r1", params=False)  # pre-0129 shape
    await _action(env, c, "join_request:r2")  # another request
    await _action(env, c, "join_request:r1", type="comment_added")  # another type
    await env.notif_repo.mark_read(na.id, a)  # read: still resolves
    handled = NotificationHandled("approved", NotificationActor("uid-s", "Søren"))
    users = await env.notif_repo.resolve_object(
        "join_request:r1", types=["space_join_requested"], handled=handled
    )
    assert sorted(users) == ["uid-a", "uid-b"]
    for note in (na, nb):
        back = await env.notif_repo.get(note.id)
        assert back.is_read
        assert back.params.handled == handled
        assert not back.is_pending_action
    assert await env.notif_repo.count_unread(c) == 2
    # The first answer stands: a late duplicate changes nothing.
    again = await env.notif_repo.resolve_object(
        "join_request:r1",
        types=["space_join_requested"],
        handled=NotificationHandled("declined"),
    )
    assert again == []
    assert (await env.notif_repo.get(na.id)).params.handled.outcome == "approved"
    assert (
        await env.notif_repo.resolve_object(
            "join_request:r1", types=[], handled=handled
        )
        == []
    )


async def test_resolve_object_can_touch_one_recipient_only(env):
    a = await _user(env, "uid-a")
    b = await _user(env, "uid-b")
    na = await _action(env, a, "space_invite:s1", type="space_invite")
    nb = await _action(env, b, "space_invite:s1", type="space_invite")
    users = await env.notif_repo.resolve_object(
        "space_invite:s1",
        types=["space_invite"],
        handled=NotificationHandled("declined"),
        user_id=a,
    )
    assert users == [a]
    assert not (await env.notif_repo.get(na.id)).is_pending_action
    assert (await env.notif_repo.get(nb.id)).is_pending_action


async def test_list_pending_actions_keeps_read_rows_until_handled(env):
    a = await _user(env, "uid-a")
    n1 = await _action(env, a, "join_request:r1")
    n2 = await _action(env, a, "space_invite:s1", type="space_invite")
    await _action(env, a, "post:p1", type="comment_added")
    await env.notif_repo.mark_read(n2.id, a)
    rows = await env.notif_repo.list_pending_actions(a)
    assert {n.id for n in rows} == {n1.id, n2.id}
    await env.notif_repo.resolve_object(
        "join_request:r1",
        types=["space_join_requested"],
        handled=NotificationHandled("declined"),
    )
    assert [n.id for n in await env.notif_repo.list_pending_actions(a)] == [n2.id]
    assert len(await env.notif_repo.list_pending_actions(a, limit=0)) == 0


# ── Subject, groups, by object / by space (0129) ─────────────────────────


async def _about(
    env,
    uid,
    key,
    *,
    type="comment_added",
    space_id=None,
    stamp="2026-10-20T10:00:00+00:00",
    read=False,
):
    note = replace(
        new_notification(user_id=uid, type=type, title="t"),
        created_at=stamp,
        object_key=key,
        space_id=space_id,
        params=NotificationParams(
            actors=(NotificationActor("u-ben", "Ben"),), actor_count=1
        ),
    )
    await env.notif_repo.save(note)
    if read:
        await env.notif_repo.mark_read(note.id, uid)
    return note


async def test_save_and_read_back_object_space_and_params(env):
    uid = await _user(env)
    note = await _about(env, uid, "space_post:p1", space_id="s1")
    back = await env.notif_repo.get(note.id)
    assert (back.object_key, back.space_id) == ("space_post:p1", "s1")
    assert back.params == note.params
    # A row from before 0129 reads with no subject.
    old = new_notification(user_id=uid, type="x", title="old")
    await env.notif_repo.save(old)
    back = await env.notif_repo.get(old.id)
    assert (back.object_key, back.space_id, back.params) == (None, None, None)


async def test_unreadable_params_read_as_none(env):
    uid = await _user(env)
    note = await _about(env, uid, "post:p1")
    await env.db.enqueue("UPDATE notifications SET params='[1]' WHERE id=?", (note.id,))
    assert (await env.notif_repo.get(note.id)).params is None


async def test_latest_in_group_matches_user_type_key_and_window(env):
    uid = await _user(env)
    other = await _user(env, "uid-o")
    await _about(env, uid, "post:p1", stamp="2026-10-01T00:00:00+00:00")
    recent = await _about(
        env, uid, "post:p1", stamp="2026-10-20T00:00:00+00:00", read=True
    )
    await _about(env, uid, "post:p1", type="post_created")
    await _about(env, other, "post:p1")
    await _about(env, uid, "post:p2")
    found = await env.notif_repo.latest_in_group(
        user_id=uid,
        type="comment_added",
        object_key="post:p1",
        since="2026-10-15T00:00:00+00:00",
    )
    assert found.id == recent.id and found.is_read  # read rows count too
    assert (
        await env.notif_repo.latest_in_group(
            user_id=uid,
            type="comment_added",
            object_key="post:p1",
            since="2026-10-21T00:00:00+00:00",
        )
        is None
    )


async def test_update_group_rewrites_the_row(env):
    uid = await _user(env)
    note = await _about(env, uid, "post:p1", read=True)
    params = NotificationParams(
        actors=(NotificationActor("u-anna", "Anna"),), actor_count=1, total=2
    )
    await env.notif_repo.update_group(
        replace(
            note,
            title="Anna commented",
            link_url="/post/p1?comment=c2",
            created_at="2026-10-21T00:00:00+00:00",
            read_at=None,
            params=params,
        )
    )
    back = await env.notif_repo.get(note.id)
    assert back.title == "Anna commented"
    assert back.link_url == "/post/p1?comment=c2"
    assert back.created_at == "2026-10-21T00:00:00+00:00"
    assert not back.is_read and back.params == params
    assert await env.notif_repo.count_unread(uid) == 1


async def test_update_group_moves_the_subject(env):
    """D10: a waiting game invite moves on to the sender's newest session."""
    uid = await _user(env)
    note = await _about(env, uid, "app_session:chess/s1")
    await env.notif_repo.update_group(replace(note, object_key="app_session:chess/s2"))
    back = await env.notif_repo.get(note.id)
    assert back.object_key == "app_session:chess/s2"


async def test_mark_read_by_object_touches_only_the_owner_and_keys(env):
    uid = await _user(env)
    other = await _user(env, "uid-o")
    await _about(env, uid, "post:p1")
    await _about(env, uid, "post:p1", type="post_created")
    keep = await _about(env, uid, "post:p2")
    await _about(env, other, "post:p1")
    assert await env.notif_repo.mark_read_by_object(uid, ["post:p1", "post:p1"]) == 2
    assert [n.id for n in await env.notif_repo.list_unread(uid)] == [keep.id]
    assert await env.notif_repo.count_unread(other) == 1
    assert await env.notif_repo.mark_read_by_object(uid, []) == 0


async def test_delete_by_object_spares_kept_types_and_names_unread_owners(env):
    a = await _user(env, "uid-a")
    b = await _user(env, "uid-b")
    await _about(env, a, "space_post:p1")
    await _about(env, b, "space_post:p1", read=True)
    decision = await _about(env, a, "space_post:p1", type="post_moderated")
    other = await _about(env, a, "space_post:p2")
    touched = await env.notif_repo.delete_by_object(
        ["space_post:p1"], keep_types=["post_moderated"]
    )
    assert touched == ["uid-a"]  # b's row was read: their badge didn't move
    left = {n.id for n in await env.notif_repo.list(a, limit=50)}
    assert left == {decision.id, other.id}
    assert await env.notif_repo.list(b, limit=50) == []
    assert await env.notif_repo.delete_by_object([]) == []


async def test_delete_by_space_for_one_user_or_everyone(env):
    a = await _user(env, "uid-a")
    b = await _user(env, "uid-b")
    await _about(env, a, "space_post:p1", space_id="s1")
    await _about(env, b, "space_post:p1", space_id="s1")
    ended = await _about(env, a, "space:s1", type="space_dissolved", space_id="s1")
    elsewhere = await _about(env, a, "space_post:p9", space_id="s2")
    assert await env.notif_repo.delete_by_space("s1", user_id=b) == ["uid-b"]
    assert await env.notif_repo.count_unread(b) == 0
    assert await env.notif_repo.count_unread(a) == 3
    touched = await env.notif_repo.delete_by_space("s1", keep_types=["space_dissolved"])
    assert touched == ["uid-a"]
    left = {n.id for n in await env.notif_repo.list(a, limit=50)}
    assert left == {ended.id, elsewhere.id}


async def test_find_filters_on_object_key_and_space(env):
    uid = await _user(env)
    note = await _about(env, uid, "post:p1", space_id="s1")
    await _about(env, uid, "post:p2")
    found = await env.notif_repo.find(
        Spec(where=[("object_key", "=", "post:p1"), ("space_id", "=", "s1")])
    )
    assert [n.id for n in found] == [note.id]


async def test_exists_in_space_since_reads_the_persisted_here_window(env):
    """The per-space @here cooldown: a ``space_here`` row about the space at
    or after ``since`` — any user's, any post's."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("u1", "uid-u1", "U1"),
    )
    note = replace(
        new_notification(user_id="uid-u1", type="space_here", title="t"),
        space_id="sp-h",
        created_at="2026-10-28T10:00:00+00:00",
    )
    await env.notif_repo.save(note)
    kw = {"type": "space_here", "space_id": "sp-h"}
    assert await env.notif_repo.exists_in_space_since(
        **kw, since="2026-10-28T09:55:00+00:00"
    )
    assert not await env.notif_repo.exists_in_space_since(
        **kw, since="2026-10-28T10:00:01+00:00"
    )
    assert not await env.notif_repo.exists_in_space_since(
        type="space_here", space_id="other", since="2026-10-28T09:00:00+00:00"
    )
    assert not await env.notif_repo.exists_in_space_since(
        type="space_mention", space_id="sp-h", since="2026-10-28T09:00:00+00:00"
    )


# ── Batched forms (the delivery worker's one-job writes) ─────────────────


async def test_latest_in_groups_matches_each_key_like_latest_in_group(env):
    uid = await _user(env)
    other = await _user(env, "uid-o")
    await _about(env, uid, "post:p1", stamp="2026-10-01T00:00:00+00:00")
    recent = await _about(env, uid, "post:p1", stamp="2026-10-20T00:00:00+00:00")
    theirs = await _about(env, other, "post:p2", type="post_created")
    await _about(env, uid, "post:p2")  # wrong type for the third key
    keys = [
        (uid, "comment_added", "post:p1"),
        (other, "post_created", "post:p2"),
        (uid, "post_created", "post:p2"),  # nothing there
    ]
    since = "2026-10-15T00:00:00+00:00"
    found = await env.notif_repo.latest_in_groups(keys, since=since)
    assert {k: n.id for k, n in found.items()} == {
        keys[0]: recent.id,
        keys[1]: theirs.id,
    }
    for key in keys:  # the same answer as one lookup each
        one = await env.notif_repo.latest_in_group(
            user_id=key[0], type=key[1], object_key=key[2], since=since
        )
        assert (one.id if one else None) == (found[key].id if key in found else None)
    assert await env.notif_repo.latest_in_groups([], since=since) == {}


async def test_save_batch_writes_inserts_and_updates_in_one_transaction(env):
    uid = await _user(env)
    other = await _user(env, "uid-o")
    old = await _about(env, uid, "post:p1", read=True)
    calls = 0
    transact = env.db.transact

    async def counting(fn):
        nonlocal calls
        calls += 1
        return await transact(fn)

    env.db.transact = counting
    fresh = [
        replace(new_notification(user_id=u, type="t", title="new"), object_key="k")
        for u in (uid, other)
    ]
    failed = await env.notif_repo.save_batch(
        fresh, [replace(old, title="bumped", read_at=None)]
    )
    assert failed == frozenset() and calls == 1
    back = await env.notif_repo.get(old.id)
    assert back.title == "bumped" and not back.is_read
    landed = {n.id for u in (uid, other) for n in await env.notif_repo.list(u)}
    assert {n.id for n in fresh} <= landed
    assert await env.notif_repo.save_batch([], []) == frozenset()
    assert calls == 1  # nothing to write: no transaction


async def test_save_batch_skips_a_failing_row_and_keeps_the_rest(env):
    uid = await _user(env)
    ok = new_notification(user_id=uid, type="t", title="ok")
    gone = new_notification(user_id="uid-deleted", type="t", title="fk fails")
    failed = await env.notif_repo.save_batch([gone, ok], [])
    assert failed == frozenset({gone.id})
    assert [n.id for n in await env.notif_repo.list(uid, limit=50)] == [ok.id]


async def test_save_batch_prunes_every_new_rows_user_to_the_cap(env):
    uid = await _user(env)
    for i in range(10):  # at the cap (10 in this env), all read
        await _save(env, uid, stamp=f"2026-02-0{i}T00:00:00+00:00", read=True)
    batch = [
        replace(
            new_notification(user_id=uid, type="t", title=f"n{i}"),
            created_at=f"2026-03-0{i + 1}T00:00:00+00:00",
        )
        for i in range(3)
    ]
    await env.notif_repo.save_batch(batch, [])
    ids = {n.id for n in await env.notif_repo.list(uid, limit=50)}
    assert len(ids) == 10 and {n.id for n in batch} <= ids


async def test_count_unread_many_counts_each_user_and_zero_for_none(env):
    uid = await _user(env)
    other = await _user(env, "uid-o")
    await _save(env, uid, stamp="2026-02-01T00:00:00+00:00")
    await _save(env, uid, stamp="2026-02-02T00:00:00+00:00")
    await _save(env, uid, stamp="2026-02-03T00:00:00+00:00", read=True)
    counts = await env.notif_repo.count_unread_many([uid, other, uid])
    assert counts == {uid: 2, other: 0}
    assert counts[uid] == await env.notif_repo.count_unread(uid)
    assert await env.notif_repo.count_unread_many([]) == {}


async def test_space_notif_prefs_in_reads_one_spaces_choices(env):
    uid = await _user(env)
    other = await _user(env, "uid-o")
    for sid in ("sp-1", "sp-2"):
        await env.db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?,?,?,?,?)",
            (sid, sid, "iid", uid, "aabb" * 16),
        )
    await env.notif_repo.set_space_notif_level(
        user_id=uid, space_id="sp-1", level="muted"
    )
    await env.notif_repo.set_space_notif_level(
        user_id=other, space_id="sp-2", level="all"
    )
    prefs = await env.notif_repo.space_notif_prefs_in("sp-1")
    assert prefs == {
        uid: await env.notif_repo.get_space_notif_pref(user_id=uid, space_id="sp-1")
    }
    assert await env.notif_repo.space_notif_prefs_in("sp-none") == {}
