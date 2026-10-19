"""The place pin in the GFS publish body: inside the signed canonical
bytes, only for a GFS that proved ``place_pin``, only for a pinned space."""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import b64url_decode, generate_identity_keypair, verify_ed25519
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.space_place import PlacePin
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService

from . import test_gfs_connection_service as _base

_make_conn = _base._make_conn
_StubSession = _base._StubSession
#: The connection-repo fixture, shared with the main suite.
env = _base.env


async def _svc(env, *, lat, lon, place_pin_cap: bool, monkeypatch):
    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-p", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id="sp-pin",
            name="Chess Zürich",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.REQUEST,
            lat=lat,
            lon=lon,
        )
    )
    probed: list[str] = []

    async def _supported(self, conn, name, cache):
        probed.append(name)
        return name == "place_pin" and place_pin_cap

    monkeypatch.setattr(
        GfsConnectionService, "_signed_capability_supported", _supported
    )
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc, session, kp, probed


async def test_a_pinned_space_ships_its_pin_signed(env, monkeypatch):
    svc, session, kp, _ = await _svc(
        env, lat=47.3769, lon=8.5417, place_pin_cap=True, monkeypatch=monkeypatch
    )
    await svc.publish_space("sp-pin", "gfs-p")
    body = dict(session._last_body)  # type: ignore[attr-defined]
    assert body["place"] == {"lat": 47.3769, "lon": 8.5417}
    sig = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))


async def test_an_older_gfs_gets_the_body_without_the_pin(env, monkeypatch):
    """It would fold no such field into the bytes it verifies and 403 the
    whole publish."""
    svc, session, _kp, _ = await _svc(
        env, lat=47.3769, lon=8.5417, place_pin_cap=False, monkeypatch=monkeypatch
    )
    await svc.publish_space("sp-pin", "gfs-p")
    assert "place" not in session._last_body  # type: ignore[attr-defined]


@pytest.mark.parametrize("lat,lon", [(None, None)])
async def test_an_unpinned_space_never_probes_or_ships_one(env, monkeypatch, lat, lon):
    svc, session, _kp, probed = await _svc(
        env, lat=lat, lon=lon, place_pin_cap=True, monkeypatch=monkeypatch
    )
    await svc.publish_space("sp-pin", "gfs-p")
    assert "place" not in session._last_body  # type: ignore[attr-defined]
    assert "place_pin" not in probed


async def test_the_radius_hint_rides_inside_the_signed_place(env, monkeypatch):
    """No new capability: the GFS folds the whole ``place`` object into the
    bytes it verifies, so the hint is signed with the pin (no GFS has
    shipped ``place_pin`` without it)."""
    svc, session, kp, _ = await _svc(
        env, lat=47.3769, lon=8.5417, place_pin_cap=True, monkeypatch=monkeypatch
    )
    await svc._space_repo.set_place_pin(  # type: ignore[union-attr]
        "sp-pin", PlacePin(47.3769, 8.5417, 10.0)
    )
    await svc.publish_space("sp-pin", "gfs-p")
    body = dict(session._last_body)  # type: ignore[attr-defined]
    assert body["place"] == {"lat": 47.3769, "lon": 8.5417, "radius_km": 10.0}
    sig = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))
