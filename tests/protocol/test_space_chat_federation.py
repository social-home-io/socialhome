"""§27.9 release blocker: a space's chat between member households (v_55).

Marked ``@pytest.mark.security``.

Four households share space ``SP`` (hosted on **a**):

* **a** — anna, the owner, and mia, a moderator;
* **b** — bob, a member;
* **c** — cara, who only *follows* the space (a follower-only household);
* **d** — dave, a member, on a household that advertises v_54 (pre-chat).

Real crypto (per-pair session keys), real SQLite, the real §24.11 inbound
pipeline (signature, replay, the space writer + archive gates) and the real
space-chat services on every household. The stand-in is the network: a
loopback HTTPS inbox that hands each envelope to the addressed household's
``handle_inbound_envelope`` and records what crossed the wire.

Proves: a create / edit / reaction / moderator delete round-trips between
the writer households; the follower-only household and the older one get
nothing; no message text, author or conversation id ever rides outside
the ciphertext; and every forged or misplaced chat write is refused.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import SystemChatScope
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import FederationCapability, OURS
from socialhome.federation.federation_service import FederationService
from socialhome.federation.owner_bound_id import (
    SPACE_CHAT_MESSAGE_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.federation.sync.space.exporters import (
    ChatMessagesDeletedExporter,
    ChatMessagesExporter,
)
from socialhome.federation.transport import FederationTransport
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_service import DmService
from socialhome.services.federation_inbound import SpaceChatInboundHandlers
from socialhome.services.space_chat_outbound import (
    SpaceChatAudience,
    SpaceChatOutbound,
)
from socialhome.services.space_chat_service import SpaceChatService
from socialhome.services.system_chat_policy import SpaceChatAccess, SystemChatPolicy

pytestmark = pytest.mark.security

FET = FederationEventType
SP = "sp-choir"
CHAT_EVENTS = {
    FET.SPACE_CHAT_MESSAGE_CREATED,
    FET.SPACE_CHAT_MESSAGE_UPDATED,
    FET.SPACE_CHAT_MESSAGE_DELETED,
    FET.SPACE_CHAT_REACTION,
}

#: household → its local users and their space roles.
ROSTER: dict[str, dict[str, str]] = {
    "a": {"anna": "owner", "mia": "moderator"},
    "b": {"bob": "member"},
    "c": {"cara": "subscriber"},
    "d": {"dave": "member"},
}

#: Long and unique, so finding it in a wire capture can only mean a leak.
SECRET = "the rehearsal moves to the crypt beneath St. Ottilien at 19:45"


class _LoopbackInbox:
    """The HTTPS inbox leg: straight into the addressed household's §24.11
    pipeline, recording every envelope that crossed the wire."""

    def __init__(self, world: dict[str, SimpleNamespace], me: str) -> None:
        self._world = world
        self._me = me
        self.posted: list[tuple[str, dict]] = []
        #: Households this one cannot reach right now (offline).
        self.down: set[str] = set()

    async def send(self, *, instance, envelope_dict):
        peer = next(h for h in self._world.values() if h.iid == instance.id)
        me = self._world[self._me]
        if peer.name in self.down:
            return False, None
        self.posted.append((peer.name, envelope_dict))
        try:
            await peer.federation.handle_inbound_envelope(
                f"{peer.name}-inbox-for-{me.name}",
                json.dumps(envelope_dict).encode(),
            )
        except ValueError:
            return False, 403
        return True, 200


async def _no_signal(*_a, **_kw):
    raise RuntimeError("no RTC signalling here")


async def _household(tmp_path, name: str, world: dict) -> SimpleNamespace:
    db = AsyncDatabase(tmp_path / f"{name}.db", batch_timeout_ms=10)
    await db.startup()
    ident = generate_identity_keypair()
    iid = derive_instance_id(ident.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, ident.private_key.hex(), ident.public_key.hex(), "aa" * 32),
    )
    km = KeyManager(os.urandom(32))
    bus = EventBus()
    fed_repo = SqliteFederationRepo(db)
    federation = FederationService(
        db,
        fed_repo,
        SqliteOutboxRepo(db),
        km,
        bus,
        iid,
        ident.private_key,
        ident.public_key,
    )
    inbox = _LoopbackInbox(world, name)
    transport = FederationTransport(
        own_instance_id=iid, https_inbox=inbox, signaling_send=_no_signal
    )
    transport.mark_ice_primed()
    federation.attach_transport(transport)
    users = SqliteUserRepo(db)
    convos = SqliteConversationRepo(db)
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    federation.attach_space_write_gate(spaces, seats)
    policy = SystemChatPolicy(users)
    access = SpaceChatAccess(spaces)
    policy.register(SystemChatScope.SPACE, access)
    chat = SpaceChatService(convos, users, spaces, access, policy, bus)
    chat.wire()
    dm = DmService(convos, users, bus, own_instance_id=iid)
    dm.attach_federation(federation, fed_repo, iid)
    dm.attach_system_chats(policy)
    SpaceChatOutbound(
        bus=bus,
        federation_service=federation,
        conversation_repo=convos,
        audience=SpaceChatAudience(seats, own_instance_id=iid),
    ).wire()
    inbound = SpaceChatInboundHandlers(
        bus=bus,
        authorship=SpaceAuthorship(
            space_repo=spaces, remote_member_repo=seats, user_repo=users
        ),
        space_repo=spaces,
        remote_member_repo=seats,
        conversation_repo=convos,
        user_repo=users,
        chat_service=chat,
        policy=policy,
    )
    inbound.attach_to(federation)
    h = SimpleNamespace(
        name=name,
        db=db,
        iid=iid,
        ident=ident,
        km=km,
        fed_repo=fed_repo,
        federation=federation,
        transport=transport,
        inbox=inbox,
        convos=convos,
        chat=chat,
        dm=dm,
        inbound=inbound,
        exports=ChatMessagesExporter(convos, spaces),
        exports_deleted=ChatMessagesDeletedExporter(convos, spaces),
    )
    world[name] = h
    return h


async def _pair(me, peer, *, peer_version: int = OURS) -> None:
    k_out, k_in = os.urandom(32), os.urandom(32)
    for side, other, out, inn in ((me, peer, k_out, k_in), (peer, me, k_in, k_out)):
        await side.fed_repo.save_instance(
            RemoteInstance(
                id=other.iid,
                display_name=f"{other.name} household",
                remote_identity_pk=other.ident.public_key.hex(),
                key_self_to_remote=side.km.encrypt(out),
                key_remote_to_self=side.km.encrypt(inn),
                remote_inbox_url=f"https://{other.name}.example/federation/inbox/x",
                local_inbox_id=f"{side.name}-inbox-for-{other.name}",
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
            ),
        )
        version = peer_version if other is peer else OURS
        await side.fed_repo.set_proto_version(other.iid, version)
        side.transport._rtc_suppressed_until[other.iid] = float("inf")


async def _seed_space(world: dict[str, SimpleNamespace]) -> None:
    host = world["a"]
    for h in world.values():
        await h.db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?, 'Choir', ?, 'anna', ?)",
            (SP, host.iid, "ab" * 32),
        )
        for other in world.values():
            if other is not h:
                await h.db.enqueue(
                    "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
                    (SP, other.iid),
                )
        for house, people in ROSTER.items():
            for username, role in people.items():
                if house == h.name:
                    await h.db.enqueue(
                        "INSERT INTO users(username, user_id, display_name)"
                        " VALUES(?,?,?)",
                        (username, f"u-{username}", username.title()),
                    )
                    await h.db.enqueue(
                        "INSERT INTO space_members(space_id, user_id, role)"
                        " VALUES(?,?,?)",
                        (SP, f"u-{username}", role),
                    )
                else:
                    # The roster mirror: a remote seat is never ``owner``.
                    await h.db.enqueue(
                        "INSERT INTO space_remote_members(space_id, instance_id,"
                        " user_id, role, display_name) VALUES(?,?,?,?,?)",
                        (
                            SP,
                            world[house].iid,
                            f"u-{username}",
                            "member" if role == "owner" else role,
                            username.title(),
                        ),
                    )


@pytest.fixture
async def world(tmp_path):
    households: dict[str, SimpleNamespace] = {}
    for name in ROSTER:
        await _household(tmp_path, name, households)
    a, b, c, d = (households[n] for n in "abcd")
    await _pair(a, b)
    await _pair(a, c)
    await _pair(b, c)
    # d advertises v_54 to everyone: it predates the space chat.
    for other in (a, b, c):
        await _pair(other, d, peer_version=FederationCapability.MIN_FOR_SPACE_CHAT - 1)
    await _seed_space(households)
    yield SimpleNamespace(**households)
    for h in households.values():
        await h.db.shutdown()


async def _chat_id(h, username: str) -> str:
    summary = await h.chat.summary(SP, username)
    assert summary.enabled and summary.conversation_id
    return summary.conversation_id


async def _messages(h) -> list[tuple]:
    chat = await h.chat.get_chat(SP)
    if chat is None:
        return []
    return [
        (r["id"], r["sender_user_id"], r["content"], r["deleted"])
        for r in await h.db.fetchall(
            "SELECT id, sender_user_id, content, deleted FROM conversation_messages"
            " WHERE conversation_id=? ORDER BY created_at",
            (chat.id,),
        )
    ]


def _chat_traffic(world) -> list[tuple[str, str, dict]]:
    """``(from, to, envelope)`` for every space-chat envelope on the wire."""
    out = []
    for h in (world.a, world.b, world.c, world.d):
        for to, env in h.inbox.posted:
            if env.get("event_type") in {e.value for e in CHAT_EVENTS}:
                out.append((h.name, to, env))
    return out


# ── The round trip ────────────────────────────────────────────────────────


async def test_a_space_chat_round_trips_between_its_writer_households(world):
    a, b, c, d = world.a, world.b, world.c, world.d
    a_chat = await _chat_id(a, "anna")
    sent = await a.dm.send_message(a_chat, sender_username="anna", content=SECRET)
    # b holds it in ITS OWN chat for the space, under the same id.
    assert await _messages(b) == [(sent.id, "u-anna", SECRET, 0)]
    b_chat = await _chat_id(b, "bob")
    assert b_chat != a_chat
    assert (await b.chat.summary(SP, "bob")).unread == 1
    # bob replies, edits, reacts — from his own household.
    reply = await b.dm.send_message(
        b_chat, sender_username="bob", content="on my way", reply_to_id=sent.id
    )
    await b.dm.edit_message(reply.id, editor_username="bob", new_content="omw!")
    await b.dm.add_reaction(sent.id, username="bob", emoji="🎶")
    got = await a.convos.get_message(reply.id)
    assert got is not None and got.content == "omw!" and got.reply_to_id == sent.id
    assert [(r.user_id, r.emoji) for r in await a.convos.list_reactions(sent.id)] == [
        ("u-bob", "🎶")
    ]
    await b.dm.remove_reaction(sent.id, username="bob", emoji="🎶")
    assert await a.convos.list_reactions(sent.id) == []
    # mia moderates bob's message on a; the delete reaches b.
    await a.dm.delete_message(reply.id, actor_username="mia")
    assert [m[3] for m in await _messages(b) if m[0] == reply.id] == [1]
    # The follower-only household and the pre-v_55 one got nothing at all —
    # and stored nothing.
    traffic = _chat_traffic(world)
    assert traffic
    assert {to for _frm, to, _env in traffic} == {"a", "b"}
    assert await _messages(c) == [] and await c.chat.get_chat(SP) is None
    assert await _messages(d) == [] and await d.chat.get_chat(SP) is None
    # Nothing was echoed back: each event crossed the wire exactly once.
    assert len(traffic) == 6


async def test_no_chat_field_rides_outside_the_ciphertext(world):
    a, b = world.a, world.b
    a_chat = await _chat_id(a, "anna")
    sent = await a.dm.send_message(a_chat, sender_username="anna", content=SECRET)
    b_chat = await _chat_id(b, "bob")
    await b.dm.add_reaction(sent.id, username="bob", emoji="🎶")
    traffic = _chat_traffic(world)
    assert traffic
    for _frm, _to, env in traffic:
        raw = json.dumps(env)
        for leak in (SECRET, sent.id, "u-anna", "u-bob", "🎶", a_chat, b_chat):
            assert leak not in raw, leak
        # Routing only in the clear: no payload field on the envelope.
        assert "encrypted_payload" in env
        for field in ("content", "author_user_id", "message_id", "conversation_id"):
            assert field not in env


# ── Refusals over the real pipeline ───────────────────────────────────────


async def _forge(
    sender, target, payload: dict, event_type=FET.SPACE_CHAT_MESSAGE_CREATED
):
    """``sender`` signs and ships one chat event to ``target`` directly."""
    await sender.federation.send_event(
        to_instance_id=target.iid,
        event_type=event_type,
        payload={"space_id": SP, **payload},
        space_id=SP,
    )


def _bound(owner: str, space_id: str = SP) -> str:
    return mint_owner_bound_id(
        SPACE_CHAT_MESSAGE_KIND, space_id=space_id, owner_user_id=owner
    )


@pytest.mark.parametrize(
    ("label", "author", "msg_owner"),
    [
        ("a forged author (anna is a's, not b's)", "u-anna", "u-anna"),
        ("an id bound to someone else", "u-bob", "u-anna"),
        ("a legacy id", "u-bob", None),
    ],
)
async def test_forged_authors_and_ids_are_refused(world, label, author, msg_owner):
    a, b = world.a, world.b
    msg_id = _bound(msg_owner) if msg_owner else "ab" * 16
    await _forge(b, a, {"message_id": msg_id, "author_user_id": author, "content": "x"})
    assert await _messages(a) == [], label


async def test_a_follower_households_message_dies_at_the_writer_gate(world):
    a, c = world.a, world.c
    await _forge(
        c,
        a,
        {"message_id": _bound("u-cara"), "author_user_id": "u-cara", "content": "x"},
    )
    assert await _messages(a) == []


async def test_a_non_member_household_cannot_post(tmp_path, world):
    a = world.a
    stranger = await _household(tmp_path, "x", {"x": None, **vars(world)})
    stranger_world = {**vars(world), "x": stranger}
    a.inbox._world = stranger_world
    stranger.inbox._world = stranger_world
    await _pair(stranger, a)
    await _forge(
        stranger,
        a,
        {
            "message_id": _bound("u-xavier"),
            "author_user_id": "u-xavier",
            "content": "x",
        },
    )
    assert await _messages(a) == []
    await stranger.db.shutdown()


async def test_chat_off_here_drops_inbound(world):
    a, b = world.a, world.b
    await b.db.enqueue("UPDATE spaces SET feature_chat=0")
    await _forge(
        a,
        b,
        {"message_id": _bound("u-anna"), "author_user_id": "u-anna", "content": "x"},
    )
    assert await _messages(b) == [] and await b.chat.get_chat(SP) is None


async def test_an_archived_space_takes_no_chat_write_but_a_delete(world):
    """A member household's chat write into a copy archived here is refused
    by the §24.11 archive gate; its delete still lands (a removal must not
    outlive itself on any copy)."""
    a, b = world.a, world.b
    b_chat = await _chat_id(b, "bob")
    sent = await b.dm.send_message(b_chat, sender_username="bob", content="before")
    assert [m[0] for m in await _messages(a)] == [sent.id]
    await a.db.enqueue("UPDATE spaces SET archived=1")
    await _forge(
        b, a, {"message_id": _bound("u-bob"), "author_user_id": "u-bob", "content": "x"}
    )
    await _forge(
        b,
        a,
        {"message_id": sent.id, "user_id": "u-bob", "emoji": "🎶", "action": "add"},
        FET.SPACE_CHAT_REACTION,
    )
    assert [m[0] for m in await _messages(a)] == [sent.id]
    assert await a.convos.list_reactions(sent.id) == []
    await _forge(
        b,
        a,
        {"message_id": sent.id, "actor_user_id": "u-bob"},
        FET.SPACE_CHAT_MESSAGE_DELETED,
    )
    assert [m[3] for m in await _messages(a)] == [1]


async def test_an_older_peer_is_never_sent_a_chat_event(world):
    a = world.a
    a_chat = await _chat_id(a, "anna")
    await a.dm.send_message(a_chat, sender_username="anna", content="hi")
    assert all(to != "d" for _frm, to, _env in _chat_traffic(world))


# ── Deletions converge: a missed delete never outlives itself ────────────


async def _joiner(tmp_path, world) -> SimpleNamespace:
    """Household j: jo just joined the space (a member), holds the roster
    mirror of a and b, and has received no chat yet."""
    j = await _household(tmp_path, "j", {**vars(world)})
    await j.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'Choir', ?, 'anna', ?)",
        (SP, world.a.iid, "ab" * 32),
    )
    await j.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('jo','u-jo','Jo')"
    )
    await j.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,'u-jo','member')",
        (SP,),
    )
    for house, uid, role in (
        (world.a, "u-anna", "member"),
        (world.a, "u-mia", "moderator"),
        (world.b, "u-bob", "member"),
    ):
        await j.db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES(?,?,?,?)",
            (SP, house.iid, uid, role),
        )
    return j


async def _catch_up(receiver, provider) -> None:
    """One §25.6 stream from ``provider`` into ``receiver``: deletions
    first, then the messages (``RESOURCE_ORDER``)."""
    await receiver.inbound.apply_sync_tombstones(
        SP, await provider.exports_deleted.list_records(SP), provider=provider.iid
    )
    await receiver.inbound.apply_sync_records(
        SP, await provider.exports.list_records(SP), provider=provider.iid
    )


async def _live(h, message_id: str) -> bool:
    msg = await h.convos.get_message(message_id)
    return msg is not None and not msg.deleted


@pytest.mark.parametrize("order", ["stale host first", "author first"])
async def test_a_delete_the_host_missed_never_reaches_a_joiner(tmp_path, world, order):
    """bob deletes his message while the host is offline. The host still
    holds it; a joiner catching up from the host AND from bob's household —
    in either order — ends with it deleted, and once the host catches up
    from b it is deleted there and never streamed again."""
    a, b = world.a, world.b
    b_chat = await _chat_id(b, "bob")
    sent = await b.dm.send_message(b_chat, sender_username="bob", content=SECRET)
    assert await _live(a, sent.id)
    b.inbox.down = {"a"}
    await b.dm.delete_message(sent.id, actor_username="bob")
    assert await _live(a, sent.id)  # the host missed it
    j = await _joiner(tmp_path, world)
    try:
        for provider in (a, b) if order == "stale host first" else (b, a):
            await _catch_up(j, provider)
        assert not await _live(j, sent.id)
        assert all(r["id"] != sent.id for r in await j.exports.list_records(SP))
        # The host heals from bob's household: deleted, never re-exported,
        # and from now on it spreads the deletion itself.
        await _catch_up(a, b)
        assert not await _live(a, sent.id)
        assert all(r["id"] != sent.id for r in await a.exports.list_records(SP))
        assert sent.id in {r["id"] for r in await a.exports_deleted.list_records(SP)}
    finally:
        await j.db.shutdown()


async def test_a_moderator_delete_that_overtakes_its_create_holds(world):
    """b receives mia's delete of anna's message before the message
    itself (a delayed create): a tombstone, so the late create — live or by
    catch-up — is refused, and nothing is ever shown."""
    a, b = world.a, world.b
    a_chat = await _chat_id(a, "anna")
    a.inbox.down = {"b"}
    sent = await a.dm.send_message(a_chat, sender_username="anna", content=SECRET)
    assert await b.convos.get_message(sent.id) is None
    a.inbox.down = set()
    await a.dm.delete_message(sent.id, actor_username="mia")
    tomb = await b.convos.get_message(sent.id)
    assert tomb is not None and tomb.deleted and tomb.content == ""
    # The create arrives late (the outbox retry) — and a stale catch-up too.
    await _forge(
        a,
        b,
        {
            "message_id": sent.id,
            "author_user_id": "u-anna",
            "content": SECRET,
        },
    )
    await b.inbound.apply_sync_records(
        SP,
        [{"id": sent.id, "author_user_id": "u-anna", "content": SECRET}],
        provider=a.iid,
    )
    assert not await _live(b, sent.id)
    b_chat = await _chat_id(b, "bob")
    assert [m.id for m in await b.convos.list_messages(b_chat)] == []
    assert (await b.chat.summary(SP, "bob")).unread == 0
