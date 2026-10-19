"""A global space's place pin on the GFS: signed in the owner's publish
body, stored at 4 dp, served on the directory, gossiped to cluster peers —
and never added, moved or stripped by anyone but the owner."""

from __future__ import annotations

import pytest

from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.cluster import _space_to_wire, _wire_to_space
from socialhome.global_server.domain import GlobalSpace

from . import test_space_publish_routes as _routes

_register_owner = _routes._register_owner
_sign_publish_body = _routes._sign_publish_body
#: The GFS test client fixture, shared with the publish-route tests.
gfs_client = _routes.gfs_client

_BASE = {
    "space_id": "sp-chess",
    "owning_instance": "owner.home",
    "name": "Chess Zürich",
    "description": "",
    "about_markdown": "",
    "cover_url": "",
    "min_age": 0,
    "category": "general",
    "accent_color": "#D2542A",
    "icon_url": "",
    "primary_color": "#D2542A",
}


async def _listed(client) -> dict:
    items = (await (await client.get("/gfs/spaces")).json())["spaces"]
    return next(sp for sp in items if sp["space_id"] == "sp-chess")


async def test_a_signed_pin_is_stored_truncated_and_listed(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body(
        {**_BASE, "place": {"lat": 47.376_912_3, "lon": 8.541_749_9}}, seed=seed
    )
    resp = await gfs_client.post("/gfs/spaces/sp-chess/publish", json=body)
    assert resp.status == 200
    listed = await _listed(gfs_client)
    assert listed["place"] == {"lat": 47.3769, "lon": 8.5417}
    assert "place_lat" not in listed and "place_lon" not in listed
    detail = await (await gfs_client.get("/gfs/spaces/sp-chess")).json()
    assert detail["place"] == {"lat": 47.3769, "lon": 8.5417}


async def test_a_pin_bolted_onto_a_signed_body_is_refused(gfs_client):
    """The pin is inside the signed bytes: a relay can't add or move one."""
    seed, _ = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body(dict(_BASE), seed=seed)
    body["place"] = {"lat": 1.0, "lon": 2.0}
    resp = await gfs_client.post("/gfs/spaces/sp-chess/publish", json=body)
    assert resp.status == 403


async def test_a_publish_without_a_pin_clears_it(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    pinned = _sign_publish_body({**_BASE, "place": {"lat": 1.0, "lon": 2.0}}, seed=seed)
    assert (
        await gfs_client.post("/gfs/spaces/sp-chess/publish", json=pinned)
    ).status == 200
    plain = _sign_publish_body(dict(_BASE), seed=seed)
    assert (
        await gfs_client.post("/gfs/spaces/sp-chess/publish", json=plain)
    ).status == 200
    assert (await _listed(gfs_client))["place"] is None


@pytest.mark.parametrize(
    "bad", [{"lat": 100, "lon": 0}, {"lat": "47", "lon": 8}, [47, 8], "47,8"]
)
async def test_a_malformed_signed_pin_lists_the_space_unpinned(gfs_client, bad):
    seed, _ = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body({**_BASE, "place": bad}, seed=seed)
    assert (
        await gfs_client.post("/gfs/spaces/sp-chess/publish", json=body)
    ).status == 200
    assert (await _listed(gfs_client))["place"] is None


async def test_the_repo_round_trips_the_pin(gfs_client):
    repo = gfs_client.server.app[gfs_fed_repo_key]
    await _register_owner(gfs_client.server.app)
    await repo.upsert_space(
        GlobalSpace(
            space_id="sp-chess",
            owning_instance="owner.home",
            name="C",
            place_lat=47.3769,
            place_lon=8.5417,
        )
    )
    got = await repo.get_space("sp-chess")
    assert (got.place_lat, got.place_lon) == (47.3769, 8.5417)


def test_cluster_gossip_carries_the_pin_revalidated():
    space = GlobalSpace(
        space_id="s", owning_instance="o", place_lat=47.3769, place_lon=8.5417
    )
    wire = _space_to_wire(space)
    assert wire["place"] == {"lat": 47.3769, "lon": 8.5417}
    back = _wire_to_space(wire)
    assert (back.place_lat, back.place_lon) == (47.3769, 8.5417)
    # No pin, or a malformed one from a peer, is none.
    assert (
        _space_to_wire(GlobalSpace(space_id="s", owning_instance="o"))["place"] is None
    )
    bad = _wire_to_space({**wire, "place": {"lat": 999, "lon": 0}})
    assert (bad.place_lat, bad.place_lon) == (None, None)


# ── the radius hint ─────────────────────────────────────────────────────


async def test_a_signed_radius_hint_is_stored_and_listed(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body(
        {**_BASE, "place": {"lat": 47.3769, "lon": 8.5417, "radius_km": 10}},
        seed=seed,
    )
    assert (
        await gfs_client.post("/gfs/spaces/sp-chess/publish", json=body)
    ).status == 200
    want = {"lat": 47.3769, "lon": 8.5417, "radius_km": 10.0}
    assert (await _listed(gfs_client))["place"] == want
    assert "place_radius_km" not in await _listed(gfs_client)
    detail = await (await gfs_client.get("/gfs/spaces/sp-chess")).json()
    assert detail["place"] == want


async def test_a_hint_bolted_onto_a_signed_pin_is_refused(gfs_client):
    """The hint is inside the signed ``place``: nobody but the owner can
    add or change it."""
    seed, _ = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body(
        {**_BASE, "place": {"lat": 47.3769, "lon": 8.5417}}, seed=seed
    )
    body["place"] = {**body["place"], "radius_km": 50}
    assert (
        await gfs_client.post("/gfs/spaces/sp-chess/publish", json=body)
    ).status == 403


async def test_a_malformed_signed_hint_keeps_the_pin(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body(
        {**_BASE, "place": {"lat": 47.3769, "lon": 8.5417, "radius_km": 999}},
        seed=seed,
    )
    assert (
        await gfs_client.post("/gfs/spaces/sp-chess/publish", json=body)
    ).status == 200
    assert (await _listed(gfs_client))["place"] == {"lat": 47.3769, "lon": 8.5417}


async def test_the_repo_round_trips_the_hint(gfs_client):
    repo = gfs_client.server.app[gfs_fed_repo_key]
    await _register_owner(gfs_client.server.app)
    await repo.upsert_space(
        GlobalSpace(
            space_id="sp-chess",
            owning_instance="owner.home",
            name="C",
            place_lat=47.3769,
            place_lon=8.5417,
            place_radius_km=25.0,
        )
    )
    got = await repo.get_space("sp-chess")
    assert got.place_radius_km == 25.0


def test_cluster_gossip_carries_the_hint():
    space = GlobalSpace(
        space_id="s",
        owning_instance="o",
        place_lat=47.3769,
        place_lon=8.5417,
        place_radius_km=5.0,
    )
    wire = _space_to_wire(space)
    assert wire["place"] == {"lat": 47.3769, "lon": 8.5417, "radius_km": 5.0}
    assert _wire_to_space(wire).place_radius_km == 5.0
    bad = _wire_to_space({**wire, "place": {**wire["place"], "radius_km": "x"}})
    assert (bad.place_lat, bad.place_radius_km) == (47.3769, None)
