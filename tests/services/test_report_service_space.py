"""Space-scoped reports: :class:`ReportService` routes a report about
content inside a space to that space's content authority (owner / admin /
moderator), never to household admins, and federates it only to the host
and the reviewer households.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import ReportFiled, ReportResolved
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import (
    ModerationAlreadyDecidedError,
    SpacePermissionError,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.page_repo import SqlitePageRepo
from socialhome.repositories.post_repo import SqlitePostRepo
from socialhome.repositories.report_repo import SqliteReportRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.report_scope import ReportScope
from socialhome.services.report_service import ReportService
from socialhome.services.user_service import UserService

OWN = "inst-self"


class _Fed:
    def __init__(self):
        self.sent: list[dict] = []
        #: Households below v_45 (``peer_supports`` says no).
        self.old: set[str] = set()

    async def peer_supports(self, instance_id, *, min_version):
        return instance_id not in self.old

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append({"to": to_instance_id, "type": event_type, "space": space_id})
        return SimpleNamespace(ok=True, error=None)

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
                "space": space_id,
            }
        )
        return SimpleNamespace(ok=to_instance_id != "inst-down", error="x")


@pytest.fixture
async def env(tmp_dir):
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
    users = SqliteUserRepo(db)
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)
    spaces = SqliteSpaceRepo(db)
    posts = SqliteSpacePostRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    svc = ReportService(
        report_repo=SqliteReportRepo(db),
        user_repo=users,
        bus=bus,
        space_repo=spaces,
        space_post_repo=posts,
        remote_member_repo=seats,
        scope=ReportScope(
            space_post_repo=posts,
            post_repo=SqlitePostRepo(db),
            page_repo=SqlitePageRepo(db),
        ),
    )
    svc.attach_federation(_Fed(), OWN)  # type: ignore[arg-type]

    e = SimpleNamespace(db=db, bus=bus, svc=svc, fed=svc._federation)

    async def person(name, *, admin=False):
        u = await user_svc.provision(username=name, display_name=name, is_admin=admin)
        return u.user_id

    e.person = person
    e.hadmin = await person("hadmin", admin=True)  # household admin, no seat
    e.owner = await person("owner")
    e.mod = await person("mod")
    e.member = await person("member")
    e.other = await person("other")  # member of space B only

    async def space(sid, *, host=OWN, space_type="private"):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key, space_type) VALUES(?,?,?,?,?,?)",
            (sid, f"Space {sid}", host, "owner", "ab" * 32, space_type),
        )

    async def seat(sid, uid, role):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            (sid, uid, role),
        )

    async def remote(sid, inst, uid, role="member", tomb=0):
        await db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
            " display_name, role, tombstoned) VALUES(?,?,?,?,?,?)",
            (sid, inst, uid, f"Remote {uid}", role, tomb),
        )

    async def post(sid, pid, author):
        await db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES(?,?,?,'text','hi')",
            (pid, sid, author),
        )

    e.space, e.seat, e.remote, e.post = space, seat, remote, post
    await space("spA")
    await space("spB")
    await seat("spA", e.owner, "owner")
    await seat("spA", e.mod, "moderator")
    await seat("spA", e.member, "member")
    await seat("spB", e.other, "member")
    await post("spA", "pA", e.member)
    await post("spB", "pB", e.other)
    yield e
    await db.shutdown()


# ── Filing ───────────────────────────────────────────────────────────────


async def test_space_post_report_is_space_scoped_and_hidden_from_household(env):
    fired: list[ReportFiled] = []

    async def _on(ev: ReportFiled) -> None:
        fired.append(ev)

    env.bus.subscribe(ReportFiled, _on)
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pA",
        category="spam",
    )
    assert report.space_id == "spA"
    assert fired[0].space_id == "spA"
    # Household admins only see household-level reports.
    assert await env.svc.list_pending(actor_username="hadmin") == []
    listed = await env.svc.list_for_space("spA", actor_user_id=env.mod)
    assert [r.id for r in listed] == [report.id]


async def test_feed_post_report_stays_household_level(env):
    await env.db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES(?,?,'text','hi')",
        ("feed-post", env.owner),
    )
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="feed-post",
        category="spam",
    )
    assert report.space_id is None
    assert [r.id for r in await env.svc.list_pending(actor_username="hadmin")] == [
        report.id
    ]


async def test_named_space_must_match_the_content(env):
    with pytest.raises(KeyError):
        await env.svc.create_report(
            reporter_user_id=env.member,
            target_type="post",
            target_id="pB",
            category="spam",
            space_id="spA",
        )


async def test_reporter_outside_the_space_gets_not_found(env):
    with pytest.raises(KeyError):
        await env.svc.create_report(
            reporter_user_id=env.other,
            target_type="post",
            target_id="pA",
            category="spam",
        )


async def test_member_report_needs_named_space_and_seated_target(env):
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id=env.mod,
        category="harassment",
        space_id="spA",
    )
    assert report.space_id == "spA"
    with pytest.raises(KeyError):
        await env.svc.create_report(
            reporter_user_id=env.member,
            target_type="user",
            target_id=env.other,  # not in spA
            category="harassment",
            space_id="spA",
        )


async def test_remote_member_can_be_reported(env):
    await env.remote("spA", "inst-peer", "remote-u")
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id="remote-u",
        category="spam",
        space_id="spA",
    )
    assert report.space_id == "spA"


async def test_same_member_reported_once_per_space(env):
    await env.seat("spB", env.member, "member")
    await env.seat("spB", env.mod, "member")
    for sid in ("spA", "spB"):
        await env.svc.create_report(
            reporter_user_id=env.member,
            target_type="user",
            target_id=env.mod,
            category="spam",
            space_id=sid,
        )


async def test_space_on_unscoped_target_is_refused(env):
    with pytest.raises(ValueError):
        await env.svc.create_report(
            reporter_user_id=env.member,
            target_type="moment",
            target_id="m1",
            category="spam",
            space_id="spA",
        )


async def test_unknown_space_only_target_is_not_found(env):
    with pytest.raises(KeyError):
        await env.svc.create_report(
            reporter_user_id=env.member,
            target_type="page",
            target_id="nope",
            category="spam",
        )


# ── Triage ──────────────────────────────────────────────────────────────


async def test_only_content_authority_lists_and_resolves(env):
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pA",
        category="spam",
    )
    for uid in (env.member, env.hadmin, env.other):
        with pytest.raises(SpacePermissionError):
            await env.svc.list_for_space("spA", actor_user_id=uid)
        with pytest.raises(SpacePermissionError):
            await env.svc.resolve_in_space("spA", report.id, actor_user_id=uid)
    # A household admin cannot reach it through the household endpoint.
    with pytest.raises(KeyError):
        await env.svc.resolve(report.id, actor_username="hadmin")

    done: list[ReportResolved] = []

    async def _on(ev: ReportResolved) -> None:
        done.append(ev)

    env.bus.subscribe(ReportResolved, _on)
    await env.svc.resolve_in_space(
        "spA", report.id, actor_user_id=env.mod, dismissed=True
    )
    assert done[0].space_id == "spA"
    assert await env.svc.list_for_space("spA", actor_user_id=env.owner) == []
    with pytest.raises(ModerationAlreadyDecidedError):
        await env.svc.resolve_in_space("spA", report.id, actor_user_id=env.owner)


async def test_cross_space_report_id_is_not_found(env):
    await env.seat("spB", env.mod, "moderator")
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pA",
        category="spam",
    )
    with pytest.raises(KeyError):
        await env.svc.resolve_in_space("spB", report.id, actor_user_id=env.mod)


async def test_unknown_space_is_not_found(env):
    with pytest.raises(KeyError):
        await env.svc.list_for_space("nope", actor_user_id=env.mod)


async def test_display_names_cover_local_and_remote(env):
    await env.remote("spA", "inst-peer", "remote-u")
    names = await env.svc.display_names("spA", {env.member, "remote-u", "ghost", ""})
    assert names == {env.member: "member", "remote-u": "Remote remote-u"}


# ── Federation outbound ─────────────────────────────────────────────────


async def test_stub_report_goes_to_host_and_reviewer_households_only(env):
    await env.space("spR", host="inst-host")
    await env.seat("spR", env.member, "member")
    await env.post("spR", "pR", env.member)
    await env.remote("spR", "inst-mod", "rmod", role="moderator")
    await env.remote("spR", "inst-adm", "radm", role="admin")
    await env.remote("spR", "inst-plain", "rplain", role="member")
    await env.remote("spR", "inst-gone", "rgone", role="moderator", tomb=1)
    report, federated = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pR",
        category="spam",
        notes="n",
    )
    assert federated is True
    sent = env.fed.sent
    assert [s["to"] for s in sent] == ["inst-host", "inst-adm", "inst-mod"]
    assert all(s["type"] is FederationEventType.SPACE_REPORT for s in sent)
    assert all(s["space"] == "spR" for s in sent)
    assert sent[0]["payload"]["space_id"] == "spR"


async def test_undelivered_reviewer_send_is_not_counted(env):
    await env.space("spD", host="inst-down")
    await env.seat("spD", env.member, "member")
    await env.post("spD", "pD", env.member)
    _, federated = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pD",
        category="spam",
    )
    assert federated is False


async def test_hosted_report_with_no_remote_reviewers_stays_local(env):
    await env.remote("spA", "inst-plain", "rplain", role="member")
    _, federated = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pA",
        category="spam",
    )
    assert federated is False
    assert env.fed.sent == []


# ── Federation inbound ──────────────────────────────────────────────────


async def _remote_report(env, **kw):
    args = {
        "reporter_user_id": "rrep",
        "reporter_instance_id": "inst-peer",
        "target_type": "post",
        "target_id": "pA",
        "category": "spam",
    }
    args.update(kw)
    return await env.svc.create_report_from_remote(**args)


async def test_inbound_report_from_seated_member_lands_in_space(env):
    await env.remote("spA", "inst-peer", "rrep")
    got = await _remote_report(env, space_id="spA")
    assert got is not None and got.space_id == "spA"
    # A household-level report needs the reporter bound to the sender.
    assert await _remote_report(env, target_type="user", target_id=env.member) is None
    await env.db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "inst-peer",
            "Peer",
            "ab" * 32,
            "00",
            "00",
            "https://p/x",
            "p",
            "confirmed",
            "manual",
        ),
    )
    await env.db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username, display_name)"
        " VALUES(?,?,?,?)",
        ("rrep", "inst-peer", "rrep", "R"),
    )
    got2 = await _remote_report(env, target_type="user", target_id=env.member)
    assert got2 is not None and got2.space_id is None


async def test_inbound_legacy_post_report_is_scoped_from_target(env):
    await env.remote("spA", "inst-peer", "rrep")
    got = await _remote_report(env)
    assert got is not None and got.space_id == "spA"


async def test_inbound_report_from_unseated_reporter_is_dropped(env):
    await env.remote("spA", "inst-other", "rrep")  # seated, but elsewhere
    assert await _remote_report(env, space_id="spA") is None


async def test_inbound_report_from_banned_reporter_is_dropped(env):
    await env.remote("spA", "inst-peer", "rrep")
    await env.db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?,?,?)",
        ("spA", "rrep", env.owner),
    )
    assert await _remote_report(env, space_id="spA") is None


async def test_inbound_cross_space_report_is_dropped(env):
    await env.remote("spA", "inst-peer", "rrep")
    assert await _remote_report(env, target_id="pB", space_id="spA") is None


async def test_inbound_report_not_ours_to_review_is_dropped(env):
    # Hosted elsewhere, and no local content authority — a plain member
    # household must not hold the report.
    await env.space("spX", host="inst-host")
    await env.seat("spX", env.member, "member")
    await env.post("spX", "pX", env.member)
    await env.remote("spX", "inst-peer", "rrep")
    assert await _remote_report(env, target_id="pX") is None
    # With a local moderator it IS ours.
    await env.seat("spX", env.mod, "moderator")
    got = await _remote_report(env, target_id="pX")
    assert got is not None and got.space_id == "spX"


async def test_inbound_member_report_needs_seated_target(env):
    await env.remote("spA", "inst-peer", "rrep")
    assert (
        await _remote_report(
            env, target_type="user", target_id=env.other, space_id="spA"
        )
        is None
    )
    got = await _remote_report(
        env, target_type="user", target_id=env.member, space_id="spA"
    )
    assert got is not None and got.space_id == "spA"


async def test_inbound_unknown_space_or_target_is_dropped(env):
    assert await _remote_report(env, target_type="page", target_id="x") is None
    assert (
        await _remote_report(env, target_type="user", target_id="u", space_id="zz")
        is None
    )
    assert await _remote_report(env, target_type="space", space_id="spA") is None
    assert await _remote_report(env, target_id="unknown-post", space_id="spA") is None


# ── GFS forward ─────────────────────────────────────────────────────────


class _Gfs:
    def __init__(self):
        self.forwarded: list[dict] = []

    async def list_connections(self):
        return [SimpleNamespace(id="g", status="active")]

    async def report_fraud(self, gfs_id, **kw):
        self.forwarded.append(kw)


async def test_gfs_forward_only_for_public_space_content(env):
    import asyncio

    gfs = _Gfs()
    env.svc.attach_gfs(gfs, signing_key=b"\x00" * 32)
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pA",
        category="spam",
        forward_gfs=False,
    )
    assert await env.svc.forward_to_gfs(report) is False
    assert gfs.forwarded == []  # private space: the GFS never hears of it

    await env.space("spP", space_type="public")
    await env.seat("spP", env.member, "member")
    await env.post("spP", "pP", env.member)
    report2, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pP",
        category="spam",
        notes="the reporter's words",
        forward_gfs=False,
    )
    assert await env.svc.forward_to_gfs(report2) is True
    await asyncio.sleep(0.01)
    sent = gfs.forwarded[0]
    assert sent["target_type"] == "space"
    assert sent["target_id"] == "spP"
    # Fraud triage needs neither who reported nor what they wrote.
    assert sent["reporter_user_id"] is None
    assert sent["notes"] is None


async def test_gfs_forward_needs_an_active_connection(env):
    class _Pending(_Gfs):
        async def list_connections(self):
            return [SimpleNamespace(id="g", status="pending")]

    env.svc.attach_gfs(_Pending(), signing_key=b"\x00" * 32)
    await env.space("spP", space_type="public")
    await env.seat("spP", env.member, "member")
    await env.post("spP", "pP", env.member)
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pP",
        category="spam",
        forward_gfs=False,
    )
    assert await env.svc.forward_to_gfs(report) is False


async def test_private_space_report_is_not_forwarded(env):
    gfs = _Gfs()
    env.svc.attach_gfs(gfs, signing_key=b"\x00" * 32)
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="space",
        target_id="spA",
        category="spam",
        forward_gfs=False,
    )
    assert await env.svc.forward_to_gfs(report) is False


# ── Review follow-ups (probe9) ──────────────────────────────────────────


class _NoBans:
    async def is_banned(self, iid):
        return False


async def test_sole_owner_dismisses_a_report_about_themself_anonymously(env):
    """I1 — the owner is the space's only content authority: they see the
    report about themself without the reporter, may dismiss it, not
    resolve it — and the space report never gates their household relay."""
    from socialhome.repositories.report_repo import SqliteReportRepo
    from socialhome.services.relay_policy import RelayPolicy

    await env.space("spC")
    await env.seat("spC", env.owner, "owner")
    await env.seat("spC", env.member, "member")
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id=env.owner,
        category="harassment",
        space_id="spC",
    )
    views = await env.svc.review_space("spC", actor_user_id=env.owner)
    assert [v.report.id for v in views] == [report.id]
    assert views[0].anonymous and views[0].dismiss_only
    with pytest.raises(SpacePermissionError):
        await env.svc.resolve_in_space("spC", report.id, actor_user_id=env.owner)
    await env.svc.resolve_in_space(
        "spC", report.id, actor_user_id=env.owner, dismissed=True
    )
    policy = RelayPolicy(ban_repo=_NoBans(), report_repo=SqliteReportRepo(env.db))
    assert await policy.allow_relay(source_instance_id="x", author_user_id=env.owner)


async def test_owner_with_a_remote_moderator_does_not_see_a_report_on_themself(env):
    await env.space("spC")
    await env.seat("spC", env.owner, "owner")
    await env.seat("spC", env.member, "member")
    await env.remote("spC", "inst-mod", "rmod", role="moderator")
    await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id=env.owner,
        category="harassment",
        space_id="spC",
    )
    assert await env.svc.review_space("spC", actor_user_id=env.owner) == []


async def test_owner_triages_a_report_about_a_moderator(env):
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id=env.mod,
        category="harassment",
        space_id="spA",
    )
    views = await env.svc.review_space("spA", actor_user_id=env.owner)
    assert [(v.report.id, v.anonymous) for v in views] == [(report.id, False)]


async def test_author_never_triages_a_report_on_their_own_content(env):
    """I2 — the item's author is the subject, like a reported member."""
    await env.post("spA", "pMod", env.mod)
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pMod",
        category="spam",
        notes="mod is spamming",
    )
    assert await env.svc.list_for_space("spA", actor_user_id=env.mod) == []
    with pytest.raises(KeyError):
        await env.svc.resolve_in_space(
            "spA", report.id, actor_user_id=env.mod, dismissed=True
        )
    assert [
        r.id for r in await env.svc.list_for_space("spA", actor_user_id=env.owner)
    ] == [report.id]


async def test_report_filed_names_the_subject(env):
    fired: list[ReportFiled] = []

    async def _on(ev: ReportFiled) -> None:
        fired.append(ev)

    env.bus.subscribe(ReportFiled, _on)
    await env.post("spA", "pMod", env.mod)
    await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pMod",
        category="spam",
    )
    assert fired[0].subject_user_id == env.mod


async def test_inbound_flood_is_capped(env, monkeypatch):
    """M1 — per (sender household, reporter), per household and per space;
    notes are cut at 1000 characters."""
    from socialhome.services import report_service as rs

    monkeypatch.setattr(rs, "MAX_PENDING_PER_REPORTER", 3)
    monkeypatch.setattr(rs, "MAX_PENDING_PER_HOUSEHOLD", 5)
    monkeypatch.setattr(rs, "MAX_PENDING_PER_SPACE", 7)
    await env.remote("spA", "inst-evil", "evil-1")
    await env.remote("spA", "inst-evil", "evil-2")
    await env.remote("spA", "inst-other", "other-1")
    stored = []
    for i in range(5):
        await env.post("spA", f"px{i}", env.member)
        stored.append(
            await env.svc.create_report_from_remote(
                reporter_user_id="evil-1",
                reporter_instance_id="inst-evil",
                target_type="post",
                target_id=f"px{i}",
                category="spam",
                notes="x" * 100000,
            )
        )
    assert sum(r is not None for r in stored) == 3
    assert stored[0] is not None and len(stored[0].notes or "") == 1000
    for i in range(5):
        await env.svc.create_report_from_remote(
            reporter_user_id="evil-2",
            reporter_instance_id="inst-evil",
            target_type="post",
            target_id=f"px{i}",
            category="spam",
        )
    assert (
        await env.svc._reports.count_pending_in_space(
            "spA", reporter_instance_id="inst-evil"
        )
        == 5
    )
    for i in range(5):
        await env.svc.create_report_from_remote(
            reporter_user_id="other-1",
            reporter_instance_id="inst-other",
            target_type="post",
            target_id=f"px{i}",
            category="spam",
        )
    assert await env.svc._reports.count_pending_in_space("spA") == 7


async def test_local_filing_is_capped_per_space(env, monkeypatch):
    from socialhome.domain.report import ReportRateLimitedError
    from socialhome.services import report_service as rs

    monkeypatch.setattr(rs, "MAX_PENDING_PER_REPORTER", 2)
    for i in range(3):
        await env.post("spA", f"pl{i}", env.owner)
    for i in range(2):
        await env.svc.create_report(
            reporter_user_id=env.member,
            target_type="post",
            target_id=f"pl{i}",
            category="spam",
        )
    with pytest.raises(ReportRateLimitedError):
        await env.svc.create_report(
            reporter_user_id=env.member,
            target_type="post",
            target_id="pl2",
            category="spam",
        )


async def test_non_member_gets_the_same_answer_as_for_an_unknown_id(env):
    """M2 — no existence oracle: a real post in a space the reporter is not
    in and an id that does not exist read the same, and neither is stored."""
    errs = []
    for pid in ("pA", "does-not-exist"):
        with pytest.raises(KeyError) as exc:
            await env.svc.create_report(
                reporter_user_id=env.other,
                target_type="post",
                target_id=pid,
                category="spam",
            )
        errs.append(str(exc.value))
    assert errs[0] == errs[1]
    rows = await env.db.fetchall("SELECT 1 FROM content_reports", ())
    assert rows == []


async def test_a_report_on_content_moved_to_another_space_reads_gone(env):
    """M3 — a preview whose scope is no longer the report's space is gone."""
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pA", category="spam"
    )
    await env.db.enqueue("UPDATE space_posts SET space_id='spB' WHERE id='pA'")
    views = await env.svc.review_space("spA", actor_user_id=env.owner)
    assert views[0].report.id == report.id
    assert views[0].gone is True and views[0].preview is None


async def test_reports_go_only_to_v45_reviewers(env):
    """I3 — a reviewer household below v_45 would file the report for its
    household admins: it is never sent one."""
    await env.space("spR", host="inst-host")
    await env.seat("spR", env.member, "member")
    await env.post("spR", "pR", env.member)
    await env.remote("spR", "inst-mod", "rmod", role="moderator")
    await env.remote("spR", "inst-old", "rold", role="moderator")
    env.fed.old.add("inst-old")
    await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pR", category="spam"
    )
    assert [s["to"] for s in env.fed.sent] == ["inst-host", "inst-mod"]


async def test_a_resolve_is_synced_to_the_other_v45_reviewers(env):
    await env.remote("spA", "inst-mod", "rmod", role="moderator")
    await env.remote("spA", "inst-old", "rold", role="moderator")
    env.fed.old.add("inst-old")
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pA", category="spam"
    )
    env.fed.sent.clear()
    await env.svc.resolve_in_space("spA", report.id, actor_user_id=env.mod)
    assert [(s["to"], s["type"]) for s in env.fed.sent] == [
        ("inst-mod", FederationEventType.SPACE_REPORT_DECIDED)
    ]
    p = env.fed.sent[0]["payload"]
    assert p == {
        "space_id": "spA",
        "target_type": "post",
        "target_id": "pA",
        "reporter_user_id": env.member,
        "decision": "resolved",
        "decided_by": env.mod,
        "decided_at": p["decided_at"],
    }


async def _decide(env, **kw):
    args = {
        "space_id": "spA",
        "target_type": "post",
        "target_id": "pA",
        "reporter_user_id": env.member,
        "decision": "dismissed",
        "decided_by": "rmod",
    }
    args.update(kw)
    return await env.svc.apply_remote_decision(**args)


async def test_a_remote_decision_applies_once(env):
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pA", category="spam"
    )
    assert await _decide(env) is True
    got = await env.svc._reports.get(report.id)
    assert got is not None and got.status.value == "dismissed"
    assert got.resolved_by == "rmod"
    # First decision wins; a replay / a later contrary one changes nothing.
    assert await _decide(env) is False
    assert await _decide(env, decision="resolved") is False


async def test_a_remote_decision_by_the_subject_or_malformed_is_refused(env):
    await env.post("spA", "pR2", "rmod")
    await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="post",
        target_id="pR2",
        category="spam",
    )
    assert await _decide(env, target_id="pR2") is False
    assert await _decide(env, target_id="pR2", decision="pending") is False
    assert await _decide(env, target_id="pR2", decision="bogus") is False
    assert await _decide(env, target_type="bogus") is False
    assert await _decide(env, target_id="unknown") is False


async def test_reports_are_purged_when_this_household_stops_reviewing(env):
    """M4 — a stub that loses its last content-authority seat drops the
    reports other households filed there; a dissolved space drops all."""
    from socialhome.domain.events import RemoteSpaceDissolved, SpaceMemberLeft

    env.svc.watch_seats(env.bus)
    await env.space("spS", host="inst-host")
    await env.seat("spS", env.mod, "moderator")
    await env.seat("spS", env.member, "member")
    await env.post("spS", "pS", env.member)
    await env.remote("spS", "inst-peer", "rrep")
    await env.svc.create_report_from_remote(
        reporter_user_id="rrep",
        reporter_instance_id="inst-peer",
        target_type="post",
        target_id="pS",
        category="spam",
    )
    await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pS", category="spam"
    )
    assert await env.svc._reports.count_pending_in_space("spS") == 2
    # Still reviewing: nothing goes.
    await env.bus.publish(SpaceMemberLeft(space_id="spS", user_id="x"))
    assert await env.svc._reports.count_pending_in_space("spS") == 2
    await env.db.enqueue(
        "UPDATE space_members SET role='member' WHERE space_id='spS' AND user_id=?",
        (env.mod,),
    )
    await env.bus.publish(SpaceMemberLeft(space_id="spS", user_id=env.mod))
    assert await env.svc._reports.count_pending_in_space("spS") == 1  # our own
    await env.db.enqueue("UPDATE spaces SET dissolved=1 WHERE id='spS'")
    await env.bus.publish(RemoteSpaceDissolved(space_id="spS"))
    assert await env.svc._reports.count_pending_in_space("spS") == 0
    # An unknown space: nothing to do.
    await env.bus.publish(RemoteSpaceDissolved(space_id="nope"))


# ── Re-review (N1–N3) ───────────────────────────────────────────────────


async def test_anonymous_row_carries_no_notes(env):
    """N1 — the subject must not learn who reported them from their words."""
    await env.space("spC")
    await env.seat("spC", env.owner, "owner")
    await env.seat("spC", env.member, "member")
    await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id=env.owner,
        category="harassment",
        notes="I'm the one you were rude to on Tuesday",
        space_id="spC",
    )
    (view,) = await env.svc.review_space("spC", actor_user_id=env.owner)
    assert view.anonymous
    assert view.report.notes is None
    assert view.report.reporter_user_id == ""
    assert view.report.reporter_instance_id is None


async def test_fallback_is_pinned_at_filing(env):
    """N2 — an owner who had a moderator when the report was filed never
    gets the anonymous fallback, even after demoting everyone."""
    from socialhome.domain.events import SpaceMemberLeft

    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id=env.owner,
        category="harassment",
        space_id="spA",  # spA has env.mod, a moderator
    )
    assert report.sole_reviewer_user_id is None
    await env.db.enqueue(
        "UPDATE space_members SET role='member' WHERE space_id='spA' AND user_id=?",
        (env.mod,),
    )
    await env.bus.publish(SpaceMemberLeft(space_id="spA", user_id="x"))
    assert await env.svc.review_space("spA", actor_user_id=env.owner) == []
    with pytest.raises(KeyError):
        await env.svc.resolve_in_space(
            "spA", report.id, actor_user_id=env.owner, dismissed=True
        )


async def test_fallback_needs_sole_authority_still(env):
    """Pinned at filing, and still true now: a moderator promoted later
    takes the report over."""
    await env.space("spC")
    await env.seat("spC", env.owner, "owner")
    await env.seat("spC", env.member, "member")
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id=env.owner,
        category="harassment",
        space_id="spC",
    )
    assert report.sole_reviewer_user_id == env.owner
    await env.seat("spC", env.mod, "moderator")
    assert await env.svc.review_space("spC", actor_user_id=env.owner) == []
    assert [
        v.report.id for v in await env.svc.review_space("spC", actor_user_id=env.mod)
    ] == [report.id]


async def test_host_redelivers_pending_reports_to_a_new_reviewer(env):
    """N3 — a household that regains an admin / moderator seat gets the
    space's pending reports again (v_45 only), on the SPACE_REPORT path."""
    from socialhome.domain.events import SpaceConfigChanged

    env.svc.watch_seats(env.bus)
    await env.remote("spA", "inst-back", "rback", role="member")
    report, _ = await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pA", category="spam"
    )
    env.fed.sent.clear()
    await env.db.enqueue(
        "UPDATE space_remote_members SET role='moderator' WHERE user_id='rback'"
    )
    await env.bus.publish(
        SpaceConfigChanged(
            space_id="spA",
            event_type="space_member_role_changed",
            payload={
                "user_id": "rback",
                "instance_id": "inst-back",
                "role": "moderator",
            },
            sequence=0,
        )
    )
    assert [(s["to"], s["type"]) for s in env.fed.sent] == [
        ("inst-back", FederationEventType.SPACE_REPORT)
    ]
    assert env.fed.sent[0]["payload"]["target_id"] == "pA"
    # A demotion, an older household, a non-host or ourselves: nothing.
    env.fed.sent.clear()
    for payload in (
        {"user_id": "rback", "instance_id": "inst-back", "role": "member"},
        {"user_id": "x", "instance_id": OWN, "role": "admin"},
    ):
        await env.bus.publish(
            SpaceConfigChanged(
                space_id="spA", event_type="x", payload=payload, sequence=0
            )
        )
    env.fed.old.add("inst-old")
    assert await env.svc.resend_pending("spA", "inst-old") == 0
    await env.space("spR", host="inst-host")
    assert await env.svc.resend_pending("spR", "inst-back") == 0
    assert env.fed.sent == []
    assert report.id


async def test_a_host_redelivered_report_is_accepted(env):
    """The receiver of a re-delivery accepts the host relaying a report by
    a member of another household — seated on the household the host names."""
    await env.space("spR", host="inst-host")
    await env.seat("spR", env.mod, "moderator")
    await env.post("spR", "pR", env.member)
    await env.remote("spR", "inst-a", "ra")
    got = await env.svc.create_report_from_remote(
        reporter_user_id="ra",
        reporter_instance_id="inst-host",
        origin_instance_id="inst-a",
        target_type="post",
        target_id="pR",
        category="spam",
        space_id="spR",
    )
    assert got is not None and got.space_id == "spR"
    # Stored, and capped, under the reporter's own household.
    assert got.reporter_instance_id == "inst-a"
    # The host naming the wrong household: no seat there, refused.
    await env.remote("spR", "inst-b", "rb")
    assert (
        await env.svc.create_report_from_remote(
            reporter_user_id="rb",
            reporter_instance_id="inst-host",
            origin_instance_id="inst-a",
            target_type="post",
            target_id="pR",
            category="spam",
            space_id="spR",
        )
        is None
    )
    # A non-host cannot relay someone else's member's report — a claimed
    # origin from it is ignored.
    assert (
        await env.svc.create_report_from_remote(
            reporter_user_id="rb",
            reporter_instance_id="inst-a",
            origin_instance_id="inst-b",
            target_type="post",
            target_id="pR",
            category="spam",
            space_id="spR",
        )
        is None
    )


async def test_household_cap_keys_on_the_origin_of_host_relayed_reports(
    env, monkeypatch
):
    """Minor 2 — the host relays reports of several households: each is
    capped under its own household, never all under the host."""
    from socialhome.services import report_service as rs

    monkeypatch.setattr(rs, "MAX_PENDING_PER_HOUSEHOLD", 2)
    await env.space("spR", host="inst-host")
    await env.seat("spR", env.mod, "moderator")
    for i in range(3):
        await env.post("spR", f"pr{i}", env.member)
    for inst, user in (("inst-a", "ra1"), ("inst-a", "ra2"), ("inst-b", "rb1")):
        await env.remote("spR", inst, user)

    async def relay(user, origin, pid):
        return await env.svc.create_report_from_remote(
            reporter_user_id=user,
            reporter_instance_id="inst-host",
            origin_instance_id=origin,
            target_type="post",
            target_id=pid,
            category="spam",
            space_id="spR",
        )

    assert await relay("ra1", "inst-a", "pr0") is not None
    assert await relay("ra2", "inst-a", "pr1") is not None
    assert await relay("ra1", "inst-a", "pr2") is None  # inst-a is full
    # inst-b's member is not counted against inst-a (or the host).
    assert await relay("rb1", "inst-b", "pr0") is not None


async def test_a_report_payload_names_the_reporters_household(env):
    await env.space("spR", host="inst-host")
    await env.seat("spR", env.member, "member")
    await env.post("spR", "pR", env.owner)
    await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pR", category="spam"
    )
    assert env.fed.sent[0]["payload"]["reporter_instance_id"] == OWN


async def test_a_report_never_lands_where_only_its_subject_reviews(env):
    """Minor 1 — a household whose only reviewer seat is the report's
    subject is skipped, on the first fan-out and on re-delivery."""
    await env.remote("spA", "inst-x", "rx", role="moderator")
    await env.remote("spA", "inst-y", "ry", role="moderator")
    await env.remote("spA", "inst-y", "ry2", role="admin")
    await env.post("spA", "pX", "rx")
    await env.post("spA", "pY", "ry")
    env.fed.sent.clear()
    await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pX", category="spam"
    )
    # inst-x reviews only through rx (the author): not sent.
    assert [s["to"] for s in env.fed.sent] == ["inst-y"]
    env.fed.sent.clear()
    await env.svc.create_report(
        reporter_user_id=env.member, target_type="post", target_id="pY", category="spam"
    )
    # inst-y has another reviewer besides ry: still sent.
    assert sorted(s["to"] for s in env.fed.sent) == ["inst-x", "inst-y"]
    env.fed.sent.clear()
    # A member report about rx likewise.
    await env.svc.create_report(
        reporter_user_id=env.member,
        target_type="user",
        target_id="rx",
        category="spam",
        space_id="spA",
    )
    assert [s["to"] for s in env.fed.sent] == ["inst-y"]
    env.fed.sent.clear()
    # Re-delivery to inst-x skips the two reports about rx, sends the other.
    assert await env.svc.resend_pending("spA", "inst-x") == 1
    assert [s["payload"]["target_id"] for s in env.fed.sent] == ["pY"]


async def test_host_owners_own_post_report_is_stored_but_not_theirs_to_triage(env):
    """The federation-demo ``space-report`` condition: a member on another
    household reports the host OWNER's post while a remote moderator
    exists. The host stores it (for the record and the moderator's
    decision sync) but the owner — its subject — never lists it, and has
    no anonymous fallback (other authority existed at filing)."""
    await env.post("spA", "pOwn", env.owner)
    await env.db.enqueue(
        "UPDATE space_members SET role='member' WHERE space_id='spA' AND user_id=?",
        (env.mod,),
    )
    await env.remote("spA", "inst-mod", "rmod", role="moderator")
    await env.remote("spA", "inst-c", "rcarol")
    got = await env.svc.create_report_from_remote(
        reporter_user_id="rcarol",
        reporter_instance_id="inst-c",
        target_type="post",
        target_id="pOwn",
        category="spam",
        space_id="spA",
    )
    assert got is not None and got.sole_reviewer_user_id is None
    assert await env.svc.review_space("spA", actor_user_id=env.owner) == []
