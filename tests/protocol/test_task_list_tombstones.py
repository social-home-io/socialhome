"""Release-blocker protocol tests: a missed task-list delete or rename heals.

Marked ``@pytest.mark.security``.

Two real households — the space's host H and a member M, each a full app
over its own SQLite file. M holds the space's list L (with a task) and
then misses H's live ``SPACE_TASK_LIST_DELETED`` / ``_UPDATED`` (it was
offline, or the event was dropped). Whichever catch-up M gets next — the
host's §25.6 sync stream or the ``SPACE_SYNC_RESUME`` replay — M must
converge on H's state: the deleted list (and its tasks) gone, the renamed
list under its new name, and a stale copy of a deleted list unable to come
back through anybody's stream or a re-announced create.
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
    TaskStatus,
    task_list_to_wire_dict,
)
from socialhome.federation.owner_bound_id import (
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.sync.space.exporter import RESOURCE_ORDER
from socialhome.federation.sync.space.resume import SpaceSyncResumeProvider

pytestmark = pytest.mark.security

FET = FederationEventType

SPACE = "sp-t"
HOST = "the-host"
MEMBER = "the-member"
OTHER = "other-member"  # a second member household that also missed it
OTHER_USER = f"u-{OTHER}"  # seated on OTHER (see ``_seed``)
AUTHOR = "u-anna"  # the host's user who made the lists
S2 = "sp-two"  # a second space M holds, hosted by H2 (the C1 squat target)
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
    # an actor-less delete is refused under a restricted level (I1).
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
    # An admin and a moderator on each remote household (restricted levels).
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


async def _both_hold(host, member, *names: str, author: str = AUTHOR) -> list[str]:
    """Each household holds one list per name (+ one task in each)."""
    ids = []
    for i, name in enumerate(names):
        lid = mint_owner_bound_id(
            SPACE_TASK_LIST_KIND, space_id=SPACE, owner_user_id=author
        )
        tid = mint_owner_bound_id(SPACE_TASK_KIND, space_id=SPACE, owner_user_id=author)
        for app in (host, member):
            assert await _repo(app).save_list(
                TaskList(id=lid, name=name, created_by=author), space_id=SPACE
            )
            assert await _repo(app).save(
                Task(
                    id=tid,
                    list_id=lid,
                    title=f"t{i}",
                    status=TaskStatus.TODO,
                    position=i,
                    created_by=author,
                    created_at=_NOW,
                    updated_at=_NOW,
                ),
                space_id=SPACE,
            )
        ids.append(lid)
    return ids


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


async def _resume(host, member, since: str) -> list[FederationEventType]:
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
    registry = member[federation_service_key]._event_registry
    for i, (event_type, payload) in enumerate(capture.sent):
        event = FederationEvent(
            msg_id=f"resume-{i}",
            event_type=event_type,
            from_instance=HOST,
            to_instance=MEMBER,
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=dict(payload),
            space_id=SPACE,
        )
        for handler in registry.handlers_for(event_type):
            await handler(event)
    return [et for et, _ in capture.sent]


async def _lists(app) -> dict[str, str]:
    return {lst.id: lst.name for lst in await _repo(app).list_lists(SPACE)}


async def _task_lists_of(app) -> set[str]:
    return {t.list_id for t in await _repo(app).list_by_space(SPACE)}


def _since() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


# ── Deletes ──────────────────────────────────────────────────────────────


async def test_host_sync_tells_a_member_that_missed_it_about_a_list_delete(
    households,
):
    host, member = households
    keep, gone = await _both_hold(host, member, "Keep", "Gone")
    assert await _repo(host).delete_list(gone, space_id=SPACE)  # M misses it

    await _host_sync(host, member)

    assert await _lists(member) == {keep: "Keep"}
    assert await _task_lists_of(member) == {keep}  # the task went with it


async def test_resume_replays_a_list_delete_the_member_missed(households):
    host, member = households
    keep, gone = await _both_hold(host, member, "Keep", "Gone")
    since = _since()
    assert await _repo(host).delete_list(gone, space_id=SPACE)

    sent = await _resume(host, member, since)

    assert FET.SPACE_TASK_LIST_DELETED in sent
    assert await _lists(member) == {keep: "Keep"}
    assert await _task_lists_of(member) == {keep}


async def test_a_stale_members_stream_cannot_bring_a_deleted_list_back(
    households,
):
    """M deleted L (it heard the delete); another member household that
    missed it still streams L — M must not take it back, nor its task.
    L's creator is seated on that household, so only the tombstone stops it."""
    host, member = households
    (gone,) = await _both_hold(host, member, "Gone", author=OTHER_USER)
    assert await _repo(member).delete_list(gone, space_id=SPACE)

    # ``host`` plays the stale co-member here: it still holds L + its task.
    await _host_sync(host, member, provider=OTHER)

    assert await _lists(member) == {}
    assert await _task_lists_of(member) == set()


async def test_the_hosts_own_stale_copy_cannot_undo_a_tombstone(households):
    host, member = households
    (gone,) = await _both_hold(host, member, "Gone")
    assert await _repo(member).delete_list(gone, space_id=SPACE)

    await _host_sync(host, member)  # the host never heard of the delete

    assert await _lists(member) == {}


async def test_a_reannounced_create_of_a_deleted_list_is_refused(households):
    host, member = households
    (gone,) = await _both_hold(host, member, "Gone", author=OTHER_USER)
    assert await _repo(member).delete_list(gone, space_id=SPACE)
    registry = member[federation_service_key]._event_registry
    lst = TaskList(id=gone, name="Gone", created_by=OTHER_USER)
    for handler in registry.handlers_for(FET.SPACE_TASK_LIST_CREATED):
        await handler(
            FederationEvent(
                msg_id="re-announce",
                event_type=FET.SPACE_TASK_LIST_CREATED,
                from_instance=OTHER,
                to_instance=MEMBER,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=task_list_to_wire_dict(lst, SPACE),
                space_id=SPACE,
            )
        )
    assert await _lists(member) == {}


# ── Renames ──────────────────────────────────────────────────────────────


async def test_host_sync_heals_a_missed_rename(households):
    host, member = households
    (lid,) = await _both_hold(host, member, "Old")
    assert await _repo(host).save_list(
        TaskList(id=lid, name="New", created_by=AUTHOR), space_id=SPACE
    )

    await _host_sync(host, member)

    assert await _lists(member) == {lid: "New"}


async def test_resume_replays_a_list_rename_the_member_missed(households):
    host, member = households
    (lid,) = await _both_hold(host, member, "Old")
    since = _since()
    # The list was created before ``since``; only its rename is newer.
    await host[db_key].enqueue(
        "UPDATE space_task_lists SET created_at=datetime('now', '-1 day') WHERE id=?",
        (lid,),
    )
    assert await _repo(host).save_list(
        TaskList(id=lid, name="New", created_by=AUTHOR), space_id=SPACE
    )

    await _resume(host, member, since)

    assert await _lists(member) == {lid: "New"}


async def test_resume_does_not_replay_an_old_name_over_a_newer_one(households):
    """A co-member that never heard of a rename must not revert it: a
    list neither created nor renamed since ``since`` is not replayed."""
    host, member = households
    (lid,) = await _both_hold(host, member, "Old")
    await host[db_key].enqueue(
        "UPDATE space_task_lists SET created_at=datetime('now', '-1 day') WHERE id=?",
        (lid,),
    )
    assert await _repo(member).save_list(
        TaskList(id=lid, name="New", created_by=AUTHOR), space_id=SPACE
    )

    sent = await _resume(host, member, _since())

    assert FET.SPACE_TASK_LIST_CREATED not in sent
    assert await _lists(member) == {lid: "New"}


async def test_a_co_members_stream_carries_a_delete_the_member_missed(
    households,
):
    """The delete was made on another member household (OPEN space: any
    writer household may delete a list) and M missed it — that
    household's own tombstone stream heals M, not only the host's."""
    host, member = households
    keep, gone = await _both_hold(host, member, "Keep", "Gone")
    assert await _repo(host).delete_list(gone, space_id=SPACE)

    await _host_sync(host, member, provider=OTHER)  # ``host`` plays OTHER

    assert await _lists(member) == {keep: "Keep"}
    assert await _task_lists_of(member) == {keep}


# ── C1: a host's tombstone cannot squat another space's list id ──────────


async def test_a_hosts_tombstone_for_another_spaces_list_id_records_nothing(
    households,
):
    """List ids are global. The host of SPACE streams a tombstone for an id
    minted in S2 (never held here): a stub would make S2's real list
    unwritable on M forever. Only an id bound to THIS space is stubbed."""
    _host, member = households
    await _seed_space(member[db_key], S2, H2, (H2,))
    x = mint_owner_bound_id(SPACE_TASK_LIST_KIND, space_id=S2, owner_user_id=H2_USER)
    receiver = member[space_sync_receiver_key]
    await receiver._dispatch(
        "task_lists_deleted",
        SPACE,
        [{"id": x, "space_id": SPACE, "created_by": H2_USER}],
        provider=HOST,
    )
    assert not await _repo(member).is_list_deleted(x, space_id=SPACE)
    real = task_list_to_wire_dict(TaskList(id=x, name="Real", created_by=H2_USER), S2)
    await receiver._dispatch("task_lists", S2, [real], provider=H2)
    assert [lst.id for lst in await _repo(member).list_lists(S2)] == [x]


async def test_a_hosts_tombstone_for_a_list_held_in_another_space_is_refused(
    households, caplog
):
    host, member = households
    await _seed_space(member[db_key], S2, H2, (H2,))
    x = mint_owner_bound_id(SPACE_TASK_LIST_KIND, space_id=S2, owner_user_id=H2_USER)
    assert await _repo(member).save_list(
        TaskList(id=x, name="Real", created_by=H2_USER), space_id=S2
    )
    for provider in (HOST, OTHER):
        with caplog.at_level("WARNING"):
            await member[space_sync_receiver_key]._dispatch(
                "task_lists_deleted",
                SPACE,
                [{"id": x, "space_id": SPACE, "created_by": H2_USER}],
                provider=provider,
            )
    assert [lst.id for lst in await _repo(member).list_lists(S2)] == [x]
    assert "held in another space" in caplog.text


async def test_a_hosts_tombstone_stubs_an_unseen_list_bound_to_this_space(
    households,
):
    _host, member = households
    lid = mint_owner_bound_id(
        SPACE_TASK_LIST_KIND, space_id=SPACE, owner_user_id=AUTHOR
    )
    receiver = member[space_sync_receiver_key]
    tomb = {"id": lid, "space_id": SPACE, "created_by": AUTHOR}
    await receiver._dispatch("task_lists_deleted", SPACE, [tomb], provider=HOST)
    assert await _repo(member).is_list_deleted(lid, space_id=SPACE)
    # A stale co-member streaming it afterwards cannot create it.
    stale = task_list_to_wire_dict(
        TaskList(id=lid, name="Stale", created_by=AUTHOR), SPACE
    )
    await receiver._dispatch("task_lists", SPACE, [stale], provider=HOST)
    assert await _lists(member) == {}


# ── I1 / I2: restricted levels judge the deleter (v_42+ peers) ───────────


def _tomb(lid: str, actor: str | None, created_by: str = AUTHOR) -> dict:
    rec = {"id": lid, "space_id": SPACE, "created_by": created_by}
    if actor is not None:
        rec["actor_user_id"] = actor
    return rec


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
        ("moderated", OTHER_USER, False),  # someone else's list: needs review
        ("moderated", None, False),
        ("open", None, True),
    ],
)
async def test_a_member_households_tombstone_is_judged_by_the_level(
    households, level, actor, lands
):
    host, member = households
    await _set_tasks_level(member, level)
    (lid,) = await _both_hold(host, member, "L")
    await member[space_sync_receiver_key]._dispatch(
        "task_lists_deleted", SPACE, [_tomb(lid, actor)], provider=OTHER
    )
    assert (await _lists(member) == {}) is lands


async def test_a_refused_member_tombstone_logs_one_line_per_chunk(households, caplog):
    host, member = households
    await _set_tasks_level(member, "admin_only")
    ids = await _both_hold(host, member, "A", "B", "C")
    with caplog.at_level("INFO"):
        await member[space_sync_receiver_key]._dispatch(
            "task_lists_deleted",
            SPACE,
            [_tomb(lid, OTHER_USER) for lid in ids],
            provider=OTHER,
        )
    assert len(await _lists(member)) == 3
    assert "access level" not in caplog.text  # no per-record WARNING
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    assert "kept 0 of 3 task_lists_deleted" in caplog.text


@pytest.mark.parametrize(
    ("level", "deleted_by", "converges"),
    [
        ("admin_only", f"u-{HOST}-admin", True),
        ("admin_only", "", False),  # nobody named: a v_42 receiver refuses
        # Another household's admin: the live rule wants the actor seated
        # on the sender, so that delete converges from its own household's
        # stream / replay (see the member-household cases), not the host's.
        ("admin_only", f"u-{OTHER}-admin", False),
        ("admin_only", "u-nobody", False),  # seated nowhere
        ("moderated", f"u-{HOST}-moderator", True),
        ("moderated", "", False),
    ],
)
async def test_resume_carries_the_deleter_so_a_restricted_space_converges(
    households, level, deleted_by, converges
):
    """The resume replay names the tombstone's ``deleted_by`` as
    ``actor_user_id``, so a household that missed the delete applies it
    under ADMIN_ONLY / MODERATED like the live event."""
    host, member = households
    await _set_tasks_level(member, level)
    keep, gone = await _both_hold(host, member, "Keep", "Gone")
    since = _since()
    assert await _repo(host).delete_list(gone, space_id=SPACE, deleted_by=deleted_by)

    await _resume(host, member, since)

    expected = {keep: "Keep"} if converges else {keep: "Keep", gone: "Gone"}
    assert await _lists(member) == expected


@pytest.mark.parametrize(
    ("actor", "lands"),
    [(f"u-{OTHER}-admin", True), (OTHER_USER, False), (None, False)],
)
async def test_a_replayed_delete_from_a_v42_member_in_an_admin_only_space(
    households, actor, lands
):
    host, member = households
    await _set_tasks_level(member, "admin_only")
    (lid,) = await _both_hold(host, member, "L")
    payload = _tomb(lid, actor)
    for handler in member[federation_service_key]._event_registry.handlers_for(
        FET.SPACE_TASK_LIST_DELETED
    ):
        await handler(
            FederationEvent(
                msg_id="replayed-delete",
                event_type=FET.SPACE_TASK_LIST_DELETED,
                from_instance=OTHER,
                to_instance=MEMBER,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
                space_id=SPACE,
            )
        )
    assert (await _lists(member) == {}) is lands
    if lands:
        row = await member[db_key].fetchone(
            "SELECT deleted_by FROM space_task_lists WHERE id=?", (lid,)
        )
        assert row["deleted_by"] == actor


async def test_the_host_stream_ships_the_deleter_and_creator(households):
    host, member = households
    (lid,) = await _both_hold(host, member, "L")
    assert await _repo(host).delete_list(lid, space_id=SPACE, deleted_by="u-x")
    exporter = host[space_sync_service_key]._exporters["task_lists_deleted"]
    assert await exporter.list_records(SPACE) == [
        {"id": lid, "space_id": SPACE, "created_by": AUTHOR, "actor_user_id": "u-x"}
    ]
