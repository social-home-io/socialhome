"""Household spaces seat everyone in the home automatically.

``space_type = household`` means "everyone in your home is a member":
every active local user of the household that HOSTS the space holds a
seat — on create, on a tier change into household, and when a new local
user is provisioned. Banned users, protected minors below the space's age
gate and existing seats (any role) are left alone; a user who leaves stays
out. ``join_mode`` is meaningless for the household tier: the server
stores ``open``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpaceMemberJoined, UserProvisioned
from socialhome.domain.space import JoinMode, Space, SpaceRole, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.cp_repo import SqliteCpRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.child_protection_service import ChildProtectionService
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


class _Env:
    db: AsyncDatabase
    bus: EventBus
    users: UserService
    spaces: SpaceService
    space_repo: SqliteSpaceRepo
    cp: ChildProtectionService
    iid: str
    joined: list[SpaceMemberJoined]


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
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0d" * 32))
    e = _Env()
    e.db = db
    e.bus = bus
    e.iid = iid
    e.space_repo = space_repo
    e.users = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    e.spaces = SpaceService(
        space_repo, SqliteSpacePostRepo(db), user_repo, bus, own_instance_id=iid
    )
    e.cp = ChildProtectionService(SqliteCpRepo(db), user_repo, bus)
    e.spaces.attach_child_protection(e.cp)
    e.joined = []

    async def _capture(event: SpaceMemberJoined) -> None:
        e.joined.append(event)

    bus.subscribe(SpaceMemberJoined, _capture)
    yield e
    await db.shutdown()


async def _provision(env, *names: str, admin: str | None = None) -> dict:
    out = {}
    for name in names:
        out[name] = await env.users.provision(
            username=name, display_name=name.title(), is_admin=name == admin
        )
    return out


async def _roles(env, space_id: str) -> dict[str, str]:
    return {m.user_id: m.role for m in await env.space_repo.list_members(space_id)}


# ─── create ──────────────────────────────────────────────────────────────


async def test_create_household_seats_every_active_local_user(env):
    u = await _provision(env, "anna", "bob", "carl", "dora", admin="anna")
    await env.users.deprovision("dora")
    space = await env.spaces.create_space(
        owner_username="anna",
        name="Home",
        space_type=SpaceType.HOUSEHOLD,
        join_mode=JoinMode.INVITE_ONLY,
    )
    roles = await _roles(env, space.id)
    assert roles == {
        u["anna"].user_id: SpaceRole.OWNER,
        u["bob"].user_id: SpaceRole.MEMBER,
        u["carl"].user_id: SpaceRole.MEMBER,
    }
    assert space.join_mode is JoinMode.OPEN
    stored = await env.space_repo.get(space.id)
    assert stored is not None and stored.join_mode is JoinMode.OPEN


async def test_create_household_publishes_automatic_joins_not_for_owner(env):
    u = await _provision(env, "anna", "bob", "carl", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    joined = [ev for ev in env.joined if ev.space_id == space.id]
    assert {ev.user_id for ev in joined} == {u["bob"].user_id, u["carl"].user_id}
    assert all(ev.automatic for ev in joined)
    assert all(ev.role == SpaceRole.MEMBER for ev in joined)


async def test_create_private_space_seats_only_the_owner(env):
    u = await _provision(env, "anna", "bob", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna",
        name="Secret",
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    assert await _roles(env, space.id) == {u["anna"].user_id: SpaceRole.OWNER}
    assert space.join_mode is JoinMode.INVITE_ONLY
    assert env.joined == []


async def test_protected_minor_without_gate_is_seated(env):
    u = await _provision(env, "anna", "lila", admin="anna")
    await env.cp.enable_protection(
        minor_username="lila", declared_age=12, actor_user_id=u["anna"].user_id
    )
    space = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    assert (await _roles(env, space.id))[u["lila"].user_id] == SpaceRole.MEMBER


# ─── tier change ─────────────────────────────────────────────────────────


async def test_tier_change_into_household_seats_everyone_and_opens(env):
    u = await _provision(env, "anna", "bob", "carl", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna",
        name="Was private",
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    updated = await env.spaces.update_config(
        space.id, actor_username="anna", space_type=SpaceType.HOUSEHOLD
    )
    assert updated.space_type is SpaceType.HOUSEHOLD
    assert updated.join_mode is JoinMode.OPEN
    assert await _roles(env, space.id) == {
        u["anna"].user_id: SpaceRole.OWNER,
        u["bob"].user_id: SpaceRole.MEMBER,
        u["carl"].user_id: SpaceRole.MEMBER,
    }
    assert all(ev.automatic for ev in env.joined)


async def test_tier_change_skips_banned_and_gated_and_keeps_existing_roles(env):
    u = await _provision(env, "anna", "bob", "carl", "lila", "max", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna", name="Gated", space_type=SpaceType.PRIVATE
    )
    # bob already holds an admin seat — never re-roled.
    await env.spaces.add_member(
        space.id, actor_username="anna", user_id=u["bob"].user_id, role="admin"
    )
    await env.space_repo.ban_member(space.id, u["carl"].user_id, u["anna"].user_id)
    await env.cp.enable_protection(
        minor_username="lila", declared_age=12, actor_user_id=u["anna"].user_id
    )
    await env.cp.update_space_age_gate(
        space.id, min_age=16, actor_user_id=u["anna"].user_id
    )
    env.joined.clear()
    await env.spaces.update_config(
        space.id, actor_username="anna", space_type=SpaceType.HOUSEHOLD
    )
    assert await _roles(env, space.id) == {
        u["anna"].user_id: SpaceRole.OWNER,
        u["bob"].user_id: SpaceRole.ADMIN,
        u["max"].user_id: SpaceRole.MEMBER,
    }
    assert [ev.user_id for ev in env.joined] == [u["max"].user_id]


async def test_tier_change_out_of_household_resets_invite_only(env):
    await _provision(env, "anna", "bob", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    updated = await env.spaces.update_config(
        space.id, actor_username="anna", space_type=SpaceType.PRIVATE
    )
    assert updated.join_mode is JoinMode.INVITE_ONLY
    stored = await env.space_repo.get(space.id)
    assert stored is not None and stored.join_mode is JoinMode.INVITE_ONLY


async def test_tier_change_out_of_household_keeps_explicit_join_mode(env):
    await _provision(env, "anna", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    updated = await env.spaces.update_config(
        space.id,
        actor_username="anna",
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.REQUEST,
    )
    assert updated.join_mode is JoinMode.REQUEST


async def test_join_mode_patch_on_household_stays_open_without_a_bump(env):
    await _provision(env, "anna", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    before = await env.space_repo.get(space.id)
    assert before is not None
    updated = await env.spaces.update_config(
        space.id, actor_username="anna", join_mode=JoinMode.REQUEST
    )
    assert updated.join_mode is JoinMode.OPEN
    after = await env.space_repo.get(space.id)
    assert after is not None
    assert after.join_mode is JoinMode.OPEN
    assert after.config_sequence == before.config_sequence


async def test_the_database_refuses_a_non_open_household_space_we_host(env):
    """Migration 0101's guard: a write that bypasses the service still
    can't store a household space we host with another join mode."""
    await _provision(env, "anna", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    with pytest.raises(sqlite3.IntegrityError, match="join_mode open"):
        await env.space_repo.save(
            _with(await env.space_repo.get(space.id), join_mode=JoinMode.INVITE_ONLY)
        )
    stored = await env.space_repo.get(space.id)
    assert stored is not None and stored.join_mode is JoinMode.OPEN


def _with(space: Space | None, **kw) -> Space:
    assert space is not None
    return replace(space, **kw)


# ─── UserProvisioned ─────────────────────────────────────────────────────


async def test_new_user_is_seated_in_hosted_household_spaces_only(env):
    env.spaces.wire_household_seating()
    await _provision(env, "anna", admin="anna")
    home = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    home2 = await env.spaces.create_space(
        owner_username="anna", name="Home 2", space_type=SpaceType.HOUSEHOLD
    )
    secret = await env.spaces.create_space(
        owner_username="anna", name="Secret", space_type=SpaceType.PRIVATE
    )
    # A household-tier stub hosted by ANOTHER household.
    foreign = _with(home, id="foreign-sp", name="Theirs", owner_instance_id="other")
    await env.space_repo.save(foreign)

    u = await _provision(env, "eve")
    eve = u["eve"].user_id
    assert (await _roles(env, home.id)).get(eve) == SpaceRole.MEMBER
    assert (await _roles(env, home2.id)).get(eve) == SpaceRole.MEMBER
    assert eve not in await _roles(env, secret.id)
    assert eve not in await _roles(env, "foreign-sp")


async def test_one_bad_space_does_not_block_the_others(env, monkeypatch):
    env.spaces.wire_household_seating()
    await _provision(env, "anna", admin="anna")
    a = await env.spaces.create_space(
        owner_username="anna", name="A", space_type=SpaceType.HOUSEHOLD
    )
    b = await env.spaces.create_space(
        owner_username="anna", name="B", space_type=SpaceType.HOUSEHOLD
    )
    real = SpaceService.seat_household_members

    async def flaky(self, space, **kw):
        if space.id == a.id:
            raise RuntimeError("boom")
        return await real(self, space, **kw)

    # SpaceService is slotted: patch the class, not the instance.
    monkeypatch.setattr(SpaceService, "seat_household_members", flaky)
    u = await _provision(env, "eve")
    assert u["eve"].user_id not in await _roles(env, a.id)
    assert (await _roles(env, b.id)).get(u["eve"].user_id) == SpaceRole.MEMBER


async def test_leaving_sticks_when_another_user_is_provisioned(env):
    env.spaces.wire_household_seating()
    u = await _provision(env, "anna", "bob", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    await env.spaces.remove_member(
        space.id, actor_username="bob", user_id=u["bob"].user_id
    )
    eve = (await _provision(env, "eve"))["eve"]
    roles = await _roles(env, space.id)
    assert u["bob"].user_id not in roles
    assert roles.get(eve.user_id) == SpaceRole.MEMBER


async def test_seat_household_members_ignores_non_household_and_foreign(env):
    await _provision(env, "anna", "bob", admin="anna")
    secret = await env.spaces.create_space(
        owner_username="anna", name="Secret", space_type=SpaceType.PRIVATE
    )
    assert await env.spaces.seat_household_members(secret, actor_user_id="x") == []
    home = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    foreign = _with(home, id="f2", owner_instance_id="other")
    await env.space_repo.save(foreign)
    assert await env.spaces.seat_household_members(foreign, actor_user_id="x") == []
    dissolved = _with(home, dissolved=True)
    assert await env.spaces.seat_household_members(dissolved, actor_user_id="x") == []


async def test_published_user_provisioned_for_unknown_user_is_harmless(env):
    env.spaces.wire_household_seating()
    await _provision(env, "anna", admin="anna")
    home = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    await env.bus.publish(
        UserProvisioned(user_id="ghost", username="ghost", is_admin=False)
    )
    assert "ghost" not in await _roles(env, home.id)


# ─── shared seat helper ──────────────────────────────────────────────────


async def test_open_join_records_cp_audit_and_gossip(env, monkeypatch):
    u = await _provision(env, "anna", "bob", admin="anna")
    space = await env.spaces.create_space(
        owner_username="anna",
        name="Open",
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.OPEN,
    )
    gossip: list[str] = []
    audits: list[tuple[str, str]] = []

    async def fake_gossip(self, sp, **kw):
        gossip.append(kw["user_id"])

    async def fake_audit(self, **kw):
        audits.append((kw["user_id"], kw["actor_id"]))

    # Both services are slotted: patch the classes, not the instances.
    monkeypatch.setattr(SpaceService, "_emit_member_roster_gossip", fake_gossip)
    monkeypatch.setattr(ChildProtectionService, "record_membership_change", fake_audit)
    assert await env.spaces.request_join(space.id, user_id=u["bob"].user_id) is None
    assert gossip == [u["bob"].user_id]
    assert audits == [(u["bob"].user_id, u["bob"].user_id)]
    assert [ev.automatic for ev in env.joined] == [False]


# ─── household directory (Browse › Your household) ───────────────────────


async def test_directory_hides_gated_and_archived_spaces_from_non_members(env):
    u = await _provision(env, "anna", "lila", admin="anna")
    anna = u["anna"].user_id
    await env.cp.enable_protection(
        minor_username="lila", declared_age=12, actor_user_id=anna
    )
    gated = await env.spaces.create_space(
        owner_username="anna", name="Gated", space_type=SpaceType.PRIVATE
    )
    await env.space_repo.save(
        _with(gated, space_type=SpaceType.PUBLIC, join_mode=JoinMode.OPEN)
    )
    await env.cp.update_space_age_gate(gated.id, min_age=16, actor_user_id=anna)
    archived = await env.spaces.create_space(
        owner_username="anna", name="Old", space_type=SpaceType.PRIVATE
    )
    await env.space_repo.save(
        _with(
            archived,
            space_type=SpaceType.PUBLIC,
            join_mode=JoinMode.OPEN,
            archived=True,
        )
    )
    home = await env.spaces.create_space(
        owner_username="anna", name="Home", space_type=SpaceType.HOUSEHOLD
    )
    lila = [r.space.id for r in await env.spaces.household_directory(u["lila"].user_id)]
    assert lila == [home.id]
    # Their owner (seated in all three) still finds every one of them.
    mine = {r.space.id for r in await env.spaces.household_directory(anna)}
    assert mine == {gated.id, archived.id, home.id}
