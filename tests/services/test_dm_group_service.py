"""Tests for :mod:`socialhome.services.dm_group_service` (v_37 group authority)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import Conversation, ConversationType
from socialhome.domain.events import DmConversationCreated, DmGroupRosterChanged
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.user import RemoteUser
from socialhome.federation.owner_bound_id import (
    GROUP_CONVERSATION_KIND,
    mint_owner_bound_id,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_group_service import (
    DmGroupService,
    GroupManagedElsewhereError,
    GroupMemberUnsupportedError,
    clean_group_name,
)
from socialhome.services.dm_service import DmService
from socialhome.services.user_service import UserService

FET = FederationEventType
PEER = "inst-peer"
OLD = "inst-old"
LINK = "inst-link"


class _Federation:
    """Records sends; every peer at v_37 unless listed in ``versions``."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, FederationEventType, dict]] = []
        self.versions: dict[str, int] = {}
        self._event_registry = SimpleNamespace(
            handlers={},
            register=lambda et, h: self._event_registry.handlers.setdefault(et, h),
        )

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        return self.versions.get(instance_id, 37) >= min_version

    async def send_event(self, *, to_instance_id, event_type, payload, **_kw):
        self.sent.append((to_instance_id, event_type, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)


class _FedRepo:
    def __init__(self) -> None:
        self.instances = {
            PEER: SimpleNamespace(
                id=PEER,
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
            ),
            OLD: SimpleNamespace(
                id=OLD,
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
            ),
            LINK: SimpleNamespace(
                id=LINK,
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.SPACE_SESSION,
            ),
        }

    async def get_instance(self, instance_id):
        return self.instances.get(instance_id)

    async def list_instances(self, *, status=None, source=None):
        return list(self.instances.values())


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "group.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    events: list = []
    bus.subscribe(DmConversationCreated, events.append)
    bus.subscribe(DmGroupRosterChanged, events.append)
    users = SqliteUserRepo(db)
    convos = SqliteConversationRepo(db)
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)
    for name in ("ann", "ben", "cid"):
        await user_svc.provision(username=name, display_name=name.title())
    for uid, inst, name in (
        ("u-pat", PEER, "pat"),
        ("u-oli", OLD, "oli"),
        ("u-lin", LINK, "lin"),
    ):
        await db.enqueue(
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status) VALUES(?,?,?,?,?,?,?,?)",
            (
                inst,
                inst,
                "00" * 32,
                "k",
                "k",
                "https://x/wh",
                f"wh-{inst}",
                "confirmed",
            ),
        )
        await users.upsert_remote(
            RemoteUser(
                user_id=uid,
                instance_id=inst,
                remote_username=name,
                display_name=name.title(),
            )
        )
    fed = _Federation()
    fed.versions[OLD] = 36
    dm = DmService(convos, users, bus, own_instance_id=iid)
    dm.attach_federation(fed, _FedRepo(), iid)
    s = SimpleNamespace(
        db=db, iid=iid, fed=fed, dm=dm, groups=dm.groups, convos=convos, events=events
    )
    yield s
    await db.shutdown()


async def _uid(stack, username: str) -> str:
    return (await stack.dm._users.get(username)).user_id


async def test_create_ships_the_roster_to_each_member_household(stack):
    conv = await stack.dm.create_group_dm(
        creator_username="ann", member_user_ids=["u-pat", await _uid(stack, "ben")]
    )
    assert stack.groups.is_authority_here(conv.id)
    ((inst, et, payload),) = stack.fed.sent
    assert (inst, et) == (PEER, FET.DM_GROUP_ROSTER)
    assert payload["version"] == 1
    assert {m["user_id"] for m in payload["members"]} == {
        "u-pat",
        await _uid(stack, "ann"),
        await _uid(stack, "ben"),
    }
    created = [e for e in stack.events if isinstance(e, DmConversationCreated)]
    assert len(created) == 1 and len(created[0].member_user_ids) == 2


@pytest.mark.parametrize(
    ("user_id", "message"),
    [
        pytest.param("u-oli", "needs a Social Home update", id="below v_37"),
        pytest.param("u-lin", "paired with yours", id="invite-link household"),
    ],
)
async def test_people_who_cannot_join_are_refused_by_name(stack, user_id, message):
    with pytest.raises(GroupMemberUnsupportedError, match=message):
        await stack.dm.create_group_dm(
            creator_username="ann", member_usernames=["ben"], member_user_ids=[user_id]
        )
    assert stack.fed.sent == []


async def test_an_unknown_person_is_not_found(stack):
    with pytest.raises(KeyError):
        await stack.dm.create_group_dm(
            creator_username="ann", member_usernames=["ben"], member_user_ids=["u-x"]
        )


async def test_a_legacy_local_group_takes_no_remote_people(stack):
    await stack.convos.apply_group_roster(
        Conversation(
            id="legacy-group",
            type=ConversationType.GROUP_DM,
            created_at=datetime.now(timezone.utc),
            membership_version=1,
        ),
        local_usernames=["ann", "ben", "cid"],
        remote_members=[],
        at="t",
    )
    with pytest.raises(GroupMemberUnsupportedError, match="start a new group"):
        await stack.dm.add_group_members(
            "legacy-group", actor_username="ann", user_ids=["u-pat"]
        )
    # …but it stays managed here, and leaving is a new local snapshot.
    await stack.dm.leave("legacy-group", username="cid")
    assert (await stack.convos.get("legacy-group")).membership_version == 2
    assert stack.fed.sent == []


async def test_add_rename_remove_bump_the_version_each_time(stack):
    conv = await stack.dm.create_group_dm(
        creator_username="ann", member_usernames=["ben", "cid"]
    )
    await stack.dm.add_group_members(conv.id, actor_username="ben", user_ids=["u-pat"])
    await stack.dm.rename_group(conv.id, actor_username="cid", name="  Lunch  ")
    await stack.dm.remove_group_member(conv.id, actor_username="ann", user_id="u-pat")
    rosters = [p for _i, et, p in stack.fed.sent if et is FET.DM_GROUP_ROSTER]
    assert [r["version"] for r in rosters] == [2, 3, 4]
    assert rosters[1]["name"] == "Lunch"
    # The removal still reaches the household it took out.
    assert "u-pat" not in {m["user_id"] for m in rosters[2]["members"]}
    assert (await stack.convos.get(conv.id)).membership_version == 4
    changed = [e for e in stack.events if isinstance(e, DmGroupRosterChanged)]
    assert len(changed) == 3


async def test_removing_someone_not_in_the_group_is_not_found(stack):
    conv = await stack.dm.create_group_dm(
        creator_username="ann", member_usernames=["ben", "cid"]
    )
    with pytest.raises(KeyError):
        await stack.dm.remove_group_member(
            conv.id, actor_username="ann", user_id="u-pat"
        )


async def test_a_group_kept_elsewhere_is_not_changed_here(stack):
    conv_id = mint_owner_bound_id(
        GROUP_CONVERSATION_KIND, space_id="", owner_user_id=PEER
    )
    await _apply_peer_roster(stack, conv_id, 1, ["ann", "ben"])
    for call in (
        stack.dm.rename_group(conv_id, actor_username="ann", name="x"),
        stack.dm.add_group_members(conv_id, actor_username="ann", usernames=["cid"]),
        stack.dm.remove_group_member(
            conv_id, actor_username="ann", user_id=await _uid(stack, "ben")
        ),
    ):
        with pytest.raises(GroupManagedElsewhereError):
            await call


async def _apply_peer_roster(stack, conv_id, version, local_names):
    members = [
        {"user_id": "u-pat", "instance_id": PEER, "username": "pat"},
        *[
            {
                "user_id": await _uid(stack, n),
                "instance_id": stack.iid,
                "username": n,
                "display_name": n.title(),
            }
            for n in local_names
        ],
    ]
    await stack.groups._on_roster(
        FederationEvent(
            msg_id="m",
            event_type=FET.DM_GROUP_ROSTER,
            from_instance=PEER,
            to_instance=stack.iid,
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload={
                "conversation_id": conv_id,
                "version": version,
                "name": "Theirs",
                "members": members,
            },
        )
    )


async def test_a_roster_seats_us_and_pulls_the_backlog(stack):
    requests: list[tuple[str, str]] = []

    class _History:
        async def enqueue_request(self, *, instance_id, conversation_id):
            requests.append((instance_id, conversation_id))
            return True

    stack.groups.attach_history(_History())
    conv_id = mint_owner_bound_id(
        GROUP_CONVERSATION_KIND, space_id="", owner_user_id=PEER
    )
    await _apply_peer_roster(stack, conv_id, 1, ["ann", "ben"])
    assert requests == [(PEER, conv_id)]
    conv = await stack.convos.get(conv_id)
    assert (conv.name, conv.membership_version) == ("Theirs", 1)
    # A later snapshot that seats nobody new pulls nothing more.
    await _apply_peer_roster(stack, conv_id, 2, ["ann"])
    assert requests == [(PEER, conv_id)]
    # Leaving tells the authority (found among our paired households).
    stack.fed.sent.clear()
    await stack.dm.leave(conv_id, username="ann")
    ((inst, et, payload),) = stack.fed.sent
    assert (inst, et) == (PEER, FET.DM_GROUP_LEAVE)
    assert payload == {"conversation_id": conv_id, "user_id": await _uid(stack, "ann")}


async def test_a_roster_is_not_shipped_to_a_household_below_v37(stack):
    conv = await stack.dm.create_group_dm(
        creator_username="ann", member_usernames=["ben"], member_user_ids=["u-pat"]
    )
    stack.fed.versions[PEER] = 36  # the peer downgraded since
    stack.fed.sent.clear()
    await stack.groups.publish_roster(conv.id)
    assert stack.fed.sent == []


async def test_commit_refuses_a_version_already_taken(stack):
    conv = await stack.dm.create_group_dm(
        creator_username="ann", member_usernames=["ben", "cid"]
    )
    with pytest.raises(ValueError, match="changed meanwhile"):
        await stack.groups.commit(conv, local_usernames=["ann"], remote_members=[])


def test_clean_group_name():
    assert clean_group_name("  Crew ") == "Crew"
    assert clean_group_name("   ") is None
    assert clean_group_name(None) is None
    assert clean_group_name(7) is None
    assert len(clean_group_name("x" * 200)) == 80


def test_a_household_without_an_identity_cannot_mint_group_ids():
    svc = DmGroupService(conversation_repo=None, user_repo=None, bus=EventBus())
    with pytest.raises(RuntimeError):
        svc.mint_conversation_id()
