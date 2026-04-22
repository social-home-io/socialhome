"""Shared in-memory fakes for :class:`CallSignalingService` tests.

Exports :class:`FakeCallRepo`, :class:`FakeConversationRepo`, and
:class:`FakeUserRepo` along with a ``make_call_service()`` helper that
returns a fully-wired service + the fakes so tests can inspect side
effects.
"""

from __future__ import annotations

from datetime import datetime, timezone

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.call import CallQualitySample, CallSession
from socialhome.domain.conversation import (
    ConversationMember,
    ConversationMessage,
    RemoteConversationMember,
)
from socialhome.domain.user import RemoteUser, User
from socialhome.services.call_service import CallSignalingService


class FakeFedRepo:
    def __init__(self, peer_pk_hex: str | None = None):
        self._peer_pk_hex = peer_pk_hex

    async def get_instance(self, iid):
        from socialhome.domain.federation import (
            InstanceSource,
            PairingStatus,
            RemoteInstance,
        )

        if self._peer_pk_hex is None:
            return None
        return RemoteInstance(
            id=iid,
            display_name="peer",
            remote_identity_pk=self._peer_pk_hex,
            key_self_to_remote="x",
            key_remote_to_self="x",
            remote_webhook_url="https://x",
            local_webhook_id="wh",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )


class FakeFederation:
    def __init__(
        self, peer_pk_hex: str | None = None, own_instance_id: str = "self-instance"
    ):
        self.own_instance_id = own_instance_id
        self.sent: list[tuple] = []
        self._federation_repo = FakeFedRepo(peer_pk_hex)

    async def send_event(self, *, to_instance_id, event_type, payload, **kw):
        self.sent.append((to_instance_id, event_type, payload))


class FakeWS:
    def __init__(self):
        self.calls: list[tuple] = []

    async def broadcast_to_user(self, user_id, payload):
        self.calls.append((user_id, payload))


class FakeUserRepo:
    def __init__(self):
        self._by_username: dict[str, User] = {}
        self._by_user_id: dict[str, User] = {}
        self._instance_for_user: dict[str, str] = {}
        self._remotes: dict[str, list[RemoteUser]] = {}

    def add_user(
        self, username: str, user_id: str, *, instance_id: str = "self-instance"
    ) -> None:
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
    ) -> None:
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


class FakeConversationRepo:
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
    ) -> None:
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


class FakeCallRepo:
    def __init__(self):
        self._sessions: dict[str, CallSession] = {}
        self.samples: list[CallQualitySample] = []

    async def save_call(self, call):
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

    async def list_history_for_conversation(self, conversation_id, *, limit=50):
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


def make_call_service(
    *,
    federation: FakeFederation | None = None,
    ws: FakeWS | None = None,
):
    """Build a ``CallSignalingService`` with fresh fakes + return them.

    Returns a ``types.SimpleNamespace`` with ``svc``, ``users``, ``convos``,
    ``fed``, ``ws``, ``call_repo`` attributes so individual tests can
    inspect side effects without threading many arguments.
    """
    from types import SimpleNamespace

    users = FakeUserRepo()
    convos = FakeConversationRepo()
    call_repo = FakeCallRepo()
    fed = federation if federation is not None else FakeFederation()
    wsm = ws if ws is not None else FakeWS()
    seed = generate_identity_keypair().private_key
    svc = CallSignalingService(
        call_repo=call_repo,
        conversation_repo=convos,
        user_repo=users,
        own_identity_seed=seed,
        federation_service=fed,
        ws_manager=wsm,
    )
    return SimpleNamespace(
        svc=svc,
        users=users,
        convos=convos,
        fed=fed,
        ws=wsm,
        call_repo=call_repo,
        seed=seed,
    )
