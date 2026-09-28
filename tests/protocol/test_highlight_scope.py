"""Release-blocker protocol tests: a household only writes its own highlights.

Marked ``@pytest.mark.security``.

``HIGHLIGHT_*`` envelopes name rows by bare id (``highlight_id``,
``frame_id``) and people by bare user id (``author_user_id``,
``viewer_user_id``, ``reactor_user_id``). The rules these tests encode,
against the real application registry and SQLite:

    A household may create, extend or change a highlight only when the
    stored row (if any) belongs to one of its own users, and a frame only
    inside the highlight it already belongs to. A view or a reaction is a
    reply to one of *our* authors' frames, on behalf of the sending
    household's own user — never a local member, never another
    household's user, never a user we don't know, never on a frame
    authored elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, event_bus_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.events import (
    HighlightFrameReactionChanged,
    HighlightFrameViewed,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.highlight import (
    Highlight,
    HighlightAudience,
    HighlightFrame,
    HighlightFrameType,
)
from socialhome.repositories.highlight_repo import SqliteHighlightRepo

pytestmark = pytest.mark.security

FET = FederationEventType

PEER = "peer-bob"  # Bob's household — the sender in every case
OTHER = "peer-dora"  # Dora's household — paired too, never the sender here
EXPIRES = "2099-01-01T00:00:00+00:00"


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "highlight.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, user_id: str) -> None:
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
        (user_id, instance_id, user_id, user_id),
    )


async def _seed_highlight(repo, owner: str, author: str) -> None:
    await repo.save_highlight(
        Highlight(
            id=f"h-{owner}",
            author_user_id=author,
            highlight_date="2026-05-01",
            audience_kind=HighlightAudience.ALL_PAIRED,
            expires_at=EXPIRES,
        )
    )
    await repo.save_frame(
        HighlightFrame(
            id=f"f-{owner}",
            highlight_id=f"h-{owner}",
            sequence=1,
            frame_type=HighlightFrameType.IMAGE,
            media_url=f"/api/media/{owner}.webp",
            caption_text=f"{owner}'s day",
        )
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    await _seed_peer(db, PEER, "u-bob")
    await _seed_peer(db, OTHER, "u-dora")
    repo = SqliteHighlightRepo(db)
    await _seed_highlight(repo, "anna", "u-anna")
    await _seed_highlight(repo, "bob", "u-bob")
    await _seed_highlight(repo, "dora", "u-dora")
    # Existing replies on Anna's frame: Dora's (from OTHER) and Anna's own.
    for user_id in ("u-dora", "u-anna"):
        await repo.mark_viewed("f-anna", user_id)
        await repo.set_reaction("f-anna", user_id, "🎉")
    published: list = []

    async def _record(event) -> None:
        published.append(event)

    bus = app[event_bus_key]
    bus.subscribe(HighlightFrameViewed, _record)
    bus.subscribe(HighlightFrameReactionChanged, _record)
    return app, db, published


async def _snapshot(db) -> dict[str, list[tuple]]:
    queries = {
        "highlights": "SELECT id, author_user_id, highlight_date, audience_kind,"
        " audience_json, expires_at FROM highlights ORDER BY id",
        "frames": "SELECT id, highlight_id, sequence, frame_type, media_url,"
        " caption_text, caption_emoji, duration_ms FROM highlight_frames"
        " ORDER BY id",
        "views": "SELECT frame_id, viewer_user_id FROM highlight_frame_views"
        " ORDER BY frame_id, viewer_user_id",
        "reactions": "SELECT frame_id, reactor_user_id, emoji"
        " FROM highlight_frame_reactions ORDER BY frame_id, reactor_user_id",
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


def _frame_payload(highlight_id: str, frame_id: str, author: str, **over) -> dict:
    payload = {
        "highlight_id": highlight_id,
        "frame_id": frame_id,
        "author_user_id": author,
        "highlight_date": "2026-05-02",
        "sequence": 9,
        "audience_kind": "households",
        "audience": [PEER],
        "frame_type": "image",
        "media_url": "/api/media/planted.webp",
        "caption_text": "planted",
        "caption_emoji": None,
        "duration_ms": None,
        "expires_at": "2026-05-03T00:00:00+00:00",
    }
    payload.update(over)
    return payload


_WRITE_ATTACKS = [
    pytest.param(
        FET.HIGHLIGHT_CREATED,
        _frame_payload("h-dora", "f-new", "u-bob"),
        PEER,
        id="create over another household's highlight id",
    ),
    pytest.param(
        FET.HIGHLIGHT_CREATED,
        _frame_payload("h-anna", "f-new", "u-bob"),
        PEER,
        id="create over a local member's highlight id",
    ),
    pytest.param(
        FET.HIGHLIGHT_CREATED,
        _frame_payload("h-bob", "f-dora", "u-bob"),
        PEER,
        id="create re-parents another household's frame",
    ),
    pytest.param(
        FET.HIGHLIGHT_CREATED,
        _frame_payload("h-new", "f-new", "u-dora"),
        PEER,
        id="create as another household's user",
    ),
    pytest.param(
        FET.HIGHLIGHT_CREATED,
        _frame_payload("h-new", "f-new", "u-anna"),
        PEER,
        id="create as a local member",
    ),
    pytest.param(
        FET.HIGHLIGHT_FRAME_APPENDED,
        _frame_payload("h-bob", "f-dora", "u-bob"),
        PEER,
        id="append replaces another household's frame",
    ),
    pytest.param(
        FET.HIGHLIGHT_FRAME_APPENDED,
        _frame_payload("h-bob", "f-anna", "u-bob"),
        PEER,
        id="append replaces a local member's frame",
    ),
    pytest.param(
        FET.HIGHLIGHT_FRAME_APPENDED,
        _frame_payload("h-new", "f-dora", "u-bob"),
        PEER,
        id="append with a new parent re-parents another frame",
    ),
    pytest.param(
        FET.HIGHLIGHT_FRAME_APPENDED,
        _frame_payload("h-dora", "f-new", "u-bob"),
        PEER,
        id="append into another household's highlight",
    ),
    pytest.param(
        FET.HIGHLIGHT_FRAME_DELETED,
        {"highlight_id": "h-bob", "frame_id": "f-dora", "author_user_id": "u-bob"},
        PEER,
        id="delete another household's frame",
    ),
    pytest.param(
        FET.HIGHLIGHT_FRAME_DELETED,
        {"highlight_id": "h-bob", "frame_id": "f-anna", "author_user_id": "u-bob"},
        PEER,
        id="delete a local member's frame",
    ),
    pytest.param(
        FET.HIGHLIGHT_DELETED,
        {"highlight_id": "h-dora", "author_user_id": "u-bob"},
        PEER,
        id="delete another household's highlight",
    ),
    pytest.param(
        FET.HIGHLIGHT_DELETED,
        {"highlight_id": "h-anna", "author_user_id": "u-bob"},
        PEER,
        id="delete a local member's highlight",
    ),
]


@pytest.mark.parametrize(("event_type", "payload", "sender"), _WRITE_ATTACKS)
async def test_highlight_write_outside_the_senders_rows_changes_nothing(
    env, event_type, payload, sender
):
    app, db, _ = env
    before = await _snapshot(db)
    await _send(app, event_type, payload, from_instance=sender)
    assert await _snapshot(db) == before


def _reply(frame_id: str, user: str, *, highlight_id: str | None = None) -> dict:
    return {
        "highlight_id": highlight_id or "h-" + frame_id.removeprefix("f-"),
        "frame_id": frame_id,
        "viewer_user_id": user,
        "reactor_user_id": user,
        "author_user_id": "u-anna",
        "emoji": "💩",
    }


_REPLY_ATTACKS = [
    pytest.param(_reply("f-anna", "u-dora"), id="as another household's user"),
    pytest.param(_reply("f-anna", "u-anna"), id="as a local member"),
    pytest.param(_reply("f-anna", "u-ghost"), id="as a user nobody knows"),
    pytest.param(_reply("f-dora", "u-bob"), id="on another household's frame"),
    pytest.param(_reply("f-bob", "u-bob"), id="on the sender's own frame"),
    pytest.param(_reply("f-missing", "u-bob"), id="on a frame that doesn't exist"),
    pytest.param(
        _reply("f-anna", "u-bob", highlight_id="h-dora"),
        id="naming the wrong highlight",
    ),
]

_REPLY_EVENTS = [
    pytest.param(FET.HIGHLIGHT_FRAME_VIEWED, id="viewed"),
    pytest.param(FET.HIGHLIGHT_FRAME_REACTED, id="reacted"),
    pytest.param(FET.HIGHLIGHT_FRAME_REACTION_REMOVED, id="reaction-removed"),
]


@pytest.mark.parametrize("event_type", _REPLY_EVENTS)
@pytest.mark.parametrize("payload", _REPLY_ATTACKS)
async def test_highlight_reply_outside_the_senders_users_changes_nothing(
    env, event_type, payload
):
    app, db, published = env
    before = await _snapshot(db)
    await _send(app, event_type, payload)
    assert await _snapshot(db) == before
    assert published == []


async def test_the_authors_household_creates_extends_and_deletes(env):
    """Control: Bob's household manages Bob's highlights."""
    app, db, _ = env
    await _send(
        app,
        FET.HIGHLIGHT_CREATED,
        _frame_payload("h-bob-2", "f-bob-2", "u-bob", sequence=1),
    )
    await _send(
        app,
        FET.HIGHLIGHT_FRAME_APPENDED,
        _frame_payload("h-bob", "f-bob-3", "u-bob", sequence=2),
    )
    # Re-delivery of Bob's own frame (same highlight) still upserts.
    await _send(
        app,
        FET.HIGHLIGHT_FRAME_APPENDED,
        _frame_payload("h-bob", "f-bob", "u-bob", caption_text="edited"),
    )
    snap = await _snapshot(db)
    assert ("h-bob-2", "u-bob") in [r[:2] for r in snap["highlights"]]
    frames = {r[0]: r for r in snap["frames"]}
    assert frames["f-bob-2"][1] == "h-bob-2"
    assert frames["f-bob-3"][1] == "h-bob"
    assert frames["f-bob"][5] == "edited"
    await _send(
        app,
        FET.HIGHLIGHT_FRAME_DELETED,
        {"highlight_id": "h-bob", "frame_id": "f-bob-3", "author_user_id": "u-bob"},
    )
    await _send(
        app,
        FET.HIGHLIGHT_DELETED,
        {"highlight_id": "h-bob-2", "author_user_id": "u-bob"},
    )
    snap = await _snapshot(db)
    assert "h-bob-2" not in [r[0] for r in snap["highlights"]]
    assert "f-bob-3" not in [r[0] for r in snap["frames"]]


async def test_the_viewers_household_views_and_reacts_on_a_local_frame(env):
    """Control: Bob views and reacts on Anna's frame; the stored author wins."""
    app, db, published = env
    payload = _reply("f-anna", "u-bob") | {"author_user_id": "u-somebody-else"}
    await _send(app, FET.HIGHLIGHT_FRAME_VIEWED, payload)
    await _send(app, FET.HIGHLIGHT_FRAME_REACTED, payload | {"emoji": "❤️"})
    snap = await _snapshot(db)
    assert ("f-anna", "u-bob") in snap["views"]
    assert ("f-anna", "u-bob", "❤️") in snap["reactions"]
    assert [e.author_user_id for e in published] == ["u-anna", "u-anna"]
    await _send(app, FET.HIGHLIGHT_FRAME_REACTION_REMOVED, payload)
    snap = await _snapshot(db)
    assert all(r[1] != "u-bob" for r in snap["reactions"])
    assert ("f-anna", "u-dora", "🎉") in snap["reactions"]
