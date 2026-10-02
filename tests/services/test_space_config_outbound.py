"""SpaceConfigOutbound — broadcast SPACE_CONFIG_CHANGED on every config edit.

Before this service the bus event fired locally but never reached remote
member stubs in realtime — only the §D1b catch-up reply
(``_push_config_to``) ever shipped the federation event. Toggling
``location_mode`` on the host left every remote stub with the prior
mode and silently broke the space map (the API's strict
``mode_filter`` filtered out otherwise-valid pin rows).
"""

from __future__ import annotations

import base64
import dataclasses
import io
import random
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from socialhome.crypto import generate_space_keypair
from socialhome.domain.media_constraints import (
    SPACE_COVER_BOOTSTRAP_MAX_BYTES,
    SPACE_COVER_SNAPSHOT_MAX_BYTES,
)
from socialhome.services.space_crypto_service import (
    strip_authority_sig_fields,
    verify_authority_event,
)
from socialhome.domain.events import SpaceConfigChanged
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.space_config_outbound import SpaceConfigOutbound


def _make_space(
    *,
    space_id: str = "sp-1",
    owner_instance_id: str = "inst-self",
    location_mode: str = "gps",
    config_sequence: int = 5,
    name: str = "Living Room",
) -> Space:
    return Space(
        id=space_id,
        name=name,
        owner_instance_id=owner_instance_id,
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=config_sequence,
        features=SpaceFeatures(location=True, location_mode=location_mode),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


@pytest.fixture
def fed():
    f = MagicMock()
    f.broadcast_to_space_members = AsyncMock()
    f._own_instance_id = "inst-self"
    return f


@pytest.fixture
def space_repo():
    r = MagicMock()
    r.get = AsyncMock(return_value=_make_space())
    # Default: no signing seed held → broadcasts go out UNSIGNED (back-compat
    # path for a pre-v_24 / pre-Phase-0 owned space). v_24 signing tests set a
    # real seed on the repo explicitly.
    r.get_space_seed = AsyncMock(return_value=None)
    return r


async def test_config_changed_broadcasts_post_mutation_snapshot(
    fed,
    space_repo,
):
    """The bus event carries the changed-fields dict; the broadcast
    must carry the FULL post-mutation snapshot so remote stubs can
    apply via ``stub_space_from_metadata``."""
    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()

    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="feature_changed",
            payload={"features": {"location_mode": "gps"}},
            sequence=5,
        ),
    )

    fed.broadcast_to_space_members.assert_awaited_once()
    call = fed.broadcast_to_space_members.await_args
    assert call.args[0] == "sp-1"
    assert call.args[1] == FederationEventType.SPACE_CONFIG_CHANGED
    payload = call.args[2]
    assert payload["space_id"] == "sp-1"
    assert payload["sequence"] == 5
    assert payload["event_type"] == "feature_changed"
    # Flat legacy fields ship for pre-§D1b consumers.
    assert payload["name"] == "Living Room"
    assert payload["join_mode"] == "invite_only"
    # space_meta carries the modern shape the inbound handler reads.
    assert payload["space_meta"]["name"] == "Living Room"
    assert payload["space_meta"]["features"]["location_mode"] == "gps"


async def test_dissolved_is_skipped_here(fed, space_repo):
    """Dissolve must NOT emit SPACE_CONFIG_CHANGED (it would refresh +
    resurrect members' stubs). ``SpaceService.dissolve_space`` broadcasts
    the dedicated SPACE_DISSOLVED itself, so this subscriber skips the
    event entirely."""
    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="dissolved",
            payload={},
            sequence=6,
        ),
    )
    fed.broadcast_to_space_members.assert_not_awaited()


@pytest.mark.parametrize(
    "event_type",
    [
        "admin_granted",
        "admin_revoked",
        "role_changed",
        "member_banned",
        "member_unbanned",
    ],
)
async def test_roster_events_are_not_federated_as_config(fed, space_repo, event_type):
    """Role / ban / unban events ride the LOCAL bus (realtime/UI) but federate
    via roster gossip (or are host-local), NOT as SPACE_CONFIG_CHANGED. They no
    longer advance config_sequence, so re-broadcasting them as config would at
    best be a no-op and at worst perturb a receiver's config_author at an equal
    sequence."""
    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type=event_type,
            payload={"user_id": "u1"},
            sequence=5,
        ),
    )
    fed.broadcast_to_space_members.assert_not_awaited()


@pytest.mark.parametrize("event_type", ["rename", "feature_changed"])
async def test_real_config_edits_are_still_federated(fed, space_repo, event_type):
    """A genuine config edit (rename / feature toggle) still federates as
    SPACE_CONFIG_CHANGED — only roster/moderation events are skipped."""
    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type=event_type,
            payload={},
            sequence=5,
        ),
    )
    fed.broadcast_to_space_members.assert_awaited_once()
    assert (
        fed.broadcast_to_space_members.await_args.args[1]
        == FederationEventType.SPACE_CONFIG_CHANGED
    )


async def test_config_changed_skipped_when_not_owner(fed):
    """Only the owner host broadcasts — a remote-stub holder must
    not re-broadcast somebody else's space config."""
    bus = EventBus()
    remote_owned = _make_space(owner_instance_id="inst-remote")
    space_repo = MagicMock()
    space_repo.get = AsyncMock(return_value=remote_owned)
    space_repo.get_space_seed = AsyncMock(return_value=None)
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="rename",
            payload={"name": "Renamed"},
            sequence=2,
        ),
    )
    fed.broadcast_to_space_members.assert_not_awaited()


async def test_config_changed_skipped_when_space_missing(fed):
    bus = EventBus()
    space_repo = MagicMock()
    space_repo.get = AsyncMock(return_value=None)
    space_repo.get_space_seed = AsyncMock(return_value=None)
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-gone",
            event_type="rename",
            payload={"name": "Nope"},
            sequence=1,
        ),
    )
    fed.broadcast_to_space_members.assert_not_awaited()


async def test_config_changed_broadcast_failure_is_swallowed(space_repo):
    bus = EventBus()
    fed = MagicMock()
    fed._own_instance_id = "inst-self"
    fed.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down"),
    )
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    # Must not raise.
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="rename",
            payload={"name": "X"},
            sequence=1,
        ),
    )


# ─── v_24: authority-signed config broadcasts ──────────────────────────


async def test_config_changed_authority_signed_when_seed_held(fed):
    """A seed-holder (owner OR delegated admin) signs the broadcast — the
    space_meta carries an authority_sig that verifies against the space pubkey,
    and the broadcast is gated on MIN_FOR_ADMIN_AUTHORITATIVE_OPS."""
    from socialhome.crypto import generate_space_keypair
    from socialhome.domain.federation_capabilities import FederationCapability
    from socialhome.services.space_crypto_service import (
        strip_authority_sig_fields,
        verify_authority_event,
    )

    kp = generate_space_keypair()
    # A space hosted ELSEWHERE for which THIS household holds the seed
    # (delegated admin) — the owner is offline.
    space = _make_space(owner_instance_id="inst-remote")
    space = type(space)(  # rebuild with the real space pubkey
        **{
            **{f: getattr(space, f) for f in space.__slots__},
            "identity_public_key": kp.public_key.hex(),
        }
    )
    space_repo = MagicMock()
    space_repo.get = AsyncMock(return_value=space)
    space_repo.get_space_seed = AsyncMock(return_value=kp.private_key)

    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="rename",
            payload={"name": "Living Room"},
            sequence=5,
        ),
    )

    fed.broadcast_to_space_members.assert_awaited_once()
    call = fed.broadcast_to_space_members.await_args
    payload = call.args[2]
    meta = payload["space_meta"]
    assert meta["authority_sig"]
    assert meta["authority_sig_suite"] == "ed25519"
    # The signed author is THIS household (the editing seed-holder).
    assert meta["config_author_instance"] == "inst-self"
    # The signature verifies against the space public key.
    assert verify_authority_event(
        event_type="space_config_changed",
        space_id="sp-1",
        payload=strip_authority_sig_fields(meta),
        authority_sig=meta["authority_sig"],
        authority_sig_suite=meta["authority_sig_suite"],
        space_public_key=kp.public_key,
    )
    # Gated on the v_24 capability.
    assert (
        call.kwargs.get("min_proto_version")
        == FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS
    )


async def test_config_changed_unsigned_when_no_seed(fed, space_repo):
    """Owner host without a stored seed (pre-Phase-0) still broadcasts, but
    UNSIGNED — back-compat. No authority_sig, no version gate."""
    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="rename",
            payload={"name": "Living Room"},
            sequence=5,
        ),
    )
    fed.broadcast_to_space_members.assert_awaited_once()
    call = fed.broadcast_to_space_members.await_args
    meta = call.args[2]["space_meta"]
    assert "authority_sig" not in meta
    assert call.kwargs.get("min_proto_version") is None


# ─── §CP.F1: age-gate changes federate to member stubs ──────────────────


async def test_age_gate_change_broadcasts_to_members(fed, space_repo):
    """A CpSpaceAgeGateChanged on the host broadcasts SPACE_AGE_GATE_UPDATED
    so member households update their stub's min_age (the inbound counterpart
    is space_membership._on_age_gate)."""
    from socialhome.domain.events import CpSpaceAgeGateChanged

    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()

    await bus.publish(
        CpSpaceAgeGateChanged(space_id="sp-1", min_age=18),
    )

    fed.broadcast_to_space_members.assert_awaited_once()
    call = fed.broadcast_to_space_members.await_args
    assert call.args[0] == "sp-1"
    assert call.args[1] == FederationEventType.SPACE_AGE_GATE_UPDATED
    assert call.args[2] == {
        "space_id": "sp-1",
        "min_age": 18,
    }


async def test_age_gate_change_on_non_host_is_not_broadcast(fed):
    """Only the owner host federates the gate — a stub on a member household
    has no authority to push the gate around."""
    from socialhome.domain.events import CpSpaceAgeGateChanged

    repo = MagicMock()
    # Owned by someone else → this household is just a member stub.
    repo.get = AsyncMock(return_value=_make_space(owner_instance_id="inst-other"))
    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=repo,
    ).wire()

    await bus.publish(
        CpSpaceAgeGateChanged(space_id="sp-1", min_age=13),
    )
    fed.broadcast_to_space_members.assert_not_awaited()


# ─── A cover / icon change ships the image, not just its hash ─────────
#
# ``SPACE_CONFIG_CHANGED`` used to carry only ``cover_hash`` / ``icon_hash``
# for a cover or icon change, so no member ever received the new bytes and
# every joiner kept the image from its join time.


def _webp(width: int, height: int, *, noisy: bool = False) -> bytes:
    if noisy:
        img = Image.frombytes(
            "RGB",
            (width, height),
            random.Random(width * height).randbytes(width * height * 3),
        )
    else:
        img = Image.new("RGB", (width, height), (40, 120, 200))
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=90)
    return out.getvalue()


def _image_repo(data: bytes | None, hash_: str = "h-new"):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=(data, hash_) if data is not None else None)
    return repo


def _space_with(**kw) -> Space:
    return dataclasses.replace(_make_space(), **kw)


async def _publish(outbound: SpaceConfigOutbound, bus, event_type: str) -> None:
    outbound.wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type=event_type,
            payload={},
            sequence=6,
        ),
    )


async def test_a_cover_change_ships_the_cover_bytes(fed, space_repo):
    cover = _webp(64, 32)
    space_repo.get = AsyncMock(return_value=_space_with(cover_hash="h-new"))
    bus = EventBus()
    await _publish(
        SpaceConfigOutbound(
            bus=bus,
            federation_service=fed,
            space_repo=space_repo,
            cover_repo=_image_repo(cover),
            icon_repo=_image_repo(_webp(16, 16)),
        ),
        bus,
        "cover_updated",
    )
    call = fed.broadcast_to_space_members.await_args
    meta = call.args[2]["space_meta"]
    assert meta["cover_hash"] == "h-new"
    assert base64.b64decode(meta["cover_webp_base64"]) == cover
    # Only the image that changed rides the event.
    assert "icon_webp_base64" not in meta
    # A small cover fits the relay bound too, so both variants carry it.
    relay_meta = call.kwargs["relay_payload"]["space_meta"]
    assert base64.b64decode(relay_meta["cover_webp_base64"]) == cover


async def test_a_link_joined_member_gets_a_rendition_that_fits_the_relay(
    fed,
    space_repo,
):
    """A paired peer's envelope takes a cover up to the snapshot bound; the
    connection-server relay a link-joined member sits behind takes only the
    bootstrap bound, so that member is sent a smaller rendition."""
    cover = _webp(400, 300, noisy=True)
    assert SPACE_COVER_BOOTSTRAP_MAX_BYTES < len(cover)
    assert len(cover) <= SPACE_COVER_SNAPSHOT_MAX_BYTES
    space_repo.get = AsyncMock(return_value=_space_with(cover_hash="h-new"))
    bus = EventBus()
    await _publish(
        SpaceConfigOutbound(
            bus=bus,
            federation_service=fed,
            space_repo=space_repo,
            cover_repo=_image_repo(cover),
        ),
        bus,
        "cover_updated",
    )
    call = fed.broadcast_to_space_members.await_args
    full = base64.b64decode(call.args[2]["space_meta"]["cover_webp_base64"])
    relay = base64.b64decode(
        call.kwargs["relay_payload"]["space_meta"]["cover_webp_base64"],
    )
    assert full == cover
    assert len(relay) <= SPACE_COVER_BOOTSTRAP_MAX_BYTES
    assert Image.open(io.BytesIO(relay)).format == "WEBP"


async def test_an_icon_change_ships_the_icon_bytes(fed, space_repo):
    icon = _webp(32, 32)
    space_repo.get = AsyncMock(
        return_value=_space_with(cover_hash="h-cover", icon_hash="h-icon"),
    )
    bus = EventBus()
    await _publish(
        SpaceConfigOutbound(
            bus=bus,
            federation_service=fed,
            space_repo=space_repo,
            cover_repo=_image_repo(_webp(64, 32), "h-cover"),
            icon_repo=_image_repo(icon, "h-icon"),
        ),
        bus,
        "icon_updated",
    )
    meta = fed.broadcast_to_space_members.await_args.args[2]["space_meta"]
    assert base64.b64decode(meta["icon_webp_base64"]) == icon
    assert "cover_webp_base64" not in meta


async def test_a_rename_ships_no_image_bytes(fed, space_repo):
    """Image bytes ride only the change that altered them — a rename must
    not re-send a few hundred KiB to every member household."""
    space_repo.get = AsyncMock(return_value=_space_with(cover_hash="h-new"))
    bus = EventBus()
    await _publish(
        SpaceConfigOutbound(
            bus=bus,
            federation_service=fed,
            space_repo=space_repo,
            cover_repo=_image_repo(_webp(64, 32)),
        ),
        bus,
        "rename",
    )
    call = fed.broadcast_to_space_members.await_args
    assert "cover_webp_base64" not in call.args[2]["space_meta"]
    assert "relay_payload" not in call.kwargs


async def test_a_cleared_cover_ships_no_bytes(fed, space_repo):
    space_repo.get = AsyncMock(return_value=_space_with(cover_hash=None))
    bus = EventBus()
    await _publish(
        SpaceConfigOutbound(
            bus=bus,
            federation_service=fed,
            space_repo=space_repo,
            cover_repo=_image_repo(None),
        ),
        bus,
        "cover_updated",
    )
    call = fed.broadcast_to_space_members.await_args
    meta = call.args[2]["space_meta"]
    assert meta["cover_hash"] is None
    assert "cover_webp_base64" not in meta
    assert "relay_payload" not in call.kwargs


async def test_the_authority_signature_covers_each_variant(fed):
    """The bytes live inside the signed ``space_meta``, so a relay cannot
    swap the picture — and each variant is signed over its own bytes."""
    kp = generate_space_keypair()
    space = _space_with(identity_public_key=kp.public_key.hex(), cover_hash="h")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=space)
    repo.get_space_seed = AsyncMock(return_value=kp.private_key)
    bus = EventBus()
    await _publish(
        SpaceConfigOutbound(
            bus=bus,
            federation_service=fed,
            space_repo=repo,
            cover_repo=_image_repo(_webp(400, 300, noisy=True), "h"),
        ),
        bus,
        "cover_updated",
    )
    call = fed.broadcast_to_space_members.await_args
    variants = [call.args[2]["space_meta"], call.kwargs["relay_payload"]["space_meta"]]
    assert variants[0]["cover_webp_base64"] != variants[1]["cover_webp_base64"]
    for meta in variants:
        assert verify_authority_event(
            event_type="space_config_changed",
            space_id="sp-1",
            payload=strip_authority_sig_fields(meta),
            authority_sig=meta["authority_sig"],
            authority_sig_suite=meta["authority_sig_suite"],
            space_public_key=kp.public_key,
        )


async def test_owner_after_rotation_signs_with_the_cert_and_reaches_old_peers_unsigned():
    """v_44 — once the authority key rotated, the owner's config ships the
    cert inside the signed space_meta; a household below v_44 (still
    pinned to the old key) gets the UNSIGNED owner variant instead."""
    from socialhome.authority_cert import verify_authority_cert
    from socialhome.crypto import derive_instance_id, generate_identity_keypair
    from socialhome.domain.federation_capabilities import FederationCapability

    owner = generate_identity_keypair()
    own = derive_instance_id(owner.public_key)
    k2 = generate_space_keypair()
    space = dataclasses.replace(
        _make_space(owner_instance_id=own),
        identity_public_key=k2.public_key.hex(),
        authority_key_epoch=1,
    )
    fed = MagicMock()
    fed.broadcast_to_space_members = AsyncMock()
    fed._own_instance_id = own
    fed.own_instance_id = own
    fed.own_identity_seed = owner.private_key
    fed.own_identity_pk = owner.public_key
    repo = MagicMock()
    repo.get = AsyncMock(return_value=space)
    repo.get_space_seed = AsyncMock(return_value=k2.private_key)
    bus = EventBus()
    SpaceConfigOutbound(bus=bus, federation_service=fed, space_repo=repo).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1", event_type="rename", payload={"name": "x"}, sequence=5
        )
    )
    call = fed.broadcast_to_space_members.await_args
    meta = call.args[2]["space_meta"]
    got = verify_authority_cert(
        meta["authority_cert"], space_id="sp-1", owner_instance_id=own
    )
    assert (got.authority_pk_hex, got.key_epoch) == (k2.public_key.hex(), 1)
    assert meta["authority_key_epoch"] == 1
    assert verify_authority_event(
        event_type="space_config_changed",
        space_id="sp-1",
        payload=strip_authority_sig_fields(meta),
        authority_sig=meta["authority_sig"],
        authority_sig_suite=meta["authority_sig_suite"],
        space_public_key=k2.public_key,
    )
    legacy = call.kwargs["legacy_payload"]["space_meta"]
    assert "authority_sig" not in legacy and "authority_cert" not in legacy
    assert (
        call.kwargs["legacy_below"]
        == FederationCapability.MIN_FOR_SPACE_AUTHORITY_ROTATION
    )
