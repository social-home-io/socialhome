"""Release-blocker protocol tests: a moment write binds to its author's home.

Marked ``@pytest.mark.security``.

``MOMENT_*`` events name a ``moment_id``, its author and a claimed
``origin_instance_id``; relays re-send them onward under this household's
signature. The rule these tests encode, against the real application
registry and SQLite:

    A moment id we hold is only rewritten or deleted for its own stored
    author and origin. A moment is never accepted as one of ours — our
    origin, or our own member as author. A reaction answers a moment
    authored here, from the reacting user's own household. Nothing that
    is refused is relayed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.federation_service import FederationService

pytestmark = pytest.mark.security

FET = FederationEventType

PEER = "peer-bob"
OTHER = "peer-dora"
THIRD = "peer-third"  # paired, no users — a relay target
EXPIRES = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "moments.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, user: tuple[str, str] | None) -> None:
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
    if user is not None:
        await db.enqueue(
            "INSERT INTO remote_users(user_id, instance_id, remote_username,"
            " display_name) VALUES(?,?,?,?)",
            (user[0], instance_id, user[1], user[1]),
        )


async def _seed_moment(db, moment_id: str, author: str, origin: str) -> None:
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at) VALUES(?,?,?,?,?)",
        (moment_id, author, f"by {author}", origin, EXPIRES),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    own = app[federation_service_key].own_instance_id
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    await _seed_peer(db, PEER, ("u-bob", "bob"))
    await _seed_peer(db, OTHER, ("u-dora", "dora"))
    await _seed_peer(db, THIRD, None)
    await _seed_moment(db, "m-anna", "u-anna", own)
    await _seed_moment(db, "m-bob", "u-bob", PEER)
    await _seed_moment(db, "m-dora", "u-dora", OTHER)
    await db.enqueue(
        "INSERT INTO moment_reactions(moment_id, reactor_user_id, emoji) VALUES(?,?,?)",
        ("m-anna", "u-dora", "👍"),
    )
    sent: list[tuple[str, FederationEventType]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    return app, db, sent, own


async def _state(db) -> dict[str, list[tuple]]:
    queries = {
        "moments": "SELECT id, author_user_id, content, origin_instance_id"
        " FROM moments ORDER BY id",
        "reactions": "SELECT moment_id, reactor_user_id, emoji"
        " FROM moment_reactions ORDER BY 1, 2",
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


def _moment(moment_id, author, origin, **extra) -> dict:
    return {
        "moment_id": moment_id,
        "author_user_id": author,
        "origin_instance_id": origin,
        "content": "forged",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": EXPIRES,
        "hop_count": 1,
        **extra,
    }


OWN = "<own>"  # placeholder, swapped for this household's instance id


def _resolve(payload: dict, own: str) -> dict:
    return {k: (own if v == OWN else v) for k, v in payload.items()}


_WRITE_ATTACKS = [
    pytest.param(
        FET.MOMENT_CREATED, _moment("m-new", "u-anna", OWN), id="as a local member"
    ),
    pytest.param(
        FET.MOMENT_CREATED,
        _moment("m-new", "u-anna", PEER),
        id="local member via the peer",
    ),
    pytest.param(
        FET.MOMENT_CREATED,
        _moment("m-new", "u-ghost", OWN, hop_count=2),
        id="claims our origin",
    ),
    pytest.param(
        FET.MOMENT_CREATED, _moment("m-dora", "u-bob", PEER), id="overwrites by id"
    ),
    pytest.param(
        FET.MOMENT_CREATED,
        _moment("m-anna", "u-bob", PEER),
        id="overwrites a local moment",
    ),
    pytest.param(
        FET.MOMENT_DELETED, _moment("m-dora", "u-bob", PEER), id="deletes another's"
    ),
    pytest.param(
        FET.MOMENT_DELETED, _moment("m-anna", "u-bob", PEER), id="deletes a local one"
    ),
    pytest.param(
        FET.MOMENT_DELETED,
        _moment("m-anna", "u-anna", OWN, hop_count=2),
        id="deletes a local one as its author",
    ),
]


@pytest.mark.parametrize(("event_type", "payload"), _WRITE_ATTACKS)
async def test_moment_write_outside_its_scope_changes_and_relays_nothing(
    env, event_type, payload
):
    app, db, sent, own = env
    before = await _state(db)
    await _send(app, event_type, _resolve(payload, own))
    assert await _state(db) == before
    assert sent == []


@pytest.mark.parametrize(
    ("moment_id", "reactor", "emoji"),
    [
        pytest.param("m-anna", "u-anna", "🔥", id="reacts as a local member"),
        pytest.param("m-anna", "u-dora", None, id="clears another's reaction"),
        pytest.param("m-anna", "u-ghost", "🔥", id="reacts as an unknown user"),
        pytest.param("m-dora", "u-bob", "🔥", id="reacts on a moment not ours"),
        pytest.param("m-none", "u-bob", "🔥", id="reacts on a missing moment"),
    ],
)
async def test_moment_reaction_outside_its_scope_changes_nothing(
    env, moment_id, reactor, emoji
):
    app, db, _, _ = env
    before = await _state(db)
    event_type = FET.MOMENT_REACTED if emoji else FET.MOMENT_REACTION_REMOVED
    await _send(
        app,
        event_type,
        {
            "moment_id": moment_id,
            "reactor_user_id": reactor,
            "author_user_id": "u-anna",
            "emoji": emoji,
        },
    )
    assert await _state(db) == before


async def test_the_author_household_deletes_its_own_moment(env):
    app, db, _, _ = env
    await _send(app, FET.MOMENT_DELETED, _moment("m-bob", "u-bob", PEER))
    assert "m-bob" not in {r[0] for r in (await _state(db))["moments"]}


async def test_a_user_of_the_sender_reacts_on_our_moment(env):
    app, db, _, _ = env
    await _send(
        app,
        FET.MOMENT_REACTED,
        {"moment_id": "m-anna", "reactor_user_id": "u-bob", "emoji": "🎉"},
    )
    assert ("m-anna", "u-bob", "🎉") in (await _state(db))["reactions"]


async def test_an_honest_relay_is_still_forwarded(env):
    """A relayed moment (sender != origin, author at the origin) travels on."""
    app, _, sent, _ = env
    await _send(
        app,
        FET.MOMENT_CREATED,
        _moment("m-relayed", "u-bob", PEER, hop_count=2),
        from_instance=OTHER,
    )
    assert (THIRD, FET.MOMENT_CREATED) in sent
