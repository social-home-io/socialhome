"""Release-blocker protocol tests: a household speaks only for its own users.

Marked ``@pytest.mark.security``.

Profile sync, removal, status, online state and contact requests carry a
bare ``user_id``. The rule these tests encode, against the real
application registry and SQLite:

    A household may publish, remove, or report on only the users homed on
    it. Never a local member, never another household's user — their
    profile, avatar, deprovisioning, status and online state stay as they
    are, and nothing is announced on their behalf.
"""

from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
from PIL import Image

from socialhome.app import create_app
from socialhome.app_keys import db_key, event_bus_key, federation_service_key
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.events import (
    DmContactRequested,
    UserCameOnline,
    UserStatusChanged,
    UserWentOffline,
)
from socialhome.domain.federation import FederationEvent, FederationEventType

pytestmark = pytest.mark.security

FET = FederationEventType

PEER = "peer-bob"
OTHER = "peer-dora"
#: Each household's pinned identity key (distinct, so ids derive apart).
PEER_PK = bytes([0x11]) * 32
OTHER_PK = bytes([0x22]) * 32


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "users.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(
    db, instance_id: str, user_id: str, username: str, pk: bytes
) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            pk.hex(),
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
        (user_id, instance_id, username, username.title()),
    )


def _webp_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 10, 10)).save(buf, format="WEBP")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    await db.enqueue(
        "INSERT INTO user_profile_pictures(user_id, bytes_webp, hash, width,"
        " height) VALUES(?,?,?,?,?)",
        ("u-anna", b"anna-avatar", "h-anna", 16, 16),
    )
    await _seed_peer(db, PEER, "u-bob", "bob", PEER_PK)
    await _seed_peer(db, OTHER, "u-dora", "dora", OTHER_PK)
    published: list[object] = []
    bus = app[event_bus_key]
    for event_type in (
        UserStatusChanged,
        UserCameOnline,
        UserWentOffline,
        DmContactRequested,
    ):
        bus.subscribe(event_type, published.append)
    return app, db, published


async def _state(db) -> dict[str, list[tuple]]:
    queries = {
        "remote_users": "SELECT user_id, instance_id, remote_username,"
        " display_name, deprovisioned_at FROM remote_users ORDER BY user_id",
        "pictures": "SELECT user_id, hash FROM user_profile_pictures ORDER BY user_id",
        "contact_requests": "SELECT from_user_id, to_user_id"
        " FROM dm_contact_requests ORDER BY 1, 2",
    }
    return {
        name: [tuple(r) for r in await db.fetchall(sql, ())]
        for name, sql in queries.items()
    }


async def _send(app, event_type, payload, *, from_instance=PEER) -> None:
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


def _profile(user_id: str, username: str, **extra) -> dict:
    return {
        "user_id": user_id,
        "username": username,
        "display_name": "Forged",
        "picture_webp_base64": _webp_b64(),
        **extra,
    }


_FOREIGN_USERS = [
    pytest.param("u-anna", "anna", id="a local member"),
    pytest.param("u-dora", "dora", id="another household's user"),
]


@pytest.mark.parametrize(("user_id", "username"), _FOREIGN_USERS)
async def test_profile_update_for_a_foreign_user_changes_nothing(
    env, user_id, username
):
    app, db, _ = env
    before = await _state(db)
    await _send(app, FET.USER_UPDATED, _profile(user_id, username))
    await _send(app, FET.USERS_SYNC, {"users": [_profile(user_id, username)]})
    assert await _state(db) == before


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        pytest.param(FET.USER_REMOVED, {"user_id": "u-anna"}, id="remove local"),
        pytest.param(FET.USER_REMOVED, {"user_id": "u-dora"}, id="remove other"),
        pytest.param(FET.USER_REMOVED, {"user_id": "u-ghost"}, id="remove unknown"),
        pytest.param(
            FET.USER_STATUS_UPDATED,
            {"user_id": "u-anna", "emoji": "🌴", "text": "away"},
            id="status of local",
        ),
        pytest.param(
            FET.USER_STATUS_UPDATED,
            {"user_id": "u-dora", "emoji": "🌴", "text": "away"},
            id="status of other",
        ),
        pytest.param(FET.USER_ONLINE, {"user_id": "u-anna"}, id="online local"),
        pytest.param(FET.USER_ONLINE, {"user_id": "u-dora"}, id="online other"),
        pytest.param(FET.USER_OFFLINE, {"user_id": "u-anna"}, id="offline local"),
        pytest.param(
            FET.DM_CONTACT_REQUEST,
            {"requester_user_id": "u-dora", "recipient_user_id": "u-anna"},
            id="contact request as other",
        ),
        pytest.param(
            FET.DM_CONTACT_REQUEST,
            {"requester_user_id": "u-ghost", "recipient_user_id": "u-anna"},
            id="contact request as unknown",
        ),
    ],
)
async def test_speaking_for_a_foreign_user_changes_and_announces_nothing(
    env, event_type, payload
):
    app, db, published = env
    before = await _state(db)
    await _send(app, event_type, payload)
    assert await _state(db) == before
    assert published == []


async def test_the_home_household_updates_and_removes_its_own_user(env):
    app, db, published = env
    await _send(
        app, FET.USER_UPDATED, {"user_id": "u-bob", "username": "bob", "bio": "hi"}
    )
    await _send(app, FET.USER_UPDATED, _profile("u-bob", "bob"))
    rows = {r[0]: r for r in (await _state(db))["remote_users"]}
    assert rows["u-bob"][3] == "Forged"
    assert "u-bob" in {r[0] for r in (await _state(db))["pictures"]}
    await _send(app, FET.USER_STATUS_UPDATED, {"user_id": "u-bob", "emoji": "🎉"})
    await _send(app, FET.USER_ONLINE, {"user_id": "u-bob"})
    await _send(
        app,
        FET.DM_CONTACT_REQUEST,
        {"requester_user_id": "u-bob", "recipient_user_id": "u-anna"},
    )
    assert {type(e) for e in published} == {
        UserStatusChanged,
        UserCameOnline,
        DmContactRequested,
    }
    await _send(app, FET.USER_REMOVED, {"user_id": "u-bob"})
    rows = {r[0]: r for r in (await _state(db))["remote_users"]}
    assert rows["u-bob"][4] is not None


async def test_a_new_user_of_the_sending_household_lands(env):
    """A first-seen user lands when its id derives from the sender's key —
    from the username (legacy ids) or from the shipped identity anchor."""
    app, db, _ = env
    by_name = derive_user_id(PEER_PK, "carl")
    by_anchor = derive_user_id(PEER_PK, "anchor-uuid-erin")
    await _send(
        app,
        FET.USERS_SYNC,
        {
            "users": [
                {"user_id": by_name, "username": "carl"},
                {
                    "user_id": by_anchor,
                    "username": "erin",
                    "identity_anchor": "anchor-uuid-erin",
                },
            ]
        },
    )
    rows = {r[0]: r for r in (await _state(db))["remote_users"]}
    assert rows[by_name][1] == PEER
    assert rows[by_anchor][1] == PEER


@pytest.mark.parametrize(
    ("user_id", "extra"),
    [
        pytest.param(derive_user_id(PEER_PK, "carl"), {}, id="another's derived id"),
        pytest.param("u-made-up", {}, id="an id that derives from nothing"),
        pytest.param(
            derive_user_id(PEER_PK, "anchor-x"),
            {"identity_anchor": "anchor-x"},
            id="another's anchor",
        ),
    ],
)
async def test_a_first_seen_id_is_never_claimed_by_another_household(
    env, user_id, extra
):
    """A household can only introduce users whose id derives from its own key:
    it cannot pre-claim another household's user before their home syncs."""
    app, db, _ = env
    before = await _state(db)
    await _send(
        app,
        FET.USERS_SYNC,
        {"users": [{"user_id": user_id, "username": "carl", **extra}]},
        from_instance=OTHER,
    )
    assert await _state(db) == before
    if user_id != "u-made-up":
        await _send(
            app,
            FET.USERS_SYNC,
            {"users": [{"user_id": user_id, "username": "carl", **extra}]},
        )
        rows = {r[0]: r for r in (await _state(db))["remote_users"]}
        assert rows[user_id][1] == PEER
