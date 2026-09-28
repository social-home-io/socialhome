"""Release-blocker protocol tests: who may seat people in a group conversation.

Marked ``@pytest.mark.security``.

A group conversation (v_37) is kept by one household — its authority, the
household its owner-bound id commits to. The rules these tests encode,
against the real application registry and SQLite:

* Seats come only from the authority's ``DM_GROUP_ROSTER``, applied only
  when its version is newer. A plain message never opens a group or seats
  anybody, and no other household — member or not — can rewrite the list.
* A roster cannot re-home a person this household already knows, nor seat
  one of our users under another household.
* A message is taken only from a user seated on the household that signed
  it — including a member on a household we never paired with, bound by
  the seat the roster wrote. A removed member's household is refused.
* The authority honours ``DM_GROUP_LEAVE`` only for the sending
  household's own seated user.
* Group content fans out to member households only: a paired household
  outside the group never receives it, and a removed one stops receiving.
* Catch-up history may carry other members' rows only when it comes from
  the authority, and never a row claimed for one of our users.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, dm_service_key, federation_service_key
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.federation.federation_service import FederationService
from socialhome.federation.owner_bound_id import (
    GROUP_CONVERSATION_KIND,
    mint_owner_bound_id,
)

pytestmark = pytest.mark.security

FET = FederationEventType

AUTH = "peer-anna"  # the group's authority household (paired with us)
BOB = "peer-bob"  # a member household (paired with us)
CARL = "peer-carl"  # a member household we never paired with
DORA = "peer-dora"  # paired with us, never in the group


def _group_id(authority: str) -> str:
    return mint_owner_bound_id(
        GROUP_CONVERSATION_KIND, space_id="", owner_user_id=authority
    )


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "group.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, users: tuple[tuple[str, str], ...]) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            "00" * 32,
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
            37,
        ),
    )
    for user_id, username in users:
        await db.enqueue(
            "INSERT INTO remote_users(user_id, instance_id, remote_username,"
            " display_name) VALUES(?,?,?,?)",
            (user_id, instance_id, username, username.title()),
        )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for username in ("ula", "uwe"):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, f"u-{username}", username.title()),
        )
    await _seed_peer(db, AUTH, (("u-anna", "anna"),))
    await _seed_peer(db, BOB, (("u-bob", "bob"),))
    await _seed_peer(db, DORA, (("u-dora", "dora"),))
    sent: list[tuple[str, FederationEventType, dict]] = []
    meshed: list[tuple[str, FederationEventType, dict]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    async def _record_mesh(_self, *, to_instance_id, event_type, payload, **_kw):
        meshed.append((to_instance_id, event_type, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _record_mesh)
    own = app[federation_service_key].own_instance_id
    return app, db, own, sent, meshed


def _member(user_id: str, instance_id: str, username: str) -> dict:
    return {
        "user_id": user_id,
        "instance_id": instance_id,
        "username": username,
        "display_name": username.title(),
    }


def _roster(conv: str, version: int, own: str, *extra: dict, name="Crew") -> dict:
    return {
        "conversation_id": conv,
        "version": version,
        "name": name,
        "members": [
            _member("u-anna", AUTH, "anna"),
            _member("u-ula", own, "ula"),
            _member("u-bob", BOB, "bob"),
            _member("u-carl", CARL, "carl"),
            *extra,
        ],
    }


async def _send(app, event_type, payload, *, from_instance) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


async def _state(db) -> dict[str, list[tuple]]:
    queries = {
        "conversations": "SELECT id, type, name, membership_version"
        " FROM conversations ORDER BY id",
        "members": "SELECT conversation_id, username, deleted_at IS NULL"
        " FROM conversation_members ORDER BY 1, 2",
        "seats": "SELECT conversation_id, instance_id, remote_username, user_id"
        " FROM conversation_remote_members ORDER BY 1, 2, 3",
        "messages": "SELECT id, conversation_id, sender_user_id, content"
        " FROM conversation_messages ORDER BY id",
        "reactions": "SELECT message_id, user_id, emoji FROM message_reactions"
        " ORDER BY 1, 2, 3",
    }
    return {
        name: [tuple(r) for r in await db.fetchall(sql, ())]
        for name, sql in queries.items()
    }


def _dm(conv, msg_id, sender, content="hi") -> dict:
    return {
        "conversation_id": conv,
        "message_id": msg_id,
        "sender_user_id": sender,
        "content": content,
        "occurred_at": "2026-09-02T10:00:00+00:00",
        "recipient_user_ids": ["u-ula", "u-uwe"],
    }


# ─── DM_GROUP_ROSTER: only the authority seats ───────────────────────────


async def test_the_authority_roster_seats_the_group(env):
    app, db, own, _, _ = env
    conv = _group_id(AUTH)
    await _send(app, FET.DM_GROUP_ROSTER, _roster(conv, 1, own), from_instance=AUTH)
    state = await _state(db)
    assert state["conversations"] == [(conv, "group_dm", "Crew", 1)]
    assert state["members"] == [(conv, "ula", 1)]
    assert state["seats"] == [
        (conv, AUTH, "anna", "u-anna"),
        (conv, BOB, "bob", "u-bob"),
        (conv, CARL, "carl", "u-carl"),
    ]


@pytest.mark.parametrize(
    "sender",
    [
        pytest.param(BOB, id="a member household"),
        pytest.param(DORA, id="a paired non-member"),
        pytest.param(CARL, id="an unpaired member household"),
    ],
)
async def test_a_roster_from_anyone_but_the_authority_changes_nothing(env, sender):
    app, db, own, _, _ = env
    conv = _group_id(AUTH)
    before = await _state(db)
    await _send(app, FET.DM_GROUP_ROSTER, _roster(conv, 1, own), from_instance=sender)
    assert await _state(db) == before
    # …nor can it rewrite a group that exists here.
    await _send(app, FET.DM_GROUP_ROSTER, _roster(conv, 1, own), from_instance=AUTH)
    seated = await _state(db)
    evil = _roster(conv, 9, own, _member("u-dora", DORA, "dora"), name="Pwned")
    await _send(app, FET.DM_GROUP_ROSTER, evil, from_instance=sender)
    assert await _state(db) == seated


async def test_a_roster_for_a_legacy_id_is_refused(env):
    """A uuid4 id binds no authority, so nobody's roster counts."""
    app, db, own, _, _ = env
    before = await _state(db)
    legacy = "0123456789abcdef0123456789abcdef"
    await _send(app, FET.DM_GROUP_ROSTER, _roster(legacy, 1, own), from_instance=AUTH)
    assert await _state(db) == before


async def test_a_stale_or_replayed_roster_is_ignored(env):
    app, db, own, _, _ = env
    conv = _group_id(AUTH)
    await _send(app, FET.DM_GROUP_ROSTER, _roster(conv, 5, own), from_instance=AUTH)
    after_v5 = await _state(db)
    for version in (5, 4, 1):
        payload = _roster(conv, version, own, name="Old")
        payload["members"] = payload["members"][:2]
        await _send(app, FET.DM_GROUP_ROSTER, payload, from_instance=AUTH)
    assert await _state(db) == after_v5


async def test_a_roster_cannot_rehome_a_known_person(env):
    app, db, own, _, _ = env
    conv = _group_id(AUTH)
    payload = _roster(conv, 1, own)
    payload["members"] += [
        _member("u-uwe", CARL, "uwe"),  # our own user, claimed on CARL
        _member("u-dora", CARL, "dora"),  # DORA's user, claimed on CARL
        _member("u-nobody", own, "ghost"),  # a local user we don't have
    ]
    await _send(app, FET.DM_GROUP_ROSTER, payload, from_instance=AUTH)
    state = await _state(db)
    assert all(s[3] not in ("u-uwe", "u-dora") for s in state["seats"])
    assert state["members"] == [(conv, "ula", 1)]


async def test_a_new_group_without_a_local_member_is_refused(env):
    app, db, own, _, _ = env
    payload = _roster(_group_id(AUTH), 1, own)
    payload["members"] = [m for m in payload["members"] if m["instance_id"] != own]
    before = await _state(db)
    await _send(app, FET.DM_GROUP_ROSTER, payload, from_instance=AUTH)
    assert await _state(db) == before


async def test_a_roster_cannot_turn_a_1to1_into_a_group(env):
    app, db, own, _, _ = env
    conv = _group_id(AUTH)
    await db.enqueue("INSERT INTO conversations(id, type) VALUES(?, 'dm')", (conv,))
    before = await _state(db)
    await _send(app, FET.DM_GROUP_ROSTER, _roster(conv, 1, own), from_instance=AUTH)
    assert await _state(db) == before


# ─── Messages never seat; senders bind to their seat ─────────────────────


async def test_a_message_never_opens_a_group(env):
    """Even the authority: a group id arrives only with its roster."""
    app, db, _, _, _ = env
    before = await _state(db)
    for sender, user in ((AUTH, "u-anna"), (BOB, "u-bob")):
        await _send(
            app,
            FET.DM_MESSAGE,
            _dm(_group_id(AUTH), f"m-{user}", user),
            from_instance=sender,
        )
    assert await _state(db) == before


@pytest.fixture
async def group(env):
    app, db, own, sent, meshed = env
    conv = _group_id(AUTH)
    await _send(app, FET.DM_GROUP_ROSTER, _roster(conv, 1, own), from_instance=AUTH)
    return conv


@pytest.mark.parametrize(
    ("sender", "user"),
    [
        pytest.param(BOB, "u-carl", id="speaks for another member household"),
        pytest.param(CARL, "u-bob", id="unpaired household speaks for bob"),
        pytest.param(DORA, "u-dora", id="a non-member household"),
        pytest.param(BOB, "u-ula", id="speaks as a local member"),
        pytest.param(AUTH, "u-carl", id="authority speaks for a member live"),
    ],
)
async def test_a_group_message_outside_its_seat_changes_nothing(
    env, group, sender, user
):
    app, db, _, _, _ = env
    before = await _state(db)
    await _send(app, FET.DM_MESSAGE, _dm(group, "m-x", user), from_instance=sender)
    assert await _state(db) == before


async def test_every_seated_member_household_writes(env, group):
    """Including CARL, which we never paired with: its seat binds u-carl."""
    app, db, _, _, _ = env
    for sender, user in ((AUTH, "u-anna"), (BOB, "u-bob"), (CARL, "u-carl")):
        await _send(
            app, FET.DM_MESSAGE, _dm(group, f"m-{user}", user), from_instance=sender
        )
    messages = {m[0]: m for m in (await _state(db))["messages"]}
    assert set(messages) == {"m-u-anna", "m-u-bob", "m-u-carl"}


async def test_a_removed_members_household_is_refused(env, group):
    app, db, own, _, _ = env
    payload = _roster(group, 2, own)
    payload["members"] = [m for m in payload["members"] if m["user_id"] != "u-carl"]
    await _send(app, FET.DM_GROUP_ROSTER, payload, from_instance=AUTH)
    before = await _state(db)
    await _send(app, FET.DM_MESSAGE, _dm(group, "m-late", "u-carl"), from_instance=CARL)
    assert await _state(db) == before


async def test_an_unpaired_members_reaction_and_delete_bind_to_its_seat(env, group):
    app, db, _, _, _ = env
    await _send(app, FET.DM_MESSAGE, _dm(group, "m-c", "u-carl"), from_instance=CARL)
    reaction = {
        "conversation_id": group,
        "message_id": "m-c",
        "user_id": "u-carl",
        "emoji": "👍",
        "action": "add",
    }
    await _send(app, FET.DM_MESSAGE_REACTION, reaction, from_instance=BOB)
    assert (await _state(db))["reactions"] == []
    await _send(app, FET.DM_MESSAGE_REACTION, reaction, from_instance=CARL)
    assert (await _state(db))["reactions"] == [("m-c", "u-carl", "👍")]
    delete = {"conversation_id": group, "message_id": "m-c"}
    await _send(app, FET.DM_MESSAGE_DELETED, delete, from_instance=BOB)
    assert ("m-c", group, "u-carl", "hi") in (await _state(db))["messages"]
    await _send(app, FET.DM_MESSAGE_DELETED, delete, from_instance=CARL)
    assert ("m-c", group, "u-carl", "") in (await _state(db))["messages"]


async def test_a_member_household_we_never_paired_with_is_reached_sealed(env, group):
    """Our member's message goes direct to paired members, over the
    E2E-sealed mesh to CARL — and to no household outside the group."""
    app, _, _, sent, meshed = env
    sent.clear()
    await app[dm_service_key].send_message(
        group, sender_username="ula", content="hi all"
    )
    assert _targets(sent, FET.DM_MESSAGE) == {AUTH, BOB}
    assert _targets(meshed, FET.DM_MESSAGE) == {CARL}
    assert DORA not in {inst for inst, _, _ in sent + meshed}


async def test_an_unpaired_members_typing_names_its_seat(env, group, monkeypatch):
    app, _, _, _, _ = env
    typing = app[federation_service_key]._typing_service
    frames: list[tuple[list[str], dict]] = []

    async def _record(_self, user_ids, frame):
        frames.append((list(user_ids), frame))
        return len(user_ids)

    monkeypatch.setattr(type(typing._ws), "broadcast_to_users", _record)
    payload = {"conversation_id": group, "sender_user_id": "u-carl"}
    await _send(app, FET.DM_USER_TYPING, payload, from_instance=BOB)
    assert frames == []
    await _send(app, FET.DM_USER_TYPING, payload, from_instance=CARL)
    assert [(t, f["sender_username"]) for t, f in frames] == [(["u-ula"], "carl")]


# ─── History: only the authority relays other members' rows ───────────────


def _chunk(conv, *rows: tuple[str, str]) -> dict:
    return {
        "conversation_id": conv,
        "chunk_index": 0,
        "is_last": True,
        "messages": [
            {
                "id": msg_id,
                "sender_user_id": sender,
                "content": "old",
                "type": "text",
                "created_at": "2026-09-01T10:00:00+00:00",
            }
            for msg_id, sender in rows
        ],
    }


async def test_the_authority_hands_over_other_members_history(env, group):
    app, db, _, _, _ = env
    await _send(
        app,
        FET.DM_HISTORY_CHUNK,
        _chunk(group, ("h-carl", "u-carl"), ("h-bob", "u-bob"), ("h-ula", "u-ula")),
        from_instance=AUTH,
    )
    ids = {m[0] for m in (await _state(db))["messages"]}
    assert ids == {"h-carl", "h-bob"}  # never a row claimed for our own user


async def test_a_member_household_cannot_relay_another_members_history(env, group):
    app, db, _, _, _ = env
    before = await _state(db)
    await _send(
        app,
        FET.DM_HISTORY_CHUNK,
        _chunk(group, ("h-carl", "u-carl")),
        from_instance=BOB,
    )
    assert await _state(db) == before


# ─── DM_GROUP_LEAVE, and delivery: members only ──────────────────────────


@pytest.fixture
async def own_group(env):
    """A group we are the authority of: ula + uwe here, bob, anna."""
    app, db, own, sent, meshed = env
    svc = app[dm_service_key]
    conv = await svc.create_group_dm(
        creator_username="ula",
        member_usernames=["uwe"],
        member_user_ids=["u-bob", "u-anna"],
        name="Ours",
    )
    return conv.id


def _targets(sent, event_type) -> set[str]:
    return {inst for inst, et, _ in sent if et is event_type}


async def test_group_content_reaches_member_households_only(env, own_group):
    app, _, _, sent, meshed = env
    assert _targets(sent, FET.DM_GROUP_ROSTER) == {BOB, AUTH}
    sent.clear()
    await app[dm_service_key].send_message(
        own_group, sender_username="ula", content="hello group"
    )
    assert _targets(sent, FET.DM_MESSAGE) == {BOB, AUTH}
    assert DORA not in {inst for inst, _, _ in sent + meshed}


async def test_a_removed_household_gets_the_final_roster_then_nothing(env, own_group):
    app, db, _, sent, meshed = env
    svc = app[dm_service_key]
    sent.clear()
    await svc.remove_group_member(own_group, actor_username="ula", user_id="u-bob")
    rosters = {i: p for i, et, p in sent if et is FET.DM_GROUP_ROSTER}
    assert set(rosters) == {BOB, AUTH}
    # The removed household learns only that it is out — not who stays.
    assert rosters[BOB]["members"] == []
    assert rosters[BOB]["version"] == 2
    assert "u-bob" not in {m["user_id"] for m in rosters[AUTH]["members"]}
    sent.clear()
    await svc.send_message(own_group, sender_username="ula", content="after")
    assert _targets(sent, FET.DM_MESSAGE) == {AUTH}


@pytest.mark.parametrize(
    ("sender", "user"),
    [
        pytest.param(BOB, "u-anna", id="leaves for another household's user"),
        pytest.param(DORA, "u-dora", id="a non-member household"),
        pytest.param(BOB, "u-ula", id="leaves for a local member"),
    ],
)
async def test_a_leave_outside_its_seat_changes_nothing(env, own_group, sender, user):
    app, db, _, sent, _ = env
    before = await _state(db)
    sent.clear()
    await _send(
        app,
        FET.DM_GROUP_LEAVE,
        {"conversation_id": own_group, "user_id": user},
        from_instance=sender,
    )
    assert await _state(db) == before
    assert sent == []


async def test_a_member_household_leaves_for_its_own_user(env, own_group):
    app, db, _, sent, _ = env
    sent.clear()
    await _send(
        app,
        FET.DM_GROUP_LEAVE,
        {"conversation_id": own_group, "user_id": "u-bob"},
        from_instance=BOB,
    )
    state = await _state(db)
    assert (own_group, BOB, "bob", "u-bob") not in state["seats"]
    assert _targets(sent, FET.DM_GROUP_ROSTER) == {BOB, AUTH}


async def test_a_leave_sent_to_a_non_authority_changes_nothing(env, group):
    app, db, _, sent, _ = env
    before = await _state(db)
    await _send(
        app,
        FET.DM_GROUP_LEAVE,
        {"conversation_id": group, "user_id": "u-bob"},
        from_instance=BOB,
    )
    assert await _state(db) == before


# ─── Review hardening ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sql", "stranger"),
    [
        pytest.param(None, "peer-stranger", id="a mesh stranger we never paired"),
        pytest.param(
            "UPDATE remote_instances SET source='space_session' WHERE id=?",
            DORA,
            id="an invite-link household we only share a space with",
        ),
        pytest.param(
            "UPDATE remote_instances SET status='unpairing' WHERE id=?",
            DORA,
            id="a household we are unpairing from",
        ),
    ],
)
async def test_only_a_paired_household_can_be_a_groups_authority(env, sql, stranger):
    """An id bound to yourself is easy to mint; seating our people in it
    still needs a household we chose to pair with."""
    app, db, own, _, _ = env
    if sql:
        await db.enqueue(sql, (stranger,))
    before = await _state(db)
    payload = _roster(_group_id(stranger), 1, own)
    await _send(app, FET.DM_GROUP_ROSTER, payload, from_instance=stranger)
    assert await _state(db) == before


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(True, id="a bool"),
        pytest.param(0, id="zero"),
        pytest.param(2**63, id="beyond SQLite"),
        pytest.param("7", id="a string"),
    ],
)
async def test_a_malformed_roster_version_is_refused(env, version):
    app, db, own, _, _ = env
    before = await _state(db)
    payload = _roster(_group_id(AUTH), 1, own)
    payload["version"] = version
    await _send(app, FET.DM_GROUP_ROSTER, payload, from_instance=AUTH)
    assert await _state(db) == before


async def test_an_authority_user_not_synced_yet_is_held_then_seated(env):
    """The authority can't seat a user_id on itself we haven't seen — the
    roster waits for that user's own sync (then the same rules apply)."""
    app, db, own, _, _ = env
    conv = _group_id(AUTH)
    user_id = derive_user_id(bytes(32), "nell")
    payload = _roster(conv, 1, own, _member(user_id, AUTH, "nell"))
    before = await _state(db)
    await _send(app, FET.DM_GROUP_ROSTER, payload, from_instance=AUTH)
    assert await _state(db) == before
    await _send(
        app,
        FET.USER_UPDATED,
        {"user_id": user_id, "username": "nell", "display_name": "Nell"},
        from_instance=AUTH,
    )
    seats = (await _state(db))["seats"]
    assert (conv, AUTH, "nell", user_id) in seats


async def test_an_authority_history_row_never_overwrites_a_members_own(env, group):
    """A relayed row fills a gap only — never rewrites, un-deletes or
    rolls back what the member's own household delivered."""
    app, db, _, _, _ = env
    await _send(
        app, FET.DM_MESSAGE, _dm(group, "m-c", "u-carl", "v2"), from_instance=CARL
    )
    await _send(
        app,
        FET.DM_MESSAGE_DELETED,
        {"conversation_id": group, "message_id": "m-c"},
        from_instance=CARL,
    )
    before = await _state(db)
    await _send(
        app,
        FET.DM_HISTORY_CHUNK,
        _chunk(group, ("m-c", "u-carl")),
        from_instance=AUTH,
    )
    assert await _state(db) == before


async def test_a_departed_member_leaves_the_group_standing(env, own_group):
    """``USER_REMOVED`` from a member's household clears their messages and
    (here, on the authority) their seat — never the whole group."""
    app, db, _, sent, _ = env
    svc = app[dm_service_key]
    await svc.send_message(own_group, sender_username="ula", content="stays")
    await _send(
        app, FET.DM_MESSAGE, _dm(own_group, "m-b", "u-bob", "bye"), from_instance=BOB
    )
    sent.clear()
    await _send(app, FET.USER_REMOVED, {"user_id": "u-bob"}, from_instance=BOB)
    state = await _state(db)
    assert any(c[0] == own_group for c in state["conversations"])
    assert all(s[3] != "u-bob" for s in state["seats"] if s[0] == own_group)
    texts = {m[3] for m in state["messages"] if m[1] == own_group}
    assert "stays" in texts and "bye" not in texts
    assert _targets(sent, FET.DM_GROUP_ROSTER) == {AUTH, BOB}


async def test_a_member_household_we_share_only_a_space_with_is_not_reached(env, group):
    """Its "direct" channel is the connection-server relay, and there is no
    social pairing — the group message is not sent to it at all."""
    app, db, _, sent, meshed = env
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            CARL,
            CARL,
            "00" * 32,
            "k1",
            "k2",
            "https://c/wh",
            "wh-c",
            "confirmed",
            "space_session",
        ),
    )
    sent.clear()
    await app[dm_service_key].send_message(group, sender_username="ula", content="x")
    assert CARL not in {inst for inst, _, _ in sent + meshed}
