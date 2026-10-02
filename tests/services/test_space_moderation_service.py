"""Tests for socialhome.services.space_moderation_service (§4.3 MODERATED).

Driven through the sticky board — the simplest feature with every action —
over a real SQLite space repo, so the queue table's conditional claim is
exercised for real.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    SpaceModerationApproved,
    SpaceModerationExpired,
    SpaceModerationQueued,
    SpaceModerationRejected,
    StickyCreated,
)
from socialhome.domain.space import (
    AccessAdminOnlyError,
    ContentAction,
    ContentQueuedForReview,
    ModerationAlreadyDecidedError,
    ModerationExpiredError,
    HostTooOldError,
    ModerationInProgressError,
    ModerationPayloadTooLargeError,
    ModerationQueueFullError,
    ModerationStatus,
    ModerationTargetGoneError,
    ModerationUnavailableError,
    SpacePermissionError,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.sticky_repo import SqliteStickyRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_moderation_service import (
    MAX_PENDING_PER_SUBMITTER,
    MAX_REJECT_REASON,
    ApplyResult,
    SpaceModerationService,
)
from socialhome.services.moderation_release import current_release
from socialhome.services.sticky_service import StickyModerationHandler, StickyService

HOST = "inst-host"


class _FakeFederationRepo:
    def __init__(self) -> None:
        self.ids: list[str] = [HOST]

    async def list_member_instance_ids(self, space_id: str) -> list[str]:
        return list(self.ids)


class _FakeModerationFederation:
    """Records what the queue sends to the other households (v_43)."""

    def __init__(self, targets=(), *, host_too_old: bool = False) -> None:
        self.targets = list(targets)
        self.host_too_old = host_too_old
        self.submitted: list[tuple[str, list[str]]] = []
        self.decided: list[tuple[str, str, str, str | None]] = []
        self.release_requests: list[tuple[str, str]] = []

    async def submission_targets(self, space) -> list[str]:
        if self.host_too_old:
            raise HostTooOldError(space.owner_instance_id)
        return list(self.targets)

    async def send_submitted(self, space, item, targets) -> None:
        self.submitted.append((item.id, list(targets)))

    async def send_decided(self, space, item, *, decision, decided_by, reason):
        self.decided.append((item.id, decision.value, decided_by, reason))

    async def send_release_request(self, space, item, *, decided_by):
        self.release_requests.append((item.id, decided_by))

    async def reviewer_households(self, space):
        return list(self.targets)

    async def display_name(self, space_id, user_id):
        return "Remote Rita" if user_id == "u-remote" else None

    async def is_remote_writer(self, space_id, user_id):
        return user_id == "u-remote"

    async def remote_role(self, space_id, user_id):
        return "admin" if user_id == "u-remote-admin" else None


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, stickies_access, feature_stickies)"
        " VALUES(?, ?, ?, 'alice', ?, ?, 1)",
        ("sp", "Space", HOST, "ab" * 32, "moderated"),
    )
    for uid, role in (
        ("alice", "owner"),
        ("mod", "moderator"),
        ("admin", "admin"),
        ("mem", "member"),
        ("other", "member"),
        ("sub", "subscriber"),
    ):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?, ?, ?)",
            (uid, uid, uid.title()),
        )
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            ("sp", uid, role),
        )
    bus = EventBus()
    events: list = []

    async def _record(event) -> None:
        events.append(event)

    for et in (
        SpaceModerationQueued,
        SpaceModerationApproved,
        SpaceModerationRejected,
        SpaceModerationExpired,
        StickyCreated,
    ):
        bus.subscribe(et, _record)

    class E:
        pass

    e = E()
    e.db = db
    e.spaces = SqliteSpaceRepo(db)
    e.fed = _FakeFederationRepo()
    e.mod = SpaceModerationService(
        e.spaces,
        user_repo=SqliteUserRepo(db),
        bus=bus,
        federation_repo=e.fed,
        own_instance_id=HOST,
    )
    e.stickies = SqliteStickyRepo(db)
    e.svc = StickyService(e.stickies, bus, space_repo=e.spaces)
    e.svc.attach_moderation(e.mod)
    handler = StickyModerationHandler(e.svc)
    for action in (ContentAction.CREATE, ContentAction.EDIT, ContentAction.DELETE):
        e.mod.register("stickies", action, handler)
    e.events = events
    yield e
    await db.shutdown()


async def _queue_create(env, author: str = "mem", content: str = "hi"):
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.create(author=author, content=content, space_id="sp")
    return exc.value.item


# ── Submit ─────────────────────────────────────────────────────────────────


async def test_member_create_queues_and_persists_nothing(env):
    item = await _queue_create(env)
    assert await env.stickies.list(space_id="sp") == []
    assert item.status is ModerationStatus.PENDING
    assert item.feature == "stickies" and item.action == "create"
    assert item.payload["entity"] == "sticky"
    assert item.expires_at - item.submitted_at == timedelta(days=7)
    assert [type(e) for e in env.events] == [SpaceModerationQueued]


async def test_content_authority_create_proceeds(env):
    for uid in ("alice", "admin", "mod"):
        await env.svc.create(author=uid, content=uid, space_id="sp")
    assert len(await env.stickies.list(space_id="sp")) == 3


async def test_own_edit_proceeds_others_edit_queues(env):
    own = await env.svc.create(author="alice", content="a", space_id="sp")
    mine = await env.svc.create(author="mod", content="m", space_id="sp")
    # A member's own note: create it via approve, then edit freely.
    item = await _queue_create(env, content="mine")
    await env.mod.approve("sp", item.id, actor_user_id="mod")
    target = item.payload["target_id"]
    edited = await env.svc.update(
        target, space_id="sp", actor_user_id="mem", content="changed"
    )
    assert edited.content == "changed"
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update(own.id, space_id="sp", actor_user_id="mem", content="x")
    queued = exc.value.item
    assert queued.payload == {
        "entity": "sticky",
        "target_id": own.id,
        "patch": {"content": "x"},
    }
    assert json.loads(queued.current_snapshot) == {"content": "a"}
    assert (await env.stickies.get(own.id)).content == "a"
    # A position-only move of somebody else's note is LAYOUT: never queued.
    moved = await env.svc.update(
        mine.id, space_id="sp", actor_user_id="mem", position_x=10
    )
    assert moved.position_x == 10.0


async def test_others_delete_queues_with_full_snapshot(env):
    note = await env.svc.create(author="alice", content="keep", space_id="sp")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.delete(note.id, space_id="sp", actor_user_id="mem")
    snap = json.loads(exc.value.item.current_snapshot)
    assert snap["content"] == "keep" and snap["author"] == "alice"
    assert await env.stickies.get(note.id) is not None


async def test_submit_on_a_stub_goes_to_the_reviewers(env):
    """v_43: a member household holds its member's item itself (the author's
    ``…/mine``) and sends it to the reviewer households — nothing reaches
    the content table."""
    await env.db.enqueue("UPDATE spaces SET owner_instance_id='elsewhere'")
    fed = _FakeModerationFederation(["elsewhere", "inst-mod"])
    env.mod.attach_federation(fed)
    item = await _queue_create(env)
    assert fed.submitted == [(item.id, ["elsewhere", "inst-mod"])]
    assert (await env.spaces.get_moderation_item(item.id)).status.value == "pending"
    assert await env.stickies.list(space_id="sp") == []


async def test_submit_on_a_stub_whose_host_is_too_old_stores_nothing(env):
    await env.db.enqueue("UPDATE spaces SET owner_instance_id='elsewhere'")
    env.mod.attach_federation(_FakeModerationFederation(host_too_old=True))
    with pytest.raises(HostTooOldError):
        await env.svc.create(author="mem", content="x", space_id="sp")
    assert await env.spaces.count_pending("sp") == 0
    assert await env.stickies.list(space_id="sp") == []


async def test_submit_on_a_stub_without_federation_fails_closed(env):
    await env.db.enqueue("UPDATE spaces SET owner_instance_id='elsewhere'")
    with pytest.raises(SpacePermissionError):
        await env.svc.create(author="mem", content="x", space_id="sp")
    assert await env.spaces.count_pending("sp") == 0
    assert await env.stickies.list(space_id="sp") == []


async def test_the_host_sends_its_members_items_to_remote_reviewers(env):
    env.fed.ids.append("inst-mod")
    fed = _FakeModerationFederation(["inst-mod"])
    env.mod.attach_federation(fed)
    item = await _queue_create(env)
    assert fed.submitted == [(item.id, ["inst-mod"])]


async def test_a_host_without_remote_reviewers_sends_nothing(env):
    fed = _FakeModerationFederation([])
    env.mod.attach_federation(fed)
    await _queue_create(env)
    assert fed.submitted == []


async def test_queue_without_attached_submitter_fails_closed(env):
    bare = StickyService(env.stickies, None, space_repo=env.spaces)
    with pytest.raises(SpacePermissionError):
        await bare.create(author="mem", content="x", space_id="sp")
    assert await env.stickies.list(space_id="sp") == []


async def test_per_submitter_cap(env):
    for i in range(MAX_PENDING_PER_SUBMITTER):
        await _queue_create(env, content=f"n{i}")
    with pytest.raises(ModerationQueueFullError):
        await env.svc.create(author="mem", content="one more", space_id="sp")
    # Another member still has room.
    await _queue_create(env, author="other")


async def test_per_space_cap(env, monkeypatch):
    monkeypatch.setattr(
        "socialhome.services.space_moderation_service.MAX_PENDING_PER_SPACE", 2
    )
    await _queue_create(env)
    await _queue_create(env, author="other")
    with pytest.raises(ModerationQueueFullError):
        await env.svc.create(author="mem", content="x", space_id="sp")


async def test_payload_cap(env):
    space = await env.spaces.get("sp")
    with pytest.raises(ModerationPayloadTooLargeError):
        await env.mod.submit(
            space,
            feature="stickies",
            action=ContentAction.EDIT,
            submitted_by="mem",
            payload={"entity": "sticky", "target_id": "t", "patch": {"content": "x"}},
            snapshot={"content": "y" * (260 * 1024)},
        )


async def test_unknown_feature_action_fails_closed(env):
    space = await env.spaces.get("sp")
    with pytest.raises(SpacePermissionError):
        await env.mod.submit(
            space,
            feature="pages",
            action=ContentAction.CREATE,
            submitted_by="mem",
            payload={},
        )


def test_register_twice_refused(env):
    with pytest.raises(ValueError):
        env.mod.register(
            "stickies", ContentAction.CREATE, StickyModerationHandler(env.svc)
        )


# ── Approve ────────────────────────────────────────────────────────────────


async def test_approve_create_persists_once_attributed_to_submitter(env):
    item = await _queue_create(env)
    result = await env.mod.approve("sp", item.id, actor_user_id="mod")
    assert result == ApplyResult(target_id=item.payload["target_id"])
    notes = await env.stickies.list(space_id="sp")
    assert [(n.id, n.author, n.content) for n in notes] == [
        (item.payload["target_id"], "mem", "hi")
    ]
    stored = await env.spaces.get_moderation_item(item.id)
    assert stored.status is ModerationStatus.APPROVED
    assert stored.reviewed_by == "mod"
    assert SpaceModerationApproved in [type(e) for e in env.events]
    with pytest.raises(ModerationAlreadyDecidedError):
        await env.mod.approve("sp", item.id, actor_user_id="alice")


async def test_double_approve_race_persists_once(env):
    item = await _queue_create(env)
    results = await asyncio.gather(
        env.mod.approve("sp", item.id, actor_user_id="mod"),
        env.mod.approve("sp", item.id, actor_user_id="alice"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ApplyResult) for r in results) == 1
    assert (
        sum(
            isinstance(r, (ModerationAlreadyDecidedError, ModerationInProgressError))
            for r in results
        )
        == 1
    )
    assert len(await env.stickies.list(space_id="sp")) == 1
    assert sum(isinstance(e, StickyCreated) for e in env.events) == 1


async def test_approve_edit_is_latest_wins_per_field(env):
    note = await env.svc.create(author="alice", content="a", space_id="sp")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update(note.id, space_id="sp", actor_user_id="mem", content="b")
    # Meanwhile the owner recolours it: the approved edit must not undo that.
    await env.svc.update(note.id, space_id="sp", actor_user_id="alice", color="#00FF00")
    described = await env.mod.describe(exc.value.item, with_current=True)
    assert described["snapshot"] == {"content": "a"}
    assert described["current"] == {"content": "a"}
    assert described["preview"] == {"content": "b"}
    await env.mod.approve("sp", exc.value.item.id, actor_user_id="mod")
    got = await env.stickies.get(note.id)
    assert (got.content, got.color, got.author) == ("b", "#00FF00", "alice")


async def test_approve_edit_of_deleted_target_expires_410(env):
    note = await env.svc.create(author="alice", content="a", space_id="sp")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update(note.id, space_id="sp", actor_user_id="mem", content="b")
    await env.svc.delete(note.id, space_id="sp", actor_user_id="alice")
    with pytest.raises(ModerationTargetGoneError):
        await env.mod.approve("sp", exc.value.item.id, actor_user_id="mod")
    stored = await env.spaces.get_moderation_item(exc.value.item.id)
    assert stored.status is ModerationStatus.EXPIRED
    assert SpaceModerationExpired in [type(e) for e in env.events]


async def test_approve_delete_of_deleted_target_is_noop(env):
    note = await env.svc.create(author="alice", content="a", space_id="sp")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.delete(note.id, space_id="sp", actor_user_id="mem")
    await env.svc.delete(note.id, space_id="sp", actor_user_id="alice")
    await env.mod.approve("sp", exc.value.item.id, actor_user_id="mod")
    stored = await env.spaces.get_moderation_item(exc.value.item.id)
    assert stored.status is ModerationStatus.APPROVED


async def test_approve_delete_removes_target(env):
    note = await env.svc.create(author="alice", content="a", space_id="sp")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.delete(note.id, space_id="sp", actor_user_id="mem")
    await env.mod.approve("sp", exc.value.item.id, actor_user_id="mod")
    assert await env.stickies.get(note.id) is None


async def test_plain_member_cannot_approve_or_list(env):
    item = await _queue_create(env)
    with pytest.raises(SpacePermissionError):
        await env.mod.approve("sp", item.id, actor_user_id="other")
    with pytest.raises(SpacePermissionError):
        await env.mod.list_items("sp", actor_user_id="other")
    with pytest.raises(SpacePermissionError):
        await env.mod.reject("sp", item.id, actor_user_id="mem")


async def test_approve_now_admin_only_needs_admin(env):
    item = await _queue_create(env)
    await env.db.enqueue("UPDATE spaces SET stickies_access='admin_only'")
    with pytest.raises(AccessAdminOnlyError):
        await env.mod.approve("sp", item.id, actor_user_id="mod")
    assert (
        await env.spaces.get_moderation_item(item.id)
    ).status is ModerationStatus.PENDING
    await env.mod.approve("sp", item.id, actor_user_id="admin")
    assert len(await env.stickies.list(space_id="sp")) == 1


async def test_approve_feature_disabled_or_archived_409_reject_works(env):
    item = await _queue_create(env)
    await env.db.enqueue("UPDATE spaces SET feature_stickies=0")
    with pytest.raises(ModerationUnavailableError):
        await env.mod.approve("sp", item.id, actor_user_id="mod")
    await env.db.enqueue("UPDATE spaces SET feature_stickies=1, archived=1")
    with pytest.raises(ModerationUnavailableError):
        await env.mod.approve("sp", item.id, actor_user_id="mod")
    await env.mod.reject("sp", item.id, actor_user_id="mod")
    assert (
        await env.spaces.get_moderation_item(item.id)
    ).status is ModerationStatus.REJECTED


async def test_approve_after_submitter_left_expires(env):
    item = await _queue_create(env)
    await env.db.enqueue("DELETE FROM space_members WHERE user_id='mem'")
    with pytest.raises(ModerationTargetGoneError):
        await env.mod.approve("sp", item.id, actor_user_id="mod")
    assert (
        await env.spaces.get_moderation_item(item.id)
    ).status is ModerationStatus.EXPIRED
    assert await env.stickies.list(space_id="sp") == []


async def test_failed_apply_releases_claim(env, monkeypatch):
    item = await _queue_create(env)

    async def boom(*a, **kw):
        raise ValueError("nope")

    monkeypatch.setattr(StickyService, "create", boom)
    with pytest.raises(ValueError):
        await env.mod.approve("sp", item.id, actor_user_id="mod")
    assert (
        await env.spaces.get_moderation_item(item.id)
    ).status is ModerationStatus.PENDING


async def test_unknown_item_or_other_space_is_404(env):
    with pytest.raises(KeyError):
        await env.mod.approve("sp", "nope", actor_user_id="mod")


# ── Reject / mine / expire ─────────────────────────────────────────────────


async def test_reject_records_reason_and_caps_it(env):
    item = await _queue_create(env)
    with pytest.raises(ValueError):
        await env.mod.reject(
            "sp", item.id, actor_user_id="mod", reason="x" * (MAX_REJECT_REASON + 1)
        )
    await env.mod.reject("sp", item.id, actor_user_id="mod", reason="  off topic ")
    stored = await env.spaces.get_moderation_item(item.id)
    assert stored.status is ModerationStatus.REJECTED
    assert stored.rejection_reason == "off topic"
    rejected = [e for e in env.events if isinstance(e, SpaceModerationRejected)]
    assert rejected[0].item.rejection_reason == "off topic"
    with pytest.raises(ModerationAlreadyDecidedError):
        await env.mod.reject("sp", item.id, actor_user_id="mod")
    assert await env.stickies.list(space_id="sp") == []


async def test_list_mine_is_own_only_any_status(env):
    a = await _queue_create(env)
    await _queue_create(env, author="other")
    await env.mod.reject("sp", a.id, actor_user_id="mod")
    b = await _queue_create(env)
    mine = await env.mod.list_mine("sp", user_id="mem")
    assert {i.id for i in mine} == {a.id, b.id}
    assert all(i.submitted_by == "mem" for i in mine)
    with pytest.raises(SpacePermissionError):
        await env.mod.list_mine("sp", user_id="stranger")


async def test_list_items_pending_and_all(env):
    a = await _queue_create(env)
    b = await _queue_create(env, author="other")
    await env.mod.reject("sp", a.id, actor_user_id="mod")
    pending = await env.mod.list_items("sp", actor_user_id="mod")
    assert [i.id for i in pending] == [b.id]
    everything = await env.mod.list_items(
        "sp", actor_user_id="mod", include_decided=True
    )
    assert {i.id for i in everything} == {a.id, b.id}


async def test_describe_shape(env):
    item = await _queue_create(env)
    d = await env.mod.describe(item)
    assert d["entity"] == "sticky" and d["target_id"] == item.payload["target_id"]
    assert d["submitted_by_display"] == "Mem"
    assert d["preview"]["content"] == "hi"
    assert d["snapshot"] is None and d["current"] is None
    assert d["status"] == "pending"


async def test_expire_due_and_purge(env):
    item = await _queue_create(env)
    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert await env.mod.expire_due(later) == 1
    assert (
        await env.spaces.get_moderation_item(item.id)
    ).status is ModerationStatus.EXPIRED
    assert SpaceModerationExpired in [type(e) for e in env.events]
    assert await env.mod.purge_decided(later + timedelta(days=8)) == 1
    assert (await env.spaces.get_moderation_item(item.id)).payload == {}


# ── Config gate ────────────────────────────────────────────────────────────


# ── A failure after the content landed (adversarial review I2) ────────────


async def test_side_effect_failure_after_create_landed_keeps_it_approved(
    env, monkeypatch, caplog
):
    """The note was persisted, then the bus publish blew up: the item must
    stay APPROVED (never reopened, never rejectable) — WARNING logged."""
    item = await _queue_create(env)

    async def boom(self, event):
        raise RuntimeError("bus down")

    monkeypatch.setattr(StickyService, "_emit", boom)
    with caplog.at_level("WARNING"):
        result = await env.mod.approve("sp", item.id, actor_user_id="mod")
    assert result.complete is False
    assert "landed" in caplog.text
    assert len(await env.stickies.list(space_id="sp")) == 1
    stored = await env.spaces.get_moderation_item(item.id)
    assert stored.status is ModerationStatus.APPROVED
    with pytest.raises(ModerationAlreadyDecidedError):
        await env.mod.reject("sp", item.id, actor_user_id="mod")


async def test_side_effect_failure_after_edit_landed_keeps_it_approved(
    env, monkeypatch
):
    note = await env.svc.create(author="alice", content="a", space_id="sp")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update(note.id, space_id="sp", actor_user_id="mem", content="b")

    async def boom(self, event):
        raise RuntimeError("bus down")

    monkeypatch.setattr(StickyService, "_emit", boom)
    await env.mod.approve("sp", exc.value.item.id, actor_user_id="mod")
    assert (await env.stickies.get(note.id)).content == "b"
    stored = await env.spaces.get_moderation_item(exc.value.item.id)
    assert stored.status is ModerationStatus.APPROVED


async def test_failure_before_anything_landed_reopens_for_retry(env, monkeypatch):
    note = await env.svc.create(author="alice", content="a", space_id="sp")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update(note.id, space_id="sp", actor_user_id="mem", content="b")

    async def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(SqliteStickyRepo, "update_content", boom)
    with pytest.raises(RuntimeError):
        await env.mod.approve("sp", exc.value.item.id, actor_user_id="mod")
    assert (await env.stickies.get(note.id)).content == "a"
    stored = await env.spaces.get_moderation_item(exc.value.item.id)
    assert stored.status is ModerationStatus.PENDING
    monkeypatch.undo()
    await env.mod.approve("sp", exc.value.item.id, actor_user_id="mod")
    assert (await env.stickies.get(note.id)).content == "b"


async def test_a_failed_apply_never_reopens_an_item_decided_meanwhile(env, monkeypatch):
    """The release is conditional on the claim: if the row moved on while
    the apply ran (here: the expiry sweep), it stays where it is."""
    item = await _queue_create(env)

    async def boom(self, **kw):
        await env.db.enqueue(
            "UPDATE space_moderation_queue SET status='expired' WHERE id=?",
            (item.id,),
        )
        raise ValueError("nope")

    monkeypatch.setattr(StickyService, "create", boom)
    with pytest.raises(ValueError):
        await env.mod.approve("sp", item.id, actor_user_id="mod")
    stored = await env.spaces.get_moderation_item(item.id)
    assert stored.status is ModerationStatus.EXPIRED


# ── M3 / M7 ────────────────────────────────────────────────────────────────


async def test_approve_past_expiry_expires_it_410(env):
    item = await _queue_create(env)
    await env.db.enqueue(
        "UPDATE space_moderation_queue SET expires_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), item.id),
    )
    with pytest.raises(ModerationExpiredError):
        await env.mod.approve("sp", item.id, actor_user_id="mod")
    stored = await env.spaces.get_moderation_item(item.id)
    assert stored.status is ModerationStatus.EXPIRED
    assert await env.stickies.list(space_id="sp") == []
    assert SpaceModerationExpired in [type(e) for e in env.events]


async def test_a_reviewer_household_hands_its_approval_to_the_host(env):
    """v_43: off the host an approve applies nothing — it is checked, sent
    to the host (which publishes from its own copy), and the row shows as
    publishing until the host's decision arrives."""
    await env.db.enqueue("UPDATE spaces SET owner_instance_id='elsewhere'")
    fed = _FakeModerationFederation(["elsewhere"])
    env.mod.attach_federation(fed)
    item = await _queue_create(env)
    result = await env.mod.approve("sp", item.id, actor_user_id="mod")
    assert result.publishing is True
    assert fed.release_requests == [(item.id, "mod")]
    assert fed.decided == []
    assert await env.stickies.list(space_id="sp") == []
    held = await env.spaces.get_moderation_item(item.id)
    assert held.status is ModerationStatus.PENDING
    assert (await env.mod.describe(held))["publishing"] is True


async def test_the_host_applies_a_remote_moderators_approval(env):
    """The host releases an item for a moderator of another household: the
    approver's verified role passes the content gate (they hold no local
    seat), the content stays the submitter's, the decision is announced."""
    fed = _FakeModerationFederation(["inst-mod"])
    env.mod.attach_federation(fed)
    seen: list = []

    async def _scope(event) -> None:
        seen.append(current_release())

    env.mod._bus.subscribe(StickyCreated, _scope)
    item = await _queue_create(env)
    await env.mod.release_remote(item, approved_by="u-remote-mod", role="moderator")
    notes = await env.stickies.list(space_id="sp")
    assert [n.author for n in notes] == ["mem"]
    assert [(r.item_id, r.approved_by) for r in seen] == [(item.id, "u-remote-mod")]
    assert fed.decided == [(item.id, "approved", "u-remote-mod", None)]


async def test_the_host_refuses_a_remote_approval_admin_only_keeps_out(env):
    item = await _queue_create(env)
    await env.db.enqueue("UPDATE spaces SET stickies_access='admin_only'")
    with pytest.raises(AccessAdminOnlyError):
        await env.mod.release_remote(item, approved_by="u-rm", role="moderator")
    assert await env.stickies.list(space_id="sp") == []


async def test_a_release_seen_from_the_host_marks_the_row_approved(env):
    item = await _queue_create(env)
    await env.mod.note_release(item, "u-rm")
    assert (await env.spaces.get_moderation_item(item.id)).status is (
        ModerationStatus.APPROVED
    )


async def test_an_early_decision_tombstone_blocks_a_late_submission(env):
    item = replace(await _queue_create(env), id="late")
    await env.mod.record_early_decision(
        item_id="late",
        space_id="sp",
        decision=ModerationStatus.REJECTED,
        decided_by="u-rm",
        reason=None,
    )
    assert not await env.mod.store_received(item)
    assert (await env.spaces.get_moderation_item("late")).status is (
        ModerationStatus.REJECTED
    )


async def test_losing_content_authority_drops_other_households_items(env):
    await env.db.enqueue("UPDATE spaces SET owner_instance_id='elsewhere'")
    env.mod.attach_federation(_FakeModerationFederation(["elsewhere"]))
    mine = await _queue_create(env)
    theirs = replace(mine, id="theirs", submitted_by="u-remote")
    await env.mod.store_received(theirs)
    assert await env.mod.drop_held_for_others("sp") == 0  # we still review
    await env.db.enqueue(
        "UPDATE space_members SET role='member' WHERE role IN"
        " ('owner', 'admin', 'moderator')"
    )
    assert await env.mod.drop_held_for_others("sp") == 1
    gone = await env.spaces.get_moderation_item("theirs")
    assert gone.status is ModerationStatus.EXPIRED and gone.payload == {}
    kept = await env.spaces.get_moderation_item(mine.id)
    assert kept.status is ModerationStatus.PENDING


async def test_a_rejection_is_announced_with_its_reason(env):
    fed = _FakeModerationFederation(["inst-mod"])
    env.mod.attach_federation(fed)
    item = await _queue_create(env)
    await env.mod.reject("sp", item.id, actor_user_id="mod", reason="spam")
    assert fed.decided == [(item.id, "rejected", "mod", "spam")]


async def test_a_received_item_is_stored_once_and_announced(env):
    item = replace(await _queue_create(env), id="remote-item", submitted_by="u-remote")
    env.events.clear()
    assert await env.mod.store_received(item)
    assert not await env.mod.store_received(replace(item, payload={"x": 1}))
    assert [type(e) for e in env.events] == [SpaceModerationQueued]
    held = await env.spaces.get_moderation_item("remote-item")
    assert held.payload == item.payload


async def test_a_received_approval_beats_a_local_rejection(env):
    """Two households decide at once: the approval wins everywhere — the
    content was published — while a late rejection never undoes it."""
    item = await _queue_create(env)
    await env.mod.reject("sp", item.id, actor_user_id="mod", reason="no")
    assert await env.mod.apply_decision(
        item,
        decision=ModerationStatus.APPROVED,
        decided_by="u-remote-mod",
        reason=None,
    )
    held = await env.spaces.get_moderation_item(item.id)
    assert held.status is ModerationStatus.APPROVED
    assert held.reviewed_by == "u-remote-mod"
    assert not await env.mod.apply_decision(
        item, decision=ModerationStatus.REJECTED, decided_by="mod", reason="late"
    )
    assert (await env.spaces.get_moderation_item(item.id)).status is (
        ModerationStatus.APPROVED
    )
    # A duplicate approval moves nothing.
    assert not await env.mod.apply_decision(
        item, decision=ModerationStatus.APPROVED, decided_by="x", reason=None
    )


async def test_a_received_rejection_of_a_pending_item_is_recorded(env):
    item = await _queue_create(env)
    env.events.clear()
    assert await env.mod.apply_decision(
        item, decision=ModerationStatus.REJECTED, decided_by="u-rm", reason="dup"
    )
    held = await env.spaces.get_moderation_item(item.id)
    assert (held.status, held.rejection_reason) == (ModerationStatus.REJECTED, "dup")
    assert [type(e) for e in env.events] == [SpaceModerationRejected]


async def test_describe_names_a_remote_submitter_from_the_roster(env):
    env.mod.attach_federation(_FakeModerationFederation())
    item = replace(await _queue_create(env), submitted_by="u-remote")
    assert (await env.mod.describe(item))["submitted_by_display"] == "Remote Rita"


async def test_payload_cap_counts_stored_utf8_bytes(env, monkeypatch):
    """M2: 300 KiB of 3-byte characters is ~100 K characters but ~300 KiB
    stored — refused on the stored size."""
    monkeypatch.setattr(
        "socialhome.services.space_moderation_service.MAX_PAYLOAD_BYTES", 1000
    )
    space = await env.spaces.get("sp")
    with pytest.raises(ModerationPayloadTooLargeError):
        await env.mod.submit(
            space,
            feature="stickies",
            action=ContentAction.EDIT,
            submitted_by="mem",
            payload={"entity": "sticky", "target_id": "t", "patch": {"content": "x"}},
            snapshot={"content": "€" * 400},  # 400 chars, 1200 bytes
        )


async def test_mine_keeps_every_pending_item_past_many_decided_ones(env, monkeypatch):
    """M4: newer decided items never crowd a pending one out of /mine, and
    the queue lists every pending item, not the first 100."""
    monkeypatch.setattr(
        "socialhome.services.space_moderation_service.MINE_RECENT_LIMIT", 2
    )
    old = await _queue_create(env, content="oldest pending")
    for i in range(3):
        decided = await _queue_create(env, content=f"r{i}")
        await env.mod.reject("sp", decided.id, actor_user_id="mod")
    mine = await env.mod.list_mine("sp", user_id="mem")
    assert old.id in {i.id for i in mine}
    stamps = [i.submitted_at for i in mine]
    assert stamps == sorted(stamps, reverse=True)  # newest first
    assert await env.mod.find_pending("sp", "mem", entity="sticky") is not None


async def test_queue_lists_more_than_100_pending(env):
    await env.db.enqueue("DELETE FROM space_members WHERE user_id='mem'")
    for n in range(6):
        await env.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES('sp', ?, 'member')",
            (f"m{n}",),
        )
        for i in range(MAX_PENDING_PER_SUBMITTER):
            await _queue_create(env, author=f"m{n}", content=f"{n}-{i}")
    items = await env.mod.list_items("sp", actor_user_id="mod")
    assert len(items) == 6 * MAX_PENDING_PER_SUBMITTER


async def test_an_approve_while_another_is_applying_is_in_progress(env, monkeypatch):
    """N1: a second approve / resume of an item whose apply is still running
    answers IN_PROGRESS — it never starts a second, racing apply."""
    item = await _queue_create(env)
    started, release = asyncio.Event(), asyncio.Event()
    real_create = StickyService.create

    async def slow_create(self, **kw):
        started.set()
        await release.wait()
        return await real_create(self, **kw)

    monkeypatch.setattr(StickyService, "create", slow_create)
    first = asyncio.create_task(env.mod.approve("sp", item.id, actor_user_id="mod"))
    await started.wait()
    with pytest.raises(ModerationInProgressError):
        await env.mod.approve("sp", item.id, actor_user_id="alice")
    release.set()
    await first
    assert len(await env.stickies.list(space_id="sp")) == 1
    with pytest.raises(ModerationAlreadyDecidedError):
        await env.mod.approve("sp", item.id, actor_user_id="alice")


async def test_only_an_equal_or_higher_role_overturns_a_rejection(env):
    """M4: rank owner > admin > moderator, by the seats the host holds; an
    unknown rejecter counts as a moderator."""
    env.mod.attach_federation(_FakeModerationFederation())
    item = await _queue_create(env)
    await env.mod.reject("sp", item.id, actor_user_id="admin")
    with pytest.raises(ModerationAlreadyDecidedError):
        await env.mod.release_remote(item, approved_by="u-rm", role="moderator")
    await env.mod.release_remote(item, approved_by="u-ra", role="admin")
    assert (await env.spaces.get_moderation_item(item.id)).status is (
        ModerationStatus.APPROVED
    )
    other = await _queue_create(env, content="two")
    await env.spaces.claim_moderation_item(
        other.id, status=ModerationStatus.REJECTED, reviewed_by="u-remote-admin"
    )
    with pytest.raises(ModerationAlreadyDecidedError):
        await env.mod.release_remote(other, approved_by="u-rm", role="moderator")
    third = await _queue_create(env, content="three")
    await env.spaces.claim_moderation_item(
        third.id, status=ModerationStatus.REJECTED, reviewed_by="u-nobody"
    )
    await env.mod.release_remote(third, approved_by="u-rm", role="moderator")
