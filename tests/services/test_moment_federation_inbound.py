"""Inbound moment federation — handlers in :class:`FederationInboundService`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.moment import Moment
from socialhome.domain.user import RemoteUser
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.federation.moment_origin import sign_moment_origin
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)

ORIGIN_KEY = generate_identity_keypair()


@pytest.fixture
async def inbound(db, bus):
    user_repo = SqliteUserRepo(db)
    moment_repo = SqliteMomentRepo(db)
    # Seed a paired remote instance + its remote user so the authority
    # check has something to look up.
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name) VALUES(?,?,?)",
        ("uid-local", "local", "Local"),
    )
    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_inbox_url, local_inbox_id
           ) VALUES(?,?,?,?,?,?,?)""",
        ("peer-a", "Peer A", "00" * 32, "k1", "k2", "https://peer-a/wh", "wh-a"),
    )
    await user_repo.upsert_remote(
        RemoteUser(
            user_id="uid-remote",
            instance_id="peer-a",
            remote_username="alice",
            display_name="Alice",
        ),
    )
    relay = MagicMock()
    relay.relay_inbound = AsyncMock()
    return FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
        moment_repo=moment_repo,
        moment_outbound=relay,
    ), relay


def _event(event_type, payload, *, from_instance="peer-a"):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


def _create_payload(**over):
    base = {
        "moment_id": "m-fed-1",
        "author_user_id": "uid-remote",
        "content": "hello",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "origin_instance_id": "peer-a",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "hop_count": 1,
    }
    base.update(over)
    return base


async def _seed_local_moment(svc) -> None:
    """``m-fed-1`` authored by our own ``uid-local`` (reactions go home)."""
    now = datetime.now(timezone.utc)
    await svc._moment_repo.save(
        Moment(
            id="m-fed-1",
            author_user_id="uid-local",
            content="hello",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="self",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=7)).isoformat(),
        )
    )


# ── MOMENT_CREATED ────────────────────────────────────────────────────────


async def test_moment_created_persists_and_publishes(db, bus, inbound):
    svc, relay = inbound
    captured: list[MomentCreated] = []
    bus.subscribe(MomentCreated, captured.append)
    await svc._on_moment_created(
        _event(FederationEventType.MOMENT_CREATED, _create_payload()),
    )
    m = await svc._moment_repo.get("m-fed-1")
    assert m is not None and m.author_user_id == "uid-remote"
    assert len(captured) == 1
    # Relay was triggered with hop_count=1 — outbound decides whether to forward.
    relay.relay_inbound.assert_awaited_once()


async def test_moment_created_impersonation_dropped(db, bus, inbound):
    """A peer claiming origin=itself for an author that lives on a
    different paired peer is dropped — that's the impersonation we
    block. (A relay envelope where from != origin is a separate path
    and IS accepted; see ``test_moment_created_relay_path_trusts_origin``.)"""
    svc, relay = inbound
    relay.relay_inbound.reset_mock()
    # peer-b claims origin=peer-b for an author whose home is peer-a.
    await svc._on_moment_created(
        _event(
            FederationEventType.MOMENT_CREATED,
            _create_payload(origin_instance_id="peer-b"),
            from_instance="peer-b",
        ),
    )
    assert await svc._moment_repo.get("m-fed-1") is None
    relay.relay_inbound.assert_not_called()


def _attach_federation(svc, *, pinned: bytes | None, signs: bool) -> None:
    fed = MagicMock()
    fed.own_instance_id = "self"
    fed.peer_identity_public_key = AsyncMock(return_value=pinned)
    fed.peer_supports = AsyncMock(return_value=signs)
    svc._federation_service = fed


async def test_moment_created_relay_path_accepts_origin_signed(db, bus, inbound):
    """A 2-hop relay (sender != origin) lands when the origin's identity
    signature over the moment verifies against the key pinned for it."""
    svc, relay = inbound
    _attach_federation(svc, pinned=ORIGIN_KEY.public_key, signs=True)
    payload = sign_moment_origin(
        seed=ORIGIN_KEY.private_key,
        identity_pk=ORIGIN_KEY.public_key,
        event_type=FederationEventType.MOMENT_CREATED,
        payload=_create_payload(),
    )
    await svc._on_moment_created(
        _event(
            FederationEventType.MOMENT_CREATED,
            {**payload, "hop_count": 2},
            from_instance="peer-relayer",  # not the origin
        ),
    )
    assert await svc._moment_repo.get("m-fed-1") is not None
    relay.relay_inbound.assert_awaited_once()


async def test_moment_created_relay_path_legacy_origin_unsigned(db, bus, inbound):
    """Unsigned relay from an origin known to predate signing: legacy window."""
    svc, relay = inbound
    _attach_federation(svc, pinned=ORIGIN_KEY.public_key, signs=False)
    await svc._on_moment_created(
        _event(
            FederationEventType.MOMENT_CREATED,
            _create_payload(hop_count=2),
            from_instance="peer-relayer",
        ),
    )
    assert await svc._moment_repo.get("m-fed-1") is not None
    relay.relay_inbound.assert_awaited_once()


@pytest.mark.parametrize("wired", [True, False], ids=["origin signs", "no fed"])
async def test_moment_created_relay_path_unsigned_refused(db, bus, inbound, wired):
    svc, relay = inbound
    if wired:
        _attach_federation(svc, pinned=ORIGIN_KEY.public_key, signs=True)
    await svc._on_moment_created(
        _event(
            FederationEventType.MOMENT_CREATED,
            _create_payload(hop_count=2),
            from_instance="peer-relayer",
        ),
    )
    assert await svc._moment_repo.get("m-fed-1") is None
    relay.relay_inbound.assert_not_called()


# ── MOMENT_DELETED ────────────────────────────────────────────────────────


async def test_moment_deleted_removes_and_relays(db, bus, inbound):
    svc, relay = inbound
    await svc._on_moment_created(
        _event(FederationEventType.MOMENT_CREATED, _create_payload()),
    )
    relay.relay_inbound.reset_mock()
    captured: list[MomentDeleted] = []
    bus.subscribe(MomentDeleted, captured.append)
    await svc._on_moment_deleted(
        _event(
            FederationEventType.MOMENT_DELETED,
            {
                "moment_id": "m-fed-1",
                "author_user_id": "uid-remote",
                "origin_instance_id": "peer-a",
                "hop_count": 1,
            },
        ),
    )
    assert await svc._moment_repo.get("m-fed-1") is None
    assert len(captured) == 1
    relay.relay_inbound.assert_awaited_once()


# ── MOMENT_REACTED / REACTION_REMOVED ────────────────────────────────────


async def test_moment_reacted_persists_and_publishes(db, bus, inbound):
    svc, relay = inbound
    await _seed_local_moment(svc)
    captured: list[MomentReactionChanged] = []
    bus.subscribe(MomentReactionChanged, captured.append)
    await svc._on_moment_reacted(
        _event(
            FederationEventType.MOMENT_REACTED,
            {
                "moment_id": "m-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
                "emoji": "🔥",
            },
        ),
    )
    rs = await svc._moment_repo.list_reactions("m-fed-1")
    assert [r.emoji for r in rs] == ["🔥"]
    assert len(captured) == 1 and captured[0].emoji == "🔥"


async def test_moment_reaction_removed_clears(db, bus, inbound):
    svc, relay = inbound
    await _seed_local_moment(svc)
    # Seed a reaction first.
    await svc._on_moment_reacted(
        _event(
            FederationEventType.MOMENT_REACTED,
            {
                "moment_id": "m-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
                "emoji": "🔥",
            },
        ),
    )
    captured: list[MomentReactionChanged] = []
    bus.subscribe(MomentReactionChanged, captured.append)
    await svc._on_moment_reaction_removed(
        _event(
            FederationEventType.MOMENT_REACTION_REMOVED,
            {
                "moment_id": "m-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
            },
        ),
    )
    assert await svc._moment_repo.list_reactions("m-fed-1") == []
    assert len(captured) == 1 and captured[0].emoji is None


async def test_moment_created_missing_fields_dropped(db, bus, inbound):
    """Empty / missing routing fields silently drop without persist or relay."""
    svc, relay = inbound
    relay.relay_inbound.reset_mock()
    await svc._on_moment_created(
        _event(FederationEventType.MOMENT_CREATED, {"moment_id": ""}),
    )
    assert await svc._moment_repo.get("") is None
    relay.relay_inbound.assert_not_called()


async def test_moment_created_unknown_media_type_falls_back(db, bus, inbound):
    """A peer sending an invalid ``media_type`` is normalised to None."""
    svc, _relay = inbound
    payload = _create_payload(media_type="audio", media_url="/api/media/x.mp3")
    await svc._on_moment_created(
        _event(FederationEventType.MOMENT_CREATED, payload),
    )
    m = await svc._moment_repo.get("m-fed-1")
    assert m is not None and m.media_type is None


async def test_moment_deleted_missing_fields_dropped(db, bus, inbound):
    svc, relay = inbound
    relay.relay_inbound.reset_mock()
    await svc._on_moment_deleted(
        _event(
            FederationEventType.MOMENT_DELETED,
            {"moment_id": ""},
        ),
    )
    relay.relay_inbound.assert_not_called()


async def test_moment_deleted_authority_mismatch_dropped(db, bus, inbound):
    """Peer-b can't delete a moment whose author lives on peer-a."""
    svc, relay = inbound
    await svc._on_moment_created(
        _event(FederationEventType.MOMENT_CREATED, _create_payload()),
    )
    relay.relay_inbound.reset_mock()
    await svc._on_moment_deleted(
        _event(
            FederationEventType.MOMENT_DELETED,
            {
                "moment_id": "m-fed-1",
                "author_user_id": "uid-remote",
                "origin_instance_id": "peer-b",  # claims itself as origin
                "hop_count": 1,
            },
            from_instance="peer-b",
        ),
    )
    # Moment still present; relay was not triggered.
    assert await svc._moment_repo.get("m-fed-1") is not None
    relay.relay_inbound.assert_not_called()


async def test_moment_reaction_missing_fields_dropped(db, bus, inbound):
    svc, _relay = inbound
    await svc._on_moment_reacted(
        _event(
            FederationEventType.MOMENT_REACTED,
            {"moment_id": ""},
        ),
    )
    # Nothing landed in the reactions table.
    assert await svc._moment_repo.list_reactions("") == []


async def test_moment_reaction_authority_mismatch_dropped(db, bus, inbound):
    svc, _relay = inbound
    await svc._on_moment_created(
        _event(FederationEventType.MOMENT_CREATED, _create_payload()),
    )
    # peer-b claims a reaction from uid-remote (lives on peer-a) — drop.
    await svc._on_moment_reacted(
        _event(
            FederationEventType.MOMENT_REACTED,
            {
                "moment_id": "m-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
                "emoji": "🔥",
            },
            from_instance="peer-b",
        ),
    )
    assert await svc._moment_repo.list_reactions("m-fed-1") == []


async def test_relay_swallowed_when_outbound_raises(db, bus, inbound):
    """A misbehaving outbound doesn't break the inbound dispatch loop."""
    svc, relay = inbound
    relay.relay_inbound.side_effect = RuntimeError("boom")
    # Should NOT raise — exception is logged and swallowed.
    await svc._on_moment_created(
        _event(FederationEventType.MOMENT_CREATED, _create_payload()),
    )
    assert await svc._moment_repo.get("m-fed-1") is not None


async def test_moment_authority_direct_path_with_unknown_author(db, bus, inbound):
    """Author unknown locally + sender == origin → accept on first sight."""
    svc, _relay = inbound
    # uid-stranger isn't in users / remote_users; from_instance == origin.
    await svc._on_moment_created(
        _event(
            FederationEventType.MOMENT_CREATED,
            _create_payload(author_user_id="uid-stranger"),
        ),
    )
    m = await svc._moment_repo.get("m-fed-1")
    assert m is not None and m.author_user_id == "uid-stranger"


async def test_handlers_skipped_when_moment_repo_missing(db, bus):
    user_repo = SqliteUserRepo(db)
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
    )
    fake_fed = type("F", (), {})()
    fake_fed._event_registry = type(
        "R",
        (),
        {
            "_handlers": {},
            "register": lambda self, t, h: self._handlers.__setitem__(t, h),
        },
    )()
    svc.attach_to(fake_fed)
    assert FederationEventType.MOMENT_CREATED not in fake_fed._event_registry._handlers


async def test_handlers_registered(db, bus):
    user_repo = SqliteUserRepo(db)
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
        moment_repo=SqliteMomentRepo(db),
    )
    fake_fed = type("F", (), {})()
    fake_fed._event_registry = type(
        "R",
        (),
        {
            "_handlers": {},
            "register": lambda self, t, h: self._handlers.__setitem__(t, h),
        },
    )()
    svc.attach_to(fake_fed)
    assert FederationEventType.MOMENT_CREATED in fake_fed._event_registry._handlers
    assert FederationEventType.MOMENT_DELETED in fake_fed._event_registry._handlers
    assert FederationEventType.MOMENT_REACTED in fake_fed._event_registry._handlers
    assert (
        FederationEventType.MOMENT_REACTION_REMOVED
        in fake_fed._event_registry._handlers
    )
