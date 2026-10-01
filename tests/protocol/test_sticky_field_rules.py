"""Release-blocker protocol tests: a federated sticky never stores a raw
colour, an unbounded coordinate, or unsanitised / oversized content.

Marked ``@pytest.mark.security``.

The SPA renders a sticky's ``color`` as a CSS ``background`` value, so a
peer that could store ``url(https://attacker/x.png)`` on every member
household would turn the note into a cross-household tracking beacon.
Both peer write paths — the per-event ``SPACE_STICKY_*`` handlers and the
snapshot-sync receiver — run on the real application here, and whatever
the payload carries, the persisted row must hold a canonical ``#RRGGBB``
colour, board-clamped coordinates and capped, sanitised content.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key, space_sync_receiver_key
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.sticky import (
    DEFAULT_STICKY_COLOR,
    MAX_STICKY_CONTENT_LENGTH,
    STICKY_BOARD_HEIGHT,
    STICKY_BOARD_WIDTH,
)

pytestmark = pytest.mark.security

SPACE = "sp-a"
SENDER = "peer-seated-in-a"
AUTHOR = "u-remote"

_CANONICAL_HEX = re.compile(r"#[0-9A-F]{6}")

_HOSTILE_COLORS = [
    "url(https://evil.example/t.png)",
    "red;background-image:url(https://evil.example/t.png)",
    "#FFF9B1; background: url(x)",
    "yellow",
    "x" * 4096,
    "expression(alert(1))",
    123,
    ["#FFF"],
]


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "sticky.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (SPACE, SPACE, "the-host", "anna", "00" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?,?,?,'member')",
        (SPACE, SENDER, AUTHOR),
    )
    return app, db


def _event(event_type, payload) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{payload['id']}",
        event_type=event_type,
        from_instance=SENDER,
        to_instance="us",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=SPACE,
    )


async def _dispatch(app, event_type, payload) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers, f"no handler registered for {event_type.value}"
    for handler in handlers:
        await handler(_event(event_type, payload))


async def _rows(db) -> list[dict]:
    rows = await db.fetchall(
        "SELECT id, content, color, position_x, position_y FROM stickies"
        " WHERE space_id=? ORDER BY id",
        (SPACE,),
    )
    return [dict(r) for r in rows]


def _assert_safe(row: dict) -> None:
    assert _CANONICAL_HEX.fullmatch(row["color"]), row["color"]
    assert 0.0 <= row["position_x"] <= STICKY_BOARD_WIDTH
    assert 0.0 <= row["position_y"] <= STICKY_BOARD_HEIGHT
    assert len(row["content"]) <= MAX_STICKY_CONTENT_LENGTH
    assert "‮" not in row["content"] and "\x00" not in row["content"]


@pytest.mark.parametrize("color", _HOSTILE_COLORS)
async def test_inbound_create_never_persists_a_non_hex_color(env, color):
    app, db = env
    await _dispatch(
        app,
        FederationEventType.SPACE_STICKY_CREATED,
        {
            "id": "st-new",
            "author": AUTHOR,
            "content": "‮hello\x00 " + "y" * 3000,
            "color": color,
            "position_x": 10**400,
            "position_y": 1e9,
        },
    )
    rows = await _rows(db)
    assert len(rows) == 1
    _assert_safe(rows[0])
    assert rows[0]["color"] == DEFAULT_STICKY_COLOR


@pytest.mark.parametrize("color", _HOSTILE_COLORS)
async def test_inbound_update_never_persists_a_non_hex_color(env, color):
    app, db = env
    await _dispatch(
        app,
        FederationEventType.SPACE_STICKY_CREATED,
        {"id": "st-1", "author": AUTHOR, "content": "ok", "color": "#abc"},
    )
    assert (await _rows(db))[0]["color"] == "#AABBCC"
    await _dispatch(
        app,
        FederationEventType.SPACE_STICKY_UPDATED,
        {"id": "st-1", "content": "edited", "color": color},
    )
    rows = await _rows(db)
    _assert_safe(rows[0])
    assert rows[0]["content"] == "edited"
    assert rows[0]["color"] == DEFAULT_STICKY_COLOR


@pytest.mark.parametrize("color", _HOSTILE_COLORS)
async def test_sync_snapshot_never_persists_a_non_hex_color(env, color):
    app, db = env
    await app[space_sync_receiver_key]._dispatch(
        "stickies",
        SPACE,
        [
            {
                "id": "st-sync",
                "author": AUTHOR,
                "content": "y" * 5000,
                "color": color,
                "position_x": -50.0,
                "position_y": 10**400,
            }
        ],
        provider="the-host",
    )
    rows = await _rows(db)
    assert len(rows) == 1
    _assert_safe(rows[0])
    assert rows[0]["color"] == DEFAULT_STICKY_COLOR
