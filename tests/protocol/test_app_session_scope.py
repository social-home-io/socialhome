"""Release-blocker protocol tests: an app session belongs to the household that opened it.

Marked ``@pytest.mark.security``.

``APP_SESSION`` / ``APP_MESSAGE`` carry a bare ``session_id``. The rule
these tests encode, against the real application registry and SQLite:

    A per-user invite names a user of the sending household. A pending
    invite is only ever refreshed by the household that opened it, and a
    household cannot suppress another household's invite by sending the
    same session id first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.apps import AppManifest, InstalledApp
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.repositories.app_repo import SqliteAppRepo

pytestmark = pytest.mark.security

FET = FederationEventType

PEER = "peer-alice"
OTHER = "peer-eve"


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


async def _seed_peer(db, instance_id: str, user_id: str, username: str) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
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
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, username, username),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "u-bob", "Bob"),
    )
    await _seed_peer(db, PEER, "u-alice", "alice")
    await _seed_peer(db, OTHER, "u-eve", "eve")
    await SqliteAppRepo(db).install(
        InstalledApp(
            app_id="chess",
            name="Chess",
            version="1.0.0",
            enabled=True,
            manifest=AppManifest(entry="index.html", icon=None, capabilities=()),
            bundle_path="apps/chess/1.0.0",
            bundle_sha256="ab" * 32,
            source_url="https://example/chess.tgz",
            installed_by=None,
            installed_at="2026-06-02T00:00:00+00:00",
        )
    )
    return app, db


async def _pending(db) -> list[tuple]:
    rows = await db.fetchall(
        "SELECT session_id, user_id, from_instance, from_user"
        " FROM app_pending_sessions ORDER BY 1, 3",
        (),
    )
    return [tuple(r) for r in rows]


async def _open(app, *, sender, from_user, session_id="sess-1") -> None:
    payload = {
        "app_id": "chess",
        "session_id": session_id,
        "verb": "open",
        "to_user": "bob",
    }
    if from_user is not None:
        payload["from_user"] = from_user
    handlers = app[federation_service_key]._event_registry.handlers_for(FET.APP_SESSION)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id=f"m-{sender}-{session_id}",
                event_type=FET.APP_SESSION,
                from_instance=sender,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


@pytest.mark.parametrize(
    "from_user",
    [
        pytest.param(None, id="no initiator"),
        pytest.param("eve", id="another household's user"),
        pytest.param("bob", id="a local member"),
    ],
)
async def test_per_user_invite_must_name_a_user_of_the_sender(env, from_user):
    app, db = env
    await _open(app, sender=PEER, from_user=from_user)
    assert await _pending(db) == []


async def test_another_household_cannot_take_over_a_pending_invite(env):
    app, db = env
    await _open(app, sender=PEER, from_user="alice")
    before = await _pending(db)
    assert before == [("sess-1", "u-bob", PEER, "alice")]
    await _open(app, sender=OTHER, from_user="eve")
    assert await _pending(db) == before


async def test_an_early_copy_from_another_household_does_not_suppress_the_invite(
    env,
):
    app, db = env
    await _open(app, sender=OTHER, from_user="alice")  # not OTHER's user: refused
    await _open(app, sender=PEER, from_user="alice")
    assert await _pending(db) == [("sess-1", "u-bob", PEER, "alice")]
