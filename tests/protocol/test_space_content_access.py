"""Release-blocker protocol tests: an ``ADMIN_ONLY`` feature (§4.3, v_42)
holds on every receiver.

Marked ``@pytest.mark.security``.

``test_space_content_authorship.py`` proves a household writes only for
its own members. This file proves the next rule on top: when the space
sets a feature to ``ADMIN_ONLY`` — as the RECEIVER's own copy of the
features says — only the owner or an admin may create, edit or delete that
feature's content, whatever household signs the envelope:

* the sender role matrix — host owner, admin, moderator, member, follower,
  non-member — × {create, edit own, edit someone else's, delete} × every
  access-levelled feature (posts, pages, tasks + lists, stickies, calendar);
* the ``actor_user_id`` a v_42 payload names must be seated on the sender
  (a forged actor naming another household's admin is refused);
* an older sender that names no actor falls back to the sending
  household's settings authority;
* a member household's sync stream cannot add what it could not send live.

Every case runs against the REAL application registry over a real SQLite
database and compares a snapshot of every content table. The allowed cases
and the ``OPEN`` controls are what make a refusal meaningful.
"""

from __future__ import annotations

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, space_sync_receiver_key
from socialhome.domain.federation import FederationEventType
from socialhome.domain.user import SYSTEM_AUTHOR
from socialhome.federation.owner_bound_id import (
    SPACE_TASK_LIST_KIND,
    mint_owner_bound_id,
)

from .test_space_content_authorship import (
    _CAL,
    _NOW,
    _SEED,
    ADMIN,
    AUTHOR,
    HOST,
    MOD,
    SP,
    STRANGER,
    _config,
    _deliver,
    _snapshot,
)

pytestmark = pytest.mark.security


@pytest.fixture
async def access_env(aiohttp_client, tmp_dir):
    """The authorship suite's real app + seeded space (``SP`` hosted by
    ``HOST``; u-a / u-adm / u-mod / u-sub seated on their households)."""
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    # Every sender advertises v_42 — it names the actor of each write.
    for inst in (HOST, AUTHOR, ADMIN, MOD):
        await db.enqueue(
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status, source, proto_version, capabilities_seen_at)"
            " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', 42, ?)",
            (inst, inst, "ab" * 32, f"https://{inst}/inbox/x", f"{inst}_local", _NOW),
        )
    # A plain member seated on the ADMIN household (the laundering cases).
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?, ?, 'u-plain', 'member')",
        (SP, ADMIN),
    )
    return app, db


async def _set_version(db, inst: str, version: int) -> None:
    await db.enqueue(
        "UPDATE remote_instances SET proto_version=? WHERE id=?", (version, inst)
    )


FET = FederationEventType

FEATURES = ("posts", "pages", "tasks", "stickies", "calendar")

#: (role label, sending household, actor) — every seat a write can come from.
SENDERS: tuple[tuple[str, str, str], ...] = (
    ("host-owner", HOST, "u-h"),
    ("admin", ADMIN, "u-adm"),
    ("moderator", MOD, "u-mod"),
    ("member", AUTHOR, "u-a"),
    ("subscriber", AUTHOR, "u-sub"),
    ("non-member", STRANGER, "u-x"),
)
ADMIN_ROLES = frozenset({"host-owner", "admin"})
ACTIONS = ("create", "edit-own", "edit-other", "delete")

#: Rows owned by each seat (u-a's come from the authorship seed).
_OWNERS = ("u-h", "u-adm", "u-mod", "u-sub")


def _row(kind: str, owner: str) -> str:
    return f"{kind}-a" if owner == "u-a" else f"{kind}-{owner}"


async def _seed_owned_rows(db) -> None:
    for o in _OWNERS:
        for sql, params in (
            (
                "INSERT INTO space_posts(id, space_id, author, type, content)"
                " VALUES(?, ?, ?, 'text', 'body')",
                (_row("post", o), SP, o),
            ),
            (
                "INSERT INTO space_pages(id, space_id, title, content, created_by)"
                " VALUES(?, ?, 'A page', 'body', ?)",
                (_row("page", o), SP, o),
            ),
            (
                "INSERT INTO space_tasks(id, list_id, space_id, title, created_by)"
                " VALUES(?, 'list-a', ?, 'A task', ?)",
                (_row("task", o), SP, o),
            ),
            (
                "INSERT INTO stickies(id, space_id, author, content)"
                " VALUES(?, ?, ?, 'A sticky')",
                (_row("sticky", o), SP, o),
            ),
            (
                "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
                " end_dt, created_by) VALUES(?, ?, 'A event', ?, ?, ?)",
                (_row("ev", o), SP, _CAL["start"], _CAL["end"], o),
            ),
        ):
            await db.enqueue(sql, params)


async def _set_level(db, feature: str, level: str) -> None:
    await db.enqueue(f"UPDATE spaces SET {feature}_access=? WHERE id=?", (level, SP))


def _write(feature: str, action: str, actor: str) -> tuple[FederationEventType, dict]:
    """The live event an ``actor`` would send for ``action`` on ``feature``."""
    own = actor
    other = "u-h" if actor == "u-a" else "u-a"
    target = own if action in ("edit-own", "delete") else other
    tag = f"{feature}-{actor}"
    match feature, action:
        case "posts", "create":
            return FET.SPACE_POST_CREATED, {
                "id": f"post-new-{tag}",
                "author": actor,
                "type": "text",
                "content": "x",
            }
        case "posts", "edit-own" | "edit-other":
            return FET.SPACE_POST_UPDATED, {
                "id": _row("post", target),
                "content": "edited",
            }
        case "posts", "delete":
            return FET.SPACE_POST_DELETED, {"id": _row("post", target)}
        case "pages", "create":
            return FET.SPACE_PAGE_CREATED, {
                "id": f"page-new-{tag}",
                "title": "x",
                "created_by": actor,
            }
        case "pages", "edit-own" | "edit-other":
            return FET.SPACE_PAGE_UPDATED, {"id": _row("page", target), "title": "ed"}
        case "pages", "delete":
            return FET.SPACE_PAGE_DELETED, {"id": _row("page", target)}
        case "tasks", "create":
            return FET.SPACE_TASK_CREATED, {
                "id": f"task-new-{tag}",
                "list_id": "list-a",
                "title": "x",
                "created_by": actor,
            }
        case "tasks", "edit-own" | "edit-other":
            return FET.SPACE_TASK_UPDATED, {
                "id": _row("task", target),
                "list_id": "list-a",
                "title": "edited",
            }
        case "tasks", "delete":
            return FET.SPACE_TASK_DELETED, {"id": _row("task", target)}
        case "stickies", "create":
            return FET.SPACE_STICKY_CREATED, {
                "id": f"sticky-new-{tag}",
                "author": actor,
                "content": "x",
            }
        case "stickies", "edit-own" | "edit-other":
            return FET.SPACE_STICKY_UPDATED, {
                "id": _row("sticky", target),
                "content": "edited",
            }
        case "stickies", "delete":
            return FET.SPACE_STICKY_DELETED, {"id": _row("sticky", target)}
        case "calendar", "create":
            return FET.SPACE_CALENDAR_EVENT_CREATED, {
                "id": f"ev-new-{tag}",
                "summary": "x",
                "created_by": actor,
                **_CAL,
            }
        case "calendar", "edit-own" | "edit-other":
            return FET.SPACE_CALENDAR_EVENT_UPDATED, {
                "id": _row("ev", target),
                "summary": "edited",
                "created_by": target,
                **_CAL,
            }
        case "calendar", "delete":
            return FET.SPACE_CALENDAR_EVENT_DELETED, {"id": _row("ev", target)}
    raise AssertionError((feature, action))


async def _send(app, db, feature, action, sender, actor, *, with_actor=True):
    event_type, payload = _write(feature, action, actor)
    if with_actor:
        payload["actor_user_id"] = actor
    before = await _snapshot(db)
    await _deliver(app, event_type, payload, sender=sender)
    return before, await _snapshot(db)


_MATRIX = [
    pytest.param(feature, action, role, sender, actor, id=f"{feature}-{action}-{role}")
    for feature in FEATURES
    for action in ACTIONS
    for role, sender, actor in SENDERS
]


@pytest.mark.parametrize(("feature", "action", "role", "sender", "actor"), _MATRIX)
async def test_admin_only_holds_on_the_receiver(
    access_env, feature, action, role, sender, actor
):
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, feature, "admin_only")
    before, after = await _send(app, db, feature, action, sender, actor)
    if role in ADMIN_ROLES:
        assert after != before, f"{role} {action} {feature} was refused"
    else:
        changed = sorted(t for t in before if before[t] != after[t])
        assert not changed, f"{role} {action} {feature} wrote {changed}"


#: The OPEN controls: the same writes by a member and a moderator land when
#: the feature is open — so the ADMIN_ONLY refusals above are the level's,
#: not some other rule's. (A member never edits / deletes someone else's
#: post: that is content authority, refused at any level.)
_OPEN_CONTROLS = [
    pytest.param(feature, action, role, sender, actor, id=f"{feature}-{action}-{role}")
    for feature in FEATURES
    for action in ACTIONS
    for role, sender, actor in SENDERS
    if role in ("member", "moderator")
    and not (feature == "posts" and action == "edit-other" and role == "member")
]


@pytest.mark.parametrize(
    ("feature", "action", "role", "sender", "actor"), _OPEN_CONTROLS
)
async def test_the_same_writes_land_when_the_feature_is_open(
    access_env, feature, action, role, sender, actor
):
    app, db = access_env
    await _seed_owned_rows(db)
    before, after = await _send(app, db, feature, action, sender, actor)
    assert after != before, f"{role} {action} {feature} under OPEN"


@pytest.mark.parametrize("feature", FEATURES)
async def test_a_forged_actor_naming_another_households_admin_is_refused(
    access_env, feature
):
    """A member household names the admin household's admin as its actor:
    the actor is not seated on the sender, so the claim fails."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, feature, "admin_only")
    event_type, payload = _write(feature, "edit-other", "u-a")
    before = await _snapshot(db)
    await _deliver(
        app, event_type, {**payload, "actor_user_id": "u-adm"}, sender=AUTHOR
    )
    assert await _snapshot(db) == before


@pytest.mark.parametrize("feature", FEATURES)
async def test_an_older_sender_without_an_actor_is_judged_by_its_household(
    access_env, feature
):
    """v_41 payloads name no actor: an admin household's write still lands,
    a member or moderator household's is refused."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, feature, "admin_only")
    for inst in (AUTHOR, MOD, ADMIN):
        await _set_version(db, inst, 41)
    for sender, actor in ((AUTHOR, "u-a"), (MOD, "u-mod")):
        before, after = await _send(
            app, db, feature, "edit-own", sender, actor, with_actor=False
        )
        assert after == before, (feature, sender)
    before, after = await _send(
        app, db, feature, "edit-own", ADMIN, "u-adm", with_actor=False
    )
    assert after != before, feature


_REVIEWED = ("pages", "tasks", "stickies", "calendar")


@pytest.mark.parametrize("feature", _REVIEWED)
@pytest.mark.parametrize(
    ("action", "sender", "actor", "lands"),
    [
        # A plain member's create / edit of somebody else's row would wait
        # for review, which no household but the host can hold: refused.
        ("create", AUTHOR, "u-a", False),
        ("edit-other", AUTHOR, "u-a", False),
        # Own rows stay the member's to change.
        ("edit-own", AUTHOR, "u-a", True),
        ("delete", AUTHOR, "u-a", True),
        # Content authority bypasses review.
        ("create", MOD, "u-mod", True),
        ("edit-other", MOD, "u-mod", True),
        ("create", ADMIN, "u-adm", True),
        ("create", HOST, "u-h", True),
    ],
)
async def test_moderated_holds_on_the_receiver_for_non_post_features(
    access_env, feature, action, sender, actor, lands
):
    """Adversarial review I3: MODERATED for a feature other than posts is
    enforced on every receiver, fail closed — a modified or older member
    household cannot publish past review."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, feature, "moderated")
    before, after = await _send(app, db, feature, action, sender, actor)
    assert (after != before) is lands, (feature, action, sender)


@pytest.mark.parametrize("feature", _REVIEWED)
async def test_moderated_refuses_an_actorless_member_household(access_env, feature):
    """An older sender naming nobody is judged by its household: a plain
    member household's create is refused, a moderator household's lands."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, feature, "moderated")
    for inst in (AUTHOR, MOD):
        await _set_version(db, inst, 41)
    before, after = await _send(
        app, db, feature, "create", AUTHOR, "u-a", with_actor=False
    )
    assert after == before
    before, after = await _send(
        app, db, feature, "create", MOD, "u-mod", with_actor=False
    )
    assert after != before


async def test_moderated_posts_keep_todays_behaviour_on_receivers(access_env):
    """Posts queue on the host only: a member household's post still lands,
    but its named actor is still bound to the sender."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, "posts", "moderated")
    before, after = await _send(app, db, "posts", "create", AUTHOR, "u-a")
    assert after != before
    event_type, payload = _write("posts", "create", "u-a")
    payload["id"] = "post-forged-actor"
    before = await _snapshot(db)
    await _deliver(
        app, event_type, {**payload, "actor_user_id": "u-adm"}, sender=AUTHOR
    )
    assert await _snapshot(db) == before


async def test_moderated_an_assignee_moves_their_task_but_not_its_title(access_env):
    """N4: as locally, an assignee owns a task's status (and position) — a
    status change of the host owner's task by its remote assignee lands; a
    title change by them is refused."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, "tasks", "moderated")
    task_id = _row("task", "u-h")
    await db.enqueue(
        "UPDATE space_tasks SET assignees_json='[\"u-a\"]' WHERE id=?", (task_id,)
    )
    before = await _snapshot(db)
    await _deliver(
        app,
        FET.SPACE_TASK_UPDATED,
        {
            "id": task_id,
            "list_id": "list-a",
            "title": "A task",
            "status": "done",
            "actor_user_id": "u-a",
        },
        sender=AUTHOR,
    )
    after = await _snapshot(db)
    assert after != before
    row = await db.fetchone("SELECT status FROM space_tasks WHERE id=?", (task_id,))
    assert row["status"] == "done"
    await _deliver(
        app,
        FET.SPACE_TASK_UPDATED,
        {
            "id": task_id,
            "list_id": "list-a",
            "title": "Renamed by assignee",
            "actor_user_id": "u-a",
        },
        sender=AUTHOR,
    )
    row = await db.fetchone("SELECT title FROM space_tasks WHERE id=?", (task_id,))
    assert row["title"] == "A task"


async def test_moderated_layout_moves_still_land(access_env):
    """A sticky drag of somebody else's note is LAYOUT — never refused."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, "stickies", "moderated")
    row = await db.fetchone(
        "SELECT content, color FROM stickies WHERE id=?", (_row("sticky", "u-h"),)
    )
    before = await _snapshot(db)
    await _deliver(
        app,
        FET.SPACE_STICKY_UPDATED,
        {
            "id": _row("sticky", "u-h"),
            "content": row["content"],
            "color": row["color"],
            "position_x": 123,
            "position_y": 45,
            "actor_user_id": "u-a",
        },
        sender=AUTHOR,
    )
    assert await _snapshot(db) != before


# ─── Sync admit: a member household cannot stream what it couldn't send ──

_LIST_NEW_A = mint_owner_bound_id(
    SPACE_TASK_LIST_KIND, space_id=SP, owner_user_id="u-a"
)
_LIST_NEW_ADM = mint_owner_bound_id(
    SPACE_TASK_LIST_KIND, space_id=SP, owner_user_id="u-adm"
)


def _sync_record(resource: str, creator: str) -> dict:
    rid = f"sync-{resource}-{creator}"
    match resource:
        case "posts":
            return {"id": rid, "author": creator, "type": "text", "content": "x"}
        case "task_lists":
            return {
                "id": _LIST_NEW_A if creator == "u-a" else _LIST_NEW_ADM,
                "name": "x",
                "created_by": creator,
            }
        case "tasks":
            return {"id": rid, "list_id": "list-a", "title": "x", "created_by": creator}
        case "pages":
            return {"id": rid, "title": "x", "created_by": creator}
        case "stickies":
            return {"id": rid, "author": creator, "content": "x"}
        case "calendar":
            return {"id": rid, "summary": "x", "created_by": creator, **_CAL}
    raise AssertionError(resource)


_SYNC_FEATURE = {
    "posts": "posts",
    "task_lists": "tasks",
    "tasks": "tasks",
    "pages": "pages",
    "stickies": "stickies",
    "calendar": "calendar",
}


async def _stream(app, db, resource, record, provider):
    before = await _snapshot(db)
    await app[space_sync_receiver_key]._dispatch(
        resource, SP, [dict(record)], provider=provider
    )
    return before, await _snapshot(db)


@pytest.mark.parametrize("resource", sorted(_SYNC_FEATURE))
async def test_a_member_households_stream_cannot_add_to_an_admin_only_feature(
    access_env, resource
):
    app, db = access_env
    await _set_level(db, _SYNC_FEATURE[resource], "admin_only")
    before, after = await _stream(
        app, db, resource, _sync_record(resource, "u-a"), AUTHOR
    )
    assert after == before, resource
    # The moderator household is refused the same way.
    before, after = await _stream(
        app, db, resource, _sync_record(resource, "u-mod"), MOD
    )
    assert after == before, resource


@pytest.mark.parametrize("resource", sorted(_SYNC_FEATURE))
async def test_an_admin_households_stream_still_adds(access_env, resource):
    app, db = access_env
    await _set_level(db, _SYNC_FEATURE[resource], "admin_only")
    before, after = await _stream(
        app, db, resource, _sync_record(resource, "u-adm"), ADMIN
    )
    assert after != before, resource


@pytest.mark.parametrize("resource", sorted(_SYNC_FEATURE))
async def test_the_hosts_stream_is_taken_whole(access_env, resource):
    """The host's snapshot is the state the space converges on — even rows
    a member made while the feature was still open."""
    app, db = access_env
    await _set_level(db, _SYNC_FEATURE[resource], "admin_only")
    before, after = await _stream(
        app, db, resource, _sync_record(resource, "u-a"), HOST
    )
    assert after != before, resource


# ─── Laundering: an admin household passing off its plain member's write ─


async def _rows(db, table: str, where: str, params: tuple) -> int:
    row = await db.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)
    return int(row["n"])


@pytest.mark.parametrize("with_actor", [True, False], ids=["actor-admin", "no-actor"])
async def test_an_admin_household_cannot_post_as_its_plain_member(
    access_env, with_actor
):
    """A create's actor is its author: naming the household's admin (or
    nobody) does not make u-plain's post an admin's."""
    app, db = access_env
    await _set_level(db, "posts", "admin_only")
    payload = {
        "id": "post-launder",
        "author": "u-plain",
        "type": "text",
        "content": "x",
    }
    if with_actor:
        payload["actor_user_id"] = "u-adm"
    await _deliver(app, FET.SPACE_POST_CREATED, payload, sender=ADMIN)
    assert await _rows(db, "space_posts", "id=?", ("post-launder",)) == 0
    # The control: the admin's own post lands.
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {
            "id": "post-adm",
            "author": "u-adm",
            "type": "text",
            "content": "x",
            "actor_user_id": "u-adm",
        },
        sender=ADMIN,
    )
    assert await _rows(db, "space_posts", "id=?", ("post-adm",)) == 1


@pytest.mark.parametrize("with_actor", [True, False], ids=["actor-admin", "no-actor"])
async def test_an_admin_household_cannot_create_a_page_as_its_plain_member(
    access_env, with_actor
):
    app, db = access_env
    await _set_level(db, "pages", "admin_only")
    payload = {"id": "page-launder", "title": "x", "created_by": "u-plain"}
    if with_actor:
        payload["actor_user_id"] = "u-adm"
    await _deliver(app, FET.SPACE_PAGE_CREATED, payload, sender=ADMIN)
    assert await _rows(db, "space_pages", "id=?", ("page-launder",)) == 0
    await _deliver(
        app,
        FET.SPACE_PAGE_CREATED,
        {"id": "page-adm", "title": "x", "created_by": "u-adm"},
        sender=ADMIN,
    )
    assert await _rows(db, "space_pages", "id=?", ("page-adm",)) == 1


@pytest.mark.parametrize(
    ("version", "lands"),
    [(42, False), (41, True)],
    ids=["v42-refused", "v41-household"],
)
async def test_an_edit_without_an_actor_depends_on_the_senders_version(
    access_env, version, lands
):
    """Every v_42 producer names the actor of an edit; one that names
    nobody is refused. A v_41 household is judged by its seats."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, "pages", "admin_only")
    await _set_version(db, ADMIN, version)
    await _deliver(
        app,
        FET.SPACE_PAGE_UPDATED,
        {"id": "page-u-adm", "title": "renamed"},
        sender=ADMIN,
    )
    row = await db.fetchone("SELECT title FROM space_pages WHERE id='page-u-adm'", ())
    assert (row["title"] == "renamed") is lands


async def test_the_host_still_releases_a_queued_post(access_env):
    """The one create whose actor is not its author: a moderation release,
    and only the host (where the queue lives) may send it."""
    app, db = access_env
    await _set_level(db, "posts", "admin_only")
    release = {
        "id": "post-rel",
        "author": "u-a",
        "type": "text",
        "content": "x",
        "actor_user_id": "u-h",
    }
    await _deliver(app, FET.SPACE_POST_CREATED, release, sender=HOST)
    assert await _rows(db, "space_posts", "id=?", ("post-rel",)) == 1
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {**release, "id": "post-rel2", "actor_user_id": "u-adm"},
        sender=ADMIN,
    )
    assert await _rows(db, "space_posts", "id=?", ("post-rel2",)) == 0


@pytest.mark.parametrize("event_type", [FET.SPACE_PAGE_UPDATED, FET.SPACE_PAGE_DELETED])
async def test_the_bot_identity_is_no_actor_for_a_members_row(access_env, event_type):
    """The shared bot identity stands for its household only on a bot's own
    row: naming it as the actor of an edit / delete of a plain member's page
    is refused — the admin household gains nothing by it."""
    app, db = access_env
    await db.enqueue(
        "INSERT INTO space_pages(id, space_id, title, content, created_by)"
        " VALUES('pg-plain', ?, 't', 'b', 'u-plain')",
        (SP,),
    )
    await _set_level(db, "pages", "admin_only")
    before = await _snapshot(db)
    await _deliver(
        app,
        event_type,
        {"id": "pg-plain", "title": "SYS-EDIT", "actor_user_id": SYSTEM_AUTHOR},
        sender=ADMIN,
    )
    assert await _snapshot(db) == before


async def test_a_bot_post_is_still_created_by_its_admin_household(access_env):
    """The bot fallback that remains: a bot's own post from a household
    holding settings authority (and nothing from a member household)."""
    app, db = access_env
    await _set_level(db, "posts", "admin_only")
    bot = {
        "id": "post-bot-new",
        "author": SYSTEM_AUTHOR,
        "type": "text",
        "content": "ding",
        "actor_user_id": SYSTEM_AUTHOR,
    }
    await _deliver(app, FET.SPACE_POST_CREATED, bot, sender=ADMIN)
    assert await _rows(db, "space_posts", "id=?", ("post-bot-new",)) == 1
    await _deliver(
        app, FET.SPACE_POST_CREATED, {**bot, "id": "post-bot-2"}, sender=AUTHOR
    )
    assert await _rows(db, "space_posts", "id=?", ("post-bot-2",)) == 0


async def test_bot_post_edits_and_deletes_still_pass(access_env):
    """A bot post's edits / deletes from its admin household land — named
    by the human who made them, or (older bot-bridge paths) by the bot
    identity on the bot's own row. A member household's do not."""
    app, db = access_env
    await _set_level(db, "posts", "admin_only")
    await _deliver(
        app,
        FET.SPACE_POST_UPDATED,
        {"id": "post-bot", "content": "tidied", "actor_user_id": "u-adm"},
        sender=ADMIN,
    )
    row = await db.fetchone("SELECT content FROM space_posts WHERE id='post-bot'", ())
    assert row["content"] == "tidied"
    before = await _snapshot(db)
    await _deliver(
        app,
        FET.SPACE_POST_DELETED,
        {"id": "post-bot", "actor_user_id": SYSTEM_AUTHOR},
        sender=AUTHOR,
    )
    assert await _snapshot(db) == before
    await _deliver(
        app,
        FET.SPACE_POST_DELETED,
        {"id": "post-bot", "actor_user_id": SYSTEM_AUTHOR},
        sender=ADMIN,
    )
    row = await db.fetchone("SELECT deleted FROM space_posts WHERE id='post-bot'", ())
    assert row["deleted"] == 1


@pytest.mark.parametrize("inst_row", ["no-row", "never-advertised"])
async def test_a_household_that_never_advertised_gets_the_household_fallback(
    access_env, inst_row
):
    """No ``remote_instances`` row, or one that never sent capabilities
    (``proto_version`` at its default): read as an older peer — an actor-less
    edit is judged by the household's seats, as for v_41."""
    app, db = access_env
    await _seed_owned_rows(db)
    await _set_level(db, "pages", "admin_only")
    if inst_row == "no-row":
        await db.enqueue(
            "DELETE FROM remote_instances WHERE id IN (?, ?)", (ADMIN, AUTHOR)
        )
    else:
        await db.enqueue(
            "UPDATE remote_instances SET proto_version=1, capabilities_seen_at=NULL"
            " WHERE id IN (?, ?)",
            (ADMIN, AUTHOR),
        )
    await _deliver(
        app,
        FET.SPACE_PAGE_UPDATED,
        {"id": "page-u-adm", "title": "renamed"},
        sender=ADMIN,
    )
    row = await db.fetchone("SELECT title FROM space_pages WHERE id='page-u-adm'", ())
    assert row["title"] == "renamed"
    before = await _snapshot(db)
    await _deliver(
        app, FET.SPACE_PAGE_UPDATED, {"id": "page-a", "title": "member"}, sender=AUTHOR
    )
    assert await _snapshot(db) == before
