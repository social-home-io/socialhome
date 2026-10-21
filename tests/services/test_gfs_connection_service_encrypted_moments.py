"""``encrypted_moments_supported`` (v_62): public moments and their keys go
only to a GFS that proved ``encrypted_moments`` under a valid signature."""

from __future__ import annotations

from . import test_gfs_connection_service as _base

#: The connection-repo fixture, shared with the main suite.
env = _base.env


async def _svc(env):
    session = _base._RecordingSession()
    svc, _kp = await _base._publish_event_svc(
        env, session, space_id="sp-m", gfs_ids=["g1", "g2"]
    )
    return svc


async def test_a_proven_server_supports_encrypted_moments(env):
    svc = await _svc(env)
    svc._encrypted_moments["g1"] = True
    assert await svc.encrypted_moments_supported("g1") is True


async def test_an_unproven_server_does_not(env):
    """The server answers ``/gfs/info`` without the capability: no."""
    svc = await _svc(env)
    assert await svc.encrypted_moments_supported("g2") is False


async def test_an_unknown_connection_does_not(env):
    svc = await _svc(env)
    assert await svc.encrypted_moments_supported("nope") is False


async def test_forgetting_a_listing_drops_the_proof(env):
    svc = await _svc(env)
    svc._encrypted_moments["g1"] = True
    svc._forget_listing("g1")
    assert "g1" not in svc._encrypted_moments
