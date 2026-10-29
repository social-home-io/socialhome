"""Release-blocker protocol tests: an invite for an app a household doesn't
have tells the sender nothing (D10).

Marked ``@pytest.mark.security``.

R2's automatic "unavailable" reply turned every household into an oracle:
open a session for app X to anyone and watch for the answer — which apps a
household has, and (person by person) who is age-limited. The rule these
tests encode, against the real wiring and SQLite:

    An ``APP_SESSION`` open for an app the household doesn't have, has
    turned off, or keeps from the addressee for their age sends **nothing**
    back — no reply, no error, no read receipt, on any transport. The
    addressee gets a waiting "Needs you" row (named from the household's
    own catalog) unless the age gate hides the app from them, a block
    separates them, or the sender isn't a user of the sending household;
    only the household and person that sent the invite can withdraw it; and
    declining it, or the app arriving, sends nothing either.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    app_federation_service_key,
    db_key,
    event_bus_key,
    federation_service_key,
    notification_delivery_key,
)
from socialhome.config import Config
from socialhome.domain.apps import AppCatalogEntry, AppManifest, InstalledApp
from socialhome.domain.events import AppEnabledChanged
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.repositories.app_repo import SqliteAppRepo
from socialhome.services.app_service import AppService

pytestmark = pytest.mark.security

FET = FederationEventType
PEER = "peer-lakeside"
OTHER = "peer-eve"

#: Every outbound the federation service could make, recorded.
sent: list[str] = []
#: Local users an ``app.message`` frame reached (nothing reaches an app
#: before Play).
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


def _entry(app_id: str, min_age: int = 0) -> AppCatalogEntry:
    return AppCatalogEntry.from_dict(
        {
            "schema": 2,
            "app_id": app_id,
            "name": app_id.capitalize(),
            "latest_version": "2026.10.29",
            "description": "A game",
            "capabilities": ["storage", "federation"],
            "bundle_url": f"https://example.com/{app_id}.tgz",
            "bundle_sha256": "ab" * 32,
            "permissions": ["storage", "people", "federation"],
            "min_age": min_age,
        }
    )


async def _seed_peer(db, instance_id: str, user_id: str, username: str) -> None:
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
            70,
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, username, username),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    """Bob (adult) and Kiki (protected, 9) here; Ben at Lakeside, Eve at
    another household. The catalog lists chess (all ages) and quiz (13+);
    sketch is installed but turned off; nothing else is installed."""
    monkeypatch.setattr(
        AppService,
        "browse_catalog",
        AsyncMock(return_value=[_entry("chess"), _entry("quiz", min_age=13)]),
    )
    sent.clear()
    frames.clear()
    for name in (
        "send_event",
        "send_event_once",
        "send_event_via_gfs",
        "send_app_message",
    ):

        async def _record(_self, *a, _name=name, **kw):
            sent.append(_name)

        monkeypatch.setattr(FederationService, name, _record)

    async def _to_user(_self, user_id, payload):
        if payload.get("type") == "app.message":
            frames.append(user_id)
        return 1

    async def _to_users(_self, user_ids, payload):
        if payload.get("type") == "app.message":
            frames.extend(user_ids)
        return len(user_ids)

    monkeypatch.setattr(WebSocketManager, "broadcast_to_user", _to_user)
    monkeypatch.setattr(WebSocketManager, "broadcast_to_users", _to_users)

    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "u-bob", "Bob"),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name,"
        " child_protection_enabled, is_minor, declared_age) VALUES(?,?,?,1,1,9)",
        ("kiki", "u-kiki", "Kiki"),
    )
    await _seed_peer(db, PEER, "u-ben", "ben")
    await _seed_peer(db, OTHER, "u-eve", "eve")
    await SqliteAppRepo(db).install(
        InstalledApp(
            app_id="sketch",
            name="Sketch",
            version="1.0.0",
            enabled=False,
            manifest=AppManifest(
                entry="index.html", icon=None, capabilities=("storage", "federation")
            ),
            bundle_path="apps/sketch/1.0.0",
            bundle_sha256="ab" * 32,
            source_url="https://example/sketch.tgz",
            installed_by=None,
            installed_at="2026-06-02T00:00:00+00:00",
        )
    )
    return app, db


async def _session(
    app,
    *,
    app_id: str = "chess",
    to_user: str = "bob",
    sender: str = PEER,
    from_user: str = "ben",
    session_id: str = "s1",
    verb: str = "open",
) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(FET.APP_SESSION)
    assert handlers
    payload = {
        "app_id": app_id,
        "session_id": session_id,
        "verb": verb,
        "to_user": to_user,
        "from_user": from_user,
    }
    if verb == "close":
        payload["reason"] = "withdrawn"
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id=f"m-{sender}-{session_id}-{verb}",
                event_type=FET.APP_SESSION,
                from_instance=sender,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload=payload,
            )
        )
    await app[notification_delivery_key].drain()


async def _waiting(db) -> list[tuple]:
    rows = await db.fetchall(
        "SELECT user_id, object_key, json_extract(params, '$.flags[0]'),"
        " json_extract(params, '$.handled.outcome')"
        " FROM notifications WHERE type = 'app_challenge_waiting' ORDER BY 2",
        (),
    )
    return [tuple(r) for r in rows]


async def test_an_invite_for_a_missing_or_turned_off_app_answers_nothing(env):
    """The oracle is closed: the sender's household hears nothing at all,
    and the person here gets the invite instead."""
    app, db = env
    await _session(app, app_id="chess", session_id="s1")
    await _session(app, app_id="sketch", session_id="s2")
    assert sent == []
    assert frames == []
    assert await _waiting(db) == [
        ("u-bob", "app_session:chess/s1", "app_missing", None),
        ("u-bob", "app_session:sketch/s2", "app_off", None),
    ]


async def test_an_age_gated_invitee_gets_nothing_and_nothing_answers(env):
    """D5: an app above a protected account's age stays hidden — no row
    for the child (or a guardian), and the same silence for the sender as
    for an adult, so the age limit can't be probed."""
    app, db = env
    await _session(app, app_id="quiz", to_user="kiki", session_id="k1")
    await _session(app, app_id="quiz", to_user="bob", session_id="b1")
    assert sent == []
    assert await _waiting(db) == [("u-bob", "app_session:quiz/b1", "app_missing", None)]


async def test_an_app_the_catalog_doesnt_list_leaves_nothing(env):
    app, db = env
    await _session(app, app_id="unknown", session_id="u1")
    assert sent == []
    assert await _waiting(db) == []


async def test_a_waiting_invite_must_name_a_user_of_the_sender(env):
    app, db = env
    await _session(app, from_user="eve")  # Eve is not Lakeside's
    await _session(app, from_user="bob", session_id="s2")  # a local name
    assert sent == []
    assert await _waiting(db) == []


async def test_only_the_sender_withdraws_a_waiting_invite(env):
    app, db = env
    await _session(app)
    await _session(app, sender=OTHER, from_user="eve", verb="close")
    assert (await _waiting(db))[0][3] is None
    await _session(app, verb="close")
    assert (await _waiting(db))[0][3] == "withdrawn"
    assert sent == []


async def test_decline_and_the_app_arriving_send_nothing(env):
    app, db = env
    await _session(app, app_id="sketch", session_id="s1")
    await _session(app, app_id="sketch", session_id="s2")
    assert await app[app_federation_service_key].decline_waiting(
        app_id="sketch", session_id="s1", actor_user_id="u-bob"
    )
    await SqliteAppRepo(db).set_enabled("sketch", enabled=True)
    await app[event_bus_key].publish(AppEnabledChanged(app_id="sketch", enabled=True))
    await app[notification_delivery_key].drain()
    assert sent == []
    assert frames == []
    assert [(o, h) for _, o, _, h in await _waiting(db)] == [
        ("app_session:sketch/s1", "declined"),
        ("app_session:sketch/s2", "done"),
    ]
    pending = await db.fetchall(
        "SELECT session_id, from_instance, from_user, state FROM app_pending_sessions",
        (),
    )
    assert [tuple(r) for r in pending] == [("s2", PEER, "ben", "invited")]


async def test_a_rematch_after_uninstall_waits_and_tells_the_sender_nothing(env):
    """#1243's open item: the opponent's app may still offer "Rematch" to a
    household that removed the app. The new invite lands as the person's
    "your household doesn't have it" row — and the sender learns nothing."""
    app, db = env
    repo = SqliteAppRepo(db)
    await repo.install(
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
            installed_at="2026-06-02T00:00:00+00:00",
        )
    )
    await _session(app, session_id="game-1")  # the old game's invite
    await repo.uninstall("chess")
    sent.clear()
    await _session(app, session_id="rematch-1")
    assert sent == []
    assert frames == []
    assert await _waiting(db) == [
        ("u-bob", "app_session:chess/rematch-1", "app_missing", None)
    ]
