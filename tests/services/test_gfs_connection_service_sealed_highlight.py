"""``sealed_highlight_stream_supported``: a household mints a public
highlight link only on a GFS whose SIGNED capability block proves its viewer
page opens the sealed stream (§highlights_public, rollout gate)."""

from __future__ import annotations

from . import test_gfs_connection_service as _base

#: The connection-repo fixture, shared with the main suite.
env = _base.env


def _info(conn, *, sealed: bool) -> dict:
    caps: dict = {"anonymous_publish": True}
    if sealed:
        caps["sealed_highlight_stream"] = True
    return _base._signed_info(gfs_instance_id=conn.gfs_instance_id, capabilities=caps)


async def test_a_server_that_signed_the_capability_is_supported_and_cached(env):
    _db, repo = env
    conn = _base._make_conn("sh-1", public_key=_base._GFS_KP.public_key.hex())
    session = _base._InviteSession(info=_info(conn, sealed=True))
    svc, conn, _pub = await _base._invite_svc(repo, session, "sh-1")
    assert await svc.sealed_highlight_stream_supported(conn) is True
    assert await svc.sealed_highlight_stream_supported(conn) is True
    assert _base._info_probes(session) == 1


async def test_an_older_server_without_the_capability_is_not(env):
    _db, repo = env
    conn = _base._make_conn("sh-2", public_key=_base._GFS_KP.public_key.hex())
    session = _base._InviteSession(info=_info(conn, sealed=False))
    svc, conn, _pub = await _base._invite_svc(repo, session, "sh-2")
    assert await svc.sealed_highlight_stream_supported(conn) is False
    assert "sh-2" not in svc._sealed_highlight_stream


async def test_an_unsigned_flag_proves_nothing(env):
    """A stripped / unsigned block (an older GFS, or an on-path attacker
    adding the flag) never counts."""
    _db, repo = env
    info = {
        "server_name": "GFS",
        "anonymous_publish": True,
        "capabilities": {"anonymous_publish": True, "sealed_highlight_stream": True},
    }
    session = _base._InviteSession(info=info)
    svc, conn, _pub = await _base._invite_svc(repo, session, "sh-3")
    assert await svc.sealed_highlight_stream_supported(conn) is False


async def test_a_signature_by_another_key_proves_nothing(env):
    _db, repo = env
    conn = _base._make_conn("sh-4", public_key=_base._GFS_KP.public_key.hex())
    forged = _base._signed_info(
        gfs_instance_id=conn.gfs_instance_id,
        capabilities={"anonymous_publish": True, "sealed_highlight_stream": True},
        kp=_base.generate_identity_keypair(),
    )
    session = _base._InviteSession(info=forged)
    svc, conn, _pub = await _base._invite_svc(repo, session, "sh-4")
    assert await svc.sealed_highlight_stream_supported(conn) is False


async def test_forgetting_a_listing_drops_the_proof(env):
    _db, repo = env
    conn = _base._make_conn("sh-5", public_key=_base._GFS_KP.public_key.hex())
    session = _base._InviteSession(info=_info(conn, sealed=True))
    svc, conn, _pub = await _base._invite_svc(repo, session, "sh-5")
    assert await svc.sealed_highlight_stream_supported(conn) is True
    svc._forget_listing("sh-5")
    assert "sh-5" not in svc._sealed_highlight_stream
