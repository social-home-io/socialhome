"""Tests for :class:`MomentPublicOutbound`.

Verifies the bus subscriber path: a local public moment fires the
signed POST per registered GFS, while non-public / GFS-received /
remote-author events are skipped.
"""

from __future__ import annotations

import pytest

from socialhome.domain.events import MomentCreated, MomentDeleted
from socialhome.domain.moment import Moment
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.moment_public_outbound import MomentPublicOutbound


class _StubResp:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _StubSession:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self.status)


@pytest.fixture
async def outbound_setup(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    # Register two GFSes so we can assert fan-out hits both.
    for gfs_id, host in (("g1", "gfs1"), ("g2", "gfs2")):
        await db.enqueue(
            "INSERT INTO gfs_connections("
            "id, gfs_instance_id, display_name, public_key, inbox_url, "
            "status, paired_at) VALUES(?,?,?,'ff'*32,?,'active', datetime('now'))",
            (gfs_id, gfs_id, gfs_id.upper(), f"https://{host}.example"),
        )
    bus = EventBus()
    moment_repo = SqliteMomentRepo(db)
    reg_repo = SqliteMomentPublicRegistrationRepo(db)
    user_repo = SqliteUserRepo(db)
    gfs_repo = SqliteGfsConnectionRepo(db)
    sub = MomentPublicOutbound(
        bus=bus,
        moment_repo=moment_repo,
        registration_repo=reg_repo,
        user_repo=user_repo,
        gfs_repo=gfs_repo,
    )
    sess = _StubSession()
    sub.attach_session(sess)
    sub.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    sub.wire()
    return {
        "bus": bus,
        "moments": moment_repo,
        "regs": reg_repo,
        "session": sess,
        "sub": sub,
    }


def _moment(
    *,
    moment_id: str = "m-1",
    is_public: bool = True,
    received_via: str = "self",
    received_via_gfs_id: str | None = None,
) -> Moment:
    return Moment(
        id=moment_id,
        author_user_id="u1",
        content="hello",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="inst-self",
        created_at="2026-05-06T12:00:00Z",
        expires_at="2026-05-07T12:00:00Z",
        is_public=is_public,
        received_via=received_via,
        received_via_gfs_id=received_via_gfs_id,
    )


def _created_event(moment_id: str = "m-1") -> MomentCreated:
    return MomentCreated(
        moment_id=moment_id,
        author_user_id="u1",
        content="hello",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        parent_author_user_id=None,
        origin_instance_id="inst-self",
        expires_at="2026-05-07T12:00:00Z",
    )


async def test_public_local_moment_fans_to_each_registered_gfs(outbound_setup):
    s = outbound_setup
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["regs"].upsert(user_id="u1", gfs_id="g2")
    await s["bus"].publish(_created_event())
    # Two POSTs, one to each GFS — each carries a signed envelope
    # ending in /gfs/moments/publish.
    assert len(s["session"].posts) == 2
    urls = {p[0] for p in s["session"].posts}
    assert any(u.endswith("/gfs/moments/publish") for u in urls)
    body = s["session"].posts[0][1]
    assert body["moment_id"] == "m-1"
    assert body["author_user_id"] == "u1"
    assert "signature" in body


async def test_non_public_moment_does_not_fan(outbound_setup):
    s = outbound_setup
    await s["moments"].save(_moment(is_public=False))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []


async def test_gfs_received_moment_does_not_re_fan(outbound_setup):
    s = outbound_setup
    # Same author, but the row is tagged as received-via-gfs.
    await s["moments"].save(_moment(received_via="gfs", received_via_gfs_id="g1"))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []


async def test_remote_author_event_is_skipped(outbound_setup):
    s = outbound_setup
    # Moment for a user that doesn't exist locally.
    await s["moments"].save(
        Moment(
            id="m-2",
            author_user_id="u-remote",
            content="hi",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="inst-remote",
            created_at="2026-05-06T12:00:00Z",
            expires_at="2026-05-07T12:00:00Z",
            is_public=True,
        )
    )
    event = MomentCreated(
        moment_id="m-2",
        author_user_id="u-remote",
        content="hi",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        parent_author_user_id=None,
        origin_instance_id="inst-remote",
        expires_at="2026-05-07T12:00:00Z",
    )
    await s["bus"].publish(event)
    assert s["session"].posts == []


async def test_no_registration_means_no_fan(outbound_setup):
    s = outbound_setup
    await s["moments"].save(_moment())
    # No regs registered.
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []


async def test_delete_event_fans_tombstone_to_each_registered_gfs(outbound_setup):
    s = outbound_setup
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["regs"].upsert(user_id="u1", gfs_id="g2")
    delete = MomentDeleted(
        moment_id="m-1",
        author_user_id="u1",
        origin_instance_id="inst-self",
    )
    await s["bus"].publish(delete)
    assert len(s["session"].posts) == 2
    assert all(p[0].endswith("/gfs/moments/delete") for p in s["session"].posts)


async def test_outbound_used_before_attach_skips_silently(db):
    """Bus event that fires before ``attach_session`` shouldn't crash."""
    bus = EventBus()
    sub = MomentPublicOutbound(
        bus=bus,
        moment_repo=SqliteMomentRepo(db),
        registration_repo=SqliteMomentPublicRegistrationRepo(db),
        user_repo=SqliteUserRepo(db),
        gfs_repo=SqliteGfsConnectionRepo(db),
    )
    sub.wire()
    # No session attached — should be a no-op even if the moment exists.
    await bus.publish(
        MomentCreated(
            moment_id="m-x",
            author_user_id="ghost",
            content="",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            parent_author_user_id=None,
            origin_instance_id="",
            expires_at="2026-05-07T12:00:00Z",
        )
    )


class _ProtectedCp:
    """Stub ChildProtectionService: every listed user is protected."""

    def __init__(self, protected: set[str]) -> None:
        self._protected = protected

    def register_gate(self, gate):
        pass

    async def is_restricted(self, user_id, capability):
        return user_id in self._protected


async def test_protected_author_moment_is_not_published(outbound_setup):
    """§CP.R at use time: a registration made before the account was
    protected no longer publishes its moments to the connection server."""
    s = outbound_setup
    s["sub"].attach_child_protection(_ProtectedCp({"u1"}))
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []


async def test_protected_author_can_still_retract_a_moment(outbound_setup):
    """Deletes keep flowing so earlier public moments can be withdrawn."""
    s = outbound_setup
    s["sub"].attach_child_protection(_ProtectedCp({"u1"}))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(
        MomentDeleted(
            moment_id="m-1", author_user_id="u1", origin_instance_id="inst-self"
        )
    )
    assert len(s["session"].posts) == 1
    assert s["session"].posts[0][0].endswith("/gfs/moments/delete")


async def test_unprotected_author_still_publishes_with_cp_wired(outbound_setup):
    s = outbound_setup
    s["sub"].attach_child_protection(_ProtectedCp(set()))
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert len(s["session"].posts) == 1
