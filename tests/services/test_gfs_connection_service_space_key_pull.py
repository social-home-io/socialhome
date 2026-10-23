"""``request_space_key`` (#5b): a follower that missed a space's content key
asks the GFS that seats it — only one that proved ``space_key_pull`` under a
valid signature — signed like a subscribe."""

from __future__ import annotations

import json

from socialhome.crypto import b64url_decode, verify_ed25519

from . import test_gfs_connection_service as _base

#: The connection-repo fixture, shared with the main suite.
env = _base.env


async def _svc(env):
    session = _base._RecordingSession(status=202)
    svc, kp = await _base._publish_event_svc(
        env, session, space_id="sp-k", gfs_ids=["g1", "g2"]
    )
    return svc, kp, session


async def test_a_proven_server_is_asked_with_a_signed_request(env):
    svc, kp, session = await _svc(env)
    svc._space_key_pull["g1"] = True
    assert await svc.space_key_pull_supported("g1") is True
    assert await svc.request_space_key("sp-k", "g1") is True
    [(url, body)] = session.posts
    assert url == "https://g1.example/gfs/spaces/sp-k/key_request"
    assert body["instance_id"] == "alpha.home"
    canonical = json.dumps(
        {
            "action": "space_key_request",
            "instance_id": "alpha.home",
            "space_id": "sp-k",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(body["signature"]))


async def test_an_unproven_server_is_never_asked(env):
    svc, _kp, session = await _svc(env)
    assert await svc.space_key_pull_supported("g2") is False
    assert await svc.request_space_key("sp-k", "g2") is False
    assert await svc.request_space_key("sp-k", "nope") is False
    assert session.posts == []


async def test_a_refused_request_is_reported(env):
    svc, _kp, _session = await _svc(env)
    svc._publish_client = svc._http_client = _base._RecordingSession(status=403)
    svc._space_key_pull["g1"] = True
    assert await svc.request_space_key("sp-k", "g1") is False


async def test_forgetting_a_listing_drops_the_proof(env):
    svc, _kp, _session = await _svc(env)
    svc._space_key_pull["g1"] = True
    svc._forget_listing("g1")
    assert "g1" not in svc._space_key_pull
