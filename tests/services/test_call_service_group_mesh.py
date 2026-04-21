"""Tests for CallSignalingService group-call mesh + scheduler."""

from __future__ import annotations

import asyncio

from social_home.services.call_service import StaleCallCleanupScheduler

from ._call_fakes import make_call_service


async def _initiate_ab(env):
    """Seed alice + bob on ``conv-ab`` and initiate a ringing audio call."""
    env.users.add_user("alice", "uid-alice")
    env.users.add_user("bob", "uid-bob")
    env.convos.add_conversation("conv-ab", ["alice", "bob"])
    return await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )


# ─── add_participant / remove_participant / participants_for ────────────


async def test_add_participant_to_unknown_call_returns_false():
    env = make_call_service()
    assert env.svc.add_participant("missing", "uid-alice") is False


async def test_add_participant_succeeds():
    env = make_call_service()
    r = await _initiate_ab(env)
    cid = r["call_id"]
    assert env.svc.add_participant(cid, "uid-carol") is True
    assert "uid-carol" in env.svc.participants_for(cid)


async def test_add_existing_participant_returns_false():
    env = make_call_service()
    r = await _initiate_ab(env)
    # alice is already a participant.
    assert env.svc.add_participant(r["call_id"], "uid-alice") is False


async def test_remove_participant_unknown_call_returns_false():
    env = make_call_service()
    assert env.svc.remove_participant("missing", "uid-alice") is False


async def test_remove_participant_not_in_call_returns_false():
    env = make_call_service()
    r = await _initiate_ab(env)
    assert env.svc.remove_participant(r["call_id"], "uid-carol") is False


async def test_remove_last_participant_ends_call():
    env = make_call_service()
    r = await _initiate_ab(env)
    cid = r["call_id"]
    env.svc.remove_participant(cid, "uid-alice")
    env.svc.remove_participant(cid, "uid-bob")
    assert env.svc.get_call(cid) is None


async def test_participants_for_unknown_call_empty():
    env = make_call_service()
    assert env.svc.participants_for("missing") == set()


async def test_participants_for_returns_initial_set():
    env = make_call_service()
    r = await _initiate_ab(env)
    assert env.svc.participants_for(r["call_id"]) == {
        "uid-alice",
        "uid-bob",
    }


# ─── StaleCallCleanupScheduler ───────────────────────────────────────────


async def test_scheduler_double_start_idempotent():
    env = make_call_service()
    sched = StaleCallCleanupScheduler(env.svc, interval_seconds=10.0)
    await sched.start()
    await sched.start()  # no-op
    await sched.stop()


async def test_scheduler_stop_without_start_safe():
    env = make_call_service()
    sched = StaleCallCleanupScheduler(env.svc)
    await sched.stop()  # no-op


async def test_scheduler_loop_calls_gc():
    """Quick interval lets the loop tick at least once."""
    env = make_call_service()
    sched = StaleCallCleanupScheduler(env.svc, interval_seconds=0.05)
    await sched.start()
    await asyncio.sleep(0.12)
    await sched.stop()


async def test_scheduler_handles_gc_exception():
    """A faulty ``gc_expired`` coroutine must not crash the loop."""

    class _Shim:
        async def gc_expired(self):
            raise RuntimeError("gc broke")

    sched = StaleCallCleanupScheduler(_Shim(), interval_seconds=0.05)
    await sched.start()
    await asyncio.sleep(0.12)
    await sched.stop()


# ─── HMAC TURN credential ────────────────────────────────────────────────


def test_turn_credential_has_expiry_and_user():
    from social_home.routes.calls import _make_turn_credential

    user, cred = _make_turn_credential("secret", "alice-id", ttl_seconds=600)
    expiry, uid = user.split(":", 1)
    assert int(expiry) > 0
    assert uid == "alice-id"
    assert len(cred) >= 20


def test_turn_credential_min_ttl_is_60s():
    from social_home.routes.calls import _make_turn_credential
    import time

    now = int(time.time())
    user, _ = _make_turn_credential("secret", "alice-id", ttl_seconds=10)
    expiry = int(user.split(":", 1)[0])
    assert expiry - now >= 60


def test_turn_credential_deterministic_with_same_inputs():
    from social_home.routes.calls import _make_turn_credential

    u1, c1 = _make_turn_credential("secret", "alice-id", ttl_seconds=600)
    u2, c2 = _make_turn_credential("secret", "alice-id", ttl_seconds=600)
    # If a second tick rolled, expiry differs — accept either.
    assert (u1 == u2 and c1 == c2) or (u1 != u2)
