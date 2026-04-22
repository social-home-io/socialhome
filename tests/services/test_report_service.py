"""Tests for :class:`ReportService`."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.report import (
    DuplicateReportError,
    ReportRateLimitedError,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.report_repo import SqliteReportRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.report_service import ReportService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    report_repo = SqliteReportRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    svc = ReportService(report_repo=report_repo, user_repo=user_repo, bus=bus)

    class S:
        pass

    s = S()
    s.db = db
    s.svc = svc
    s.user_svc = user_svc

    async def provision(name, is_admin=False):
        return await user_svc.provision(
            username=name,
            display_name=name,
            is_admin=is_admin,
        )

    s.provision = provision
    yield s
    await db.shutdown()


async def test_create_report_publishes_event(stack):
    from socialhome.domain.events import ReportFiled

    reporter = await stack.provision("anna")
    await stack.provision("bob")
    fired: list[ReportFiled] = []

    async def _on(event: ReportFiled) -> None:
        fired.append(event)

    stack.svc._bus.subscribe(ReportFiled, _on)
    report, federated = await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="post",
        target_id="p-1",
        category="spam",
    )
    assert report.category.value == "spam"
    assert federated is False  # no federation attached in this test
    assert fired and fired[0].report_id == report.id


async def test_duplicate_report_raises(stack):
    reporter = await stack.provision("anna")
    await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="post",
        target_id="p-1",
        category="spam",
    )
    with pytest.raises(DuplicateReportError):
        await stack.svc.create_report(
            reporter_user_id=reporter.user_id,
            target_type="post",
            target_id="p-1",
            category="harassment",
        )


async def test_rate_limit_caps_reports_per_day(stack, monkeypatch):
    reporter = await stack.provision("anna")
    # Lower the cap for the test so we don't insert 20 rows.
    monkeypatch.setattr(
        "socialhome.services.report_service.MAX_REPORTS_PER_DAY",
        3,
    )
    # ReportService snapshots the constant at import time into the module,
    # so patch via the module attribute and re-reference.
    from socialhome.services import report_service as rs

    rs.MAX_REPORTS_PER_DAY = 3
    for i in range(3):
        await stack.svc.create_report(
            reporter_user_id=reporter.user_id,
            target_type="post",
            target_id=f"p-{i}",
            category="spam",
        )
    with pytest.raises(ReportRateLimitedError):
        await stack.svc.create_report(
            reporter_user_id=reporter.user_id,
            target_type="post",
            target_id="p-last",
            category="spam",
        )


async def test_list_pending_requires_admin(stack):
    await stack.provision("pascal", is_admin=True)
    non_admin = await stack.provision("bob")
    await stack.svc.create_report(
        reporter_user_id=non_admin.user_id,
        target_type="comment",
        target_id="c-1",
        category="other",
    )
    items = await stack.svc.list_pending(actor_username="pascal")
    assert len(items) == 1
    with pytest.raises(PermissionError):
        await stack.svc.list_pending(actor_username="bob")


async def test_resolve_marks_resolved_and_double_resolve_raises(stack):
    from socialhome.domain.space import ModerationAlreadyDecidedError

    await stack.provision("pascal", is_admin=True)
    reporter = await stack.provision("bob")
    report, _ = await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="post",
        target_id="p-1",
        category="spam",
    )
    await stack.svc.resolve(report.id, actor_username="pascal")
    # Pending list is empty now.
    assert await stack.svc.list_pending(actor_username="pascal") == []
    with pytest.raises(ModerationAlreadyDecidedError):
        await stack.svc.resolve(report.id, actor_username="pascal")


async def test_invalid_category_raises_value_error(stack):
    reporter = await stack.provision("anna")
    with pytest.raises(ValueError):
        await stack.svc.create_report(
            reporter_user_id=reporter.user_id,
            target_type="post",
            target_id="p-1",
            category="bogus",
        )


# ── Federation (§CP.R1) ──────────────────────────────────────────────


class _FakeFederation:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
            }
        )


class _StubUserRepoWithInstance:
    """Wrap the real user_repo so we can control ``get_instance_for_user``."""

    def __init__(self, inner, map_):
        self._inner = inner
        self._map = map_

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def get_instance_for_user(self, user_id):
        if user_id in self._map:
            return self._map[user_id]
        return await self._inner.get_instance_for_user(user_id)


async def test_create_report_federates_when_user_target_is_remote(stack):
    """Reporting a remote user → SPACE_REPORT sent to their instance."""
    from socialhome.domain.federation import FederationEventType

    fed = _FakeFederation()
    reporter = await stack.provision("anna")
    # Patch instance resolution: remote user lives on peer-a.
    stack.svc._users = _StubUserRepoWithInstance(
        stack.svc._users,
        {"remote-uid": "peer-a"},
    )
    stack.svc.attach_federation(fed, own_instance_id="self")
    report, federated = await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="user",
        target_id="remote-uid",
        category="harassment",
    )
    assert federated is True
    sent = [s for s in fed.sent if s["type"] == FederationEventType.SPACE_REPORT]
    assert len(sent) == 1
    assert sent[0]["to"] == "peer-a"
    assert sent[0]["payload"]["category"] == "harassment"
    assert sent[0]["payload"]["reporter_user_id"] == reporter.user_id


async def test_create_report_stays_local_when_target_is_local(stack):
    fed = _FakeFederation()
    reporter = await stack.provision("anna")
    stack.svc._users = _StubUserRepoWithInstance(
        stack.svc._users,
        {"local-uid": "self"},
    )
    stack.svc.attach_federation(fed, own_instance_id="self")
    _, federated = await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="user",
        target_id="local-uid",
        category="spam",
    )
    assert federated is False
    assert fed.sent == []


async def test_create_report_from_remote_persists_and_publishes(stack):
    from socialhome.domain.events import ReportFiled

    fired: list[ReportFiled] = []

    async def _on(event: ReportFiled) -> None:
        fired.append(event)

    stack.svc._bus.subscribe(ReportFiled, _on)
    result = await stack.svc.create_report_from_remote(
        reporter_user_id="remote-uid",
        reporter_instance_id="peer-a",
        target_type="post",
        target_id="p-local",
        category="spam",
        notes="looks like spam",
    )
    assert result is not None
    assert result.reporter_instance_id == "peer-a"
    assert len(fired) == 1


async def test_create_report_from_remote_dedup_on_replay(stack):
    first = await stack.svc.create_report_from_remote(
        reporter_user_id="remote-uid",
        reporter_instance_id="peer-a",
        target_type="post",
        target_id="p-1",
        category="spam",
    )
    assert first is not None
    second = await stack.svc.create_report_from_remote(
        reporter_user_id="remote-uid",
        reporter_instance_id="peer-a",
        target_type="post",
        target_id="p-1",
        category="spam",
    )
    # Second call is a replay — must not raise; returns None for the dup.
    assert second is None


async def test_create_report_auto_forwards_to_paired_gfs(stack):
    """With attach_gfs wired, create_report fires a background forward."""
    import asyncio
    from types import SimpleNamespace

    reporter = await stack.provision("anna")

    forwarded: list[dict] = []

    class _FakeGfs:
        async def list_connections(self):
            return [
                SimpleNamespace(id="gfs-1", endpoint_url="http://g", status="active")
            ]

        async def report_fraud(self, gfs_id, **kwargs):
            forwarded.append({"gfs_id": gfs_id, **kwargs})

    stack.svc._users = _StubUserRepoWithInstance(
        stack.svc._users,
        {"remote-uid": "peer-a"},
    )
    stack.svc.attach_gfs(_FakeGfs(), signing_key=b"\x01" * 32)
    _, _ = await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="user",
        target_id="remote-uid",
        category="spam",
        forward_gfs=True,
    )
    # Let the background task run.
    await asyncio.sleep(0.05)
    assert len(forwarded) == 1
    assert forwarded[0]["target_type"] == "instance"
    assert forwarded[0]["target_id"] == "peer-a"


async def test_auto_forward_resolves_space_target_unchanged(stack):
    """Space target forwards as-is to every paired GFS."""
    import asyncio
    from types import SimpleNamespace

    reporter = await stack.provision("anna")
    forwarded: list[dict] = []

    class _FakeGfs:
        async def list_connections(self):
            return [SimpleNamespace(id="g", endpoint_url="", status="active")]

        async def report_fraud(self, gfs_id, **kwargs):
            forwarded.append(kwargs)

    stack.svc.attach_gfs(_FakeGfs(), signing_key=b"\x00" * 32)
    await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="space",
        target_id="sp-1",
        category="other",
    )
    await asyncio.sleep(0.05)
    assert len(forwarded) == 1
    assert forwarded[0]["target_type"] == "space"
    assert forwarded[0]["target_id"] == "sp-1"


async def test_auto_forward_resolves_post_target_to_space(stack):
    """A report on a post forwards as 'space' with the owning space_id."""
    import asyncio
    from types import SimpleNamespace

    reporter = await stack.provision("anna")
    forwarded: list[dict] = []

    class _FakePostRepo:
        async def get(self, post_id):
            return ("sp-owner", SimpleNamespace(author="remote-uid"))

        async def get_comment(self, comment_id):
            return None

    class _FakeGfs:
        async def list_connections(self):
            return [SimpleNamespace(id="g", endpoint_url="", status="active")]

        async def report_fraud(self, gfs_id, **kwargs):
            forwarded.append(kwargs)

    stack.svc._space_post_repo = _FakePostRepo()
    stack.svc._users = _StubUserRepoWithInstance(
        stack.svc._users,
        {"remote-uid": "peer-a"},
    )
    stack.svc.attach_gfs(_FakeGfs(), signing_key=b"\x00" * 32)
    await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="post",
        target_id="post-xyz",
        category="spam",
    )
    await asyncio.sleep(0.05)
    assert len(forwarded) == 1
    assert forwarded[0]["target_type"] == "space"
    assert forwarded[0]["target_id"] == "sp-owner"


async def test_create_report_forward_gfs_false_skips_background(stack):
    import asyncio
    from types import SimpleNamespace

    reporter = await stack.provision("anna")
    forwarded: list[dict] = []

    class _FakeGfs:
        async def list_connections(self):
            return [
                SimpleNamespace(id="gfs-1", endpoint_url="http://g", status="active")
            ]

        async def report_fraud(self, gfs_id, **kwargs):
            forwarded.append(kwargs)

    stack.svc._users = _StubUserRepoWithInstance(
        stack.svc._users,
        {"remote-uid": "peer-a"},
    )
    stack.svc.attach_gfs(_FakeGfs(), signing_key=b"\x02" * 32)
    await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="user",
        target_id="remote-uid",
        category="spam",
        forward_gfs=False,
    )
    await asyncio.sleep(0.05)
    assert forwarded == []


async def test_create_report_from_remote_skips_bad_payload(stack):
    # Missing reporter_user_id → None.
    assert (
        await stack.svc.create_report_from_remote(
            reporter_user_id="",
            reporter_instance_id="peer-a",
            target_type="post",
            target_id="p-1",
            category="spam",
        )
        is None
    )
    # Missing reporter_instance_id → None.
    assert (
        await stack.svc.create_report_from_remote(
            reporter_user_id="uid-x",
            reporter_instance_id="",
            target_type="post",
            target_id="p-1",
            category="spam",
        )
        is None
    )
    # Bad category → None.
    assert (
        await stack.svc.create_report_from_remote(
            reporter_user_id="uid-x",
            reporter_instance_id="peer-a",
            target_type="post",
            target_id="p-1",
            category="not_a_category",
        )
        is None
    )


async def test_resolve_target_instance_returns_none_for_missing_repos(stack):
    """When space_repo / space_post_repo aren't attached, target resolution
    falls through to None — no crash.
    """
    from socialhome.domain.report import ReportTargetType

    # Drop the optional repos to simulate a minimal wiring.
    stack.svc._space_repo = None
    stack.svc._space_post_repo = None
    assert (
        await stack.svc._resolve_target_instance(
            ReportTargetType.POST,
            "p-1",
        )
        is None
    )
    assert (
        await stack.svc._resolve_target_instance(
            ReportTargetType.COMMENT,
            "c-1",
        )
        is None
    )
    assert (
        await stack.svc._resolve_target_instance(
            ReportTargetType.SPACE,
            "s-1",
        )
        is None
    )


async def test_create_report_without_federation_skips_send(stack):
    """No federation attached → ``federated`` is False even if target is remote."""
    reporter = await stack.provision("anna")
    stack.svc._users = _StubUserRepoWithInstance(
        stack.svc._users,
        {"remote-uid": "peer-a"},
    )
    # Explicitly do NOT call attach_federation.
    _, federated = await stack.svc.create_report(
        reporter_user_id=reporter.user_id,
        target_type="user",
        target_id="remote-uid",
        category="spam",
    )
    assert federated is False
