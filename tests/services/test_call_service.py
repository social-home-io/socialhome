"""Tests for CallSignalingService (§26)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.call import CallQualitySample, CallSession
from socialhome.domain.conversation import (
    ConversationMember,
    ConversationMessage,
    RemoteConversationMember,
)
from socialhome.domain.federation import FederationEventType
from socialhome.domain.user import RemoteUser, User
from socialhome.services.call_service import (
    MAX_CALLS_PER_USER,
    CallConversationError,
    CallNotFoundError,
    CallSignalingService,
    StaleCallCleanupScheduler,
)


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeFederation:
    """Federation stub that records sent events."""

    def __init__(self, own_instance_id: str = "self-instance"):
        self.own_instance_id = own_instance_id
        self.sent: list[tuple] = []
        self._federation_repo = _FakeFedRepo()

    async def send_event(self, *, to_instance_id, event_type, payload, **kw):
        self.sent.append((to_instance_id, event_type, payload))
        return type("R", (), {"ok": True})()


class _FakeFedRepo:
    async def get_instance(self, iid):
        return None


class _FakeWS:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def broadcast_to_user(self, user_id, payload):
        self.calls.append((user_id, payload))


class _FakePush:
    def __init__(self):
        self.missed: list[dict] = []

    async def notify_missed_call(
        self,
        *,
        recipient_user_ids,
        caller_user_id,
        call_id,
        conversation_id,
    ):
        self.missed.append(
            {
                "recipients": list(recipient_user_ids),
                "caller": caller_user_id,
                "call_id": call_id,
                "conversation_id": conversation_id,
            }
        )


class _FakeUserRepo:
    """Minimal user repo fulfilling the :class:`AbstractUserRepo` subset
    used by :class:`CallSignalingService`."""

    def __init__(self):
        self._by_username: dict[str, User] = {}
        self._by_user_id: dict[str, User] = {}
        self._instance_for_user: dict[str, str] = {}
        self._remotes: dict[str, list[RemoteUser]] = {}

    def add_user(
        self, username: str, user_id: str, *, instance_id: str = "self-instance"
    ):
        u = User(
            user_id=user_id,
            username=username,
            display_name=username,
        )
        self._by_username[username] = u
        self._by_user_id[user_id] = u
        self._instance_for_user[user_id] = instance_id

    def add_remote(
        self,
        *,
        user_id: str,
        instance_id: str,
        remote_username: str,
    ):
        ru = RemoteUser(
            user_id=user_id,
            instance_id=instance_id,
            remote_username=remote_username,
            display_name=remote_username,
        )
        self._remotes.setdefault(instance_id, []).append(ru)
        self._instance_for_user[user_id] = instance_id

    async def get(self, username):
        return self._by_username.get(username)

    async def get_by_user_id(self, user_id):
        return self._by_user_id.get(user_id)

    async def get_instance_for_user(self, user_id):
        return self._instance_for_user.get(user_id)

    async def list_remote_for_instance(self, instance_id):
        return self._remotes.get(instance_id, [])


class _FakeConversationRepo:
    """Minimal conversation repo — members + messages."""

    def __init__(self):
        self._members: dict[str, list[ConversationMember]] = {}
        self._remote_members: dict[str, list[RemoteConversationMember]] = {}
        self.messages: list[ConversationMessage] = []
        self.touched: list[str] = []

    def add_conversation(
        self,
        conversation_id: str,
        usernames: list[str],
        *,
        remotes: list[tuple[str, str, str]] = (),
    ):
        """``remotes`` is a list of (instance_id, remote_username, user_id)."""
        self._members[conversation_id] = [
            ConversationMember(
                conversation_id=conversation_id,
                username=u,
                joined_at="2026-01-01T00:00:00+00:00",
                last_read_at=None,
                history_visible_from=None,
                deleted_at=None,
            )
            for u in usernames
        ]
        self._remote_members[conversation_id] = [
            RemoteConversationMember(
                conversation_id=conversation_id,
                instance_id=inst,
                remote_username=ru,
                joined_at="2026-01-01T00:00:00+00:00",
            )
            for inst, ru, _uid in remotes
        ]

    async def list_members(self, conversation_id):
        return list(self._members.get(conversation_id, []))

    async def list_remote_members(self, conversation_id):
        return list(self._remote_members.get(conversation_id, []))

    async def save_message(self, message):
        self.messages.append(message)
        return message

    async def touch_last_message(self, conversation_id, *, at=None):
        self.touched.append(conversation_id)


class _FakeCallRepo:
    """In-memory AbstractCallRepo used by the service tests."""

    def __init__(self):
        self._sessions: dict[str, CallSession] = {}
        self.samples: list[CallQualitySample] = []

    async def save_call(self, call):
        # Emulate DB default for started_at on first insert.
        if call.id in self._sessions:
            self._sessions[call.id] = call
        else:
            started = (
                call.started_at
                if call.started_at
                else datetime.now(timezone.utc).isoformat()
            )
            self._sessions[call.id] = CallSession(
                id=call.id,
                conversation_id=call.conversation_id,
                initiator_user_id=call.initiator_user_id,
                callee_user_id=call.callee_user_id,
                call_type=call.call_type,
                status=call.status,
                participant_user_ids=call.participant_user_ids,
                started_at=started,
                connected_at=call.connected_at,
                ended_at=call.ended_at,
                duration_seconds=call.duration_seconds,
            )
        return self._sessions[call.id]

    async def get_call(self, call_id):
        return self._sessions.get(call_id)

    async def list_active(self, *, user_id):
        return [
            c
            for c in self._sessions.values()
            if c.status in ("ringing", "active")
            and (
                c.initiator_user_id == user_id
                or c.callee_user_id == user_id
                or user_id in c.participant_user_ids
            )
        ]

    async def list_history_for_conversation(
        self,
        conversation_id,
        *,
        limit=50,
    ):
        rows = [
            c for c in self._sessions.values() if c.conversation_id == conversation_id
        ]
        rows.sort(key=lambda c: c.started_at, reverse=True)
        return rows[:limit]

    async def transition(
        self,
        call_id,
        *,
        status,
        connected_at=None,
        ended_at=None,
        duration_seconds=None,
        participant_user_ids=None,
    ):
        cur = self._sessions.get(call_id)
        if cur is None:
            return None
        self._sessions[call_id] = CallSession(
            id=cur.id,
            conversation_id=cur.conversation_id,
            initiator_user_id=cur.initiator_user_id,
            callee_user_id=cur.callee_user_id,
            call_type=cur.call_type,
            status=status,
            participant_user_ids=(
                participant_user_ids
                if participant_user_ids is not None
                else cur.participant_user_ids
            ),
            started_at=cur.started_at,
            connected_at=connected_at or cur.connected_at,
            ended_at=ended_at or cur.ended_at,
            duration_seconds=(
                duration_seconds
                if duration_seconds is not None
                else cur.duration_seconds
            ),
        )
        return self._sessions[call_id]

    async def end_stale_calls(self, *, older_than_seconds=90):
        missed = []
        for cid, c in list(self._sessions.items()):
            if c.status != "ringing":
                continue
            try:
                started = datetime.fromisoformat(c.started_at)
            except ValueError:
                continue
            age = (datetime.now(timezone.utc) - started).total_seconds()
            if age > older_than_seconds:
                await self.transition(
                    cid,
                    status="missed",
                    ended_at=datetime.now(timezone.utc).isoformat(),
                )
                missed.append(self._sessions[cid])
        return missed

    async def save_quality_sample(self, sample):
        self.samples.append(sample)

    async def list_quality_samples(self, call_id):
        return [s for s in self.samples if s.call_id == call_id]


# ─── Helpers ──────────────────────────────────────────────────────────────


def _seed():
    return generate_identity_keypair().private_key


@pytest.fixture
def env():
    """Standard fixture: alice + bob on self-instance, one 1:1 DM."""
    users = _FakeUserRepo()
    users.add_user("alice", "uid-alice")
    users.add_user("bob", "uid-bob")
    convos = _FakeConversationRepo()
    convos.add_conversation("conv-ab", ["alice", "bob"])
    fed = _FakeFederation()
    ws = _FakeWS()
    push = _FakePush()
    call_repo = _FakeCallRepo()
    svc = CallSignalingService(
        call_repo=call_repo,
        conversation_repo=convos,
        user_repo=users,
        own_identity_seed=_seed(),
        federation_service=fed,
        ws_manager=ws,
    )
    svc.attach_push_service(push)
    return type(
        "Env",
        (),
        {
            "svc": svc,
            "users": users,
            "convos": convos,
            "fed": fed,
            "ws": ws,
            "push": push,
            "call_repo": call_repo,
        },
    )()


# ─── initiate_call ────────────────────────────────────────────────────────


async def test_initiate_call_validates_call_type(env):
    with pytest.raises(ValueError):
        await env.svc.initiate_call(
            caller_user_id="uid-alice",
            conversation_id="conv-ab",
            call_type="hologram",
            sdp_offer="v=0\r\n",
        )


async def test_initiate_call_rejects_empty_offer(env):
    with pytest.raises(ValueError):
        await env.svc.initiate_call(
            caller_user_id="uid-alice",
            conversation_id="conv-ab",
            call_type="audio",
            sdp_offer="",
        )


async def test_initiate_call_rejects_unknown_caller(env):
    with pytest.raises(PermissionError):
        await env.svc.initiate_call(
            caller_user_id="uid-who",
            conversation_id="conv-ab",
            call_type="audio",
            sdp_offer="v=0\r\n",
        )


async def test_initiate_call_rejects_non_member(env):
    env.users.add_user("carol", "uid-carol")
    with pytest.raises(PermissionError):
        await env.svc.initiate_call(
            caller_user_id="uid-carol",
            conversation_id="conv-ab",
            call_type="audio",
            sdp_offer="v=0\r\n",
        )


async def test_initiate_call_rejects_empty_conversation(env):
    env.convos.add_conversation("solo", ["alice"])
    with pytest.raises(CallConversationError):
        await env.svc.initiate_call(
            caller_user_id="uid-alice",
            conversation_id="solo",
            call_type="audio",
            sdp_offer="v=0\r\n",
        )


async def test_initiate_call_local_path_emits_ws_ringing(env):
    result = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    assert result["status"] == "ringing"
    assert result["callee_user_id"] == "uid-bob"
    rings = [c for c in env.ws.calls if c[1].get("type") == "call.ringing"]
    assert rings and rings[0][0] == "uid-bob"
    assert rings[0][1]["signed_sdp"]["sdp"] == "v=0\r\n"
    assert env.fed.sent == []


async def test_initiate_call_persists_session_and_emits_started_message(env):
    result = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="video",
        sdp_offer="v=0\r\n",
    )
    cid = result["call_id"]
    persisted = await env.call_repo.get_call(cid)
    assert persisted is not None
    assert persisted.status == "ringing"
    assert persisted.conversation_id == "conv-ab"
    assert set(persisted.participant_user_ids) == {"uid-alice", "uid-bob"}
    # A call_event 'started' system message is now in the DM thread.
    started = [m for m in env.convos.messages if m.type == "call_event"]
    assert started
    payload = json.loads(started[0].content)
    assert payload["event"] == "started"
    assert payload["call_id"] == cid


async def test_initiate_call_federated_path_emits_call_offer(env):
    env.users.add_remote(
        user_id="uid-remote",
        instance_id="other-inst",
        remote_username="remote_user",
    )
    env.convos.add_conversation(
        "conv-remote",
        ["alice"],
        remotes=[("other-inst", "remote_user", "uid-remote")],
    )
    result = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-remote",
        call_type="video",
        sdp_offer="v=0\r\n",
    )
    assert result["callee_instance_id"] == "other-inst"
    assert len(env.fed.sent) == 1
    target, et, payload = env.fed.sent[0]
    assert target == "other-inst"
    assert et == FederationEventType.CALL_OFFER
    assert payload["conversation_id"] == "conv-remote"


async def test_initiate_call_enforces_per_user_cap(env):
    # Build N extra conversations each with alice + a fresh bob.
    for i in range(MAX_CALLS_PER_USER):
        env.users.add_user(f"b{i}", f"uid-b{i}")
        env.convos.add_conversation(f"cc{i}", ["alice", f"b{i}"])
        await env.svc.initiate_call(
            caller_user_id="uid-alice",
            conversation_id=f"cc{i}",
            call_type="audio",
            sdp_offer="v=0\r\n",
        )
    env.convos.add_conversation("overflow", ["alice", "bob"])
    with pytest.raises(RuntimeError):
        await env.svc.initiate_call(
            caller_user_id="uid-alice",
            conversation_id="overflow",
            call_type="audio",
            sdp_offer="v=0\r\n",
        )


# ─── answer_call ──────────────────────────────────────────────────────────


async def test_answer_call_only_callee_may_answer(env):
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    with pytest.raises(PermissionError):
        await env.svc.answer_call(
            call_id=r["call_id"],
            answerer_user_id="uid-alice",
            sdp_answer="v=0\r\n",
        )


async def test_answer_call_unknown_id(env):
    with pytest.raises(CallNotFoundError):
        await env.svc.answer_call(
            call_id="missing",
            answerer_user_id="uid-bob",
            sdp_answer="x",
        )


async def test_answer_call_marks_active_and_persists(env):
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    await env.svc.answer_call(
        call_id=r["call_id"],
        answerer_user_id="uid-bob",
        sdp_answer="v=0\r\nans\r\n",
    )
    rec = env.svc.get_call(r["call_id"])
    assert rec.status == "in_progress"
    persisted = await env.call_repo.get_call(r["call_id"])
    assert persisted.status == "active"
    assert persisted.connected_at


# ─── ICE / hangup / decline ───────────────────────────────────────────────


async def test_add_ice_candidate_routes_to_other_party(env):
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    await env.svc.add_ice_candidate(
        call_id=r["call_id"],
        from_user_id="uid-alice",
        candidate={"candidate": "x", "sdpMid": "0"},
    )
    ice = [c for c in env.ws.calls if c[1].get("type") == "call.ice_candidate"]
    assert ice and ice[0][0] == "uid-bob"


async def test_add_ice_candidate_unknown_call_raises(env):
    with pytest.raises(CallNotFoundError):
        await env.svc.add_ice_candidate(
            call_id="nope",
            from_user_id="uid-alice",
            candidate={"candidate": "x"},
        )


async def test_hangup_persists_ended_with_duration(env):
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    await env.svc.answer_call(
        call_id=r["call_id"],
        answerer_user_id="uid-bob",
        sdp_answer="ans",
    )
    # Simulate a small gap before hangup.
    persisted = await env.call_repo.get_call(r["call_id"])
    # Back-date connected_at so duration > 0.
    await env.call_repo.transition(
        r["call_id"],
        status=persisted.status,
        connected_at="2020-01-01T00:00:00+00:00",
    )
    await env.svc.hangup(
        call_id=r["call_id"],
        hanger_user_id="uid-alice",
    )
    ended = await env.call_repo.get_call(r["call_id"])
    assert ended.status == "ended"
    assert ended.duration_seconds is not None
    assert ended.duration_seconds > 0
    # call_event 'ended' written.
    ends = [
        m
        for m in env.convos.messages
        if m.type == "call_event" and json.loads(m.content)["event"] == "ended"
    ]
    assert ends


async def test_hangup_rejects_non_participant(env):
    env.users.add_user("carol", "uid-carol")
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    with pytest.raises(PermissionError):
        await env.svc.hangup(
            call_id=r["call_id"],
            hanger_user_id="uid-carol",
        )


async def test_decline_only_callee_may_decline(env):
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    with pytest.raises(PermissionError):
        await env.svc.decline(
            call_id=r["call_id"],
            decliner_user_id="uid-alice",
        )


async def test_decline_writes_declined_row_and_message(env):
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    await env.svc.decline(
        call_id=r["call_id"],
        decliner_user_id="uid-bob",
    )
    ended = await env.call_repo.get_call(r["call_id"])
    assert ended.status == "declined"
    decl = [
        m
        for m in env.convos.messages
        if m.type == "call_event" and json.loads(m.content)["event"] == "declined"
    ]
    assert decl


# ─── gc_expired ───────────────────────────────────────────────────────────


async def test_gc_expired_marks_missed_and_emits_call_event(env):
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    # Back-date to 200 s so it's past the 90 s TTL.
    persisted = await env.call_repo.get_call(r["call_id"])
    past = datetime.now(timezone.utc).timestamp() - 200
    back = datetime.fromtimestamp(past, tz=timezone.utc).isoformat()
    env.call_repo._sessions[r["call_id"]] = CallSession(
        id=persisted.id,
        conversation_id=persisted.conversation_id,
        initiator_user_id=persisted.initiator_user_id,
        callee_user_id=persisted.callee_user_id,
        call_type=persisted.call_type,
        status="ringing",
        participant_user_ids=persisted.participant_user_ids,
        started_at=back,
    )
    missed = await env.svc.gc_expired()
    assert missed == 1
    ended = await env.call_repo.get_call(r["call_id"])
    assert ended.status == "missed"
    msgs = [
        m
        for m in env.convos.messages
        if m.type == "call_event" and json.loads(m.content)["event"] == "missed"
    ]
    assert msgs
    # And a push was fired to the callee.
    assert env.push.missed and env.push.missed[0]["recipients"] == ["uid-bob"]


# ─── join_call ────────────────────────────────────────────────────────────


async def test_join_call_membership_guard(env):
    env.users.add_user("carol", "uid-carol")
    env.users.add_user("eve", "uid-eve")
    env.convos.add_conversation("conv-abc", ["alice", "bob", "carol"])
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-abc",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    with pytest.raises(PermissionError):
        await env.svc.join_call(
            call_id=r["call_id"],
            joiner_user_id="uid-eve",
            sdp_offers={"uid-alice": "v=0\r\n"},
        )


async def test_join_call_adds_participant_and_fanouts(env):
    env.users.add_user("carol", "uid-carol")
    env.convos.add_conversation("conv-abc", ["alice", "bob", "carol"])
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-abc",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    result = await env.svc.join_call(
        call_id=r["call_id"],
        joiner_user_id="uid-carol",
        sdp_offers={"uid-alice": "offer-a", "uid-bob": "offer-b"},
    )
    assert set(result["joined"]) == {"uid-alice", "uid-bob"}
    persisted = await env.call_repo.get_call(r["call_id"])
    assert "uid-carol" in persisted.participant_user_ids


async def test_join_call_unknown_is_404(env):
    with pytest.raises(CallNotFoundError):
        await env.svc.join_call(
            call_id="no-such",
            joiner_user_id="uid-alice",
            sdp_offers={},
        )


# ─── CALL_QUALITY ─────────────────────────────────────────────────────────


async def test_record_quality_sample_persists_and_federates(env):
    env.users.add_remote(
        user_id="uid-remote",
        instance_id="other-inst",
        remote_username="remote_user",
    )
    env.convos.add_conversation(
        "conv-remote",
        ["alice"],
        remotes=[("other-inst", "remote_user", "uid-remote")],
    )
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-remote",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    sample = CallQualitySample(
        call_id=r["call_id"],
        reporter_user_id="uid-alice",
        sampled_at=int(time.time()),
        rtt_ms=42,
    )
    await env.svc.record_quality_sample(sample)
    saved = await env.call_repo.list_quality_samples(r["call_id"])
    assert len(saved) == 1
    # Federated peer received a CALL_QUALITY event.
    quality_events = [
        s for s in env.fed.sent if s[1] == FederationEventType.CALL_QUALITY
    ]
    assert len(quality_events) == 1


async def test_handle_federated_call_quality_persists_sample(env):
    class _Event:
        def __init__(self, et, from_inst, payload):
            self.event_type = et
            self.from_instance = from_inst
            self.payload = payload

    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_QUALITY,
            "other-inst",
            {
                "call_id": "c-remote",
                "reporter_user": "uid-remote",
                "sampled_at": 1700000000,
                "rtt_ms": 55,
                "jitter_ms": 3,
            },
        )
    )
    samples = await env.call_repo.list_quality_samples("c-remote")
    assert samples and samples[0].rtt_ms == 55


# ─── handle_federated_signal ──────────────────────────────────────────────


class _Event:
    def __init__(self, et, from_inst, payload):
        self.event_type = et
        self.from_instance = from_inst
        self.payload = payload


async def test_handle_federated_call_offer_creates_record_and_rings(env):
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
            },
        )
    )
    rec = env.svc.get_call("c1")
    assert rec is not None
    assert rec.callee_user_id == "uid-bob"
    assert rec.callee_instance_id == "remote-inst"
    assert rec.conversation_id == "conv-ab"
    rings = [c for c in env.ws.calls if c[1].get("type") == "call.ringing"]
    assert rings and rings[0][0] == "uid-bob"


async def test_handle_federated_hangup_cleans_record(env):
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
            },
        )
    )
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_HANGUP,
            "remote-inst",
            {"call_id": "c1", "hanger_user": "uid-alice"},
        )
    )
    assert env.svc.get_call("c1") is None


async def test_handle_federated_signal_ignores_missing_call_id(env):
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {},
        )
    )


# ─── list_calls_for_user ──────────────────────────────────────────────────


async def test_list_calls_for_user_covers_initiator(env):
    env.users.add_user("c1", "uid-c1")
    env.users.add_user("c2", "uid-c2")
    env.convos.add_conversation("cc1", ["alice", "c1"])
    env.convos.add_conversation("cc2", ["alice", "c2"])
    r1 = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="cc1",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    r2 = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="cc2",
        call_type="video",
        sdp_offer="v=0\r\n",
    )
    out = env.svc.list_calls_for_user("uid-alice")
    ids = {c.call_id for c in out}
    assert r1["call_id"] in ids and r2["call_id"] in ids


# ─── StaleCallCleanupScheduler lifecycle ─────────────────────────────────


async def test_stale_call_scheduler_start_and_stop(env):
    sched = StaleCallCleanupScheduler(env.svc, interval_seconds=0.01)
    await sched.start()
    # Second start is a no-op.
    await sched.start()
    await sched.stop()
