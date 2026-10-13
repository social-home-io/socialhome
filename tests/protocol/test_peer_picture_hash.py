"""Release-blocker protocol tests: a peer's ``picture_hash`` is never trusted.

Marked ``@pytest.mark.security`` — CLAUDE.md requires these to run before
every commit touching federation code.

Every picture hash this household hands out is its own
``compute_picture_hash(webp)`` — 16 lowercase hex characters. A peer's
payload can still carry a ``picture_hash`` of its own (when it ships no
bytes, or bytes we reject), and that string ends up interpolated into
``…/picture?v={hash}`` URLs. A hostile one (``ab&sig=evil``, ``../x``,
megabytes of text) must be dropped at the inbound boundary — every path a
peer can set a user / member / remote picture hash through is pinned here
against the REAL SQLite repos.
"""

from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from socialhome.crypto import derive_user_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.picture_hash import compute_picture_hash
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.user import DisplayableUser, UserIdentityAssertion
from socialhome.federation.sync.space.receiver import HeldBack, SpaceSyncReceiver
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteUserRepo,
)
from socialhome.repositories.profile_picture_repo import SqliteProfilePictureRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)

pytestmark = pytest.mark.security

OWN = "this-household"
PEER = "household-b"
PEER_PK = bytes.fromhex("ab" * 32)
SPACE_ID = "sp-pic"
MEMBER = "u-b"
VALID = "0123456789abcdef"

HOSTILE = [
    "ab&sig=evil",
    "../x",
    "0123456789abcdef#frag",
    '0123456789abcd"x',
    "0123456789ABCDEF",
    "a" * 10_000,
    12345,
    ["0123456789abcdef"],
]


def _event(event_type, payload, *, space_id=None) -> FederationEvent:
    return FederationEvent(
        msg_id="m-" + event_type.value,
        event_type=event_type,
        from_instance=PEER,
        to_instance=OWN,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


class _Fed:
    """Federation stub: pins the sender's identity key."""

    own_instance_id = OWN

    def __init__(self) -> None:
        self._event_registry = MagicMock()

    async def peer_identity_public_key(self, instance_id: str):
        return PEER_PK if instance_id == PEER else None


@pytest.fixture
async def stack(tmp_dir):
    db = AsyncDatabase(tmp_dir / "pichash.db", batch_timeout_ms=10)
    await db.startup()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db)
    remote = SqliteSpaceRemoteMemberRepo(db)
    pictures = SqliteProfilePictureRepo(db)
    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_inbox_url, local_inbox_id,
               status, source, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            PEER,
            "B",
            "ab" * 32,
            "enc",
            "enc",
            "https://b/wh",
            "wh-b",
            "confirmed",
            "manual",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    await spaces.save(
        Space(
            id=SPACE_ID,
            name="Shared",
            owner_instance_id=OWN,
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await spaces.save_member(
        SpaceMember(
            space_id=SPACE_ID,
            user_id=MEMBER,
            role=SpaceRole.MEMBER.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    await remote.add(
        space_id=SPACE_ID,
        instance_id=PEER,
        user_id=MEMBER,
        user_pk=None,
        display_name=None,
    )
    svc = FederationInboundService(
        bus=EventBus(),
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=spaces,
        user_repo=users,
        space_remote_member_repo=remote,
    )
    svc._federation_service = _Fed()  # type: ignore[assignment]
    receiver = SpaceSyncReceiver(
        bus=EventBus(),
        encoder=MagicMock(),
        crypto=MagicMock(),
        federation_repo=AsyncMock(),
        space_repo=spaces,
        space_post_repo=AsyncMock(),
        space_task_repo=AsyncMock(),
        page_repo=AsyncMock(),
        sticky_repo=AsyncMock(),
        space_calendar_repo=AsyncMock(),
        gallery_repo=AsyncMock(),
        profile_picture_repo=pictures,
    )
    yield SimpleNamespace(
        db=db,
        svc=svc,
        users=users,
        spaces=spaces,
        pictures=pictures,
        receiver=receiver,
    )
    await db.shutdown()


def _user_payload(picture_hash: object) -> dict:
    return {
        "user_id": derive_user_id(PEER_PK, "bob"),
        "username": "bob",
        "display_name": "Bob",
        "picture_hash": picture_hash,
    }


async def _remote_hash(stack) -> str | None:
    remote = await stack.users.get_remote(derive_user_id(PEER_PK, "bob"))
    assert remote is not None
    return remote.picture_hash


async def _member_hash(stack) -> str | None:
    member = await stack.spaces.get_member(SPACE_ID, MEMBER)
    assert member is not None
    return member.picture_hash


# ── USER_UPDATED ────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", HOSTILE)
async def test_user_updated_drops_hostile_hash(stack, bad):
    await stack.svc._on_user_updated(
        _event(FederationEventType.USER_UPDATED, _user_payload(bad))
    )
    stored = await _remote_hash(stack)
    assert stored is None
    remote = await stack.users.get_remote(derive_user_id(PEER_PK, "bob"))
    assert DisplayableUser.from_remote_user(remote).picture_url is None


async def test_user_updated_keeps_valid_hash(stack):
    await stack.svc._on_user_updated(
        _event(FederationEventType.USER_UPDATED, _user_payload(VALID))
    )
    assert await _remote_hash(stack) == VALID


# ── USERS_SYNC ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", HOSTILE)
async def test_users_sync_drops_hostile_hash(stack, bad):
    await stack.svc._on_users_sync(
        _event(FederationEventType.USERS_SYNC, {"users": [_user_payload(bad)]})
    )
    assert await _remote_hash(stack) is None


async def test_users_sync_keeps_valid_hash(stack):
    await stack.svc._on_users_sync(
        _event(FederationEventType.USERS_SYNC, {"users": [_user_payload(VALID)]})
    )
    assert await _remote_hash(stack) == VALID


# ── SPACE_MEMBER_PROFILE_UPDATED ────────────────────────────────────────


def _member_update(picture_hash: object) -> FederationEvent:
    return _event(
        FederationEventType.SPACE_MEMBER_PROFILE_UPDATED,
        {"user_id": MEMBER, "space_display_name": "B", "picture_hash": picture_hash},
        space_id=SPACE_ID,
    )


@pytest.mark.parametrize("bad", HOSTILE)
async def test_member_profile_update_drops_hostile_hash(stack, bad):
    await stack.svc._on_space_member_profile_updated(_member_update(bad))
    assert await _member_hash(stack) is None


async def test_member_profile_update_keeps_valid_hash(stack):
    await stack.svc._on_space_member_profile_updated(_member_update(VALID))
    assert await _member_hash(stack) == VALID


# ── Space sync ``member_pictures`` ──────────────────────────────────────


def _png(size: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color="green").save(buf, format="PNG")
    return buf.getvalue()


async def _sync_member_picture(stack, raw: bytes, picture_hash: object = VALID):
    await stack.receiver._persist(
        "member_pictures",
        SPACE_ID,
        [
            {
                "user_id": MEMBER,
                "picture_webp_base64": base64.b64encode(raw).decode("ascii"),
                "picture_hash": picture_hash,
                "width": 1,
                "height": 1,
            }
        ],
        provider=PEER,
        held_back=HeldBack(),
    )


@pytest.mark.parametrize("bad", HOSTILE)
async def test_member_pictures_sync_never_stores_the_peer_hash(stack, bad):
    await _sync_member_picture(stack, _png(), bad)
    got = await stack.pictures.get_member_picture(SPACE_ID, MEMBER)
    assert got is not None
    stored_bytes, stored_hash = got
    # The hash is always our own digest of the bytes we hold.
    assert stored_hash == compute_picture_hash(stored_bytes)
    assert await _member_hash(stack) == stored_hash


#: Bytes a peer might plant in the blob table that are not a picture: an
#: SVG (script-capable), HTML, a bare WebP header, plain garbage.
NOT_IMAGES = [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b"<!DOCTYPE html><html><script>alert(1)</script></html>",
    b"RIFF\x00\x00\x00\x00WEBPVP8 ",
    b"\x00garbage" * 10,
]


@pytest.mark.parametrize("raw", NOT_IMAGES)
async def test_member_pictures_sync_refuses_bytes_that_are_not_an_image(
    stack, raw, caplog
):
    """The live handlers re-encode a peer's picture so it cannot plant
    arbitrary bytes in the blob table; the §25.6 stream holds the same line:
    a record whose bytes do not decode as an image is dropped (WARNING
    names provider / space / member, never the bytes)."""
    await _sync_member_picture(stack, raw)
    assert await stack.pictures.get_member_picture(SPACE_ID, MEMBER) is None
    assert await _member_hash(stack) is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a refused picture must be logged at WARNING"
    msg = warnings[-1].getMessage()
    assert PEER in msg and SPACE_ID in msg and MEMBER in msg
    assert "script" not in msg


async def test_member_pictures_sync_strips_a_polyglot_payload(stack):
    """An image with markup riding after it (a polyglot) lands as a freshly
    encoded WebP — the trailing payload is gone."""
    raw = _png() + b"<html><script>alert(1)</script></html>"
    await _sync_member_picture(stack, raw)
    got = await stack.pictures.get_member_picture(SPACE_ID, MEMBER)
    assert got is not None
    stored, stored_hash = got
    assert b"<script>" not in stored
    assert Image.open(io.BytesIO(stored)).format == "WEBP"
    assert stored_hash == compute_picture_hash(stored)


# ── UserIdentityAssertion wire parse ────────────────────────────────────


@pytest.mark.parametrize("bad", HOSTILE)
def test_identity_assertion_parse_drops_hostile_hash(bad):
    assertion = UserIdentityAssertion.from_wire_dict(
        {
            "user_id": "u",
            "instance_id": PEER,
            "username": "bob",
            "display_name": "Bob",
            "issued_at": "2026-01-01T00:00:00+00:00",
            "signature": "sig",
            "picture_hash": bad,
        }
    )
    assert assertion.picture_hash is None
