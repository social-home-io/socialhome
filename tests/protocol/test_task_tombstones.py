"""Release-blocker protocol tests: a missed single-task delete heals.

Marked ``@pytest.mark.security``.

Two real households — the space's host H and a member M, each a full app
over its own SQLite file. Both hold the space's list L with tasks; then M
misses H's live ``SPACE_TASK_DELETED`` (offline, or the event dropped).
Whichever catch-up M gets next — the host's §25.6 sync stream or the
``SPACE_SYNC_RESUME`` replay — M must converge: the deleted task gone, and
a stale copy unable to come back through anybody's stream or a
re-announced create / update. The same class of bug the task-LIST
tombstones (``test_task_list_tombstones.py``) fixed for lists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    space_repo_key,
    space_sync_receiver_key,
    space_sync_service_key,
    space_task_service_key,
)
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.task import (
    Task,
    TaskList,
    TaskPriority,
    TaskStatus,
    task_to_wire_dict,
)
from socialhome.federation.owner_bound_id import (
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
)
from socialhome.federation.sync.space.resume import SpaceSyncResumeProvider

pytestmark = pytest.mark.security

FET = FederationEventType

SPACE = "sp-t"
HOST = "the-host"
MEMBER = "the-member"
OTHER = "other-member"  # a second member household that also missed it
OTHER_USER = f"u-{OTHER}"  # seated on OTHER (see ``_seed``)
AUTHOR = "u-anna"  # the host's user who made the tasks
S2 = "sp-two"  # a second space M holds, hosted by H2 (the squat target)
H2 = "host-two"
H2_USER = "u-bob"
_NOW = datetime.now(timezone.utc) - timedelta(days=1)


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
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://t.example"})},
        ),
    )


async def _seed(db, *, seats: tuple[str, ...]) -> None:
    await _seed_space(db, SPACE, HOST, seats)
    # Every peer advertises v_42+, so it names the actor of each write and
    # an actor-less delete is refused under a restricted level.
    for instance in (HOST, OTHER, H2):
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
    for instance in (HOST, OTHER):
        for role in ("admin", "moderator"):
            await db.enqueue(
                "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
                " role) VALUES(?,?,?,?)",
                (SPACE, instance, f"u-{instance}-{role}", role),
            )


async def _seed_space(db, space_id: str, host: str, seats: tuple[str, ...]) -> None:
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (space_id, space_id, host, "anna", "00" * 32),
    )
    for instance in seats:
        await db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
            (space_id, instance),
        )
        await db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES(?,?,?,?)",
            (
                space_id,
                instance,
                AUTHOR if instance == HOST else f"u-{instance}",
                "member",
            ),
        )


async def _set_tasks_level(app, level: str) -> None:
    await app[db_key].enqueue(
        "UPDATE spaces SET tasks_access=? WHERE id=?", (level, SPACE)
    )


@pytest.fixture
async def households(aiohttp_client, tmp_path):
    host = create_app(_config(tmp_path, "host"))
    member = create_app(_config(tmp_path, "member"))
    await aiohttp_client(host)
    await aiohttp_client(member)
    await _seed(host[db_key], seats=(MEMBER, OTHER))
    await _seed(member[db_key], seats=(HOST, OTHER))
    return host, member


def _repo(app):
    return app[space_task_service_key]._repo


def _task(tid: str, lid: str, i: int, author: str, **kw) -> Task:
    return Task(
        id=tid,
        list_id=lid,
        title=f"t{i}",
        status=TaskStatus.TODO,
        position=i,
        created_by=author,
        created_at=_NOW,
        updated_at=_NOW,
        **kw,
    )


async def _both_hold(
    host, member, n: int, *, author: str = AUTHOR, archived: bool = False
) -> tuple[str, list[str]]:
    """Both households hold one list with ``n`` tasks; ``(list, tasks)``."""
    lid = mint_owner_bound_id(
        SPACE_TASK_LIST_KIND, space_id=SPACE, owner_user_id=author
    )
    tids = [
        mint_owner_bound_id(SPACE_TASK_KIND, space_id=SPACE, owner_user_id=author)
        for _ in range(n)
    ]
    for app in (host, member):
        assert await _repo(app).save_list(
            TaskList(id=lid, name="L", created_by=author), space_id=SPACE
        )
        for i, tid in enumerate(tids):
            assert await _repo(app).save(
                _task(tid, lid, i, author, archived_at=_NOW if archived else None),
                space_id=SPACE,
            )
    return lid, tids


async def _host_sync(host, member, *, provider: str = HOST) -> None:
    """Stream every resource of ``host``'s space to ``member``'s receiver,
    in wire order, as the §25.6 provider does (chunking + crypto aside)."""
    exporters = host[space_sync_service_key]._exporters
    receiver = member[space_sync_receiver_key]
    for resource in RESOURCE_ORDER:
        exporter = exporters.get(resource)
        if exporter is None:
            continue
        records = await exporter.list_records(SPACE)
        await receiver._dispatch(resource, SPACE, records, provider=provider)


class _Capture:
    def __init__(self) -> None:
        self.sent: list[tuple[FederationEventType, dict]] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id):
        self.sent.append((event_type, payload))


async def _deliver(member, event_type, payload, *, sender: str = HOST, i: int = 0):
    registry = member[federation_service_key]._event_registry
    event = FederationEvent(
        msg_id=f"ev-{event_type}-{i}",
        event_type=event_type,
        from_instance=sender,
        to_instance=MEMBER,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=dict(payload),
        space_id=SPACE,
    )
    for handler in registry.handlers_for(event_type):
        await handler(event)


async def _resume(host, member, since: str) -> list[tuple[FederationEventType, dict]]:
    """H answers M's ``SPACE_SYNC_RESUME``; M applies each replayed event
    through its real registry."""
    capture = _Capture()
    provider = SpaceSyncResumeProvider(
        federation_service=capture,  # type: ignore[arg-type]
        space_repo=host[space_repo_key],
        space_post_repo=host[space_sync_service_key]._exporters["posts"]._repo,
        space_task_repo=_repo(host),
    )
    await provider.replay_space_to(space_id=SPACE, instance_id=MEMBER, since=since)
    for i, (event_type, payload) in enumerate(capture.sent):
        await _deliver(member, event_type, payload, i=i)
    return capture.sent


async def _tasks(app) -> set[str]:
    return {t.id for t in await _repo(app).list_by_space(SPACE)}


def _since() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


# ── Missed deletes heal ─────────────────────────────────────────────────


async def test_host_sync_tells_a_member_that_missed_it_about_a_task_delete(
    households,
):
    host, member = households
    _lid, (keep, gone) = await _both_hold(host, member, 2)
    assert await _repo(host).delete(gone, space_id=SPACE)  # M misses it

    await _host_sync(host, member)

    assert await _tasks(member) == {keep}


async def test_host_sync_heals_a_missed_delete_of_an_archived_task(households):
    host, member = households
    _lid, (gone,) = await _both_hold(host, member, 1, archived=True)
    assert await _repo(host).delete(gone, space_id=SPACE)

    await _host_sync(host, member)

    assert await _tasks(member) == set()


async def test_resume_replays_a_task_delete_the_member_missed(households):
    host, member = households
    _lid, (keep, gone) = await _both_hold(host, member, 2)
    since = _since()
    assert await _repo(host).delete(gone, space_id=SPACE)

    sent = await _resume(host, member, since)

    assert FET.SPACE_TASK_DELETED in [et for et, _ in sent]
    # The tombstone is not ALSO replayed as a live task.
    assert gone not in {p.get("id") for et, p in sent if et is FET.SPACE_TASK_CREATED}
    assert await _tasks(member) == {keep}


async def test_a_co_members_stream_carries_a_delete_the_member_missed(
    households,
):
    """OPEN space: any writer household may delete a task, so the deleting
    household's own tombstone stream heals M, not only the host's."""
    host, member = households
    _lid, (keep, gone) = await _both_hold(host, member, 2)
    assert await _repo(host).delete(gone, space_id=SPACE)

    await _host_sync(host, member, provider=OTHER)  # ``host`` plays OTHER

    assert await _tasks(member) == {keep}


# ── A tombstone wins ────────────────────────────────────────────────────


async def test_a_stale_members_stream_cannot_bring_a_deleted_task_back(
    households,
):
    """M heard the delete; another member household that missed it still
    streams the task, whose creator is seated on it — only the tombstone
    stops it."""
    host, member = households
    _lid, (gone,) = await _both_hold(host, member, 1, author=OTHER_USER)
    assert await _repo(member).delete(gone, space_id=SPACE)

    await _host_sync(host, member, provider=OTHER)  # ``host`` plays OTHER

    assert await _tasks(member) == set()


async def test_the_hosts_own_stale_copy_cannot_undo_a_tombstone(households):
    host, member = households
    _lid, (gone,) = await _both_hold(host, member, 1)
    assert await _repo(member).delete(gone, space_id=SPACE)

    await _host_sync(host, member)  # the host never heard of the delete

    assert await _tasks(member) == set()


async def test_a_stale_archived_copy_cannot_come_back(households):
    host, member = households
    _lid, (gone,) = await _both_hold(host, member, 1, author=OTHER_USER, archived=True)
    assert await _repo(member).delete(gone, space_id=SPACE)

    await _host_sync(host, member, provider=OTHER)
    await _host_sync(host, member)

    assert await _tasks(member) == set()


@pytest.mark.parametrize("event_type", [FET.SPACE_TASK_CREATED, FET.SPACE_TASK_UPDATED])
async def test_a_reannounced_task_is_refused(households, event_type):
    host, member = households
    lid, (gone,) = await _both_hold(host, member, 1, author=OTHER_USER)
    assert await _repo(member).delete(gone, space_id=SPACE)

    payload = task_to_wire_dict(_task(gone, lid, 0, OTHER_USER), SPACE)
    await _deliver(member, event_type, payload, sender=OTHER)

    assert await _tasks(member) == set()
    assert await _repo(member).get(gone) is None


async def test_a_repo_save_never_overwrites_a_tombstone(households):
    host, member = households
    lid, (gone,) = await _both_hold(host, member, 1)
    assert await _repo(member).delete(gone, space_id=SPACE)
    assert not await _repo(member).save(_task(gone, lid, 0, AUTHOR), space_id=SPACE)
    assert await _repo(member).is_task_deleted(gone, space_id=SPACE)


# ── Stubs for never-held ids (host only, owner-bound to THIS space) ─────


def _tomb(tid: str, lid: str, actor: str | None = None, created_by=AUTHOR) -> dict:
    rec = {"id": tid, "space_id": SPACE, "list_id": lid, "created_by": created_by}
    if actor is not None:
        rec["actor_user_id"] = actor
    return rec


async def test_a_hosts_tombstone_stubs_an_unseen_task_bound_to_this_space(
    households,
):
    host, member = households
    lid, _ = await _both_hold(host, member, 0)
    tid = mint_owner_bound_id(SPACE_TASK_KIND, space_id=SPACE, owner_user_id=AUTHOR)
    receiver = member[space_sync_receiver_key]
    await receiver._dispatch("tasks_deleted", SPACE, [_tomb(tid, lid)], provider=HOST)
    assert await _repo(member).is_task_deleted(tid, space_id=SPACE)
    # A stale stream of it afterwards cannot create it.
    stale = task_to_wire_dict(_task(tid, lid, 0, AUTHOR), SPACE)
    await receiver._dispatch("tasks", SPACE, [stale], provider=HOST)
    assert await _tasks(member) == set()


async def test_a_member_households_tombstone_never_stubs(households):
    host, member = households
    lid, _ = await _both_hold(host, member, 0)
    tid = mint_owner_bound_id(SPACE_TASK_KIND, space_id=SPACE, owner_user_id=AUTHOR)
    await member[space_sync_receiver_key]._dispatch(
        "tasks_deleted", SPACE, [_tomb(tid, lid)], provider=OTHER
    )
    assert not await _repo(member).is_task_deleted(tid, space_id=SPACE)


async def test_a_hosts_tombstone_for_another_spaces_task_id_records_nothing(
    households,
):
    """Task ids are global: a stub for an id minted in S2 would make S2's
    real task unwritable on M forever."""
    host, member = households
    lid, _ = await _both_hold(host, member, 0)
    await _seed_space(member[db_key], S2, H2, (H2,))
    l2 = mint_owner_bound_id(SPACE_TASK_LIST_KIND, space_id=S2, owner_user_id=H2_USER)
    assert await _repo(member).save_list(
        TaskList(id=l2, name="L2", created_by=H2_USER), space_id=S2
    )
    x = mint_owner_bound_id(SPACE_TASK_KIND, space_id=S2, owner_user_id=H2_USER)
    receiver = member[space_sync_receiver_key]
    await receiver._dispatch(
        "tasks_deleted", SPACE, [_tomb(x, lid, created_by=H2_USER)], provider=HOST
    )
    assert not await _repo(member).is_task_deleted(x, space_id=SPACE)
    real = task_to_wire_dict(_task(x, l2, 0, H2_USER), S2)
    await receiver._dispatch("tasks", S2, [real], provider=H2)
    assert [t.id for t in await _repo(member).list_by_space(S2)] == [x]


async def test_a_tombstone_for_a_task_held_in_another_space_is_refused(
    households, caplog
):
    host, member = households
    lid, _ = await _both_hold(host, member, 0)
    await _seed_space(member[db_key], S2, H2, (H2,))
    l2 = mint_owner_bound_id(SPACE_TASK_LIST_KIND, space_id=S2, owner_user_id=H2_USER)
    assert await _repo(member).save_list(
        TaskList(id=l2, name="L2", created_by=H2_USER), space_id=S2
    )
    x = mint_owner_bound_id(SPACE_TASK_KIND, space_id=S2, owner_user_id=H2_USER)
    assert await _repo(member).save(_task(x, l2, 0, H2_USER), space_id=S2)
    for provider in (HOST, OTHER):
        with caplog.at_level("WARNING"):
            await member[space_sync_receiver_key]._dispatch(
                "tasks_deleted",
                SPACE,
                [_tomb(x, lid, created_by=H2_USER)],
                provider=provider,
            )
    assert [t.id for t in await _repo(member).list_by_space(S2)] == [x]
    assert "held in another space" in caplog.text


async def test_a_hosts_tombstone_under_a_list_not_held_here_records_nothing(
    households,
):
    """``space_tasks.list_id`` is a FK: a stub needs its list live here in
    this space. A list held in another space never anchors one."""
    host, member = households
    await _seed_space(member[db_key], S2, H2, (H2,))
    l2 = mint_owner_bound_id(SPACE_TASK_LIST_KIND, space_id=S2, owner_user_id=H2_USER)
    assert await _repo(member).save_list(
        TaskList(id=l2, name="L2", created_by=H2_USER), space_id=S2
    )
    tid = mint_owner_bound_id(SPACE_TASK_KIND, space_id=SPACE, owner_user_id=AUTHOR)
    receiver = member[space_sync_receiver_key]
    for lid in (l2, "list-never-seen"):
        await receiver._dispatch(
            "tasks_deleted", SPACE, [_tomb(tid, lid)], provider=HOST
        )
    assert not await _repo(member).is_task_deleted(tid, space_id=SPACE)


# ── Restricted levels judge the deleter (v_42+ peers) ───────────────────


@pytest.mark.parametrize(
    ("level", "actor", "lands"),
    [
        ("admin_only", f"u-{OTHER}-admin", True),
        ("admin_only", OTHER_USER, False),  # a plain member
        ("admin_only", f"u-{OTHER}-moderator", False),
        ("admin_only", None, False),  # actor-less from a v_42 household
        ("admin_only", "u-not-seated-on-other", False),
        ("moderated", f"u-{OTHER}-moderator", True),
        ("moderated", f"u-{OTHER}-admin", True),
        ("moderated", OTHER_USER, False),  # someone else's task: needs review
        ("moderated", None, False),
        ("open", None, True),
    ],
)
async def test_a_member_households_tombstone_is_judged_by_the_level(
    households, level, actor, lands
):
    host, member = households
    await _set_tasks_level(member, level)
    lid, (tid,) = await _both_hold(host, member, 1)
    await member[space_sync_receiver_key]._dispatch(
        "tasks_deleted", SPACE, [_tomb(tid, lid, actor)], provider=OTHER
    )
    assert (await _tasks(member) == set()) is lands


async def test_a_refused_member_tombstone_logs_one_line_per_chunk(households, caplog):
    host, member = households
    await _set_tasks_level(member, "admin_only")
    lid, tids = await _both_hold(host, member, 3)
    with caplog.at_level("INFO"):
        await member[space_sync_receiver_key]._dispatch(
            "tasks_deleted",
            SPACE,
            [_tomb(t, lid, OTHER_USER) for t in tids],
            provider=OTHER,
        )
    assert len(await _tasks(member)) == 3
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    assert "kept 0 of 3 tasks_deleted" in caplog.text


@pytest.mark.parametrize(
    ("level", "deleted_by", "converges"),
    [
        ("admin_only", f"u-{HOST}-admin", True),
        ("admin_only", "", False),  # nobody named: a v_42 receiver refuses
        ("admin_only", f"u-{OTHER}-admin", False),  # not seated on the host
        ("moderated", f"u-{HOST}-moderator", True),
        ("moderated", "", False),
    ],
)
async def test_resume_carries_the_deleter_so_a_restricted_space_converges(
    households, level, deleted_by, converges
):
    host, member = households
    await _set_tasks_level(member, level)
    _lid, (keep, gone) = await _both_hold(host, member, 2)
    since = _since()
    assert await _repo(host).delete(gone, space_id=SPACE, deleted_by=deleted_by)

    await _resume(host, member, since)

    assert await _tasks(member) == ({keep} if converges else {keep, gone})


async def test_a_live_delete_records_the_deleter(households):
    host, member = households
    await _set_tasks_level(member, "admin_only")
    lid, (tid,) = await _both_hold(host, member, 1)
    actor = f"u-{OTHER}-admin"
    await _deliver(
        member,
        FET.SPACE_TASK_DELETED,
        {"id": tid, "list_id": lid, "space_id": SPACE, "actor_user_id": actor},
        sender=OTHER,
    )
    assert await _tasks(member) == set()
    row = await member[db_key].fetchone(
        "SELECT deleted_by, deleted_at FROM space_tasks WHERE id=?", (tid,)
    )
    assert row["deleted_by"] == actor and row["deleted_at"] is not None


async def test_a_local_delete_records_the_deleter(households):
    host, _member = households
    _lid, (tid,) = await _both_hold(host, _member, 1)
    await host[space_task_service_key].delete_task(
        tid, space_id=SPACE, actor_user_id=AUTHOR
    )
    assert await _repo(host).is_task_deleted(tid, space_id=SPACE)
    tombstones = await _repo(host).list_task_tombstones(SPACE)
    assert [(t.id, t.deleted_by) for t in tombstones] == [(tid, AUTHOR)]


async def test_the_host_stream_ships_the_deleter_creator_and_list(households):
    host, member = households
    lid, (tid,) = await _both_hold(host, member, 1)
    assert await _repo(host).delete(tid, space_id=SPACE, deleted_by="u-x")
    exporter = host[space_sync_service_key]._exporters["tasks_deleted"]
    assert await exporter.list_records(SPACE) == [
        {
            "id": tid,
            "space_id": SPACE,
            "list_id": lid,
            "created_by": AUTHOR,
            "actor_user_id": "u-x",
        }
    ]


def test_tasks_deleted_streams_after_lists_before_tasks_and_is_a_removal():
    order = list(RESOURCE_ORDER)
    assert order.index("task_lists_deleted") < order.index("tasks_deleted")
    assert order.index("tasks_deleted") < order.index("tasks")
    assert "tasks_deleted" in REMOVAL_RESOURCES


# ── Interaction with list tombstones ─────────────────────────────────────


async def test_a_list_delete_supersedes_task_tombstones_under_it(households):
    """A list delete tombstones its tasks in place (0071 trigger); the task
    tombstones under it are not shipped again — the list's covers them."""
    host, member = households
    lid, (a, b) = await _both_hold(host, member, 2)
    assert await _repo(host).delete(a, space_id=SPACE)
    assert await _repo(host).delete_list(lid, space_id=SPACE)

    await _host_sync(host, member)  # list + task tombstones, in that order

    assert await _tasks(member) == set()
    assert await _repo(member).is_list_deleted(lid, space_id=SPACE)
    assert await _repo(host).list_task_tombstones(SPACE) == []
    # And a stale copy of either task still cannot come back.
    stale = [task_to_wire_dict(_task(t, lid, 0, AUTHOR), SPACE) for t in (a, b)]
    await member[space_sync_receiver_key]._dispatch(
        "tasks", SPACE, stale, provider=HOST
    )
    assert await _tasks(member) == set()


async def test_a_task_tombstone_under_a_deleted_list_is_a_quiet_noop(
    households, caplog
):
    host, member = households
    lid, (tid,) = await _both_hold(host, member, 1)
    assert await _repo(member).delete_list(lid, space_id=SPACE)
    with caplog.at_level("INFO"):
        await member[space_sync_receiver_key]._dispatch(
            "tasks_deleted", SPACE, [_tomb(tid, lid, "u-x")], provider=HOST
        )
    # Already a tombstone by the list's delete; the record rewrites nothing.
    assert await _repo(member).is_task_deleted(tid, space_id=SPACE)
    row = await member[db_key].fetchone(
        "SELECT deleted_by FROM space_tasks WHERE id=?", (tid,)
    )
    assert row["deleted_by"] is None  # the list's (nobody named), not u-x
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


async def test_a_deleted_task_keeps_its_row_out_of_every_read(households):
    host, _member = households
    lid, (keep, gone) = await _both_hold(host, _member, 2)
    repo = _repo(host)
    assert await repo.delete(gone, space_id=SPACE)
    assert not await repo.delete(gone, space_id=SPACE)  # already a tombstone
    assert await repo.get(gone) is None
    assert [t.id for t in await repo.list_by_list(lid, space_id=SPACE)] == [keep]
    assert await repo.open_counts(SPACE) == {lid: 1}
    since = (_NOW - timedelta(days=1)).isoformat()
    assert [t.id for t in await repo.list_since(SPACE, since)] == [keep]
    # The tombstone keeps no content: a deleted task's text is gone.
    held = await host[db_key].fetchone(
        "SELECT deleted_at, title, description, assignees_json, labels_json"
        " FROM space_tasks WHERE id=?",
        (gone,),
    )
    assert held["deleted_at"] is not None
    assert (held["title"], held["description"]) == ("", None)
    assert (held["assignees_json"], held["labels_json"]) == ("[]", "[]")


async def test_a_task_of_a_deleted_list_cannot_be_refiled_under_another_list(
    households,
):
    """The list delete tombstones its tasks in place (0071 trigger), so the
    owner household cannot re-create the same task id under a live list —
    not by a live create, a member stream, nor the host's stream."""
    host, member = households
    lid, (tid,) = await _both_hold(host, member, 1, author=OTHER_USER)
    other_lid, _ = await _both_hold(host, member, 0, author=OTHER_USER)
    assert await _repo(member).delete_list(lid, space_id=SPACE, deleted_by="u-d")
    assert await _repo(member).is_task_deleted(tid, space_id=SPACE)

    refiled = task_to_wire_dict(_task(tid, other_lid, 0, OTHER_USER), SPACE)
    await _deliver(member, FET.SPACE_TASK_CREATED, refiled, sender=OTHER)
    receiver = member[space_sync_receiver_key]
    for provider in (OTHER, HOST):
        await receiver._dispatch("tasks", SPACE, [refiled], provider=provider)
    assert not await _repo(member).save(
        _task(tid, other_lid, 0, OTHER_USER), space_id=SPACE
    )

    assert await _tasks(member) == set()
    row = await member[db_key].fetchone(
        "SELECT list_id, deleted_by, title, priority FROM space_tasks WHERE id=?",
        (tid,),
    )
    assert (row["list_id"], row["deleted_by"], row["title"]) == (lid, "u-d", "")
    assert row["priority"] is None


async def test_a_tombstone_blanks_the_priority(households):
    host, _member = households
    lid, _ = await _both_hold(host, _member, 0)
    tid = mint_owner_bound_id(SPACE_TASK_KIND, space_id=SPACE, owner_user_id=AUTHOR)
    assert await _repo(host).save(
        _task(tid, lid, 0, AUTHOR, priority=TaskPriority.HIGH), space_id=SPACE
    )
    assert await _repo(host).delete(tid, space_id=SPACE)
    row = await host[db_key].fetchone(
        "SELECT priority FROM space_tasks WHERE id=?", (tid,)
    )
    assert row["priority"] is None
