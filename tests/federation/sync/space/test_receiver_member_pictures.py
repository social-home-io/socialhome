"""§25.6 space sync — per-space member pictures survive and land re-encoded.

Real SQLite repos: the bugs pinned here live in the interplay between the
``members`` resource's roster upsert, ``space_members.picture_hash`` and the
``member_pictures`` blob table.
"""

from __future__ import annotations

import base64
import io
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.picture_hash import compute_picture_hash
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.federation.sync.space.receiver import HeldBack, SpaceSyncReceiver
from socialhome.infrastructure.event_bus import EventBus
from socialhome.media.profile_picture import PROFILE_PICTURE_MAX_DIMENSION
from socialhome.repositories.profile_picture_repo import SqliteProfilePictureRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo

HOST = "host-household"
SPACE_ID = "sp-sync-pic"
MEMBER = "u-remote"
LOCAL = "u-local"


def _png(width: int = 500, height: int = 500, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
async def stack(tmp_dir):
    db = AsyncDatabase(tmp_dir / "syncpic.db", batch_timeout_ms=10)
    await db.startup()
    spaces = SqliteSpaceRepo(db)
    pictures = SqliteProfilePictureRepo(db)
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("local", LOCAL, "Local"),
    )
    await spaces.save(
        Space(
            id=SPACE_ID,
            name="Shared",
            owner_instance_id=HOST,
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    for uid in (MEMBER, LOCAL):
        await spaces.save_member(
            SpaceMember(
                space_id=SPACE_ID,
                user_id=uid,
                role=SpaceRole.MEMBER.value,
                joined_at="2026-01-01T00:00:00+00:00",
            )
        )
    # The host is at the current protocol version unless a test says not.
    federation_repo = AsyncMock()
    federation_repo.get_instance.return_value = SimpleNamespace(
        proto_version=FederationCapability.MIN_FOR_MEMBER_PICTURE_RESET_SYNC
    )
    receiver = SpaceSyncReceiver(
        bus=EventBus(),
        encoder=MagicMock(),
        crypto=MagicMock(),
        federation_repo=federation_repo,
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
        spaces=spaces,
        pictures=pictures,
        receiver=receiver,
        federation_repo=federation_repo,
    )
    await db.shutdown()


async def _persist(stack, resource: str, records: list[dict]) -> None:
    await stack.receiver._persist(
        resource, SPACE_ID, records, provider=HOST, held_back=HeldBack()
    )


def _picture_record(raw: bytes, user_id: str = MEMBER) -> dict:
    return {
        "space_id": SPACE_ID,
        "user_id": user_id,
        "picture_hash": "ffffffffffffffff",
        "picture_webp_base64": base64.b64encode(raw).decode("ascii"),
    }


# ── Bug 1: the ``members`` catch-up must not wipe the per-space picture ──


async def test_members_catch_up_keeps_an_existing_per_space_picture(stack):
    await stack.spaces.set_member_profile(
        SPACE_ID, MEMBER, space_display_name="Remy", picture_hash="0123456789abcdef"
    )
    # The provider's record exactly as ``MembersExporter`` ships it.
    record = asdict(
        SpaceMember(
            space_id=SPACE_ID,
            user_id=MEMBER,
            role=SpaceRole.ADMIN.value,
            joined_at="2026-01-01T00:00:00+00:00",
            space_display_name="Remy R.",
            picture_hash="fedcba9876543210",
        )
    )
    await _persist(stack, "members", [record])
    member = await stack.spaces.get_member(SPACE_ID, MEMBER)
    assert member is not None
    assert member.role == SpaceRole.ADMIN.value
    # The record carries the display name: it converges.
    assert member.space_display_name == "Remy R."
    # The picture hash is ours (the provider's is untrusted): untouched.
    assert member.picture_hash == "0123456789abcdef"


async def test_members_catch_up_seats_a_new_member_without_a_picture(stack):
    record = {
        "user_id": "u-new",
        "role": "member",
        "joined_at": "2026-01-01T00:00:00+00:00",
        "picture_hash": "ab&sig=evil",
    }
    await _persist(stack, "members", [record])
    member = await stack.spaces.get_member(SPACE_ID, "u-new")
    assert member is not None
    assert member.picture_hash is None


# ── Bug 2: ``member_pictures`` re-encodes like the live handlers ──────────


async def test_member_picture_is_reencoded_and_its_hash_is_ours(stack):
    raw = _png()
    await _persist(stack, "member_pictures", [_picture_record(raw)])
    got = await stack.pictures.get_member_picture(SPACE_ID, MEMBER)
    assert got is not None
    stored, stored_hash = got
    assert stored != raw
    img = Image.open(io.BytesIO(stored))
    assert img.format == "WEBP"
    assert img.size == (PROFILE_PICTURE_MAX_DIMENSION, PROFILE_PICTURE_MAX_DIMENSION)
    assert stored_hash == compute_picture_hash(stored)
    # ``space_members`` points at the blob we hold, so the picture renders
    # for a fresh joiner too.
    member = await stack.spaces.get_member(SPACE_ID, MEMBER)
    assert member is not None
    assert member.picture_hash == stored_hash


async def test_members_then_member_pictures_catch_up_round_trip(stack):
    """The whole catch-up in exporter order keeps a working avatar."""
    await stack.spaces.set_member_profile(
        SPACE_ID, MEMBER, picture_hash="0123456789abcdef"
    )
    await _persist(
        stack,
        "members",
        [{"user_id": MEMBER, "role": "member", "joined_at": "2026-01-01"}],
    )
    await _persist(stack, "member_pictures", [_picture_record(_png(color="blue"))])
    member = await stack.spaces.get_member(SPACE_ID, MEMBER)
    got = await stack.pictures.get_member_picture(SPACE_ID, MEMBER)
    assert member is not None and got is not None
    assert member.picture_hash == got[1]


async def test_member_picture_of_our_own_person_is_never_taken(stack):
    """Our people's per-space pictures are authored here — a provider's
    (re-encoded) copy never replaces them."""
    await stack.spaces.set_member_profile(
        SPACE_ID, LOCAL, picture_hash="0123456789abcdef"
    )
    await _persist(stack, "member_pictures", [_picture_record(_png(), LOCAL)])
    assert await stack.pictures.get_member_picture(SPACE_ID, LOCAL) is None
    member = await stack.spaces.get_member(SPACE_ID, LOCAL)
    assert member is not None
    assert member.picture_hash == "0123456789abcdef"


async def test_a_truncated_picture_is_dropped_and_the_rest_of_the_chunk_lands(
    stack, caplog
):
    """A corrupt record (here: a truncated PNG — Pillow opens the header,
    the decode fails later with ``OSError``) is dropped with a WARNING; the
    next record in the same chunk is still stored and the stream stays
    clean — so the catch-up does not fail on it forever."""
    other = "u-other"
    await stack.spaces.save_member(
        SpaceMember(
            space_id=SPACE_ID,
            user_id=other,
            role=SpaceRole.MEMBER.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    truncated = _png()[:200]
    clean = await stack.receiver._dispatch(
        "member_pictures",
        SPACE_ID,
        [_picture_record(truncated), _picture_record(_png(color="blue"), other)],
        provider=HOST,
    )
    assert clean is True
    assert await stack.pictures.get_member_picture(SPACE_ID, MEMBER) is None
    assert await stack.pictures.get_member_picture(SPACE_ID, other) is not None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(MEMBER in r.getMessage() for r in warnings)
    b64 = _picture_record(truncated)["picture_webp_base64"]
    for r in caplog.records:
        assert b64[:40] not in r.getMessage()


# ── Bug 3: a picture reset missed while offline converges on catch-up ──


async def _stale_picture(stack, user_id: str = MEMBER) -> str:
    """Seat ``user_id`` with a per-space picture we hold (hash + blob), as
    a live ``SPACE_MEMBER_PROFILE_UPDATED`` left it before we went offline."""
    webp = b"RIFF-stale-webp"
    hash_ = compute_picture_hash(webp)
    await stack.pictures.set_member_picture(
        SPACE_ID, user_id, bytes_webp=webp, hash=hash_, width=1, height=1
    )
    await stack.spaces.set_member_profile(
        SPACE_ID, user_id, space_display_name="Remy", picture_hash=hash_
    )
    return hash_


def _members_record(user_id: str = MEMBER, **extra) -> dict:
    record = asdict(
        SpaceMember(
            space_id=SPACE_ID,
            user_id=user_id,
            role=SpaceRole.MEMBER.value,
            joined_at="2026-01-01T00:00:00+00:00",
            space_display_name="Remy",
        )
    )
    record.update(extra)
    return record


async def _still_stale(stack, hash_: str, user_id: str = MEMBER) -> None:
    member = await stack.spaces.get_member(SPACE_ID, user_id)
    assert member is not None
    assert member.picture_hash == hash_
    assert await stack.pictures.get_member_picture(SPACE_ID, user_id) is not None


async def _cleared(stack, user_id: str = MEMBER) -> None:
    member = await stack.spaces.get_member(SPACE_ID, user_id)
    assert member is not None
    assert member.picture_hash is None
    # The display name is not part of the reset.
    assert member.space_display_name == "Remy"
    assert await stack.pictures.get_member_picture(SPACE_ID, user_id) is None


async def test_host_members_record_with_no_picture_clears_a_stale_one(stack):
    """The member reset their per-space picture while we were offline: the
    host's ``members`` record says ``picture_hash: null`` (and
    ``member_pictures`` ships nothing for them) — our stale copy goes."""
    await _stale_picture(stack)
    await _persist(stack, "members", [_members_record(picture_hash=None)])
    await _cleared(stack)


async def test_reset_from_a_host_below_the_capability_is_not_trusted(stack):
    """Before v_58 a host's roster upsert could wipe a seat's hash (#910),
    so its ``null`` may be damage, not a reset: keep ours (degraded
    fallback — a live event still converges it)."""
    hash_ = await _stale_picture(stack)
    stack.federation_repo.get_instance.return_value = SimpleNamespace(
        proto_version=FederationCapability.MIN_FOR_MEMBER_PICTURE_RESET_SYNC - 1
    )
    await _persist(stack, "members", [_members_record(picture_hash=None)])
    await _still_stale(stack, hash_)


async def test_reset_from_an_unknown_provider_version_is_not_trusted(stack):
    hash_ = await _stale_picture(stack)
    stack.federation_repo.get_instance.return_value = None
    stack.federation_repo.get_space_member_version.return_value = None
    await _persist(stack, "members", [_members_record(picture_hash=None)])
    await _still_stale(stack, hash_)


async def test_reset_from_a_mesh_only_host_uses_its_version_claim(stack):
    await _stale_picture(stack)
    stack.federation_repo.get_instance.return_value = None
    stack.federation_repo.get_space_member_version.return_value = (
        FederationCapability.MIN_FOR_MEMBER_PICTURE_RESET_SYNC,
        "2026-10-13T00:00:00+00:00",
    )
    await _persist(stack, "members", [_members_record(picture_hash=None)])
    await _cleared(stack)


async def test_reset_from_a_delegated_admin_not_speaking_for_the_member(stack):
    """A delegated admin household's roster is taken like the host's, but
    only the member's own household (or the owner) decides about their
    picture — an admin household's ``null`` for someone else's member is
    no reset."""
    hash_ = await _stale_picture(stack)
    authorship = MagicMock()
    authorship.acts_for = AsyncMock(return_value=False)
    stack.receiver._authorship = authorship
    await stack.receiver._persist(
        "members",
        SPACE_ID,
        [_members_record(picture_hash=None)],
        provider="admin-household",
        held_back=HeldBack(),
    )
    await _still_stale(stack, hash_)
    authorship.acts_for.assert_awaited()
    event = authorship.acts_for.await_args.args[0]
    assert event.from_instance == "admin-household"
    assert authorship.acts_for.await_args.kwargs == {"any_role": True}


async def test_reset_from_the_members_own_household_clears_it(stack):
    await _stale_picture(stack)
    authorship = MagicMock()
    authorship.acts_for = AsyncMock(return_value=True)
    stack.receiver._authorship = authorship
    await stack.receiver._persist(
        "members",
        SPACE_ID,
        [_members_record(picture_hash=None)],
        provider="admin-household",
        held_back=HeldBack(),
    )
    await _cleared(stack)


async def test_reset_from_a_non_owner_without_an_authorship_binder(stack):
    hash_ = await _stale_picture(stack)
    await stack.receiver._persist(
        "members",
        SPACE_ID,
        [_members_record(picture_hash=None)],
        provider="admin-household",
        held_back=HeldBack(),
    )
    await _still_stale(stack, hash_)


async def test_reset_never_touches_our_own_people(stack):
    hash_ = await _stale_picture(stack, LOCAL)
    await _persist(stack, "members", [_members_record(LOCAL, picture_hash=None)])
    await _still_stale(stack, hash_, LOCAL)


async def test_members_record_without_a_picture_key_changes_nothing(stack):
    """An older provider shape with no ``picture_hash`` key says nothing
    about the picture."""
    hash_ = await _stale_picture(stack)
    record = _members_record()
    del record["picture_hash"]
    await _persist(stack, "members", [record])
    await _still_stale(stack, hash_)


async def test_a_non_null_provider_hash_is_never_taken(stack):
    """A provider's hash is untrusted: non-null is a no-op here (the
    ``member_pictures`` resource ships and re-hashes the bytes)."""
    hash_ = await _stale_picture(stack)
    await _persist(stack, "members", [_members_record(picture_hash="fedcba9876543210")])
    await _still_stale(stack, hash_)


async def test_a_changed_picture_still_lands_through_member_pictures(stack):
    """The picture changed (not cleared) while we were offline: ``members``
    carries the host's non-null hash (no-op), then ``member_pictures`` in
    the same session replaces the blob and points the seat at it."""
    stale = await _stale_picture(stack)
    await _persist(stack, "members", [_members_record(picture_hash="fedcba9876543210")])
    await _persist(stack, "member_pictures", [_picture_record(_png(color="blue"))])
    member = await stack.spaces.get_member(SPACE_ID, MEMBER)
    got = await stack.pictures.get_member_picture(SPACE_ID, MEMBER)
    assert member is not None and got is not None
    assert member.picture_hash == got[1] != stale


async def test_reset_of_a_member_without_a_picture_is_a_no_op(stack):
    await _persist(stack, "members", [_members_record(picture_hash=None)])
    member = await stack.spaces.get_member(SPACE_ID, MEMBER)
    assert member is not None and member.picture_hash is None
    assert await stack.pictures.get_member_picture(SPACE_ID, MEMBER) is None
