"""A real-SQLite household with one space and its chat — for the space-chat
service, outbound and inbound tests (not a test module itself).

Household ``HERE`` hosts space ``SP``: ``anna`` owns it, ``bob`` is a
member, ``mod`` a moderator, ``finn`` a follower (subscriber), ``zoe`` a
local user with no seat. Remote seats: ``u-rb`` (member) on ``HOUSE_B``,
``u-radm`` (admin) on ``HOUSE_B``, ``u-rf`` (follower) on ``HOUSE_F`` — a
follower-only household.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import SystemChatScope
from socialhome.domain.events import (
    DmMessageCreated,
    DmMessageDeleted,
    DmMessageReactionChanged,
    DmMessageUpdated,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.owner_bound_id import (
    SPACE_CHAT_MESSAGE_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_service import DmService
from socialhome.services.federation_inbound.space_chat import (
    SpaceChatInboundHandlers,
)
from socialhome.services.space_chat_outbound import (
    SpaceChatAudience,
    SpaceChatOutbound,
)
from socialhome.services.space_chat_service import SpaceChatService
from socialhome.services.system_chat_policy import SpaceChatAccess, SystemChatPolicy

SP = "sp-chat"
HOUSE_B = "house-b"
HOUSE_F = "house-f"
LOCAL = {
    "anna": "owner",
    "bob": "member",
    "mod": "moderator",
    "finn": "subscriber",
}


def chat_id(owner: str, space_id: str = SP) -> str:
    """A fresh owner-bound space-chat message id."""
    return mint_owner_bound_id(
        SPACE_CHAT_MESSAGE_KIND, space_id=space_id, owner_user_id=owner
    )


@dataclass
class Broadcast:
    space_id: str
    event_type: FederationEventType
    payload: dict
    min_proto_version: int | None
    only_instances: frozenset[str] | None


@dataclass
class FakeFederation:
    """Records ``broadcast_to_space_members`` calls; owns a handler registry."""

    sent: list[Broadcast] = field(default_factory=list)
    handlers: dict[FederationEventType, list] = field(default_factory=dict)
    #: Households that advertise (or claimed) a version below the chat's.
    older: set[str] = field(default_factory=set)

    async def space_member_supports(self, instance_id, *, min_version):
        return instance_id not in self.older

    async def broadcast_to_space_members(
        self,
        space_id,
        event_type,
        payload,
        *,
        min_proto_version=None,
        only_instances=None,
        **_kw,
    ):
        self.sent.append(
            Broadcast(
                space_id,
                event_type,
                dict(payload),
                min_proto_version,
                frozenset(only_instances) if only_instances is not None else None,
            )
        )

    async def send_event(self, **_kw):  # DmService fan-out (never for chats)
        raise AssertionError("a space chat never rides the DM path")

    @property
    def _event_registry(self):
        return self

    def register(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    async def deliver(self, event_type, payload, *, sender, space_id=SP):
        for handler in self.handlers.get(event_type, []):
            await handler(
                FederationEvent(
                    msg_id=f"m-{event_type.value}",
                    event_type=event_type,
                    from_instance=sender,
                    to_instance="us",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    payload={"space_id": space_id, **payload},
                    space_id=space_id,
                )
            )


class Stack:
    """Everything a space-chat test touches, over one real database."""

    db: AsyncDatabase
    iid: str
    bus: EventBus
    users: SqliteUserRepo
    convos: SqliteConversationRepo
    spaces: SqliteSpaceRepo
    seats: SqliteSpaceRemoteMemberRepo
    policy: SystemChatPolicy
    access: SpaceChatAccess
    chat: SpaceChatService
    dm: DmService
    federation: FakeFederation
    audience: SpaceChatAudience
    inbound: SpaceChatInboundHandlers
    uid: dict[str, str]

    def events(self, kind: type) -> list:
        return [e for e in self.published if isinstance(e, kind)]

    published: list


async def build_stack(tmp_dir) -> Stack:
    s = Stack()
    kp = generate_identity_keypair()
    s.iid = derive_instance_id(kp.public_key)
    s.db = AsyncDatabase(tmp_dir / "space_chat.db", batch_timeout_ms=10)
    await s.db.startup()
    await s.db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (s.iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    s.uid = {}
    for name in (*LOCAL, "zoe"):
        s.uid[name] = f"u-{name}"
        await s.db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (name, f"u-{name}", name.title()),
        )
    await s.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'Choir', ?, 'anna', ?)",
        (SP, s.iid, "ab" * 32),
    )
    for name, role in LOCAL.items():
        await s.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            (SP, f"u-{name}", role),
        )
    for inst, uid, role in (
        (HOUSE_B, "u-rb", "member"),
        (HOUSE_B, "u-radm", "admin"),
        (HOUSE_F, "u-rf", "subscriber"),
    ):
        await s.db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
            " role, display_name) VALUES(?,?,?,?,?)",
            (SP, inst, uid, role, uid.upper()),
        )
    s.bus = EventBus()
    s.published = []

    async def _record(event) -> None:
        s.published.append(event)

    for kind in (
        DmMessageCreated,
        DmMessageUpdated,
        DmMessageDeleted,
        DmMessageReactionChanged,
    ):
        s.bus.subscribe(kind, _record)
    s.users = SqliteUserRepo(s.db)
    s.convos = SqliteConversationRepo(s.db)
    s.spaces = SqliteSpaceRepo(s.db)
    s.seats = SqliteSpaceRemoteMemberRepo(s.db)
    s.policy = SystemChatPolicy(s.users)
    s.access = SpaceChatAccess(s.spaces)
    s.policy.register(SystemChatScope.SPACE, s.access)
    s.chat = SpaceChatService(s.convos, s.users, s.spaces, s.access, s.policy, s.bus)
    s.chat.wire()
    s.federation = FakeFederation()
    s.dm = DmService(s.convos, s.users, s.bus, own_instance_id=s.iid)
    s.dm.attach_federation(s.federation, None, s.iid)  # type: ignore[arg-type]
    s.dm.attach_system_chats(s.policy)
    s.audience = SpaceChatAudience(s.seats, own_instance_id=s.iid)
    SpaceChatOutbound(
        bus=s.bus,
        federation_service=s.federation,  # type: ignore[arg-type]
        conversation_repo=s.convos,
        audience=s.audience,
    ).wire()
    s.inbound = SpaceChatInboundHandlers(
        bus=s.bus,
        authorship=SpaceAuthorship(
            space_repo=s.spaces, remote_member_repo=s.seats, user_repo=s.users
        ),
        space_repo=s.spaces,
        remote_member_repo=s.seats,
        conversation_repo=s.convos,
        user_repo=s.users,
        chat_service=s.chat,
        policy=s.policy,
    )
    s.inbound.attach_to(s.federation)  # type: ignore[arg-type]
    return s
