"""``receive_zones``: space zones by §25.6 sync."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from tests.federation.sync.space.receiver_fakes import (
    _CalendarRepoStub,
    _FakeCrypto,
    _FakeFedRepo,
    _FakeRepos,
    _GalleryRepoStub,
    _PageRepoStub,
    _PostRepoStub,
    _send,
    _SpaceRepoStub,
    _StickyRepoStub,
    _TaskRepoStub,
)


async def test_space_zones(setup):
    """The zone catalogue (§23.8.7) rides the chunked sync so a remote
    member instance joining mid-life picks up every zone, not only
    the ones added after the join."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "space_zones",
        [
            {
                "id": "z_office",
                "space_id": "sp-1",
                "name": "Office",
                "latitude": 47.3769,
                "longitude": 8.5417,
                "radius_m": 150,
                "color": "#3b82f6",
                "created_by": "u-1",
                "created_at": "2026-04-27T00:00:00+00:00",
                "updated_at": "2026-04-27T00:00:00+00:00",
            },
        ],
    )
    assert len(c.zones) == 1
    z = c.zones[0]
    assert z.id == "z_office"
    assert z.name == "Office"
    assert z.latitude == 47.3769
    assert z.radius_m == 150
    assert z.color == "#3b82f6"


async def test_space_zones_never_land_on_a_follower_only_household(setup):
    """The members' places are not a follower's (owner decision 13): an
    older sender — or any provider — streaming ``space_zones`` /
    ``space_zones_deleted`` to a household whose local users only follow
    the space stores nothing, like the live ``SPACE_ZONE_UPSERTED`` (#1006)."""
    r, c, kp = setup
    c.follower_only = True
    await _send(r, kp, "space_zones", [dict(_GOOD_ZONE_RECORD)])
    assert c.zones == []
    await _send(r, kp, "space_zones_deleted", [{"id": "z_ok"}], sync_id="sync-2")
    assert c.zones == []


async def test_space_zones_malformed_record_dropped(setup):
    """Lenient receiver: a record missing latitude is dropped, others
    in the same chunk still apply."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "space_zones",
        [
            {"id": "z_bad", "name": "NoCoords"},  # malformed
            {
                "id": "z_ok",
                "name": "Office",
                "latitude": 47.0,
                "longitude": 8.0,
                "radius_m": 200,
                "created_by": "u-1",
                "created_at": "2026-04-27T00:00:00+00:00",
                "updated_at": "2026-04-27T00:00:00+00:00",
            },
        ],
    )
    assert [z.id for z in c.zones] == ["z_ok"]


_GOOD_ZONE_RECORD = {
    "id": "z_ok",
    "name": "Office",
    "latitude": 47.0,
    "longitude": 8.0,
    "radius_m": 200,
    "created_by": "u-1",
    "created_at": "2026-04-27T00:00:00+00:00",
    "updated_at": "2026-04-27T00:00:00+00:00",
}


@pytest.mark.parametrize(
    "over",
    [
        {"name": "x" * 10_240},
        {"name": "Of\x1bfice<img src=x>"},
        {"name": " "},
        {"color": "red;background:url(x)"},
    ],
    ids=["10kb-name", "control-char-name", "blank-name", "css-color"],
)
async def test_space_zones_invalid_display_data_dropped(setup, caplog, over):
    """A synced zone with a hostile name or colour is dropped (WARNING with
    space id + provider, never the name); the rest of the chunk applies."""
    r, c, kp = setup
    with caplog.at_level("WARNING"):
        await _send(
            r,
            kp,
            "space_zones",
            [dict(_GOOD_ZONE_RECORD, id="z_bad", **over), dict(_GOOD_ZONE_RECORD)],
        )
    assert [z.id for z in c.zones] == ["z_ok"]
    warnings = [x for x in caplog.records if x.levelname == "WARNING"]
    text = " ".join(x.getMessage() for x in warnings)
    assert "z_bad" in text and "sp-1" in text and "peer-a" in text
    assert "<img" not in text and "xxxxxxxx" not in text
    assert "background" not in text


async def test_space_zones_normalises_valid_display_data(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "space_zones",
        [dict(_GOOD_ZONE_RECORD, name="  Office ", color="#ABCDEF")],
    )
    assert c.zones[0].name == "Office"
    assert c.zones[0].color == "#abcdef"


async def test_space_zones_icon_applied(setup):
    r, c, kp = setup
    await _send(r, kp, "space_zones", [dict(_GOOD_ZONE_RECORD, icon="🏫")])
    assert c.zones[-1].icon == "🏫"


async def test_space_zones_missing_icon_keeps_the_stored_icon(setup):
    """A record from an older (pre-icon) provider carries no ``icon`` key:
    the stored icon stays rather than being wiped. Explicit null clears."""
    r, c, kp = setup
    await _send(r, kp, "space_zones", [dict(_GOOD_ZONE_RECORD, icon="🏫")])
    await _send(r, kp, "space_zones", [dict(_GOOD_ZONE_RECORD, radius_m=300)])
    assert c.zones[-1].radius_m == 300
    assert c.zones[-1].icon == "🏫"
    await _send(r, kp, "space_zones", [dict(_GOOD_ZONE_RECORD, icon=None)])
    assert c.zones[-1].icon is None


async def test_space_zones_missing_icon_never_copies_another_spaces_icon(setup):
    """The keep-the-stored-icon lookup is by zone id only: a row with the
    same id in ANOTHER space must never lend this space its icon."""
    r, c, kp = setup
    r._zone_repo.stored["z_ok"] = SimpleNamespace(
        id="z_ok", space_id="sp-other", icon="🏫"
    )
    await _send(r, kp, "space_zones", [dict(_GOOD_ZONE_RECORD)])
    assert c.zones[-1].space_id == "sp-1"
    assert c.zones[-1].icon is None


async def test_space_zones_missing_icon_new_zone_has_none(setup):
    r, c, kp = setup
    await _send(r, kp, "space_zones", [dict(_GOOD_ZONE_RECORD)])
    assert c.zones[-1].icon is None


@pytest.mark.parametrize("icon", ["school", "🏫🏠", "", "<b>", 5])
async def test_space_zones_invalid_icon_dropped(setup, caplog, icon):
    r, c, kp = setup
    with caplog.at_level("WARNING"):
        await _send(
            r,
            kp,
            "space_zones",
            [dict(_GOOD_ZONE_RECORD, id="z_bad", icon=icon), dict(_GOOD_ZONE_RECORD)],
        )
    assert [z.id for z in c.zones] == ["z_ok"]
    text = " ".join(x.getMessage() for x in caplog.records)
    assert "z_bad" in text and "icon" in text


async def test_space_zones_skipped_when_repo_not_wired(bus, peer):
    """An older deployment without a zone repo wired silently skips
    inbound zone chunks rather than erroring."""
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        # zone_repo deliberately omitted
    )
    await _send(
        r,
        peer_kp,
        "space_zones",
        [
            {
                "id": "z_office",
                "name": "Office",
                "latitude": 47.0,
                "longitude": 8.0,
                "radius_m": 200,
                "created_by": "u-1",
                "created_at": "2026-04-27T00:00:00+00:00",
                "updated_at": "2026-04-27T00:00:00+00:00",
            },
        ],
    )
    assert c_zones_count(collector) == 0


def c_zones_count(collector) -> int:
    return len(collector.zones)


@pytest.mark.parametrize(
    "over",
    [
        {"latitude": "nan"},
        {"longitude": "inf"},
        {"latitude": float("-inf")},
        {"latitude": 90.5},
        {"longitude": -180.01},
        {"radius_m": -5},
        {"radius_m": 0},
        {"radius_m": 10**12},
        {"radius_m": True},
    ],
    ids=[
        "nan-lat",
        "inf-lon",
        "neg-inf-lat",
        "lat-91",
        "lon-181",
        "neg-radius",
        "zero-radius",
        "huge-radius",
        "bool-radius",
    ],
)
async def test_space_zones_invalid_geometry_dropped(setup, over):
    """F8: a synced zone must pass the same coordinate / radius rules as a
    local one — NaN, ±inf, out-of-range lat/lon and a radius outside
    25 m – 50 km are dropped; the rest of the chunk applies."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "space_zones",
        [dict(_GOOD_ZONE_RECORD, id="z_bad", **over), dict(_GOOD_ZONE_RECORD)],
    )
    assert [z.id for z in c.zones] == ["z_ok"]


async def test_space_zones_coordinates_truncated_to_4dp(setup):
    """F8 / CLAUDE.md GPS rule: synced zone coords are stored at 4 dp."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "space_zones",
        [dict(_GOOD_ZONE_RECORD, latitude=47.376912345, longitude=-8.541789)],
    )
    assert (c.zones[0].latitude, c.zones[0].longitude) == (47.3769, -8.5418)
