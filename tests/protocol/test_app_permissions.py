"""Release-blocker protocol tests: an app only gets the permissions the household agreed to.

Marked ``@pytest.mark.security``.

The household agrees to an app's permissions when an admin installs it;
the installed manifest is that agreement. The rule these tests encode,
against the real application registry, SQLite and federation service:

    An app without ``federation`` sends nothing to another household — no
    invite, no move, no withdrawal, no decline — and another household's
    copy of the app can't reach it either. ``people`` without
    ``federation`` still plays inside the household. Chess (``storage`` +
    ``federation``) keeps everything it had.
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
    instance_id_key,
)
from socialhome.config import Config
from socialhome.domain.apps import (
    AppManifest,
    AppPermissionDeniedError,
    InstalledApp,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.repositories.app_repo import SqliteAppRepo

pytestmark = pytest.mark.security

PEER = "peer-alice"
REMOTE = {"instance_id": PEER, "user_ref": "alice", "is_local": False}


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


def _app(app_id: str, caps: tuple[str, ...]) -> InstalledApp:
    return InstalledApp(
        app_id=app_id,
        name=app_id.title(),
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=caps),
        bundle_path=f"apps/{app_id}/1.0.0",
        bundle_sha256="ab" * 32,
        source_url=f"https://example/{app_id}.tgz",
        installed_by=None,
        installed_at="2026-10-29T00:00:00+00:00",
    )


#: Everything the federation service was asked to send.
sent: list[tuple[str, str]] = []
#: Local users an ``app.message`` frame was delivered to.
frames: list[str] = []


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for username, user_id in (("bob", "u-bob"), ("carl", "u-carl")):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, user_id, username.title()),
        )
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            PEER,
            PEER,
            "00" * 32,
            "k1",
            "k2",
            f"https://{PEER}/wh",
            "wh-a",
            "confirmed",
            "manual",
            33,
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        ("u-alice", PEER, "alice", "Alice"),
    )
    repo = SqliteAppRepo(db)
    # A family quiz: plays with people at home, never leaves the household.
    await repo.install(_app("quiz", ("storage", "people")))
    # Sudoku: saves its own data, nothing else.
    await repo.install(_app("sudoku", ("storage",)))
    # Chess as published: storage + federation (people implied).
    await repo.install(_app("chess", ("storage", "federation")))

    sent.clear()
    frames.clear()

    async def _send_event(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type.value))

    async def _send_app_message(_self, *, to_instance_id, app_id, **_kw):
        sent.append((to_instance_id, f"app_message:{app_id}"))

    async def _to_user(_self, user_id, payload):
        frames.append(user_id)
        return 1

    async def _to_users(_self, user_ids, payload):
        frames.extend(user_ids)
        return len(user_ids)

    monkeypatch.setattr(FederationService, "send_event", _send_event)
    monkeypatch.setattr(FederationService, "send_app_message", _send_app_message)
    monkeypatch.setattr(WebSocketManager, "broadcast_to_user", _to_user)
    monkeypatch.setattr(WebSocketManager, "broadcast_to_users", _to_users)
    return app, db


@pytest.mark.parametrize("app_id", ["quiz", "sudoku"])
async def test_an_app_without_federation_cannot_invite_another_household(env, app_id):
    app, _ = env
    svc = app[app_federation_service_key]
    with pytest.raises(AppPermissionDeniedError):
        await svc.open_session(app_id=app_id, target=REMOTE, actor_user_id="u-bob")
    assert sent == []


@pytest.mark.parametrize("app_id", ["quiz", "sudoku"])
async def test_an_app_without_federation_cannot_send_to_another_household(env, app_id):
    app, _ = env
    svc = app[app_federation_service_key]
    with pytest.raises(AppPermissionDeniedError) as ei:
        await svc.send_message(
            app_id=app_id,
            target=REMOTE,
            session_id="s1",
            payload={"anything": "it stored"},
            actor_user_id="u-bob",
        )
    assert ei.value.permission == "federation"
    with pytest.raises(AppPermissionDeniedError):
        await svc.cancel_session(
            app_id=app_id, session_id="s1", target=REMOTE, actor_user_id="u-bob"
        )
    assert sent == []


async def test_another_household_cannot_reach_an_app_without_federation(env):
    app, db = env
    handlers = app[federation_service_key]._event_registry.handlers_for(
        FederationEventType.APP_SESSION
    )
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m-quiz",
                event_type=FederationEventType.APP_SESSION,
                from_instance=PEER,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload={
                    "app_id": "quiz",
                    "session_id": "s1",
                    "verb": "open",
                    "to_user": "bob",
                    "from_user": "alice",
                },
            )
        )
    await app[app_federation_service_key].on_inbound_message(
        PEER, "quiz", "s1", {"move": 1}, to_user="bob", from_user="alice"
    )
    assert frames == []
    assert await db.fetchall("SELECT * FROM app_pending_sessions", ()) == []


async def test_people_without_federation_still_plays_at_home(env):
    app, _ = env
    svc = app[app_federation_service_key]
    local = {
        "instance_id": app[instance_id_key],
        "user_ref": "u-carl",
        "is_local": True,
    }
    session_id = await svc.open_session(
        app_id="quiz", target=local, actor_user_id="u-bob"
    )
    await svc.send_message(
        app_id="quiz",
        target=local,
        session_id=session_id,
        payload={"q": 1},
        actor_user_id="u-bob",
    )
    assert sent == []
    assert "u-carl" in frames


async def test_storage_only_cannot_even_pick_people_at_home(env):
    app, _ = env
    local = {
        "instance_id": app[instance_id_key],
        "user_ref": "u-carl",
        "is_local": True,
    }
    with pytest.raises(AppPermissionDeniedError) as ei:
        await app[app_federation_service_key].open_session(
            app_id="sudoku", target=local, actor_user_id="u-bob"
        )
    assert ei.value.permission == "people"


async def test_chess_still_sends_to_another_household(env):
    app, _ = env
    svc = app[app_federation_service_key]
    await svc.open_session(app_id="chess", target=REMOTE, actor_user_id="u-bob")
    assert sent == [(PEER, "app_session")]
