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


async def test_encrypted_moment_media_needs_its_own_proof(env):
    """v_63: a server that proved ``encrypted_moments`` but not
    ``encrypted_moment_media`` gets no blobs."""
    svc = await _svc(env)
    svc._encrypted_moments["g1"] = True
    assert await svc.encrypted_moment_media_supported("g1") is False
    svc._encrypted_moment_media["g1"] = True
    assert await svc.encrypted_moment_media_supported("g1") is True
    assert await svc.encrypted_moment_media_supported("nope") is False
    svc._forget_listing("g1")
    assert "g1" not in svc._encrypted_moment_media


async def test_the_delete_log_and_month_long_media_need_their_own_proofs(env):
    """v_66: certificates go only to a server that proved
    ``moment_delete_log``; a blob is asked past a week only of one that
    proved ``moment_media_30d``."""
    svc = await _svc(env)
    svc._encrypted_moments["g1"] = True
    svc._encrypted_moment_media["g1"] = True
    assert await svc.moment_delete_log_supported("g1") is False
    assert await svc.moment_media_30d_supported("g1") is False
    svc._moment_delete_log["g1"] = True
    svc._moment_media_30d["g1"] = True
    assert await svc.moment_delete_log_supported("g1") is True
    assert await svc.moment_media_30d_supported("g1") is True
    assert await svc.moment_delete_log_supported("nope") is False
    assert await svc.moment_media_30d_supported("nope") is False
    svc._forget_listing("g1")
    assert "g1" not in svc._moment_delete_log
    assert "g1" not in svc._moment_media_30d


async def test_reactions_need_their_own_proof(env):
    """v_68: a sealed reaction is posted only to a server that proved
    ``moment_reactions`` — encrypted moments alone are not enough."""
    svc = await _svc(env)
    svc._encrypted_moments["g1"] = True
    assert await svc.moment_reactions_supported("g1") is False
    svc._moment_reactions["g1"] = True
    assert await svc.moment_reactions_supported("g1") is True
    assert await svc.moment_reactions_supported("nope") is False
    svc._forget_listing("g1")
    assert "g1" not in svc._moment_reactions


async def test_totals_need_their_own_proof(env):
    """v_69: reaction totals (and an ``ops`` declaration) go only to a
    server that proved ``moment_counts``."""
    svc = await _svc(env)
    svc._encrypted_moments["g1"] = True
    assert await svc.moment_counts_supported("g1") is False
    svc._moment_counts["g1"] = True
    assert await svc.moment_counts_supported("g1") is True
    assert await svc.moment_counts_supported("nope") is False
    svc._forget_listing("g1")
    assert "g1" not in svc._moment_counts
