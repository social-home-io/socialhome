"""Tests for TypingService — local + federated typing indicator relay."""

from __future__ import annotations


from social_home.domain.federation import FederationEventType
from social_home.services.typing_service import (
    TYPING_TTL_SECONDS,
    TypingService,
)


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeMember:
    def __init__(self, user_id: str):
        self.user_id = user_id


class _FakeRemoteMember:
    def __init__(self, instance_id: str):
        self.instance_id = instance_id


class _FakeConvoRepo:
    def __init__(self, members=None, remote=None):
        self._m = members or []
        self._r = remote or []

    async def list_members(self, cid):
        return self._m

    async def list_remote_members(self, cid):
        return self._r


class _FakeUserRepo:
    pass


class _FakeWS:
    def __init__(self):
        self.calls: list[tuple[list, dict]] = []

    async def broadcast_to_users(self, user_ids, payload):
        self.calls.append((list(user_ids), payload))
        return len(user_ids)


class _FakeFed:
    def __init__(self):
        self.own_instance_id = "self"
        self.sent: list[tuple] = []

    async def send_event(self, *, to_instance_id, event_type, payload, **kw):
        self.sent.append((to_instance_id, event_type, payload))


class _Event:
    def __init__(self, et, from_inst, payload):
        self.event_type = et
        self.from_instance = from_inst
        self.payload = payload


# ─── Local fan-out ───────────────────────────────────────────────────────


async def test_user_started_typing_fans_to_other_local_members():
    repo = _FakeConvoRepo(
        members=[
            _FakeMember("alice"),
            _FakeMember("bob"),
            _FakeMember("carol"),
        ]
    )
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    n = await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="alice",
    )
    assert n == 2
    targets, payload = ws.calls[0]
    assert set(targets) == {"bob", "carol"}
    assert payload["type"] == "conversation.user_typing"
    assert payload["sender_user_id"] == "alice"


async def test_typing_does_not_fan_to_self():
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="alice",
    )
    targets, _ = ws.calls[0]
    assert "alice" not in targets


async def test_typing_throttle_within_one_second():
    """Two events within 1s for the same (conv,user) → second is dropped."""
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.0,
    )
    n2 = await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.5,
    )
    assert n2 == 0
    assert len(ws.calls) == 1


async def test_typing_throttle_lifts_after_a_second():
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.0,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=101.5,
    )
    assert len(ws.calls) == 2


# ─── is_typing / active_typers ───────────────────────────────────────────


async def test_is_typing_true_within_ttl():
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.0,
    )
    assert svc.is_typing("c1", "alice", now=103.0) is True
    # After TTL it expires.
    assert svc.is_typing("c1", "alice", now=100.0 + TYPING_TTL_SECONDS + 0.1) is False


async def test_active_typers_filters_by_conversation():
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.0,
    )
    await svc.user_started_typing(
        conversation_id="c2",
        sender_user_id="bob",
        sender_username="b",
        now=100.0,
    )
    assert svc.active_typers("c1", now=101.0) == ["alice"]
    assert svc.active_typers("c2", now=101.0) == ["bob"]


# ─── Federation fan-out ──────────────────────────────────────────────────


async def test_typing_relays_to_remote_instances():
    repo = _FakeConvoRepo(
        members=[_FakeMember("alice"), _FakeMember("bob")],
        remote=[
            _FakeRemoteMember("remote-1"),
            _FakeRemoteMember("remote-2"),
            _FakeRemoteMember("remote-1"),  # duplicate — dedup
            _FakeRemoteMember("self"),  # own instance — skip
        ],
    )
    fed = _FakeFed()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
        federation_service=fed,
        own_instance_id="self",
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="alice",
    )
    targets = {t for t, _, _ in fed.sent}
    assert targets == {"remote-1", "remote-2"}
    for _, et, payload in fed.sent:
        assert et == FederationEventType.DM_USER_TYPING
        assert payload["conversation_id"] == "c1"


async def test_typing_no_federation_when_unattached():
    repo = _FakeConvoRepo(
        members=[_FakeMember("alice"), _FakeMember("bob")],
        remote=[_FakeRemoteMember("remote-x")],
    )
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
    )
    # No federation attached → silent skip.
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
    )


# ─── Inbound (federation → local WS) ─────────────────────────────────────


async def test_handle_remote_typing_fans_to_local_members():
    repo = _FakeConvoRepo(
        members=[
            _FakeMember("alice"),
            _FakeMember("bob"),
        ]
    )
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    n = await svc.handle_remote_typing(
        _Event(
            FederationEventType.DM_USER_TYPING,
            "remote-1",
            {
                "conversation_id": "c1",
                "sender_user_id": "remote-eve",
                "sender_username": "eve",
            },
        )
    )
    assert n == 2
    targets, payload = ws.calls[0]
    assert set(targets) == {"alice", "bob"}
    assert payload["from_instance"] == "remote-1"
    assert payload["sender_user_id"] == "remote-eve"


async def test_handle_remote_typing_drops_self_target():
    repo = _FakeConvoRepo(
        members=[
            _FakeMember("alice"),
            _FakeMember("remote-eve"),
        ]
    )
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    await svc.handle_remote_typing(
        _Event(
            FederationEventType.DM_USER_TYPING,
            "remote-1",
            {
                "conversation_id": "c1",
                "sender_user_id": "remote-eve",
                "sender_username": "eve",
            },
        )
    )
    targets, _ = ws.calls[0]
    assert "remote-eve" not in targets


async def test_handle_remote_typing_missing_fields_returns_zero():
    svc = TypingService(
        conversation_repo=_FakeConvoRepo(),
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
    )
    n = await svc.handle_remote_typing(
        _Event(
            FederationEventType.DM_USER_TYPING,
            "remote-1",
            {},
        )
    )
    assert n == 0


# ─── _resolve_user_id branches ──────────────────────────────────────────


class _UsernameMember:
    def __init__(self, username):
        self.username = username


class _RealUser:
    def __init__(self, username, user_id):
        self.username = username
        self.user_id = user_id


class _FakeUserRepoWithLookup:
    def __init__(self, mapping):
        self._mapping = mapping

    async def get(self, username):
        return self._mapping.get(username)


async def test_resolve_user_id_from_username_lookup():
    """Member has only ``username`` → resolve via user_repo."""
    repo = _FakeConvoRepo(members=[_UsernameMember("alice")])
    ws = _FakeWS()
    user_repo = _FakeUserRepoWithLookup(
        {
            "alice": _RealUser("alice", "alice-uid"),
        }
    )
    svc = TypingService(
        conversation_repo=repo,
        user_repo=user_repo,
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="bob-uid",
        sender_username="bob",
    )
    targets, _ = ws.calls[0]
    assert "alice-uid" in targets


async def test_resolve_user_id_lookup_failure_drops_member():
    """user_repo failure → member silently dropped."""
    repo = _FakeConvoRepo(members=[_UsernameMember("ghost")])
    ws = _FakeWS()

    class _Raises:
        async def get(self, _):
            raise RuntimeError("DB down")

    svc = TypingService(
        conversation_repo=repo,
        user_repo=_Raises(),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="b",
        sender_username="b",
    )
    targets, _ = ws.calls[0]
    assert targets == []


async def test_resolve_user_id_unknown_username_returns_none():
    repo = _FakeConvoRepo(members=[_UsernameMember("nobody")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepoWithLookup({}),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="b",
        sender_username="b",
    )
    targets, _ = ws.calls[0]
    assert targets == []
