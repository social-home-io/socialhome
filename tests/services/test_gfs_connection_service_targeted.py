"""``publish_space_event_to`` (v_61): one event to ONE subscriber, only on a
GFS that proved ``targeted_publish`` under a valid signature."""

from __future__ import annotations

from . import test_gfs_connection_service as _base

#: The connection-repo fixture, shared with the main suite.
env = _base.env


async def _svc(env, *, targeted: bool, session=None):
    session = session or _base._RecordingSession()
    svc, _kp = await _base._publish_event_svc(
        env, session, space_id="sp-t", gfs_ids=["g1", "g2"]
    )
    svc._anon_publish.update({"g1": True, "g2": True})
    if targeted:
        svc._targeted_publish["g1"] = True
    return svc, session


async def test_a_proven_server_gets_the_event_for_one_subscriber(env):
    svc, session = await _svc(env, targeted=True)
    ok = await svc.publish_space_event_to(
        "g1",
        space_id="sp-t",
        event_type="space_author_profiles_bundle",
        payload={"space_id": "sp-t", "sealed": {}},
        to_instance_id="sub-1",
    )
    assert ok is True
    # Only the server the subscriber was named on — not every listing.
    [(url, body)] = session.posts
    assert url == "https://g1.example/gfs/publish"
    assert body == {
        "space_id": "sp-t",
        "event_type": "space_author_profiles_bundle",
        "payload": {"space_id": "sp-t", "sealed": {}},
        "to": "sub-1",
    }
    # Still no household identity on the wire.
    assert "from_instance" not in body and "signature" not in body


async def test_an_unproven_server_gets_nothing_and_the_caller_falls_back(env):
    svc, session = await _svc(env, targeted=False)
    # The server answers /gfs/info without ``targeted_publish``.
    ok = await svc.publish_space_event_to(
        "g1",
        space_id="sp-t",
        event_type="space_subscriber_key_handoff",
        payload={},
        to_instance_id="sub-1",
    )
    assert ok is None
    assert session.posts == []


async def test_an_unknown_connection_or_no_target_sends_nothing(env):
    svc, session = await _svc(env, targeted=True)
    assert (
        await svc.publish_space_event_to(
            "nope",
            space_id="sp-t",
            event_type="space_subscriber_key_handoff",
            payload={},
            to_instance_id="sub-1",
        )
        is None
    )
    assert (
        await svc.publish_space_event_to(
            "g1",
            space_id="sp-t",
            event_type="space_subscriber_key_handoff",
            payload={},
            to_instance_id="",
        )
        is None
    )
    assert session.posts == []


async def test_a_refusal_is_reported(env):
    svc, session = await _svc(
        env, targeted=True, session=_base._RecordingSession(status=403)
    )
    ok = await svc.publish_space_event_to(
        "g1",
        space_id="sp-t",
        event_type="space_subscriber_key_handoff",
        payload={},
        to_instance_id="sub-1",
    )
    assert ok is False
    assert len(session.posts) == 1


async def test_forgetting_a_listing_drops_the_proof(env):
    svc, _session = await _svc(env, targeted=True)
    svc._forget_listing("g1")
    assert "g1" not in svc._targeted_publish
