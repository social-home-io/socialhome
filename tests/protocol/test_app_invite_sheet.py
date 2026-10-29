"""Release-blocker protocol tests: invites go through Social Home, never straight from an app.

Marked ``@pytest.mark.security``.

Owner decisions D3 and D5 (Apps R3 PR 9). The rule these tests encode,
against the real routes, application registry, SQLite and federation
service:

    An app learns only who the person picked in Social Home's invite sheet
    and who they play with in that app — never the roster. It can message
    only the games the household knows for that person and that very
    opponent (opened through the sheet, or taken in with Play): a session
    id it makes up, or an old opponent on a new id, is refused before
    anything is sent. One person sends at most 10 unanswered invites and
    30 a day per app. A protected account's sheet lists only people it
    can already message, and nothing outside them reaches it.

The app's bridge (``client/src/features/apps/bridge.ts``) offers no way to
open a session at all: ``POST /sessions`` is only called by the sheet's
Send. That half of the boundary is pinned in ``bridge.test.ts`` and
``InviteSheet.test.tsx``; here the server refuses whatever an app could
still try through the routes it can reach.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    app_federation_service_key,
    db_key,
    federation_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.apps import AppManifest, InstalledApp
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.repositories.app_repo import SqliteAppRepo

pytestmark = pytest.mark.security

PEER = "peer-alice"  # paired directly (QR)
SPACE_ONLY = "peer-eve"  # met only through a shared space
ALICE = {"instance_id": PEER, "user_ref": "alice", "is_local": False}
EVE = {"instance_id": SPACE_ONLY, "user_ref": "eve", "is_local": False}
BASE = "/api/apps/chess"

#: Everything the federation service was asked to send.
sent: list[tuple[str, str]] = []
#: Local users an ``app.message`` frame was delivered to.
frames: list[str] = []


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "apps.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


def _auth(name: str) -> dict:
    return {"Authorization": f"Bearer {name}-tok"}


async def _peer(db, instance_id: str, source: str, uid: str, username: str) -> None:
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
            source,
            33,
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (uid, instance_id, username, username.title()),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    client = await aiohttp_client(app)
    db = app[db_key]
    for name in ("bob", "carl"):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (name, f"u-{name}", name.title()),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            (f"t-{name}", f"u-{name}", "web", sha256_token_hash(f"{name}-tok")),
        )
    await _peer(db, PEER, "manual", "u-alice", "alice")
    await _peer(db, SPACE_ONLY, "space_session", "u-eve", "eve")
    await SqliteAppRepo(db).install(
        InstalledApp(
            app_id="chess",
            name="Chess",
            version="1.0.0",
            enabled=True,
            manifest=AppManifest(
                entry="index.html", icon=None, capabilities=("storage", "federation")
            ),
            bundle_path="apps/chess/1.0.0",
            bundle_sha256="ab" * 32,
            source_url="https://example/chess.tgz",
            installed_by=None,
            installed_at="2026-10-29T00:00:00+00:00",
        )
    )
    sent.clear()
    frames.clear()

    async def _send_event(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type.value))

    async def _send_app_message(_self, *, to_instance_id, app_id, **_kw):
        sent.append((to_instance_id, f"app_message:{app_id}"))

    async def _to_user(_self, user_id, payload):
        # Only what reaches an app (not the bell's own frames).
        if isinstance(payload, dict) and payload.get("type") == "app.message":
            frames.append(user_id)
        return 1

    monkeypatch.setattr(FederationService, "send_event", _send_event)
    monkeypatch.setattr(FederationService, "send_app_message", _send_app_message)
    monkeypatch.setattr(WebSocketManager, "broadcast_to_user", _to_user)
    return app, client, db


async def _contacts(client, who: str = "bob") -> set[str]:
    r = await client.get(f"{BASE}/contacts", headers=_auth(who))
    assert r.status == 200
    return {c["user_ref"] for c in (await r.json())["contacts"]}


async def _invitable(client, who: str = "bob") -> set[str]:
    r = await client.get(f"{BASE}/invitable", headers=_auth(who))
    assert r.status == 200
    return {c["user_ref"] for c in (await r.json())["people"]}


async def _invite(client, target: dict, who: str = "bob"):
    """The invite sheet's Send."""
    return await client.post(
        f"{BASE}/sessions", json={"target": target}, headers=_auth(who)
    )


async def _send(client, session_id: str, target: dict, who: str = "bob"):
    """What ``app.send`` posts through the bridge."""
    return await client.post(
        f"{BASE}/messages",
        json={"session_id": session_id, "target": target, "payload": {"m": 1}},
        headers=_auth(who),
    )


async def test_an_app_sees_only_who_was_picked_never_the_roster(env):
    _, client, _ = env
    # Bob knows Carl (home) and Alice (paired): the sheet lists them…
    assert await _invitable(client) >= {"u-carl", "alice"}
    # …but the app's own read of people starts empty.
    assert await _contacts(client) == set()
    r = await _invite(client, ALICE)
    assert r.status == 201
    assert await _contacts(client) == {"alice"}
    assert sent == [(PEER, "app_session")]


async def test_an_app_cannot_message_a_session_nobody_opened(env):
    """Real hole 2: a made-up session id to an old opponent is refused
    before anything leaves — and the person doesn't become a contact."""
    _, client, _ = env
    sid = (await (await _invite(client, ALICE)).json())["session_id"]
    assert (await _send(client, sid, ALICE)).status == 200  # the real game
    sent.clear()
    r = await _send(client, "brand-new-session", ALICE)
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "APP_SESSION_UNKNOWN"
    r = await _send(client, "another", {**ALICE, "user_ref": "u-carl"})
    assert r.status == 403
    # Carl is in the household but never picked: no session, no frame.
    r = await _send(
        client, "x", {"instance_id": "", "user_ref": "u-carl", "is_local": True}
    )
    assert r.status in (400, 403)
    assert sent == [] and frames == []


async def test_an_app_cannot_turn_a_game_to_someone_else(env):
    app, client, _ = env
    own = app[federation_service_key].own_instance_id
    carl = {"instance_id": own, "user_ref": "u-carl", "is_local": True}
    sid = (await (await _invite(client, ALICE)).json())["session_id"]
    sent.clear()
    frames.clear()
    r = await _send(client, sid, carl)
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "APP_SESSION_UNKNOWN"
    assert sent == [] and frames == []


async def test_an_invite_received_is_not_the_apps_until_play(env):
    """An inviter waiting in Needs you is not a contact (the app learns of
    them on Play), and the app can't answer them before Play either way
    but by playing it."""
    app, client, _ = env
    handlers = app[federation_service_key]._event_registry.handlers_for(
        FederationEventType.APP_SESSION
    )
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m-1",
                event_type=FederationEventType.APP_SESSION,
                from_instance=PEER,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload={
                    "app_id": "chess",
                    "session_id": "from-alice",
                    "verb": "open",
                    "to_user": "bob",
                    "from_user": "alice",
                },
            )
        )
    assert await _contacts(client) == set()
    r = await client.post(f"{BASE}/sessions/from-alice/accept", headers=_auth("bob"))
    assert r.status == 200
    assert await _contacts(client) == {"alice"}
    assert (await _send(client, "from-alice", ALICE)).status == 200


async def test_at_most_ten_open_invites_then_a_coded_refusal(env):
    _, client, _ = env
    for _ in range(10):
        assert (await _invite(client, ALICE)).status == 201
    r = await _invite(client, ALICE)
    assert r.status == 429
    err = (await r.json())["error"]
    assert err["code"] == "APP_INVITES_OPEN_LIMIT"
    assert err["params"] == {"limit": 10}
    assert len(sent) == 10
    r = await client.get(f"{BASE}/invitable", headers=_auth("bob"))
    assert (await r.json())["invites"]["open"] == 10
    # Per person: Carl still can.
    assert (await _invite(client, ALICE, who="carl")).status == 201


async def test_a_household_addressed_body_names_nobody(env):
    _, client, _ = env
    r = await client.post(
        f"{BASE}/sessions",
        json={"peer_instance_id": PEER},
        headers=_auth("bob"),
    )
    assert r.status == 400
    r = await client.post(
        f"{BASE}/messages",
        json={"session_id": "s", "peer_instance_id": PEER, "payload": {}},
        headers=_auth("bob"),
    )
    assert r.status == 400
    assert sent == []


async def test_a_protected_childs_picker_lists_only_people_it_can_message(env):
    """D5: Bob is protected. Eve's household he only shares a space with
    (no direct pairing) — he can't DM her, so she isn't in his sheet, an
    invite to her is refused and her invite never reaches him. Carl (an
    adult) still sees her."""
    app, client, db = env
    await db.enqueue(
        "UPDATE users SET child_protection_enabled=1, is_minor=1, declared_age=12"
        " WHERE user_id='u-bob'",
        (),
    )
    assert "eve" not in await _invitable(client)
    assert {"u-carl", "alice"} <= await _invitable(client)
    assert "eve" in await _invitable(client, who="carl")
    r = await _invite(client, EVE)
    assert r.status == 403
    assert sent == []
    handlers = app[federation_service_key]._event_registry.handlers_for(
        FederationEventType.APP_SESSION
    )
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m-eve",
                event_type=FederationEventType.APP_SESSION,
                from_instance=SPACE_ONLY,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload={
                    "app_id": "chess",
                    "session_id": "from-eve",
                    "verb": "open",
                    "to_user": "bob",
                    "from_user": "eve",
                },
            )
        )
    assert frames == []
    rows = await db.fetchall("SELECT session_id FROM app_pending_sessions", ())
    assert [r["session_id"] for r in rows] == []
    svc = app[app_federation_service_key]
    assert await svc.list_contacts(app_id="chess", self_user_id="u-bob") == []


# ── Continue game (owner decision 2026-10-29) ────────────────────────────


async def _continue(client, sid: str, target: dict, who: str = "bob"):
    """The sheet's "Continue game with …"."""
    return await client.post(
        f"{BASE}/sessions/{sid}/continue", json={"target": target}, headers=_auth(who)
    )


async def _inbound(
    app, sender: str, payload: dict, kind=FederationEventType.APP_SESSION
):
    handlers = app[federation_service_key]._event_registry.handlers_for(kind)
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id=f"m-{sender}-{payload.get('session_id')}-{payload.get('verb')}",
                event_type=kind,
                from_instance=sender,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload={"app_id": "chess", "to_user": "bob", **payload},
            )
        )


async def _row(db, sid: str):
    return await db.fetchone(
        "SELECT state, from_instance, from_user FROM app_pending_sessions"
        " WHERE user_id='u-bob' AND session_id=?",
        (sid,),
    )


async def test_continue_picks_an_unknown_game_up_through_the_sheet_only(env):
    _, client, db = env
    # The app alone can't: the game is unknown.
    assert (await _send(client, "old-game", ALICE)).status == 403
    r = await _continue(client, "old-game", ALICE)
    assert r.status == 200 and (await r.json()) == {"session_id": "old-game"}
    assert sent == [(PEER, "app_session")]
    assert tuple(await _row(db, "old-game")) == ("active", PEER, "alice")
    assert (await _send(client, "old-game", ALICE)).status == 200
    # A bad id never reaches the service.
    assert (await _continue(client, "bad.id", ALICE)).status == 400


async def test_continue_never_moves_a_known_session_to_someone_else(env):
    app, client, db = env
    own = app[federation_service_key].own_instance_id
    carl = {"instance_id": own, "user_ref": "u-carl", "is_local": True}
    sid = (await (await _invite(client, ALICE)).json())["session_id"]
    r = await _continue(client, sid, carl)
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "APP_SESSION_TAKEN"
    assert tuple(await _row(db, sid)) == ("active", PEER, "alice")


async def test_a_third_party_cannot_hijack_a_game_with_a_continue(env):
    """Eve (another paired household) sends ``continue`` for a session id
    Bob plays with Alice, and for one where Alice's invite waits: both are
    dropped — the rows still name Alice, nothing reaches Bob, no bell."""
    app, client, db = env
    sid = (await (await _invite(client, ALICE)).json())["session_id"]
    await _inbound(
        app, SPACE_ONLY, {"session_id": sid, "verb": "continue", "from_user": "eve"}
    )
    await _inbound(
        app, PEER, {"session_id": "waiting", "verb": "open", "from_user": "alice"}
    )
    await _inbound(
        app,
        SPACE_ONLY,
        {"session_id": "waiting", "verb": "continue", "from_user": "eve"},
    )
    assert tuple(await _row(db, sid)) == ("active", PEER, "alice")
    assert tuple(await _row(db, "waiting")) == ("invited", PEER, "alice")
    assert frames == []
    rows = await db.fetchall("SELECT type FROM notifications WHERE user_id='u-bob'", ())
    assert "app_challenge_continue" not in {r["type"] for r in rows}


async def test_an_inbound_continue_takes_only_the_continuers_moves(env):
    """Alice continues an old game with Bob: it waits in Needs you; her
    moves are held, Eve's on the same id never are (nor reach Bob); after
    Play only Alice's arrive and Bob can answer her."""
    app, client, db = env
    await _inbound(
        app, PEER, {"session_id": "g", "verb": "continue", "from_user": "alice"}
    )
    assert tuple(await _row(db, "g")) == ("invited", PEER, "alice")
    for sender, user in ((PEER, "alice"), (SPACE_ONLY, "eve")):
        await _inbound(
            app,
            sender,
            {"session_id": "g", "from_user": user, "data": {"by": user}},
            kind=FederationEventType.APP_MESSAGE,
        )
    assert frames == []
    rows = await db.fetchall("SELECT type FROM notifications WHERE user_id='u-bob'", ())
    assert "app_challenge_continue" in {r["type"] for r in rows}
    r = await client.post(f"{BASE}/sessions/g/accept", headers=_auth("bob"))
    assert r.status == 200
    held = (await (await client.post(f"{BASE}/held", headers=_auth("bob"))).json())[
        "messages"
    ]
    assert [m["payload"] for m in held] == [{"by": "alice"}]
    assert (await _send(client, "g", ALICE)).status == 200
